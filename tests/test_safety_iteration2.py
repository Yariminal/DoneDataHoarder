"""Safety regressions based on disposable analogues of corpus findings."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.core.dedup import (
    find_exact_duplicates, generate_dedup_proposals, refresh_group_proposals,
)
from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.core.process_lock import OperationBusyError, operation_lock
from donedatahoarder.core.undo_log import undo_operations, get_last_session_entries
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
    Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.executor import execute, plan_execution


def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="safety")
        db.add(user)
        db.commit()
        return root, user.id


def test_large_obj_header_protects_referenced_mtl_and_fbm(tmp_path):
    root = tmp_path / "files"
    mesh_dir = root / "AssetBundle.fbm" / "models"
    mesh_dir.mkdir(parents=True)
    obj = mesh_dir / "Washer Model.obj"
    mtl = mesh_dir / "Washer Model.mtl"
    mtl.write_text("newmtl appliance\n", encoding="utf-8")
    with obj.open("wb") as stream:
        stream.write(b"# large mesh\n\n  mtllib Washer Model.mtl\n")
        stream.truncate(28_386_283)
    index = ProtectionIndex(root)
    assert index.assess(obj).protected
    assert index.assess(mtl).protected
    assert "Referenced by Washer Model.obj" in " ".join(index.assess(mtl).evidence)
    assert index.assess(root / "AssetBundle.fbm").protected


def test_unresolved_obj_reference_and_cad_font_resource_are_protected(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "model.obj").write_text("mtllib missing.mtl\n", encoding="utf-8")
    sibling = root / "other.mtl"
    sibling.write_text("newmtl other\n", encoding="utf-8")
    font = root / "font-a.SHX"
    font.write_bytes(b"font")
    index = ProtectionIndex(root)
    assert index.assess(root / "model.obj").protected
    assert index.assess(sibling).protected
    assert index.assess(font).protected


def test_tabbed_obj_reference_and_opaque_cross_directory_cad_fbx(tmp_path):
    root = tmp_path / "files"
    models = root / "models"
    resources = root / "resources"
    models.mkdir(parents=True)
    resources.mkdir()
    obj = models / "mesh.obj"
    mtl = models / "material.mtl"
    obj.write_text("mtllib\tmaterial.mtl\n", encoding="utf-8")
    mtl.write_text("newmtl one\n", encoding="utf-8")
    cad = models / "plan.dwg"
    cad.write_bytes(b"opaque CAD")
    (resources / "font.shx").write_bytes(b"resource")
    fbx = models / "scene.fbx"
    fbx.write_bytes(b"opaque FBX")
    index = ProtectionIndex(root)
    assert index.assess(mtl).protected
    assert "Referenced by mesh.obj" in " ".join(index.assess(mtl).evidence)
    assert index.assess(cad).protected
    assert index.assess(fbx).protected


def test_obj_mtl_references_never_probe_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "files"
    models, shared, textures = root / "models", root / "shared", root / "textures"
    for directory in (models, shared, textures):
        directory.mkdir(parents=True)
    obj = models / "model.obj"
    mtl = shared / "material.mtl"
    texture = textures / "panel image.png"
    obj.write_text(
        "mtllib ../shared/material.mtl\n"
        "mtllib \\\\server\\share\\remote.mtl\n"
        "mtllib C:\\outside\\external.mtl\n"
        "mtllib ../../outside.mtl\n", encoding="utf-8",
    )
    mtl.write_text(
        "newmtl panel\nmap_Kd ../textures/panel image.png\n"
        "map_Kd \\\\server\\share\\remote.png\n"
        "map_Kd C:\\outside\\external.png\n"
        "map_Kd ../../outside.png\n", encoding="utf-8",
    )
    texture.write_bytes(b"synthetic texture")
    (tmp_path / "outside.mtl").write_text("newmtl outside", encoding="utf-8")
    (tmp_path / "outside.png").write_bytes(b"outside")

    original_resolve = Path.resolve
    original_exists = Path.exists
    original_is_file = Path.is_file

    def assert_local(path):
        rendered = str(path)
        assert not rendered.startswith("\\\\server"), "UNC path was probed"
        assert not rendered.startswith("C:\\outside"), "drive path was probed"
        normalized = Path(os.path.normpath(rendered))
        assert normalized not in {tmp_path / "outside.mtl", tmp_path / "outside.png"}, (
            "parent traversal outside root was probed")

    def checked_resolve(path, *args, **kwargs):
        assert_local(path)
        return original_resolve(path, *args, **kwargs)

    def checked_exists(path, *args, **kwargs):
        assert_local(path)
        return original_exists(path, *args, **kwargs)

    def checked_is_file(path, *args, **kwargs):
        assert_local(path)
        return original_is_file(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "resolve", checked_resolve)
        patcher.setattr(Path, "exists", checked_exists)
        patcher.setattr(Path, "is_file", checked_is_file)
        index = ProtectionIndex(root)

    assert index.assess(obj).protected
    assert "Referenced by model.obj" in " ".join(index.assess(mtl).evidence)
    assert "Referenced by material.mtl" in " ".join(index.assess(texture).evidence)


def test_cad_backup_sidecar_is_protected_only_with_matching_source(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    drawing = root / "Panel Layout.dwg"
    backup = root / "Panel Layout.bak"
    unrelated = root / "unrelated.bak"
    for path in (drawing, backup, unrelated):
        path.write_bytes(b"synthetic CAD fixture")
    index = ProtectionIndex(root)
    assert index.assess(drawing).protected
    assert index.assess(backup).protected
    assert "CAD drawing sidecar" in " ".join(index.assess(backup).evidence)
    assert not index.assess(unrelated).protected

    with Session(get_engine()) as db:
        file_rec = File(session_id=sid, path=str(backup), filename=backup.name,
                        size_bytes=backup.stat().st_size, status=FileStatus.PROPOSED)
        db.add(file_rec)
        db.flush()
        proposals = [
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.RENAME,
                     current_value=str(backup), proposed_value=str(root / "renamed.bak"),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.MOVE,
                     current_value=str(backup), proposed_value=str(root / "other" / backup.name),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.MARK_DUPLICATE,
                     current_value=str(backup), proposed_value=str(drawing),
                     status=ProposalStatus.APPROVED),
        ]
        db.add_all(proposals)
        db.flush()
        steps = plan_execution(db, proposals, root, protection=index)
        assert all(step.error and "Protected dependency" in step.error for step in steps)
        db.commit()
    result = execute(dry_run=False, session_id=sid)
    assert result["applied"] == 0 and result["failed"] == 3
    assert backup.read_bytes() == b"synthetic CAD fixture"


def test_vendor_font_maps_and_etransmit_inventory_protect_dependencies(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    bundle = root / "Project Bundle"
    (bundle / "BITE").mkdir(parents=True)
    (bundle / "renders").mkdir()
    adobe = bundle / "AdbWSFnt07.lst"
    microstation = bundle / "MstnFontConfig.xml"
    ordinary_list = bundle / "shopping.lst"
    ordinary_xml = bundle / "settings.xml"
    report = bundle / "transmittal.txt"
    referenced = bundle / "BITE" / "mail signature.png"
    fallback = bundle / "renders" / "2.jpg"
    unrelated = bundle / "notes.txt"
    adobe.write_text("%!Adobe-FontList 1.06\nOutlineFileName:Fonts\\A.SHX\n",
                     encoding="utf-8")
    microstation.write_text(
        "<?xml version='1.0'?><FontConfig><DefaultShxFont>simplex</DefaultShxFont></FontConfig>",
        encoding="utf-8",
    )
    ordinary_list.write_text("one\ntwo\n", encoding="utf-8")
    ordinary_xml.write_text("<Settings><Theme>dark</Theme></Settings>", encoding="utf-8")
    report.write_bytes(
        b"Transmittal Report:\nCreated by AutoCAD eTransmit\n"
        b"Files:\nBITE\\mail signature.png\n\xfflegacy\\missing.jpg\n"
        b"\\\\server\\share\\external.png\nC:\\outside\\other.jpg\n"
    )
    referenced.write_bytes(b"referenced image")
    fallback.write_bytes(b"fallback image")
    unrelated.write_text("ordinary note", encoding="utf-8")

    original_resolve = Path.resolve
    original_is_file = Path.is_file

    def in_bundle_only(path):
        assert not str(path).startswith("\\\\server"), "UNC target was resolved"
        assert not str(path).startswith("C:\\outside"), "drive target was resolved"

    def no_network_resolve(path, *args, **kwargs):
        in_bundle_only(path)
        return original_resolve(path, *args, **kwargs)

    def no_network_probe(path, *args, **kwargs):
        in_bundle_only(path)
        return original_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", no_network_resolve)
    monkeypatch.setattr(Path, "is_file", no_network_probe)
    index = ProtectionIndex(root)
    for path in (adobe, microstation, report, referenced, fallback):
        assert index.assess(path).protected, path
    assert "Listed by AutoCAD eTransmit" in " ".join(index.assess(referenced).evidence)
    assert "Possible unresolved eTransmit" in " ".join(index.assess(fallback).evidence)
    for path in (ordinary_list, ordinary_xml, unrelated):
        assert not index.assess(path).protected, path

    with Session(get_engine()) as db:
        file_rec = File(session_id=sid, path=str(referenced), filename=referenced.name,
                        size_bytes=referenced.stat().st_size, status=FileStatus.PROPOSED)
        db.add(file_rec)
        db.flush()
        proposals = [
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.RENAME,
                     current_value=str(referenced), proposed_value=str(referenced.with_name("new.png")),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.MOVE,
                     current_value=str(referenced), proposed_value=str(bundle / "elsewhere" / referenced.name),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=file_rec.id, proposal_type=ProposalType.MARK_DUPLICATE,
                     current_value=str(referenced), proposed_value=str(fallback),
                     status=ProposalStatus.APPROVED),
        ]
        db.add_all(proposals)
        db.flush()
        steps = plan_execution(db, proposals, root, protection=index)
        assert all(step.error and "Protected dependency" in step.error for step in steps)
        db.commit()
    result = execute(dry_run=False, session_id=sid)
    assert result["applied"] == 0 and result["failed"] == 3
    assert referenced.read_bytes() == b"referenced image"


def test_protected_exact_duplicate_stays_in_place(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    victim_path = root / "font-a.SHX"
    keeper_path = root / "font-b.SHX"
    for path in (victim_path, keeper_path):
        path.write_bytes(b"identical font bytes")
    md5 = hashlib.md5(b"identical font bytes").hexdigest()
    with Session(get_engine()) as db:
        victim = File(session_id=sid, path=str(victim_path), filename=victim_path.name,
                      hash_md5=md5, status=FileStatus.PROPOSED)
        keeper = File(session_id=sid, path=str(keeper_path), filename=keeper_path.name,
                      hash_md5=md5, status=FileStatus.PROPOSED)
        db.add_all([victim, keeper])
        db.flush()
        group = DuplicateGroup(session_id=sid, dupe_type=DupeType.EXACT,
                               group_hash=md5, keep_file_id=keeper.id)
        db.add(group)
        db.flush()
        db.add_all([DuplicateMember(group_id=group.id, file_id=victim.id),
                    DuplicateMember(group_id=group.id, file_id=keeper.id)])
        db.add(Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                        duplicate_group_id=group.id, current_value=str(victim_path),
                        proposed_value=str(keeper_path), status=ProposalStatus.APPROVED))
        db.commit()
    result = execute(dry_run=False, session_id=sid)
    assert result["applied"] == 0 and result["failed"] == 1
    assert victim_path.read_bytes() == keeper_path.read_bytes()


def test_unprotected_exact_copy_bulk_review_trash_and_undo(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    left, right = root / "copy-a.txt", root / "copy-b.txt"
    content = b"the same ordinary text file\n"
    for path in (left, right):
        path.write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()
    sha256 = hashlib.sha256(content).hexdigest()
    with Session(get_engine()) as db:
        for path in (left, right):
            db.add(File(session_id=sid, path=str(path), filename=path.name,
                        hash_md5=md5, hash_sha256=sha256,
                        size_bytes=len(content), status=FileStatus.PROPOSED))
        db.commit()
    assert find_exact_duplicates(session_id=sid) == {"groups": 1, "duplicates": 1}
    assert generate_dedup_proposals(session_id=sid)["created"] == 1
    from donedatahoarder.cli import _bulk_approve
    assert _bulk_approve(min_confidence=0.9, session_id=sid) == 1
    with Session(get_engine()) as db:
        proposal = db.query(Proposal).filter_by(proposal_type=ProposalType.MARK_DUPLICATE).one()
        assert proposal.review_kind == "bulk"
        victim_path = Path(proposal.current_value)
        keeper_path = Path(proposal.proposed_value)
        victim_id = proposal.file_id
        proposal_id = proposal.id
    preview = execute(dry_run=True, session_id=sid)
    assert preview["applied"] == 1 and preview["failed"] == 0
    result = execute(dry_run=False, session_id=sid)
    assert result["applied"] == 1 and result["failed"] == 0
    assert not victim_path.exists()
    assert keeper_path.read_bytes() == content
    with Session(get_engine()) as db:
        assert ".ddh_trash" in Path(db.get(File, victim_id).path).parts
        assert db.get(Proposal, proposal_id).status == ProposalStatus.APPLIED
    restored = undo_operations(session_id=sid, force=True)
    assert restored["undone"] == 1 and restored["failed"] == 0
    assert left.read_bytes() == right.read_bytes() == content
    with Session(get_engine()) as db:
        assert db.get(File, victim_id).path == str(victim_path)
        assert db.get(File, victim_id).status == FileStatus.PROPOSED
        assert db.get(Proposal, proposal_id).status == ProposalStatus.APPROVED
    assert not (root / ".ddh_trash").exists()


def test_keeper_refresh_remeasures_and_invalidates_review(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    paths = [root / name for name in ("a.png", "b.png", "c.png")]
    for path in paths:
        path.write_bytes(b"image")
    hashes = ["0" * 16, "0" * 15 + "3", "0" * 15 + "f"]
    with Session(get_engine()) as db:
        files = [File(session_id=sid, path=str(path), filename=path.name,
                      hash_perceptual=phash, status=FileStatus.PROPOSED)
                 for path, phash in zip(paths, hashes)]
        db.add_all(files)
        db.flush()
        group = DuplicateGroup(session_id=sid, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="test", keep_file_id=files[0].id)
        db.add(group)
        db.flush()
        members = [DuplicateMember(group_id=group.id, file_id=file.id,
                                   similarity_score=0.95) for file in files]
        db.add_all(members)
        proposal = Proposal(file_id=files[2].id, proposal_type=ProposalType.MARK_DUPLICATE,
                            duplicate_group_id=group.id, current_value=str(paths[2]),
                            proposed_value=str(paths[0]), status=ProposalStatus.APPROVED,
                            review_kind="individual")
        db.add(proposal)
        db.commit()
        group_id, proposal_id = group.id, proposal.id
        file_ids = [file.id for file in files]
    with Session(get_engine()) as db:
        group = db.get(DuplicateGroup, group_id)
        group.keep_file_id = file_ids[1]
        counts = refresh_group_proposals(db, group_id)
        db.commit()
    assert counts["changed"] >= 1
    with Session(get_engine()) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal.proposed_value == str(paths[1])
        assert proposal.status == ProposalStatus.PENDING
        assert proposal.review_kind is None
        member = db.query(DuplicateMember).filter_by(group_id=group_id,
                                                      file_id=file_ids[2]).one()
        assert member.distance_to_keeper == 2.0  # 0x3 versus 0xf
        assert member.similarity_score == pytest.approx(1 - 2 / 64)


def test_individually_reviewed_near_match_blocks_changed_keeper(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    victim_path, keeper_path = root / "draft.txt", root / "final.txt"
    victim_path.write_bytes(b"draft")
    keeper_path.write_bytes(b"final")
    with Session(get_engine()) as db:
        victim = File(session_id=sid, path=str(victim_path), filename=victim_path.name,
                      hash_md5=hashlib.md5(b"draft").hexdigest(),
                      status=FileStatus.PROPOSED)
        keeper = File(session_id=sid, path=str(keeper_path), filename=keeper_path.name,
                      hash_sha256=hashlib.sha256(b"final").hexdigest(),
                      status=FileStatus.PROPOSED)
        db.add_all([victim, keeper])
        db.flush()
        proposal = Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                            current_value=str(victim_path), proposed_value=str(keeper_path),
                            status=ProposalStatus.APPROVED, review_kind="individual")
        db.add(proposal)
        db.flush()
        assert plan_execution(db, [proposal], root)[0].error is None
        db.commit()
        proposal_id = proposal.id
    keeper_path.write_bytes(b"other")  # same size, changed content
    with Session(get_engine()) as db:
        step = plan_execution(db, [db.get(Proposal, proposal_id)], root)[0]
        assert step.error == "Duplicate keeper changed since analysis"
    result = execute(dry_run=False, session_id=sid)
    assert result["applied"] == 0 and result["failed"] == 1
    assert victim_path.read_bytes() == b"draft"


def test_writer_lock_is_exclusive_across_processes(tmp_path):
    db_path = tmp_path / "index.db"
    child = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys; from pathlib import Path; "
            "from donedatahoarder.core.process_lock import operation_lock; "
            "lock=operation_lock('child', db_path=Path(sys.argv[1])); "
            "lock.__enter__(); print('ready', flush=True); "
            "sys.stdin.readline(); lock.__exit__(None,None,None)"
        ), str(db_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(OperationBusyError):
            with operation_lock("parent", db_path=db_path):
                pass
    finally:
        child.communicate(input="\n", timeout=10)
    with operation_lock("parent", db_path=db_path):
        with operation_lock("nested", db_path=db_path):
            pass


def test_plan_projects_rename_move_keeper_and_collision(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    source = root / "a.txt"
    source.write_bytes(b"A")
    victim_path = root / "victim.txt"
    victim_path.write_bytes(b"V")
    occupied = root / "occupied" / "victim.txt"
    occupied.parent.mkdir()
    occupied.write_bytes(b"O")
    with Session(get_engine()) as db:
        source_file = File(session_id=sid, path=str(source), filename=source.name,
                           status=FileStatus.PROPOSED)
        victim = File(session_id=sid, path=str(victim_path), filename=victim_path.name,
                      status=FileStatus.PROPOSED)
        db.add_all([source_file, victim])
        db.flush()
        rename = Proposal(file_id=source_file.id, proposal_type=ProposalType.RENAME,
                          current_value=str(source), proposed_value=str(root / "b.txt"),
                          status=ProposalStatus.APPROVED)
        move = Proposal(file_id=source_file.id, proposal_type=ProposalType.MOVE,
                        current_value=str(source), proposed_value=str(root / "sorted" / "a.txt"),
                        status=ProposalStatus.APPROVED)
        duplicate = Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                             current_value=str(victim_path), proposed_value=str(source),
                             status=ProposalStatus.APPROVED, review_kind="individual")
        collision = Proposal(file_id=victim.id, proposal_type=ProposalType.MOVE,
                             current_value=str(victim_path), proposed_value=str(occupied),
                             status=ProposalStatus.APPROVED)
        db.add_all([rename, move, duplicate, collision])
        db.flush()
        ids = [p.id for p in (rename, move, duplicate, collision)]
        steps = {step.proposal_id: step for step in plan_execution(
            db, [rename, move, duplicate, collision], root)}
        assert steps[ids[0]].source == str(source)
        assert steps[ids[1]].source == str(root / "b.txt")
        assert steps[ids[1]].destination == str(root / "sorted" / "b.txt")
        assert steps[ids[2]].keeper == str(root / "sorted" / "b.txt")
        assert "Destination already exists" in steps[ids[3]].error


def test_undo_refuses_to_overwrite_post_commit_review_edit(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    original = root / "original.txt"
    original.write_bytes(b"content")
    renamed = root / "renamed.txt"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(original), filename=original.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        applied = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                           current_value=str(original), proposed_value=str(renamed),
                           status=ProposalStatus.APPROVED)
        pending = Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                           current_value=str(original),
                           proposed_value=str(root / "folder" / original.name),
                           status=ProposalStatus.PENDING)
        db.add_all([applied, pending])
        db.commit()
        pending_id = pending.id
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    with Session(get_engine()) as db:
        row = db.get(Proposal, pending_id)
        expected = row.proposed_value
        row.proposed_value = str(root / "my-edited-choice.txt")
        db.commit()
    result = undo_operations(session_id=sid, force=True)
    assert result["failed"] == 1 and renamed.exists()
    with Session(get_engine()) as db:
        assert db.get(Proposal, pending_id).proposed_value == str(root / "my-edited-choice.txt")
        db.get(Proposal, pending_id).proposed_value = expected
        db.commit()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert original.read_bytes() == b"content"


def test_folder_undo_recovers_after_reverse_before_database_update(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    before = root / "before"
    before.mkdir()
    item = before / "item.txt"
    item.write_bytes(b"content")
    after = root / "after"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(item), filename=item.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                        current_value=str(before), proposed_value=str(after),
                        status=ProposalStatus.APPROVED))
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    from donedatahoarder.core import undo_log
    original_restore = undo_log._restore_database

    def crash_once(_entry):
        raise OSError("injected database-reconcile crash")

    monkeypatch.setattr(undo_log, "_restore_database", crash_once)
    assert undo_operations(session_id=sid, force=True)["failed"] == 1
    assert before.is_dir() and not after.exists()
    assert get_last_session_entries(sid)
    monkeypatch.setattr(undo_log, "_restore_database", original_restore)
    retried = undo_operations(session_id=sid, force=True)
    assert retried["undone"] == 1 and retried["failed"] == 0
    assert (before / "item.txt").read_bytes() == b"content"


def test_undo_removes_only_new_empty_directories(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    original = root / "item.txt"
    original.write_bytes(b"content")
    destination = root / "new" / "nested" / "item.txt"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(original), filename=original.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add(Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                        current_value=str(original), proposed_value=str(destination),
                        status=ProposalStatus.APPROVED))
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    assert destination.exists()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert original.exists()
    assert not (root / "new").exists()


def test_planner_reuses_vacated_destination_and_rejects_stale_source(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    a, d = root / "a.txt", root / "d.txt"
    a.write_bytes(b"A")
    d.write_bytes(b"D")
    with Session(get_engine()) as db:
        first = File(session_id=sid, path=str(a), filename=a.name, status=FileStatus.PROPOSED)
        second = File(session_id=sid, path=str(d), filename=d.name, status=FileStatus.PROPOSED)
        db.add_all([first, second])
        db.flush()
        one = Proposal(file_id=first.id, proposal_type=ProposalType.RENAME,
                       current_value=str(a), proposed_value=str(root / "b.txt"),
                       status=ProposalStatus.APPROVED)
        two = Proposal(file_id=first.id, proposal_type=ProposalType.RENAME,
                       current_value=str(a), proposed_value=str(root / "c.txt"),
                       status=ProposalStatus.APPROVED)
        three = Proposal(file_id=second.id, proposal_type=ProposalType.RENAME,
                         current_value=str(d), proposed_value=str(root / "b.txt"),
                         status=ProposalStatus.APPROVED)
        stale = Proposal(file_id=second.id, proposal_type=ProposalType.MOVE,
                         current_value=str(root / "ghost.txt"),
                         proposed_value=str(root / "sorted" / "ghost.txt"),
                         status=ProposalStatus.APPROVED)
        db.add_all([one, two, three, stale])
        db.flush()
        steps = {step.proposal_id: step for step in plan_execution(
            db, [one, two, three, stale], root)}
        assert steps[two.id].source == str(root / "b.txt")
        assert steps[three.id].error is None
        assert "source no longer matches" in steps[stale.id].error


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive Windows path semantics")
def test_planner_catches_case_variant_collision(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    a, c = root / "a.txt", root / "c.txt"
    a.write_bytes(b"A")
    c.write_bytes(b"C")
    with Session(get_engine()) as db:
        files = [File(session_id=sid, path=str(path), filename=path.name,
                      status=FileStatus.PROPOSED) for path in (a, c)]
        db.add_all(files)
        db.flush()
        first = Proposal(file_id=files[0].id, proposal_type=ProposalType.RENAME,
                         current_value=str(a), proposed_value=str(root / "B.txt"),
                         status=ProposalStatus.APPROVED)
        second = Proposal(file_id=files[1].id, proposal_type=ProposalType.RENAME,
                          current_value=str(c), proposed_value=str(root / "b.txt"),
                          status=ProposalStatus.APPROVED)
        db.add_all([first, second])
        db.flush()
        steps = {step.proposal_id: step for step in plan_execution(db, [first, second], root)}
        assert steps[first.id].error is None
        assert "Destination already exists" in steps[second.id].error


def test_complete_journal_without_db_commit_can_be_undone(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    original = root / "old.txt"
    original.write_bytes(b"contents")
    destination = root / "new.txt"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(original), filename=original.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                        current_value=str(original), proposed_value=str(destination),
                        status=ProposalStatus.APPROVED))
        db.commit()
    real_commit = Session.commit

    def fail_commit(_self):
        raise RuntimeError("injected final DB commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected"):
            execute(dry_run=False, session_id=sid)
    assert destination.exists() and not original.exists()
    entries = get_last_session_entries(sid)
    assert len(entries) == 1 and entries[0]["phase"] == "complete"
    assert not entries[0].get("_db_complete")
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert original.read_bytes() == b"contents"


def test_folder_undo_rejects_replacement_directory_identity(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    before = root / "before"
    before.mkdir()
    (before / "item.txt").write_bytes(b"item")
    after = root / "after"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(before / "item.txt"),
                    filename="item.txt", status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                        current_value=str(before), proposed_value=str(after),
                        status=ProposalStatus.APPROVED))
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    holding = root / "holding"
    after.rename(holding)
    after.mkdir()
    try:
        result = undo_operations(session_id=sid, force=True)
        assert result["failed"] == 1 and after.is_dir() and holding.is_dir()
    finally:
        after.rmdir()
        holding.rename(after)
    assert undo_operations(session_id=sid, force=True)["undone"] == 1


def test_second_db_commit_failure_undoes_rename_move_batch(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    original = root / "a.txt"
    original.write_bytes(b"same bytes")
    renamed = root / "b.txt"
    moved = root / "folder" / "b.txt"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(original), filename=original.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add_all([
            Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                     current_value=str(original), proposed_value=str(renamed),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                     current_value=str(original),
                     proposed_value=str(root / "folder" / "a.txt"),
                     status=ProposalStatus.APPROVED),
        ])
        db.commit()
        file_id = file.id
    real_commit = Session.commit
    calls = 0

    def fail_second(self):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second DB commit failure")
        return real_commit(self)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_second)
        with pytest.raises(RuntimeError, match="second DB commit"):
            execute(dry_run=False, session_id=sid)
    assert moved.read_bytes() == b"same bytes"
    entries = get_last_session_entries(sid)
    assert len(entries) == 2
    assert entries[0].get("_db_complete") is True
    assert not entries[1].get("_db_complete")
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(renamed)
    result = undo_operations(session_id=sid, force=True)
    assert result["undone"] == 2 and result["failed"] == 0
    assert original.read_bytes() == b"same bytes"
    assert not (root / "folder").exists()


def test_folder_plan_and_commit_agree_on_child_move(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    before = root / "before"
    before.mkdir()
    item = before / "item.txt"
    item.write_bytes(b"child")
    after = root / "after"
    moved = root / "sorted" / "item.txt"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(item), filename=item.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        folder = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                          current_value=str(before), proposed_value=str(after),
                          status=ProposalStatus.APPROVED)
        move = Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                        current_value=str(item), proposed_value=str(moved),
                        status=ProposalStatus.APPROVED)
        db.add_all([folder, move])
        db.flush()
        steps = {step.proposal_id: step for step in plan_execution(db, [move, folder], root)}
        assert steps[move.id].source == str(after / "item.txt")
        assert steps[move.id].destination == str(moved)
        assert steps[move.id].error is None
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 2
    assert moved.read_bytes() == b"child"
    assert undo_operations(session_id=sid, force=True)["undone"] == 2
    assert item.read_bytes() == b"child"


def test_planner_rejects_destination_swap_without_staging(tmp_path, monkeypatch):
    root, sid = _workspace(tmp_path, monkeypatch)
    first_path, second_path = root / "a.txt", root / "b.txt"
    first_path.write_bytes(b"A")
    second_path.write_bytes(b"B")
    with Session(get_engine()) as db:
        files = [File(session_id=sid, path=str(path), filename=path.name,
                      status=FileStatus.PROPOSED) for path in (first_path, second_path)]
        db.add_all(files)
        db.flush()
        proposals = [
            Proposal(file_id=files[0].id, proposal_type=ProposalType.RENAME,
                     current_value=str(first_path), proposed_value=str(second_path),
                     status=ProposalStatus.APPROVED),
            Proposal(file_id=files[1].id, proposal_type=ProposalType.RENAME,
                     current_value=str(second_path), proposed_value=str(first_path),
                     status=ProposalStatus.APPROVED),
        ]
        db.add_all(proposals)
        db.flush()
        steps = plan_execution(db, proposals, root)
        assert all("Destination already exists" in step.error for step in steps)

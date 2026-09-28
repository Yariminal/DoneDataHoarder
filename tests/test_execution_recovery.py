"""Filesystem execution and undo recovery, isolated from user data."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from donedatahoarder.core.undo_log import (
    get_last_session_entries, get_undo_log_path, undo_operations,
)
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
    Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.executor import execute, select_executable_proposals


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="test")
        db.add(user)
        db.commit()
        session_id = user.id
    return root, session_id


def _file_and_proposals(root: Path, session_id: str, with_move: bool = False):
    source = root / "original.txt"
    source.write_bytes(b"original content")
    with Session(get_engine()) as db:
        file = File(session_id=session_id, path=str(source), filename=source.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        rename = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                          current_value=str(source), proposed_value=str(root / "renamed.txt"),
                          status=ProposalStatus.APPROVED)
        db.add(rename)
        db.flush()
        ids = [rename.id]
        if with_move:
            moved = root / "sorted" / "original.txt"
            move = Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                            current_value=str(source), proposed_value=str(moved),
                            status=ProposalStatus.APPROVED)
            db.add(move)
            db.flush()
            ids.append(move.id)
        file_id = file.id
        db.commit()
    return source, file_id, ids


def test_commit_selects_reviewed_only(workspace):
    root, sid = workspace
    source, _, ids = _file_and_proposals(root, sid)
    with Session(get_engine()) as db:
        pending = Proposal(file_id=db.get(Proposal, ids[0]).file_id,
                           proposal_type=ProposalType.ADD_TAGS,
                           current_value="[]", proposed_value='["x"]',
                           status=ProposalStatus.PENDING, confidence=1.0)
        db.add(pending)
        db.commit()
        assert [p.id for p in select_executable_proposals(db, session_id=sid)] == ids
        assert select_executable_proposals(db, session_id=sid, proposal_ids=[]) == []
        assert {p.id for p in select_executable_proposals(
            db, session_id=sid, include_pending=True)} == {ids[0], pending.id}
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    assert not source.exists()


def test_rename_move_undo_and_redo(workspace):
    root, sid = workspace
    source, file_id, ids = _file_and_proposals(root, sid, with_move=True)
    assert execute(dry_run=False, session_id=sid)["applied"] == 2
    moved = root / "sorted" / "renamed.txt"
    assert moved.read_bytes() == b"original content"
    result = undo_operations(session_id=sid, force=True)
    assert result["undone"] == 2 and result["failed"] == 0
    assert source.read_bytes() == b"original content"
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)
        assert db.get(File, file_id).status == FileStatus.PROPOSED
        rename, move = (db.get(Proposal, id) for id in ids)
        assert rename.status == move.status == ProposalStatus.APPROVED
        assert move.current_value == str(source)
        assert move.proposed_value == str(root / "sorted" / "original.txt")
    assert undo_operations(session_id=sid, force=True)["undone"] == 0
    assert execute(dry_run=False, session_id=sid)["applied"] == 2


@pytest.mark.parametrize("rename_status", [
    ProposalStatus.PENDING, ProposalStatus.REJECTED, ProposalStatus.APPROVED,
])
def test_selected_move_cannot_apply_unselected_rename(workspace, rename_status):
    root, sid = workspace
    source = root / "original.txt"
    source.write_bytes(b"original content")
    destination = root / "sorted" / "suggested.txt"
    with Session(get_engine()) as db:
        file_rec = File(session_id=sid, path=str(source), filename=source.name,
                        status=FileStatus.PROPOSED)
        db.add(file_rec)
        db.flush()
        db.add(Proposal(
            file_id=file_rec.id, proposal_type=ProposalType.RENAME,
            current_value=str(source), proposed_value=str(root / "suggested.txt"),
            status=rename_status,
        ))
        move = Proposal(
            file_id=file_rec.id, proposal_type=ProposalType.MOVE,
            current_value=str(source), proposed_value=str(destination),
            status=ProposalStatus.APPROVED,
        )
        db.add(move)
        db.commit()
        move_id = move.id
        file_id = file_rec.id

    assert execute(dry_run=True, session_id=sid, proposal_ids=[move_id]) == {
        "applied": 0, "failed": 1, "skipped": 0,
    }
    assert execute(dry_run=False, session_id=sid, proposal_ids=[move_id]) == {
        "applied": 0, "failed": 1, "skipped": 0,
    }
    assert source.read_bytes() == b"original content"
    assert not destination.exists()
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)
        assert db.get(Proposal, move_id).status == ProposalStatus.APPROVED


def test_selective_move_cascades_pending_rename_and_keeper_then_undo(workspace):
    root, sid = workspace
    source = root / "keeper.txt"
    source.write_bytes(b"keeper")
    victim = root / "victim.txt"
    victim.write_bytes(b"victim")
    destination = root / "sorted" / source.name
    with Session(get_engine()) as db:
        other_session = UserSession(root_path=str(root), name="other")
        db.add(other_session)
        db.flush()
        keeper = File(session_id=sid, path=str(source), filename=source.name,
                      status=FileStatus.PROPOSED)
        duplicate = File(session_id=sid, path=str(victim), filename=victim.name,
                         status=FileStatus.PROPOSED)
        other_keeper = File(session_id=other_session.id, path=str(source),
                            filename=source.name, status=FileStatus.PROPOSED)
        other_duplicate = File(session_id=other_session.id, path=str(victim),
                               filename=victim.name, status=FileStatus.PROPOSED)
        db.add_all([keeper, duplicate, other_keeper, other_duplicate])
        db.flush()
        move = Proposal(file_id=keeper.id, proposal_type=ProposalType.MOVE,
                        current_value=str(source), proposed_value=str(destination),
                        status=ProposalStatus.APPROVED)
        rename = Proposal(file_id=keeper.id, proposal_type=ProposalType.RENAME,
                          current_value=str(source), proposed_value=str(root / "better.txt"),
                          status=ProposalStatus.PENDING)
        dedup = Proposal(file_id=duplicate.id, proposal_type=ProposalType.MARK_DUPLICATE,
                         current_value=str(victim), proposed_value=str(source),
                         status=ProposalStatus.PENDING)
        other_dedup = Proposal(file_id=other_duplicate.id, proposal_type=ProposalType.MARK_DUPLICATE,
                               current_value=str(victim), proposed_value=str(source),
                               status=ProposalStatus.PENDING)
        db.add_all([move, rename, dedup, other_dedup])
        db.commit()
        keeper_id, move_id, rename_id, dedup_id, other_id = (
            keeper.id, move.id, rename.id, dedup.id, other_dedup.id)
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    with Session(get_engine()) as db:
        assert db.get(File, keeper_id).path == str(destination)
        assert db.get(Proposal, rename_id).current_value == str(destination)
        assert db.get(Proposal, rename_id).proposed_value == str(destination.with_name("better.txt"))
        assert db.get(Proposal, dedup_id).proposed_value == str(destination)
        assert db.get(Proposal, other_id).proposed_value == str(source)
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    with Session(get_engine()) as db:
        assert db.get(File, keeper_id).path == str(source)
        assert db.get(Proposal, move_id).status == ProposalStatus.APPROVED
        assert db.get(Proposal, rename_id).status == ProposalStatus.PENDING
        assert db.get(Proposal, rename_id).current_value == str(source)
        assert db.get(Proposal, rename_id).proposed_value == str(root / "better.txt")
        assert db.get(Proposal, dedup_id).proposed_value == str(source)
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    with Session(get_engine()) as db:
        db.get(Proposal, rename_id).status = ProposalStatus.APPROVED
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    renamed = destination.with_name("better.txt")
    assert renamed.exists()
    with Session(get_engine()) as db:
        assert db.get(Proposal, dedup_id).proposed_value == str(renamed)
    # The later explicit approval of the dependent rename is a review edit
    # relative to the earlier MOVE journal. Undo stops before overwriting it.
    partial = undo_operations(session_id=sid, force=True)
    assert partial["undone"] == 1 and partial["failed"] == 1
    with Session(get_engine()) as db:
        db.get(Proposal, rename_id).status = ProposalStatus.PENDING
        db.commit()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert source.exists()
    with Session(get_engine()) as db:
        assert db.get(Proposal, rename_id).proposed_value == str(root / "better.txt")
        assert db.get(Proposal, dedup_id).proposed_value == str(source)
def test_undo_collision_can_be_retried(workspace):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid)
    execute(dry_run=False, session_id=sid)
    source.write_bytes(b"new unrelated content")
    failed = undo_operations(session_id=sid, force=True)
    assert failed["failed"] == 1
    assert source.read_bytes() == b"new unrelated content"
    assert (root / "renamed.txt").exists()
    source.unlink()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)


def test_partial_undo_retries_only_failed_operation(workspace):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid, with_move=True)
    assert execute(dry_run=False, session_id=sid)["applied"] == 2
    source.write_bytes(b"unrelated replacement")
    result = undo_operations(session_id=sid, force=True)
    assert result["undone"] == 1 and result["failed"] == 1
    assert len(get_last_session_entries(sid)) == 1
    assert (root / "renamed.txt").read_bytes() == b"original content"
    source.unlink()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert source.read_bytes() == b"original content"
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)


def test_folder_rename_restores_cascaded_proposals(workspace):
    root, sid = workspace
    old_folder = root / "before"
    old_folder.mkdir()
    source = old_folder / "item.txt"
    source.write_bytes(b"item")
    new_folder = root / "after"
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(source), filename=source.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        rename = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                          current_value=str(old_folder), proposed_value=str(new_folder),
                          status=ProposalStatus.APPROVED)
        move = Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                        current_value=str(source), proposed_value=str(new_folder / source.name),
                        status=ProposalStatus.APPROVED)
        db.add_all([rename, move])
        db.commit()
        file_id, rename_id, move_id = file.id, rename.id, move.id
    assert execute(dry_run=False, session_id=sid)["failed"] == 0
    assert (new_folder / "item.txt").exists()
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert source.exists()
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)
        rename, move = db.get(Proposal, rename_id), db.get(Proposal, move_id)
        assert rename.proposed_value == str(new_folder)
        assert rename.status == move.status == ProposalStatus.APPROVED
        assert move.current_value == str(source)
        assert move.proposed_value == str(new_folder / "item.txt")
    assert execute(dry_run=False, session_id=sid)["failed"] == 0


def test_folder_cascade_is_scoped_and_restores_pending_proposals(workspace):
    root, sid = workspace
    before = root / "before"
    before.mkdir()
    source = before / "item.txt"
    source.write_bytes(b"item")
    after = root / "after"
    with Session(get_engine()) as db:
        other_session = UserSession(root_path=str(root), name="other")
        db.add(other_session)
        db.flush()
        first = File(session_id=sid, path=str(source), filename=source.name,
                     status=FileStatus.PROPOSED)
        second = File(session_id=other_session.id, path=str(source), filename=source.name,
                      status=FileStatus.PROPOSED)
        db.add_all([first, second])
        db.flush()
        folder = Proposal(file_id=first.id, proposal_type=ProposalType.RENAME_FOLDER,
                          current_value=str(before), proposed_value=str(after),
                          status=ProposalStatus.APPROVED)
        pending_first = Proposal(file_id=first.id, proposal_type=ProposalType.RENAME,
                                 current_value=str(source), proposed_value=str(before / "later.txt"),
                                 status=ProposalStatus.PENDING)
        pending_second = Proposal(file_id=second.id, proposal_type=ProposalType.RENAME,
                                  current_value=str(source), proposed_value=str(before / "later.txt"),
                                  status=ProposalStatus.PENDING)
        db.add_all([folder, pending_first, pending_second])
        db.commit()
        ids = first.id, second.id, pending_first.id, pending_second.id
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    with Session(get_engine()) as db:
        assert db.get(File, ids[0]).path == str(after / "item.txt")
        assert db.get(File, ids[1]).path == str(source)
        assert db.get(Proposal, ids[2]).current_value == str(after / "item.txt")
        assert db.get(Proposal, ids[3]).current_value == str(source)
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    with Session(get_engine()) as db:
        assert db.get(Proposal, ids[2]).current_value == str(source)
        assert db.get(Proposal, ids[3]).current_value == str(source)
def test_noop_rename_does_not_journal(workspace):
    root, sid = workspace
    source, _, ids = _file_and_proposals(root, sid)
    with Session(get_engine()) as db:
        db.get(Proposal, ids[0]).proposed_value = str(source)
        db.commit()
    result = execute(dry_run=False, session_id=sid)
    assert result["skipped"] == 1 and result["applied"] == 0
    assert source.exists()
    assert get_last_session_entries(sid) == []


def test_tags_restore_review_state_without_changing_bytes(workspace):
    root, sid = workspace
    source, file_id, ids = _file_and_proposals(root, sid)
    with Session(get_engine()) as db:
        db.get(Proposal, ids[0]).status = ProposalStatus.REJECTED
        tags = Proposal(file_id=file_id, proposal_type=ProposalType.ADD_TAGS,
                        current_value="[]", proposed_value='["tag"]',
                        status=ProposalStatus.APPROVED)
        db.add(tags)
        db.commit()
        tag_id = tags.id
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    assert source.read_bytes() == b"original content"
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    with Session(get_engine()) as db:
        assert db.get(Proposal, tag_id).status == ProposalStatus.APPROVED


def test_changed_duplicate_source_is_not_trashed(workspace):
    root, sid = workspace
    victim = root / "copy.txt"
    keeper = root / "keeper.txt"
    victim.write_bytes(b"same")
    keeper.write_bytes(b"same")
    with Session(get_engine()) as db:
        duplicate = File(session_id=sid, path=str(victim), filename=victim.name,
                         hash_md5=hashlib.md5(b"same").hexdigest(),
                         status=FileStatus.PROPOSED)
        canonical = File(session_id=sid, path=str(keeper), filename=keeper.name,
                         hash_md5=hashlib.md5(b"same").hexdigest(),
                         status=FileStatus.PROPOSED)
        db.add_all([duplicate, canonical])
        db.flush()
        db.add(Proposal(file_id=duplicate.id,
                        proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(victim), proposed_value=str(keeper),
                        status=ProposalStatus.APPROVED))
        db.commit()
    victim.write_bytes(b"unique now")
    result = execute(dry_run=False, session_id=sid)
    assert result["failed"] == 1
    assert victim.read_bytes() == b"unique now"
    assert not (root / ".ddh_trash" / victim.name).exists()


@pytest.mark.parametrize("proposal_type", [ProposalType.RENAME, ProposalType.MOVE])
def test_file_proposal_refuses_directory_replacing_indexed_file(workspace, proposal_type):
    root, sid = workspace
    source = root / "indexed.txt"
    source.write_bytes(b"indexed file")
    destination = (root / "renamed.txt" if proposal_type == ProposalType.RENAME
                   else root / "sorted" / "indexed.txt")
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(source), filename=source.name,
                    status=FileStatus.PROPOSED)
        db.add(file)
        db.flush()
        db.add(Proposal(file_id=file.id, proposal_type=proposal_type,
                        current_value=str(source), proposed_value=str(destination),
                        status=ProposalStatus.APPROVED))
        db.commit()
    source.unlink()
    source.mkdir()
    (source / "important.txt").write_bytes(b"must stay")
    result = execute(dry_run=False, session_id=sid)
    assert result["failed"] == 1 and result["applied"] == 0
    assert (source / "important.txt").read_bytes() == b"must stay"
    assert not destination.exists()
    assert get_last_session_entries(sid) == []


def test_duplicate_refuses_directory_replacing_indexed_file(workspace):
    root, sid = workspace
    victim = root / "copy.txt"
    keeper = root / "keeper.txt"
    victim.write_bytes(b"same")
    keeper.write_bytes(b"same")
    with Session(get_engine()) as db:
        duplicate = File(session_id=sid, path=str(victim), filename=victim.name,
                         hash_md5=hashlib.md5(b"same").hexdigest(),
                         status=FileStatus.PROPOSED)
        canonical = File(session_id=sid, path=str(keeper), filename=keeper.name,
                         status=FileStatus.PROPOSED)
        db.add_all([duplicate, canonical])
        db.flush()
        db.add(Proposal(file_id=duplicate.id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(victim), proposed_value=str(keeper),
                        status=ProposalStatus.APPROVED))
        db.commit()
    victim.unlink()
    victim.mkdir()
    (victim / "important.txt").write_bytes(b"must stay")
    assert execute(dry_run=False, session_id=sid)["failed"] == 1
    assert (victim / "important.txt").read_bytes() == b"must stay"
    assert not (root / ".ddh_trash" / victim.name).exists()


def test_duplicate_rejects_keeper_changed_after_proposal(workspace):
    root, sid = workspace
    victim_path = root / "victim.txt"
    old_keeper_path = root / "old_keeper.txt"
    new_keeper_path = root / "new_keeper.txt"
    for path in (victim_path, old_keeper_path, new_keeper_path):
        path.write_bytes(b"same")
    md5 = hashlib.md5(b"same").hexdigest()
    with Session(get_engine()) as db:
        records = [File(session_id=sid, path=str(path), filename=path.name,
                        hash_md5=md5, status=FileStatus.PROPOSED)
                   for path in (victim_path, old_keeper_path, new_keeper_path)]
        db.add_all(records)
        db.flush()
        victim, old_keeper, new_keeper = records
        exact = DuplicateGroup(session_id=sid, dupe_type=DupeType.EXACT,
                               group_hash=md5, keep_file_id=old_keeper.id)
        db.add(exact)
        db.flush()
        db.add_all([DuplicateMember(group_id=exact.id, file_id=file.id)
                    for file in records])
        near = DuplicateGroup(session_id=sid, dupe_type=DupeType.SEMANTIC,
                              group_hash="same-topic", keep_file_id=old_keeper.id)
        db.add(near)
        db.flush()
        db.add_all([DuplicateMember(group_id=near.id, file_id=file.id)
                    for file in (victim, old_keeper)])
        db.add(Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(victim_path), proposed_value=str(old_keeper_path),
                        status=ProposalStatus.APPROVED))
        db.commit()
        exact.keep_file_id = new_keeper.id
        db.commit()
    result = execute(dry_run=False, session_id=sid)
    assert result["failed"] == 1 and result["applied"] == 0
    assert victim_path.read_bytes() == b"same"
    assert not (root / ".ddh_trash" / victim_path.name).exists()


def test_reviewed_near_match_without_group_can_be_trashed(workspace):
    root, sid = workspace
    victim_path = root / "draft.txt"
    keeper_path = root / "final.txt"
    victim_path.write_bytes(b"draft")
    keeper_path.write_bytes(b"final")
    with Session(get_engine()) as db:
        victim = File(session_id=sid, path=str(victim_path), filename=victim_path.name,
                      hash_md5=hashlib.md5(b"draft").hexdigest(),
                      status=FileStatus.PROPOSED)
        keeper = File(session_id=sid, path=str(keeper_path), filename=keeper_path.name,
                      hash_md5=hashlib.md5(b"final").hexdigest(),
                      status=FileStatus.PROPOSED)
        db.add_all([victim, keeper])
        db.flush()
        db.add(Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(victim_path), proposed_value=str(keeper_path),
                        status=ProposalStatus.APPROVED, review_kind="individual"))
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    assert (root / ".ddh_trash" / victim_path.name).read_bytes() == b"draft"


def test_unrelated_second_group_does_not_override_selected_pair(workspace):
    root, sid = workspace
    paths = [root / name for name in ("victim.txt", "keeper.txt", "other.txt")]
    for path in paths:
        path.write_bytes(b"same")
    md5 = hashlib.md5(b"same").hexdigest()
    with Session(get_engine()) as db:
        victim, keeper, other = [
            File(session_id=sid, path=str(path), filename=path.name,
                 hash_md5=md5, status=FileStatus.PROPOSED)
            for path in paths
        ]
        db.add_all([victim, keeper, other])
        db.flush()
        exact = DuplicateGroup(session_id=sid, dupe_type=DupeType.EXACT,
                               group_hash=md5, keep_file_id=keeper.id)
        near = DuplicateGroup(session_id=sid, dupe_type=DupeType.SEMANTIC,
                              group_hash="other-pair", keep_file_id=other.id)
        db.add_all([exact, near])
        db.flush()
        db.add_all([
            DuplicateMember(group_id=exact.id, file_id=victim.id),
            DuplicateMember(group_id=exact.id, file_id=keeper.id),
            DuplicateMember(group_id=near.id, file_id=victim.id),
            DuplicateMember(group_id=near.id, file_id=other.id),
        ])
        db.add(Proposal(file_id=victim.id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(paths[0]), proposed_value=str(paths[1]),
                        status=ProposalStatus.APPROVED, duplicate_group_id=exact.id))
        db.commit()
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    assert (root / ".ddh_trash" / paths[0].name).read_bytes() == b"same"


def test_cli_undo_initializes_database_in_fresh_process_state(workspace, monkeypatch):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid)
    db_path = root.parent / "index.db"
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    import donedatahoarder.db.session as db_module
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr("donedatahoarder.cli._maybe_show_welcome", lambda: None)
    from donedatahoarder.cli import app
    result = CliRunner().invoke(app, ["undo", "--db", str(db_path),
                                      "--session", sid, "--force"])
    assert result.exit_code == 0, result.output
    assert source.read_bytes() == b"original content"
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)


def test_intent_recovers_when_completion_record_fails(workspace, monkeypatch):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid)
    def fail_completion(_entry):
        raise OSError("injected journal completion failure")
    monkeypatch.setattr("donedatahoarder.executor.complete_operation", fail_completion)
    assert execute(dry_run=False, session_id=sid)["failed"] == 1
    assert not source.exists()
    entries = get_last_session_entries(sid)
    assert len(entries) == 1 and entries[0]["phase"] == "intent"
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert source.read_bytes() == b"original content"
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)


def test_missing_destination_does_not_accept_unrelated_replacement(workspace):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid)
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    destination = root / "renamed.txt"
    destination.rename(root / "set_aside.txt")
    source.write_bytes(b"unrelated replacement")
    result = undo_operations(session_id=sid, force=True)
    assert result["failed"] == 1 and result["undone"] == 0
    assert source.read_bytes() == b"unrelated replacement"
    assert len(get_last_session_entries(sid)) == 1
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(destination)


def test_matching_already_restored_source_reconciles_database(workspace):
    root, sid = workspace
    source, file_id, _ = _file_and_proposals(root, sid)
    assert execute(dry_run=False, session_id=sid)["applied"] == 1
    (root / "renamed.txt").rename(source)
    assert undo_operations(session_id=sid, force=True)["undone"] == 1
    assert undo_operations(session_id=sid, force=True)["undone"] == 0
    with Session(get_engine()) as db:
        assert db.get(File, file_id).path == str(source)


def test_legacy_undo_marker_is_not_replayed(workspace):
    root, sid = workspace
    log = get_undo_log_path(sid)
    log.write_text("\n".join(json.dumps(e) for e in [
        {"operation": "RENAME", "original_path": str(root / "a"),
         "new_path": str(root / "b"), "timestamp": "2026-01-01T00:00:00+00:00",
         "session_id": sid},
        {"operation": "UNDO_MARKER", "undone_count": 1,
         "timestamp": "2026-01-01T00:00:01+00:00", "session_id": sid},
    ]) + "\n", encoding="utf-8")
    assert get_last_session_entries(sid) == []
    assert undo_operations(session_id=sid, force=True)["undone"] == 0


def test_legacy_marker_does_not_hide_later_operation(workspace):
    root, sid = workspace
    log = get_undo_log_path(sid)
    events = [
        {"operation": "RENAME", "original_path": str(root / "a"),
         "new_path": str(root / "b"), "timestamp": "2026-01-01T00:00:00+00:00",
         "session_id": sid},
        {"operation": "UNDO_MARKER", "undone_count": 1,
         "timestamp": "2026-01-01T00:00:01+00:00", "session_id": sid},
        {"operation": "RENAME", "original_path": str(root / "c"),
         "new_path": str(root / "d"), "timestamp": "2026-01-01T00:00:02+00:00",
         "session_id": sid},
    ]
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    assert [e["original_path"] for e in get_last_session_entries(sid)] == [str(root / "c")]

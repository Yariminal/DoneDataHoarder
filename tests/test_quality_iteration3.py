"""Synthetic regression controls for naming and independent-file organization."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    BackgroundJob, DupeType, DuplicateGroup, DuplicateMember, File, FileStatus, Proposal,
    ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import init_db
from donedatahoarder.proposals.namer.core import (
    _restore_descriptive_identity, _suppress_unsafe_rename_postpasses,
    _unsupported_name_inference, generate_proposals,
)
from donedatahoarder.proposals.namer.naming import _resolve_collision, build_new_name
from donedatahoarder.proposals.organizer.core import _emit_standalone_moves


@pytest.mark.parametrize("filename,suggestion,description,expect,reason", [
    ("IMG_7104.jpg", "amber_L_shaped_ceramic_object",
     "Orange render; the silhouette is ambiguous", "amber_ceramic_object", None),
    ("scan_12.pdf", "310th_floor_plan", "Architectural plan labeled 310th floor",
     "310th_floor_plan", "unverified_ordinal"),
    ("drawing_44.pdf", "level_225_ceiling_plan", "Ceiling plan with a technical level",
     "level_225_ceiling_plan", "unverified_technical_level"),
    ("untitled.pdf", "signed_service_agreement", "Signed service agreement",
     "signed_service_agreement", None),
    ("PROJECT_A_v3.py", "python_data_import_script", "Python data import script",
     "python_data_import_script", None),
    ("Invoice_2025_Final.docx", "supplier_invoice", "Supplier invoice",
     "supplier_invoice", None),
    ("presentation_07.png", "blue_U_shaped_display", "A blue display",
     "blue_display", None),
    ("notes_4.txt", "fourth_floor_notes", "Notes from the fourth floor",
     "fourth_floor_notes", "unverified_ordinal"),
    ("תכנית.pdf", "קומה_רביעית_תכנית", "תכנית קומה רביעית",
     "קומה_רביעית_תכנית", "unverified_technical_level"),
    ("level-3.10.pdf", "ceiling_plan", "Technical ceiling plan",
     "ceiling_plan", None),
    ("B2_L2_v4.py", "inventory_import_script", "Inventory import script",
     "inventory_import_script", None),
    ("15.12.pdf", "building_elevations", "Elevation drawing",
     "building_elevations", "source_numeric_identifier_lost"),
])
def test_naming_regression_controls(filename, suggestion, description, expect, reason):
    record = SimpleNamespace(
        path=str(Path("C:/synthetic") / filename),
        ai_suggested_name=suggestion, ai_description=description,
        ai_tags=None, ai_confidence=0.8, date_exif=None,
        date_modified=None, date_created=None,
    )
    proposed = build_new_name(record, root_path="C:/synthetic")
    assert proposed is not None
    assert Path(proposed).stem == expect
    assert _unsupported_name_inference(record, Path(proposed).stem) == reason
    if filename == "level-3.10.pdf":
        assert _restore_descriptive_identity(Path(filename).stem, Path(proposed).stem).startswith("level-3.10_")
    if reason is None and not filename.lower().startswith(("img", "untitled")):
        merged = _restore_descriptive_identity(Path(filename).stem, Path(proposed).stem)
        assert len(merged) <= 96


@pytest.mark.parametrize("filename,source,suggestion,description,subject", [
    ("note_2024.txt", "Pump inspection on 2024-04-05: replace seal before next service.",
     "pump_inspection_seal_replacement", "Pump inspection notes say replace the seal before next service.", "pump_inspection"),
    ("scan_88.txt", "The greenhouse irrigation schedule lists watering days Monday and Thursday.",
     "greenhouse_irrigation_schedule", "Greenhouse irrigation schedule with Monday and Thursday watering.", "greenhouse_irrigation"),
    ("untitled.txt", "Meeting minutes: the audit team assigned Maya to update the privacy checklist.",
     "privacy_audit_meeting_minutes", "Meeting minutes assigning a privacy checklist update.", "privacy_audit"),
])
def test_source_backed_useful_name_controls(tmp_path, filename, source, suggestion, description, subject):
    """Independent literal text, rather than model prose alone, supports the subject."""
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    assert all(token in path.read_text(encoding="utf-8").lower()
               for token in subject.split("_"))
    record = SimpleNamespace(
        path=str(path), ai_suggested_name=suggestion,
        ai_description=description, ai_tags=None, ai_confidence=0.8,
        date_exif=None, date_modified=None, date_created=None,
    )
    proposed = build_new_name(record, root_path=str(tmp_path))
    assert proposed and subject in Path(proposed).stem
    assert _unsupported_name_inference(record, Path(proposed).stem) is None


def test_project_subtree_preserved_while_mixed_inbox_and_root_files_group(tmp_path):
    engine = init_db(tmp_path / "quality.db")
    root = tmp_path / "collection"
    paths = {
        "project_source": root / "Downloads" / "Project Nova" / "scene.obj",
        "project_material": root / "Downloads" / "Project Nova" / "scene.mtl",
        "project_image": root / "Downloads" / "Project Nova" / "renders" / "hero.png",
        "download_doc": root / "Downloads" / "meeting_minutes.pdf",
        "invoice_a": root / "Inbox" / "invoice_alpha.pdf",
        "invoice_b": root / "Inbox" / "invoice_beta.pdf",
        "photo": root / "Inbox" / "holiday.png",
        "root_doc": root / "loose_report.pdf",
    }
    with Session(engine) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.flush()
        for key, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic")
            ext = path.suffix
            file_rec = File(
                session_id=owner.id, path=str(path), filename=path.name,
                extension=ext, mime_type=("image/png" if ext == ".png" else "application/pdf"),
                status=FileStatus.PROPOSED, analysis_outcome="content_verified",
                analysis_evidence_source="text", ai_description="Invoice or report",
                date_exif=datetime(2024, 5, 20) if key == "photo" else None,
            )
            db.add(file_rec)
        db.commit()
        sid = owner.id

    made = _emit_standalone_moves(sid, str(root))
    assert made == 5
    from donedatahoarder.web.api.pipeline import get_organize_coverage
    coverage = get_organize_coverage(sid)
    assert coverage["total_indexed"] == 8
    assert coverage["project_preserved"] == 3
    assert coverage["independent_proposed"] == 5
    assert coverage["needs_review"] == 0
    with Session(engine) as db:
        moves = db.query(Proposal).filter(Proposal.proposal_type == ProposalType.MOVE).all()
        assert len(moves) == 5
        assert all(p.status == ProposalStatus.PENDING for p in moves)
        assert all("Project Nova" not in p.current_value for p in moves)
        assert all(str(root / "Independent_Files") in p.proposed_value for p in moves)
        assert any("2024" in p.proposed_value for p in moves)
        assert any("Invoice" in p.proposed_value for p in moves)


def test_project_subtree_rename_is_suppressed_but_loose_rename_survives(tmp_path):
    engine = init_db(tmp_path / "rename-quality.db")
    root = tmp_path / "collection"
    project = root / "Downloads" / "Project Nova"
    project.mkdir(parents=True)
    (project / "scene.obj").write_text("mtllib scene.mtl\n", encoding="utf-8")
    (project / "scene.mtl").write_text("newmtl matte\n", encoding="utf-8")
    loose = root / "Downloads" / "untitled.pdf"
    loose.write_bytes(b"synthetic report")
    with Session(engine) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.flush()
        db.add(File(session_id=owner.id, path=str(project / "scene.mtl"),
                    filename="scene.mtl", extension=".mtl", status=FileStatus.SKIPPED))
        for path, proposed in ((project / "scene.obj", "descriptive_scene.obj"),
                               (loose, "studio_report.pdf")):
            file_rec = File(session_id=owner.id, path=str(path), filename=path.name,
                            analysis_outcome="content_verified", analysis_evidence_source="text",
                            ai_confidence=0.8, ai_description="Studio report")
            db.add(file_rec)
            db.flush()
            db.add(Proposal(file_id=file_rec.id, proposal_type=ProposalType.RENAME,
                            current_value=str(path), proposed_value=str(path.with_name(proposed)),
                            reasoning="Own content suggestion", status=ProposalStatus.PENDING))
        db.commit()
        sid = owner.id
    removed = _suppress_unsafe_rename_postpasses(sid, set())
    assert removed["project_subtree_preserved"] == 1
    with Session(engine) as db:
        remaining = db.query(Proposal).all()
        assert len(remaining) == 1
        assert Path(remaining[0].current_value) == loose


def test_selected_root_project_manifest_preserves_its_source_files(tmp_path):
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.proposals.organizer.core import (
        _inside_project, _loose_source, _project_roots,
    )

    root = tmp_path / "Downloads"
    root.mkdir()
    manifest = root / "pyproject.toml"
    source = root / "app.py"
    manifest.write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("print('hello')\n", encoding="utf-8")
    files = [SimpleNamespace(path=str(path)) for path in (manifest, source)]
    roots = _project_roots(root, files, ProtectionIndex(root))
    assert roots == {root}
    assert _inside_project(source, roots)
    assert not _loose_source(files[1], root, {}, roots)


def test_solution_and_nested_project_preserved_without_freezing_downloads(tmp_path):
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.proposals.organizer.core import (
        _inside_project, _loose_source, _project_roots,
    )

    root = tmp_path / "Downloads"
    solution = root / "Client App"
    nested = solution / "Client"
    nested.mkdir(parents=True)
    paths = [solution / "Client.sln", nested / "Client.csproj",
             nested / "Program.cs", root / "receipt.pdf"]
    for path in paths:
        path.write_text("synthetic content", encoding="utf-8")
    files = [SimpleNamespace(path=str(path)) for path in paths]
    roots = _project_roots(root, files, ProtectionIndex(root))
    assert solution in roots and nested in roots
    assert root not in roots
    assert all(_inside_project(path, roots) for path in paths[:3])
    assert not _inside_project(paths[3], roots)
    assert _loose_source(files[3], root, {}, roots)


def test_preflight_api_validates_mode_and_uses_selected_skips(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from donedatahoarder.web.app import create_app

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    root = tmp_path / "collection"
    hidden = root / "skipme"
    hidden.mkdir(parents=True)
    (root / "main.txt").write_text("main", encoding="utf-8")
    (hidden / "other.txt").write_text("other", encoding="utf-8")
    app = create_app(tmp_path / "preflight.db")
    with Session(init_db(tmp_path / "preflight.db")) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.commit()
        sid = owner.id
    with TestClient(app) as client:
        bad = client.get("/api/pipeline/preflight", params={
            "session_id": sid, "mode": "representative", "sequence_sample_stride": 0,
        })
        assert bad.status_code == 400
        response = client.get("/api/pipeline/preflight", params={
            "session_id": sid, "mode": "full", "skip_dirs": "skipme",
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["files"] == 1
        assert result["full_collection_files"] == 2
        assert result["excluded_files"] == 1
        coverage = client.get("/api/pipeline/organize/coverage", params={"session_id": sid})
        assert coverage.status_code == 200, coverage.text
        numbers = coverage.json()
        assert numbers["total_indexed"] == 0  # preflight never indexes or mutates


def test_dedup_coverage_persists_unknown_deferred_count(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from donedatahoarder.web.app import create_app

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    db_path = tmp_path / "coverage.db"
    app = create_app(db_path)
    with Session(init_db(db_path)) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        db.add(BackgroundJob(
            id="dedup-completed", session_id=owner.id, job_type="dedup",
            state="completed", progress_json=json.dumps({
                "done": True, "exact": {"groups": 1},
                "perceptual": {
                    "candidate_coverage": "bounded_incomplete",
                    "candidate_pair_opportunities_deferred": None,
                    "candidate_pair_opportunities_deferred_lower_bound": 1,
                    "candidate_pair_cap": 250000,
                },
            }),
        ))
        db.commit()
        sid = owner.id
    with TestClient(app) as client:
        response = client.get("/api/pipeline/dedup/coverage", params={"session_id": sid})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["stages"]["exact"]["groups"] == 1
        perceptual = result["stages"]["perceptual"]
        assert perceptual["candidate_coverage"] == "bounded_incomplete"
        assert perceptual["candidate_pair_opportunities_deferred"] is None
        assert perceptual["candidate_pair_cap"] == 250000


def test_dashboard_counts_only_unique_exact_candidates(tmp_path):
    from donedatahoarder.web.api.dashboard import get_stats

    engine = init_db(tmp_path / "stats.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        files = []
        for number in range(3):
            file_rec = File(session_id=owner.id, path=str(tmp_path / f"image_{number}.png"),
                            filename=f"image_{number}.png", size_bytes=100 + number)
            db.add(file_rec)
            db.flush()
            files.append(file_rec)
        for kind, hash_label, member in ((DupeType.EXACT, "copy-a", files[1]),
                                         (DupeType.EXACT, "copy-b", files[1]),
                                         (DupeType.PERCEPTUAL, "lookalike", files[2])):
            group = DuplicateGroup(session_id=owner.id, dupe_type=kind,
                                   group_hash=hash_label, keep_file_id=files[0].id)
            db.add(group)
            db.flush()
            db.add_all([DuplicateMember(group_id=group.id, file_id=files[0].id),
                        DuplicateMember(group_id=group.id, file_id=member.id)])
        db.commit()
        sid = owner.id
    stats = get_stats(sid)
    assert stats.duplicate_wasted_bytes == 101


def test_duplicate_api_exposes_direct_closest_peer_without_changing_keeper(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from donedatahoarder.web.app import create_app

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    db_path = tmp_path / "peers.db"
    app = create_app(db_path)
    with Session(init_db(db_path)) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        records = []
        for label, phash in (("keeper", "ffffffffffffffff"),
                             ("candidate", "0000000000000000"),
                             ("peer", "0000000000000001")):
            record = File(session_id=owner.id, path=str(tmp_path / f"{label}.png"),
                          filename=f"{label}.png", size_bytes=100,
                          hash_perceptual=phash)
            db.add(record)
            db.flush()
            records.append(record)
        group = DuplicateGroup(session_id=owner.id, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="visual", keep_file_id=records[0].id)
        db.add(group)
        db.flush()
        db.add_all(DuplicateMember(group_id=group.id, file_id=record.id)
                   for record in records)
        db.commit()
        sid = owner.id
        candidate_id = records[1].id
        peer_id = records[2].id
        keeper_id = records[0].id
    with TestClient(app) as client:
        response = client.get("/api/duplicates", params={"session_id": sid})
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        candidate = next(file for file in item["files"] if file["id"] == candidate_id)
        assert item["keep_file_id"] == keeper_id
        assert candidate["closest_observed_peer"]["file_id"] == peer_id
        assert candidate["closest_observed_peer"]["distance"] == 1
        assert candidate["closest_observed_peer"]["comparison_coverage"] == "exact"


def test_naming_never_marks_equal_sized_similar_names_as_duplicate(tmp_path):
    from donedatahoarder.proposals.namer.postpass import _flag_near_duplicate_proposals

    engine = init_db(tmp_path / "names.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path), preferred_language="leave_as_is")
        db.add(owner)
        db.flush()
        for filename, contents, proposed in (
            ("notes_a.txt", "RED!", "annual_report_2026.txt"),
            ("notes_b.txt", "BLUE", "annual_reports_2026.txt"),
        ):
            path = tmp_path / filename
            path.write_text(contents, encoding="utf-8")
            record = File(session_id=owner.id, path=str(path), filename=filename,
                          size_bytes=4, status=FileStatus.ANALYZED,
                          analysis_outcome="content_verified", analysis_evidence_source="text",
                          ai_description="Annual report about distinct source content",
                          ai_confidence=0.8)
            db.add(record)
            db.flush()
            db.add(Proposal(file_id=record.id, proposal_type=ProposalType.RENAME,
                            current_value=str(path), proposed_value=str(tmp_path / proposed),
                            status=ProposalStatus.PENDING, confidence=0.8))
        db.commit()
        sid = owner.id
    assert _flag_near_duplicate_proposals(sid) == 0
    generate_proposals(session_id=sid)
    with Session(engine) as db:
        assert db.query(Proposal).filter(
            Proposal.proposal_type == ProposalType.MARK_DUPLICATE
        ).count() == 0
        assert db.query(Proposal).filter(
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.status == ProposalStatus.PENDING,
        ).count() == 2


def test_generic_and_normalized_name_collisions_keep_source_prefixes(tmp_path):
    from donedatahoarder.proposals.namer.postpass import _disambiguate_generic_stems_in_dir

    engine = init_db(tmp_path / "collisions.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        for original, proposed in (("10.8-binoy.pdf", "floor_plan.pdf"),
                                   ("11.9-binoy.pdf", "floor-plan.pdf")):
            record = File(session_id=owner.id, path=str(tmp_path / original),
                          filename=original, size_bytes=10)
            db.add(record)
            db.flush()
            db.add(Proposal(file_id=record.id, proposal_type=ProposalType.RENAME,
                            current_value=record.path,
                            proposed_value=str(tmp_path / proposed),
                            status=ProposalStatus.PENDING, confidence=0.8))
        db.commit()
        sid = owner.id
    assert _disambiguate_generic_stems_in_dir(sid) == 2
    with Session(engine) as db:
        names = {Path(value).stem for (value,) in db.query(Proposal.proposed_value)}
    assert "10_8_floor_plan" in names
    assert "11_9_floor-plan" in names


def test_batch_collision_allocator_uses_one_stat_per_chosen_name(tmp_path, monkeypatch):
    calls = []
    original_exists = Path.exists

    def counted_exists(path):
        calls.append(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", counted_exists)
    proposed = tmp_path / "shared.txt"
    reserved = set()
    next_suffixes = {}
    for number in range(100):
        original = tmp_path / f"source_{number:03d}.txt"
        chosen = _resolve_collision(proposed, original, reserved,
                                    next_suffixes=next_suffixes)
        assert chosen not in reserved
        reserved.add(chosen)
    assert len(reserved) == 100
    assert len(calls) == 100
    assert proposed in reserved
    assert tmp_path / "shared_99.txt" in reserved


def test_collision_allocator_preserves_disk_and_self_rename(tmp_path):
    base = tmp_path / "shared.txt"
    base.write_text("existing", encoding="utf-8")
    (tmp_path / "shared_1.txt").write_text("existing suffix", encoding="utf-8")
    reserved = set()
    next_suffixes = {}
    assert _resolve_collision(base, base, reserved, next_suffixes) == base
    first = _resolve_collision(base, tmp_path / "source_a.txt", reserved,
                               next_suffixes=next_suffixes)
    assert first == tmp_path / "shared_2.txt"
    reserved.add(first)
    second = _resolve_collision(base, tmp_path / "source_b.txt", reserved,
                                next_suffixes=next_suffixes)
    assert second == tmp_path / "shared_3.txt"

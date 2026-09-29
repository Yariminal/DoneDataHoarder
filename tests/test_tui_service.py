"""Terminal review uses real files, durable plans and shared safety contracts."""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.core import jobs
from donedatahoarder.core.jobs import JobInfo, JobManager, JobState
from donedatahoarder.core.review import ReviewError
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
    Proposal, ProposalStatus, ProposalType, RelationGroup, RelationMember,
    RunPlan,
)
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.tui import service as service_module
from donedatahoarder.tui.service import WorkspaceService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    init_db(tmp_path / "index.db")
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    monkeypatch.setattr(jobs, "job_manager", manager)
    monkeypatch.setattr(service_module, "job_manager", manager)
    root = tmp_path / "files"
    root.mkdir()
    service = WorkspaceService(root)
    yield service, root, manager
    for job in list(manager._jobs.values()):
        if job.state.value in {"running", "paused", "cancelling"}:
            manager.force_cancel(job.job_id)
    for thread in list(manager._worker_threads.values()):
        thread.join(timeout=5)


def add_file(service, root, name, content=None, verified=True):
    source = root / name
    source.parent.mkdir(parents=True, exist_ok=True)
    data = content if content is not None else name.encode()
    source.write_bytes(data)
    with Session(get_engine()) as db:
        file = File(session_id=service.session_id, path=str(source), filename=source.name,
                    size_bytes=len(data), hash_md5=hashlib.md5(data).hexdigest(),
                    hash_sha256=hashlib.sha256(data).hexdigest(),
                    status=FileStatus.PROPOSED,
                    analysis_outcome="content_verified" if verified else None,
                    analysis_evidence_source="text" if verified else None)
        db.add(file)
        db.commit()
        return file.id


def add_proposal(file_id, destination, *, kind=ProposalType.RENAME, confidence=.95,
                 status=ProposalStatus.PENDING, group_id=None):
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        row = Proposal(file_id=file.id, proposal_type=kind, current_value=file.path,
                       proposed_value=str(destination), confidence=confidence,
                       status=status, duplicate_group_id=group_id)
        db.add(row)
        db.commit()
        return row.id


def duplicate_pair(service, root, *, kind=DupeType.PERCEPTUAL):
    first = add_file(service, root, "keeper.jpg", b"keeper")
    second = add_file(service, root, "candidate.jpg", b"keeper" if kind == DupeType.EXACT else b"candidate")
    with Session(get_engine()) as db:
        group = DuplicateGroup(session_id=service.session_id, dupe_type=kind,
                               group_hash="group-1", keep_file_id=first)
        db.add(group)
        db.flush()
        db.add_all([DuplicateMember(group_id=group.id, file_id=first, similarity_score=1),
                    DuplicateMember(group_id=group.id, file_id=second, similarity_score=.93)])
        db.commit()
        group_id = group.id
    proposal = add_proposal(second, root / "keeper.jpg", kind=ProposalType.MARK_DUPLICATE,
                            group_id=group_id)
    return first, second, group_id, proposal


def proposal_row(service, identity):
    return next(row for row in service.snapshot()["proposals"] if row["id"] == identity)


def test_open_does_not_scan_and_reopen_preserves_session(workspace):
    service, root, _ = workspace
    (root / "not-yet-indexed.txt").write_text("untouched")
    snapshot = service.snapshot()
    assert snapshot["counts"]["files"] == 0
    assert snapshot["plan"] is None
    reopened = WorkspaceService(session_id=service.session_id)
    assert reopened.snapshot()["session"]["root_path"] == str(root)
    assert (root / "not-yet-indexed.txt").read_text() == "untouched"
    with pytest.raises(ReviewError, match="differs"):
        WorkspaceService(root.parent, session_id=service.session_id)


def test_read_models_and_mutations_are_scoped_and_pagination_is_explicit(workspace):
    service, root, _ = workspace
    one = add_file(service, root, "one.txt")
    two = add_file(service, root, "two.txt")
    other_root = root.parent / "other"
    other_root.mkdir()
    other = WorkspaceService(other_root)
    foreign = add_file(other, other_root, "private.txt")
    foreign_proposal = add_proposal(foreign, other_root / "renamed.txt")
    for file_id in [one, two]:
        add_proposal(file_id, root / f"{file_id}-new.txt")
    with Session(get_engine()) as db:
        group = RelationGroup(session_id=service.session_id, label="pair", reason="Related notes")
        db.add(group)
        db.flush()
        db.add(RelationMember(group_id=group.id, file_id=one))
        db.commit()
    snapshot = service.snapshot(limit=1)
    assert snapshot["counts"]["files"] == 2
    assert snapshot["page"]["files_truncated"] is True
    assert snapshot["page"]["proposals_truncated"] is True
    assert snapshot["collections"][0]["members"][0]["id"] == one
    assert service.snapshot(limit=1, offset=1)["files"][0]["id"] == two
    with pytest.raises(ReviewError, match="this session"):
        service.get_file(foreign)
    with pytest.raises(ReviewError, match="this session"):
        service.approve(foreign_proposal)
    with pytest.raises(ReviewError, match="this session"):
        service.reject(foreign_proposal)


def test_later_pages_include_collections_and_the_visible_files_duplicate_group(workspace):
    service, root, _ = workspace
    first = add_file(service, root, "a.txt", b"first-pair")
    second = add_file(service, root, "b.txt", b"second-pair")
    first_keeper = add_file(service, root, "y.txt", b"first-pair")
    second_keeper = add_file(service, root, "z.txt", b"second-pair")
    groups = []
    with Session(get_engine()) as db:
        for index, (candidate, keeper) in enumerate(((first, first_keeper), (second, second_keeper))):
            collection = RelationGroup(session_id=service.session_id, label=f"Collection {index}")
            group = DuplicateGroup(session_id=service.session_id, dupe_type=DupeType.EXACT,
                                   group_hash=f"group-{index}", keep_file_id=keeper)
            db.add_all([collection, group])
            db.flush()
            groups.append(group.id)
            db.add(RelationMember(group_id=collection.id, file_id=candidate))
            db.add_all([DuplicateMember(group_id=group.id, file_id=candidate),
                        DuplicateMember(group_id=group.id, file_id=keeper)])
        db.commit()
    first_page = service.snapshot(limit=1)
    assert first_page["collections"][0]["label"] == "Collection 0"
    assert first_page["page"]["collections_truncated"] is True
    later_page = service.snapshot(limit=1, offset=1)
    assert later_page["files"][0]["id"] == second
    assert later_page["collections"][0]["label"] == "Collection 1"
    assert later_page["page"]["collections_shown"] == 1
    assert later_page["page"]["collections_truncated"] is False
    assert later_page["counts"]["collections"] == 2
    assert [group["id"] for group in later_page["duplicates"]] == [groups[1]]
    assert {member["id"] for member in later_page["duplicates"][0]["members"]} == {second, second_keeper}
    assert later_page["page"]["duplicates_shown"] == 1
    assert later_page["page"]["duplicates_truncated"] is True
    assert later_page["counts"]["duplicates"] == 2


def test_review_preview_apply_undo_real_files_and_reconfirmation(workspace):
    service, root, _ = workspace
    file_id = add_file(service, root, "original.txt")
    proposal = add_proposal(file_id, root / "renamed.txt")
    service.approve(proposal, proposal_row(service, proposal)["review_token"])
    original = root / "original.txt"
    preview = service.preview()
    assert preview["total"] == 1 and preview["errors"] == 0
    with pytest.raises(ReviewError, match="Confirm"):
        service.apply(preview["token"])
    service.edit(proposal, "edited.txt")
    with pytest.raises(ReviewError, match="changed"):
        service.apply(preview["token"], confirmed=True)
    assert original.exists()
    actual = service.preview()
    assert actual["items"][0]["destination"] == str(root / "edited.txt")
    assert service.apply(actual["token"], confirmed=True)["applied"] == 1
    assert (root / "edited.txt").read_bytes() == b"original.txt"
    assert not original.exists()
    assert service.history()[0]["undoable"] is True
    recovery = service.undo_preview()
    assert recovery["items"][0]["destination"] == str(original)
    with pytest.raises(ReviewError, match="Confirm"):
        service.undo(recovery["token"])
    assert service.undo(recovery["token"], confirmed=True)["undone"] == 1
    assert original.read_bytes() == b"original.txt"
    assert service.history()[0]["state"] == "undone"
    with pytest.raises(ReviewError, match="changed"):
        service.undo(recovery["token"], confirmed=True)


def test_edit_validation_shared_with_browser_and_duplicate_target_is_not_editable(workspace):
    service, root, _ = workspace
    identity = add_file(service, root, "report.txt")
    rename = add_proposal(identity, root / "renamed.txt")
    with pytest.raises(ReviewError, match="filename"):
        service.edit(rename, "../outside.txt")
    move = add_proposal(identity, root / "nested" / "report.txt", kind=ProposalType.MOVE)
    with pytest.raises(ReviewError, match="inside"):
        service.edit(move, str(root.parent / "outside.txt"))
    assert service.edit(move, str(root / "sorted" / "report.txt"))["status"] == "modified"
    _, _, _, duplicate = duplicate_pair(service, root)
    with pytest.raises(ReviewError, match="keeper"):
        service.edit(duplicate, str(root / "different.jpg"))


def test_near_duplicate_requires_displayed_pair_and_stale_keeper_invalidates_it(workspace):
    service, root, _ = workspace
    keeper, candidate, group_id, proposal = duplicate_pair(service, root)
    row = proposal_row(service, proposal)
    assert row["duplicate_evidence"]["keeper_id"] == keeper
    assert row["duplicate_evidence"]["exact_bytes"] is False
    assert service.snapshot()["duplicates"][0]["members"][1]["id"] == candidate
    with pytest.raises(ReviewError, match="Inspect"):
        service.approve(proposal)
    replacement = add_file(service, root, "replacement.jpg")
    with Session(get_engine()) as db:
        db.get(DuplicateGroup, group_id).keep_file_id = replacement
        db.add(DuplicateMember(group_id=group_id, file_id=replacement))
        db.commit()
    with pytest.raises(ReviewError, match="changed"):
        service.approve(proposal, row["review_token"])
    assert proposal_row(service, proposal)["status"] == "pending"


def test_bulk_skips_near_matches_unverified_names_and_existing_rejection(workspace):
    service, root, _ = workspace
    verified = add_file(service, root, "verified.txt")
    legacy = add_file(service, root, "legacy.txt", verified=False)
    rejected = add_file(service, root, "rejected.txt")
    yes = add_proposal(verified, root / "yes.txt")
    unknown = add_proposal(legacy, root / "unknown.txt")
    no = add_proposal(rejected, root / "no.txt", status=ProposalStatus.REJECTED)
    _, _, _, near = duplicate_pair(service, root)
    result = service.approve_clear()
    assert result["approved"] == 1
    assert result["skipped_unverified_rename"] == 1
    assert result["skipped_near_duplicate"] == 1
    states = {row["id"]: row["status"] for row in service.snapshot()["proposals"]}
    assert states == {yes: "approved", unknown: "pending", no: "rejected", near: "pending"}


def test_dependency_protection_blocks_individual_and_bulk_mutation(workspace):
    service, root, _ = workspace
    (root / "index.html").write_text('<img src="picture.png">')
    file_id = add_file(service, root, "picture.png")
    proposal = add_proposal(file_id, root / "renamed.png")
    assert proposal_row(service, proposal)["protected"] is True
    with pytest.raises(ReviewError, match="Protected"):
        service.approve(proposal)
    with pytest.raises(ReviewError, match="Protected"):
        service.edit(proposal, "renamed.png")
    assert service.approve_clear()["skipped_protected"] == 1


def test_preview_flags_collisions_changed_bytes_and_blocks_commit(workspace):
    service, root, _ = workspace
    file_id = add_file(service, root, "source.txt")
    proposal = add_proposal(file_id, root / "destination.txt")
    service.approve(proposal)
    (root / "destination.txt").write_text("occupied")
    preview = service.preview()
    assert preview["errors"] == 1
    with pytest.raises(ReviewError, match="failing"):
        service.apply(preview["token"], confirmed=True)
    (root / "destination.txt").unlink()
    preview = service.preview()
    (root / "source.txt").write_text("different")
    with pytest.raises(ReviewError, match="changed"):
        service.apply(preview["token"], confirmed=True)
    assert (root / "source.txt").read_text() == "different"


def test_live_worker_blocks_commit_even_when_slot_is_clear(workspace, monkeypatch):
    service, root, manager = workspace
    file_id = add_file(service, root, "source.txt")
    proposal = add_proposal(file_id, root / "destination.txt")
    service.approve(proposal)
    preview = service.preview()
    monkeypatch.setattr(manager, "has_live_workers", lambda: True)
    with pytest.raises(ReviewError, match="worker"):
        service.apply(preview["token"], confirmed=True)
    assert (root / "source.txt").exists()


def test_metadata_plan_runs_without_ai_and_preserves_originals(workspace, monkeypatch):
    service, root, manager = workspace
    (root / "one.txt").write_text("identical text for exact duplicate detection")
    (root / "two.txt").write_text("identical text for exact duplicate detection")
    def no_ai(**kwargs):
        pytest.fail("Metadata-only mode must not initialize AI")
    monkeypatch.setattr("donedatahoarder.ai.router.init_ai", no_ai)
    result = service.start_pipeline(metadata_only=True)
    plan_id = result["plan"]["plan_id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        plan = manager.get_run_plan(plan_id)
        if plan["state"] in {"completed", "failed", "interrupted", "cancelled"}:
            break
        time.sleep(.02)
    assert plan["state"] == "completed", service.snapshot()["job"]
    assert plan["steps"] == ["scan", "enrich", "dedup", "execute_dry"]
    assert set(plan["options"]["skipped_steps"]) == {"analyze", "relate", "propose", "organize"}
    assert service.snapshot()["counts"]["files"] == 2
    assert service.snapshot()["counts"]["duplicates"] >= 1
    assert (root / "one.txt").exists() and (root / "two.txt").exists()


def test_ready_plan_resume_uses_persisted_host_and_checkpoint(workspace, monkeypatch):
    service, root, manager = workspace
    requested = []
    monkeypatch.setattr(manager, "advance_run_plan", lambda plan_id: requested.append(plan_id))
    service.ollama_host = "http://127.0.0.1:11435"
    result = service.start_pipeline()
    plan_id = result["plan"]["plan_id"]
    reopened = WorkspaceService(session_id=service.session_id)
    reopened.resume_pipeline()
    assert requested == [plan_id, plan_id]
    assert manager.get_run_plan(plan_id)["options"]["ollama_host"] == service.ollama_host
    assert reopened.snapshot()["counts"]["files"] == 0


def test_terminal_cannot_resume_saved_cloud_plan(workspace, monkeypatch):
    service, root, manager = workspace
    manager.create_run_plan(service.session_id, ["analyze"], {"backend": "gemini"})
    dispatched = []
    monkeypatch.setattr(manager, "advance_run_plan", lambda plan_id: dispatched.append(plan_id))
    with pytest.raises(ReviewError, match="cloud backend"):
        service.resume_pipeline()
    assert dispatched == []


def test_job_dispatch_forwards_saved_host_to_ai_stages(workspace, monkeypatch):
    service, root, manager = workspace
    received = []
    for step in ("analyze", "relate", "propose", "organize"):
        monkeypatch.setattr(manager, "start_" + step, lambda *args, **kwargs: received.append(kwargs))
        plan_id = manager.create_run_plan(service.session_id, [step], {
            "backend": "ollama", "ollama_host": "http://example.test:11435",
        })
        manager.advance_run_plan(plan_id)
        with Session(get_engine()) as db:
            db.get(RunPlan, plan_id).state = "completed"
            db.commit()
    assert len(received) == 4
    assert all(options["ollama_host"] == "http://example.test:11435" for options in received)


def test_other_session_activity_is_not_reported_as_this_sessions_job(workspace, monkeypatch):
    service, root, manager = workspace
    other = WorkspaceService(root)
    active = JobInfo("foreign-job", "analyze", other.session_id)
    monkeypatch.setattr(manager, "get_active", lambda: active)
    snapshot = service.snapshot()
    assert snapshot["active_job"] is None
    assert snapshot["other_session_busy"] is True
    assert other.snapshot()["active_job"]["job_id"] == "foreign-job"
    assert other.snapshot()["other_session_busy"] is False


def test_owned_worker_liveness_survives_cleared_active_slot(workspace):
    service, root, manager = workspace
    draining = JobInfo("draining-job", "enrich", service.session_id, state=JobState.CANCELLED)
    manager._jobs[draining.job_id] = draining
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    manager.start_tracked_worker(worker, draining.job_id)
    try:
        assert manager.get_active() is None
        snapshot = service.snapshot()
        assert snapshot["active_job"] is None
        assert snapshot["has_live_workers"] is True
        assert snapshot["owned_live_workers"] is True
        assert manager.has_live_workers("another-session") is False
        with pytest.raises(ReviewError, match="worker"):
            service.preview()
    finally:
        release.set()
        worker.join(timeout=2)
    assert service.snapshot()["owned_live_workers"] is False

"""Review decisions and commit previews stay inside the selected session."""

from pathlib import Path
from datetime import datetime
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event, Thread
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    BackgroundJob, DuplicateGroup, DuplicateMember, DupeType, File, FileStatus, Proposal,
    ProposalStatus, ProposalType, RunPlan, UserSession,
)
from donedatahoarder.db.session import get_engine
from donedatahoarder.web.app import create_app


@pytest.fixture
def review_db(tmp_path: Path, monkeypatch):
    journal = tmp_path / "journal"
    monkeypatch.setenv("DDH_DATA_DIR", str(journal))
    app = create_app(tmp_path / "review.db")
    roots = (tmp_path / "one", tmp_path / "two")
    for root in roots:
        root.mkdir()
    with Session(get_engine()) as db:
        sessions = [UserSession(name=f"session-{i}", root_path=str(root)) for i, root in enumerate(roots)]
        db.add_all(sessions)
        db.commit()
        ids = [row.id for row in sessions]
    with TestClient(app) as client:
        yield client, ids, roots


def _proposal(sid: str, root: Path, name: str, status=ProposalStatus.PENDING,
              proposal_type=ProposalType.RENAME, destination: str | None = None) -> int:
    source = root / name
    source.write_text(name, encoding="utf-8")
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(source), filename=name)
        db.add(file)
        db.flush()
        proposal = Proposal(
            file_id=file.id, proposal_type=proposal_type,
            current_value=str(source),
            proposed_value=destination or str(root / f"new-{name}"),
            confidence=0.9, status=status,
        )
        db.add(proposal)
        db.commit()
        return proposal.id


def _status(proposal_id: int) -> ProposalStatus:
    with Session(get_engine()) as db:
        return db.get(Proposal, proposal_id).status


def test_dashboard_pending_review_counts_are_scoped_and_bounded(review_db):
    client, (one, two), (root_one, root_two) = review_db
    _proposal(one, root_one, "rename.txt", proposal_type=ProposalType.RENAME)
    _proposal(one, root_one, "move.txt", proposal_type=ProposalType.MOVE)
    _proposal(one, root_one, "done.txt", status=ProposalStatus.APPROVED,
              proposal_type=ProposalType.RENAME)
    _proposal(two, root_two, "other.txt", proposal_type=ProposalType.MARK_DUPLICATE)

    first = client.get(f"/api/stats?session_id={one}")
    assert first.status_code == 200
    assert first.json()["proposal_counts"] == {"pending": 2, "approved": 1}
    assert first.json()["pending_by_type"] == {"rename": 1, "move": 1}
    second = client.get(f"/api/stats?session_id={two}")
    assert second.json()["pending_by_type"] == {"mark_duplicate": 1}
    assert client.get("/api/stats").json()["pending_by_type"] == {
        "rename": 1, "move": 1, "mark_duplicate": 1,
    }


def test_saved_results_require_explicit_session_owner(review_db, tmp_path, monkeypatch):
    from donedatahoarder.web import results_manager

    monkeypatch.setattr(results_manager, "RESULTS_DIR", tmp_path / "saved-results")
    client, (one, two), (root_one, root_two) = review_db
    first = _proposal(one, root_one, "first.txt")
    _proposal(two, root_two, "second.txt")
    with Session(get_engine()) as db:
        file_id = db.get(Proposal, first).file_id
        group = DuplicateGroup(session_id=one, dupe_type=DupeType.EXACT,
                               group_hash="snapshot-group", keep_file_id=file_id)
        db.add(group)
        db.flush()
        db.add(DuplicateMember(group_id=group.id, file_id=file_id))
        db.commit()

    assert client.post("/api/results/save/files").status_code == 422
    for kind in ("files", "proposals", "duplicates"):
        saved = client.post(f"/api/results/save/{kind}", params={"session_id": one, "name": "review"})
        assert saved.status_code == 200, saved.text
        filename = saved.json()["filename"]
        own = client.get(f"/api/results/load/{filename}",
                         params={"session_id": one, "result_type": kind})
        assert own.status_code == 200
        assert own.json()["session_id"] == one
        assert len(own.json()["data"]["items"]) == 1
        assert client.get(f"/api/results/load/{filename}",
                          params={"session_id": two, "result_type": kind}).status_code == 404
        assert client.get(f"/api/results/load/{filename}",
                          params={"session_id": one, "result_type": "files" if kind != "files" else "proposals"}).status_code == 404
        listed = client.get("/api/results/list", params={"session_id": one, "result_type": kind})
        assert [item["filename"] for item in listed.json()] == [filename]
        assert client.get("/api/results/list", params={"session_id": two, "result_type": kind}).json() == []

    legacy = results_manager.RESULTS_DIR / "legacy.json"
    legacy.write_text(json.dumps({"type": "files", "saved_at": "old", "data": {"items": []}}),
                      encoding="utf-8")
    assert client.get("/api/results/load/legacy.json",
                      params={"session_id": one, "result_type": "files"}).status_code == 409
    assert "legacy.json" not in [row["filename"] for row in client.get(
        "/api/results/list", params={"session_id": one, "result_type": "files"}).json()]

    def bounded_groups(page, per_page, session_id):
        assert (page, per_page, session_id) == (1, 100, one)
        return {"items": [{"id": number} for number in range(100)], "total": 125}

    monkeypatch.setattr("donedatahoarder.web.api.results.list_duplicates", bounded_groups)
    capped = client.post("/api/results/save/duplicates",
                         params={"session_id": one, "name": "capped"})
    assert capped.status_code == 200
    assert "100 of 125" in capped.json()["message"]
    retained = client.get(f"/api/results/load/{capped.json()['filename']}",
                          params={"session_id": one, "result_type": "duplicates"}).json()
    assert len(retained["data"]["items"]) == 100
    assert retained["data"]["total"] == 125


def test_bulk_review_only_changes_selected_session(review_db):
    client, (one, two), (root_one, root_two) = review_db
    first = _proposal(one, root_one, "first.txt")
    second = _proposal(two, root_two, "second.txt")
    with Session(get_engine()) as db:
        file = db.get(File, db.get(Proposal, first).file_id)
        file.analysis_outcome = "content_verified"
        file.analysis_evidence_source = "text"
        db.commit()

    response = client.post("/api/proposals/bulk-approve", json={"session_id": one, "min_confidence": 0.8})
    assert response.status_code == 200
    assert response.json()["approved"] == 1
    assert _status(first) == ProposalStatus.APPROVED
    assert _status(second) == ProposalStatus.PENDING

    response = client.post("/api/proposals/bulk-reject", json={"session_id": two})
    assert response.status_code == 200
    assert response.json()["rejected"] == 1
    assert _status(second) == ProposalStatus.REJECTED
    assert client.post("/api/proposals/bulk-approve", json={"session_id": "missing"}).status_code == 404
    assert client.post("/api/proposals/bulk-reject", json={}).status_code == 422


def test_bulk_rename_requires_verified_content_but_allows_individual_review(review_db):
    client, (sid, _), (root, _) = review_db
    proposal_id = _proposal(sid, root, "legacy.txt")
    bulk = client.post("/api/proposals/bulk-approve", json={"session_id": sid})
    assert bulk.status_code == 200
    assert bulk.json()["approved"] == 0
    assert bulk.json()["skipped_unverified_rename"] == 1
    listed = client.get(f"/api/proposals?session_id={sid}").json()["items"]
    assert listed[0]["analysis_outcome"] is None
    assert _status(proposal_id) == ProposalStatus.PENDING
    assert client.post(f"/api/proposals/{proposal_id}/approve", json={"session_id": sid}).status_code == 200
    assert _status(proposal_id) == ProposalStatus.APPROVED


def test_proposal_date_labels_distinguish_source_and_legacy_dates(review_db):
    client, (sid, _), (root, _) = review_db
    cases = [
        ("report.docx", "2018-01-17_policy_report.docx", None,
         datetime(2018, 1, 17), "Matches filesystem modified date; event date unverified"),
        ("IMG_1234.jpg", "2020-05-06_family_photo.jpg", datetime(2020, 5, 6),
         datetime(2026, 9, 28), "Stored photo EXIF metadata; capture date unverified"),
        ("notes_2021.10.12.docx", "2021-10-12_meeting_notes.docx", None,
         datetime(2019, 5, 1), "Original filename date identifier; event date unverified"),
        ("unknown.txt", "2017-02-03_unknown.txt", None,
         datetime(2019, 5, 1), "Unverified date in proposed name"),
    ]
    for source_name, proposed_name, exif, modified, _ in cases:
        proposal_id = _proposal(sid, root, source_name, destination=str(root / proposed_name))
        with Session(get_engine()) as db:
            file = db.get(File, db.get(Proposal, proposal_id).file_id)
            file.date_exif = exif
            file.date_modified = modified
            db.commit()
    response = client.get(f"/api/proposals?session_id={sid}&per_page=10")
    assert response.status_code == 200
    labels = {item["filename"]: item["name_date_source"] for item in response.json()["items"]}
    assert labels == {source_name: expected for source_name, _, _, _, expected in cases}


def test_review_mutations_and_preview_reject_concurrent_writer(review_db):
    client, (sid, _), (root, _) = review_db
    proposal_id = _proposal(sid, root, "busy.txt")
    from donedatahoarder.core.process_lock import operation_lock

    locked = Event()
    release = Event()

    def hold_writer():
        with operation_lock("test writer"):
            locked.set()
            assert release.wait(5)

    worker = Thread(target=hold_writer)
    worker.start()
    try:
        assert locked.wait(5)
        edit = client.post(f"/api/proposals/{proposal_id}/edit", json={
            "session_id": sid, "proposed_value": "new-busy.txt",
        })
        assert edit.status_code == 409
        preview = client.get("/api/execute/preview", params={"session_id": sid})
        assert preview.status_code == 409
        settings = client.patch(f"/api/sessions/{sid}", json={"model": "example:1b"})
        assert settings.status_code == 409
        assert _status(proposal_id) == ProposalStatus.PENDING
    finally:
        release.set()
        worker.join(timeout=5)


def test_run_plan_is_persisted_and_scoped_to_session(review_db, monkeypatch):
    client, (one, two), (root, _) = review_db
    from donedatahoarder.core.jobs import job_manager

    # Keep dispatch out of this API-contract test. Durable worker/checkpoint
    # behavior has its own integration tests.
    monkeypatch.setattr(job_manager, "advance_run_plan", lambda _plan_id: None)
    with Session(get_engine()) as db:
        owner = db.get(UserSession, one)
        owner.analyze_model = "gemma4:26b"
        owner.propose_model = "gemma4:26b"
        db.commit()
    response = client.post("/api/pipeline/runs", json={
        "session_id": one, "root_path": str(root), "steps": ["scan", "analyze"],
    })
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["state"] == "ready"
    assert plan["steps"] == ["scan", "analyze"]
    assert plan["options"]["analyze_model"] == "gemma4:26b"
    assert plan["options"]["propose_model"] == "gemma4:26b"
    with Session(get_engine()) as db:
        assert db.get(RunPlan, plan["plan_id"]) is not None
    latest = client.get("/api/pipeline/runs/latest", params={"session_id": one})
    assert latest.json()["plan"]["plan_id"] == plan["plan_id"]
    assert client.get(f"/api/pipeline/runs/{plan['plan_id']}", params={"session_id": two}).status_code == 404
    assert client.post(f"/api/pipeline/runs/{plan['plan_id']}/cancel", json={"session_id": two}).status_code == 404
    cancelled = client.post(f"/api/pipeline/runs/{plan['plan_id']}/cancel", json={"session_id": one})
    assert cancelled.status_code == 200
    assert cancelled.json()["plan"]["state"] == "cancelled"


def test_provider_errors_are_actionable_and_web_retry_is_explicit(review_db, monkeypatch):
    client, (sid, _), (root, _) = review_db
    from donedatahoarder.core.jobs import job_manager
    with Session(get_engine()) as db:
        owner = db.get(UserSession, sid)
        owner.analyze_model = "gemma4:26b"
        db.add_all([
            File(session_id=sid, path=str(root / "timeout.txt"), filename="timeout.txt",
                 status=FileStatus.ERROR, analysis_reason="provider_timeout"),
            File(session_id=sid, path=str(root / "unknown.bin"), filename="unknown.bin",
                 status=FileStatus.ERROR, analysis_reason="unsupported_type"),
        ])
        db.commit()
    counts = client.get("/api/pipeline/analyze/errors", params={"session_id": sid})
    assert counts.status_code == 200
    assert counts.json() == {"total": 2, "retryable": 1,
                             "reasons": {"provider_timeout": 1, "unsupported_type": 1}}
    called = {}

    def fake_start_analyze(**kwargs):
        called.update(kwargs)
        return "synthetic-job"

    monkeypatch.setattr(job_manager, "start_analyze", fake_start_analyze)
    started = client.post("/api/pipeline/analyze", json={
        "session_id": sid, "retry_errors": True,
    })
    assert started.status_code == 200, started.text
    assert started.json()["job_id"] == "synthetic-job"
    assert called["retry_errors"] is True
    assert called["model"] == "gemma4:26b"


def test_cancel_endpoint_reports_cancelling_until_worker_exit(review_db, monkeypatch):
    from types import SimpleNamespace
    from donedatahoarder.core.jobs import JobState, job_manager

    client, _, _ = review_db
    monkeypatch.setattr(job_manager, "force_cancel", lambda _job_id: None)
    monkeypatch.setattr(job_manager, "get_job", lambda _job_id: SimpleNamespace(state=JobState.CANCELLING))
    response = client.post("/api/pipeline/jobs/synthetic-job/cancel")
    assert response.status_code == 200
    assert response.json() == {"status": "cancelling", "job_id": "synthetic-job"}


def test_relate_numeric_directory_progress_keeps_job_live(review_db, monkeypatch):
    from donedatahoarder.core.jobs import JobState, job_manager

    _, (session_id, _), _ = review_db
    entered = Event()
    release = Event()
    second_entered = Event()
    release_second = Event()

    def fake_relate(*, progress_cb, **_kwargs):
        progress_cb({"phase": "directory_complete", "done": 1,
                     "directories": 1, "groups": 1})
        entered.set()
        assert release.wait(5)
        progress_cb({"phase": "directory_complete", "done": 2,
                     "directories": 2, "groups": 2})
        second_entered.set()
        assert release_second.wait(12)
        return {"directories": 2, "groups": 2}

    monkeypatch.setattr("donedatahoarder.ai.router.init_ai", lambda **_kwargs: None)
    monkeypatch.setattr("donedatahoarder.ai.router.get_client", lambda: None)
    monkeypatch.setattr("donedatahoarder.core.relate.relate", fake_relate)
    job_id = job_manager.start_relate(session_id)
    try:
        assert entered.wait(5)
        deadline = time.monotonic() + 5
        while job_manager.get_job(job_id).progress.get("directories_done") != 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        job = job_manager.get_job(job_id)
        assert job.progress["done"] == 1
        assert job.state == JobState.RUNNING
        assert job_manager.get_active().job_id == job_id
        assert job_manager.has_live_workers()
        # A newly attached SSE subscriber must not treat numeric count as terminal.
        stream = job_manager.subscribe(job_id)
        assert next(stream)["done"] == 1
        # Reproduce the valid initial-snapshot/queued-update race explicitly.
        job.push_progress(job.progress.copy())
        release.set()
        assert second_entered.wait(5)
        deadline = time.monotonic() + 8
        for _ in range(6):
            update = next(stream)
            assert update.get("done") is not True
            if update.get("done") == 2:
                break
            assert update.get("done") == 1 or update.get("heartbeat") is True
            assert time.monotonic() < deadline
        else:
            pytest.fail("SSE stream did not reach the second numeric directory update")
        assert job_manager.get_job(job_id).state == JobState.RUNNING
        release_second.set()
        deadline = time.monotonic() + 8
        for _ in range(6):
            update = next(stream)
            if update.get("done") is True:
                break
            assert update.get("done") in (1, 2) or update.get("heartbeat") is True
            assert time.monotonic() < deadline
        else:
            pytest.fail("SSE stream did not reach Boolean completion")
        stream.close()
    finally:
        release.set()
        release_second.set()
    deadline = time.monotonic() + 5
    while job_manager.get_job(job_id).state == JobState.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert job_manager.get_job(job_id).state == JobState.COMPLETED
    assert job_manager.get_job(job_id).progress["directories_done"] == 2


def test_relate_cancel_waits_for_blocked_model_worker(review_db, monkeypatch):
    from donedatahoarder.core.jobs import JobState, job_manager

    _, (session_id, _), _ = review_db
    entered = Event()
    release = Event()
    after_model = Event()

    def fake_relate(*, progress_cb, **_kwargs):
        progress_cb({"phase": "grouping", "done": 0, "directories": 1,
                     "chunk_active": 1, "groups": 0})
        entered.set()
        assert release.wait(8)
        # This callback must abort before any post-model write can occur.
        progress_cb({"phase": "grouping", "done": 0, "directories": 1,
                     "chunk_done": 1, "groups": 0})
        after_model.set()
        return {"directories": 1, "groups": 1}

    monkeypatch.setattr("donedatahoarder.ai.router.init_ai", lambda **_kwargs: None)
    monkeypatch.setattr("donedatahoarder.ai.router.get_client", lambda: None)
    monkeypatch.setattr("donedatahoarder.core.relate.relate", fake_relate)
    job_id = job_manager.start_relate(session_id)
    try:
        assert entered.wait(5)
        job_manager.force_cancel(job_id)
        deadline = time.monotonic() + 5
        while job_manager.get_job(job_id).progress.get("phase") != "cancelling":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job_manager.get_job(job_id).progress["heartbeat"] is True
        assert job_manager.get_job(job_id).state == JobState.CANCELLING
        assert job_manager.get_active().job_id == job_id
        assert job_manager.has_live_workers()
        with pytest.raises(RuntimeError, match="running|worker|lease|live|cancelling"):
            job_manager._create_job("test", session_id)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while job_manager.get_job(job_id).state == JobState.CANCELLING:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert job_manager.get_job(job_id).state == JobState.CANCELLED
    assert not after_model.is_set()


@pytest.mark.parametrize("state", ["paused", "running"])
def test_session_delete_waits_for_foreign_live_worker_but_allows_other_session(review_db, state):
    from donedatahoarder.core.job_store import process_started_at

    client, (one, two), _ = review_db
    job_id = f"foreign-{state}"
    with Session(get_engine()) as db:
        db.add(BackgroundJob(id=job_id, session_id=one, job_type="analyze",
                             state=state, owner_pid=os.getpid(),
                             owner_token="foreign-owner",
                             owner_started_at=process_started_at(os.getpid())))
        db.commit()
    response = client.delete(f"/api/sessions/{one}")
    assert response.status_code == 409
    assert "worker to exit" in response.json()["detail"]
    with Session(get_engine()) as db:
        assert db.get(UserSession, one) is not None
        assert db.get(BackgroundJob, job_id).cancel_requested is True
    unrelated = client.delete(f"/api/sessions/{two}")
    assert unrelated.status_code == 200, unrelated.text


def test_protected_resource_cannot_be_approved_and_bulk_skips_it(review_db):
    client, (sid, _), (root, _) = review_db
    proposal_id = _proposal(sid, root, "CADFONT.SHX")
    response = client.post(f"/api/proposals/{proposal_id}/approve", json={"session_id": sid})
    assert response.status_code == 409
    assert "Protected resource" in response.json()["detail"]
    edited = client.post(f"/api/proposals/{proposal_id}/edit", json={
        "session_id": sid, "proposed_value": "renamed.SHX",
    })
    assert edited.status_code == 409
    assert "Protected resource" in edited.json()["detail"]
    response = client.post("/api/proposals/bulk-approve", json={"session_id": sid})
    assert response.status_code == 200
    assert response.json()["approved"] == 0
    assert response.json()["skipped_protected"] == 1
    assert _status(proposal_id) == ProposalStatus.PENDING
    listed = client.get(f"/api/proposals?session_id={sid}").json()["items"]
    assert listed[0]["protected"] is True
    assert listed[0]["protection_reason"]


def test_near_duplicate_requires_individual_review_not_bulk(review_db):
    client, (sid, _), (root, _) = review_db
    victim_id = _proposal(sid, root, "victim.png", proposal_type=ProposalType.MARK_DUPLICATE,
                          destination=str(root / "keeper.png"))
    keeper_path = root / "keeper.png"
    keeper_path.write_bytes(b"different image")
    with Session(get_engine()) as db:
        keeper = File(session_id=sid, path=str(keeper_path), filename="keeper.png")
        db.add(keeper)
        db.flush()
        group = DuplicateGroup(session_id=sid, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="near-pair", keep_file_id=keeper.id)
        db.add(group)
        db.flush()
        proposal = db.get(Proposal, victim_id)
        proposal.duplicate_group_id = group.id
        db.add_all([DuplicateMember(group_id=group.id, file_id=keeper.id, similarity_score=1.0),
                    DuplicateMember(group_id=group.id, file_id=proposal.file_id, similarity_score=0.8,
                                    distance_to_keeper=13)])
        db.commit()
    response = client.post("/api/proposals/bulk-approve", json={"session_id": sid})
    assert response.json()["approved"] == 0
    assert response.json()["skipped_near_duplicate"] == 1
    review = client.get(f"/api/proposals?session_id={sid}").json()["items"][0]
    evidence = review["duplicate_evidence"]
    comparison = {
        "session_id": sid,
        "expected_duplicate_group_id": evidence["group_id"],
        "expected_duplicate_type": evidence["type"],
        "expected_keeper_id": evidence["keeper_id"],
        "expected_candidate_path": review["file_path"],
        "expected_keeper_path": evidence["keeper_path"],
    }
    assert client.post(f"/api/proposals/{victim_id}/approve", json={"session_id": sid}).status_code == 409
    assert client.post(f"/api/proposals/{victim_id}/approve", json={
        **comparison, "expected_keeper_id": -1,
    }).status_code == 409
    with Session(get_engine()) as db:
        other = File(session_id=sid, path=str(root / "other.png"), filename="other.png")
        db.add(other)
        db.flush()
        db.get(DuplicateGroup, evidence["group_id"]).keep_file_id = other.id
        db.commit()
    assert client.post(f"/api/proposals/{victim_id}/approve", json=comparison).status_code == 409
    with Session(get_engine()) as db:
        db.get(DuplicateGroup, evidence["group_id"]).keep_file_id = evidence["keeper_id"]
        assert db.get(Proposal, victim_id).status == ProposalStatus.PENDING
        db.commit()
    assert client.post(f"/api/proposals/{victim_id}/approve", json=comparison).status_code == 200
    with Session(get_engine()) as db:
        assert db.get(Proposal, victim_id).review_kind == "individual"
    listed = client.get(f"/api/proposals?session_id={sid}&status=approved").json()["items"]
    assert listed[0]["duplicate_evidence"]["distance_to_keeper"] == 13
    group = client.get(f"/api/duplicates?session_id={sid}").json()["items"][0]
    candidate = next(file for file in group["files"] if file["filename"] == "victim.png")
    assert candidate["distance_to_keeper"] == 13
    assert candidate["exact_bytes_to_keeper"] is None
    assert group["evidence_label"].startswith("Visual similarity")


def test_exact_md5_group_reports_unknown_stored_sha_until_available(review_db):
    from donedatahoarder.core.dedup import find_exact_duplicates

    client, (sid, _), (root, _) = review_db
    payload = b"same indexed content"
    digest = hashlib.md5(payload).hexdigest()
    with Session(get_engine()) as db:
        files = []
        for name in ("copy-one.txt", "copy-two.txt"):
            path = root / name
            path.write_bytes(payload)
            file = File(session_id=sid, path=str(path), filename=name,
                        status=FileStatus.ENRICHED, hash_md5=digest)
            db.add(file)
            files.append(file)
        db.commit()
        file_ids = [file.id for file in files]

    assert find_exact_duplicates(session_id=sid) == {"groups": 1, "duplicates": 1}
    with Session(get_engine()) as db:
        group = db.query(DuplicateGroup).filter_by(session_id=sid, dupe_type=DupeType.EXACT).one()
        victim_id = next(file_id for file_id in file_ids if file_id != group.keep_file_id)
        keeper = db.get(File, group.keep_file_id)
        victim = db.get(File, victim_id)
        db.add(Proposal(file_id=victim_id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=victim.path, proposed_value=keeper.path,
                        duplicate_group_id=group.id, status=ProposalStatus.PENDING))
        db.commit()

    group_json = client.get(f"/api/duplicates?session_id={sid}").json()["items"][0]
    candidate = next(file for file in group_json["files"] if not file["is_keeper"])
    evidence = client.get(f"/api/proposals?session_id={sid}").json()["items"][0]["duplicate_evidence"]
    assert candidate["matching_indexed_md5"] is True
    assert candidate["exact_bytes_to_keeper"] is None
    assert evidence["matching_indexed_md5"] is True
    assert evidence["exact_bytes"] is None
    assert "MD5" in group_json["evidence_label"]
    assert "SHA-256" in group_json["evidence_label"]

    with Session(get_engine()) as db:
        db.get(File, victim_id).hash_sha256 = hashlib.sha256(payload).hexdigest()
        db.get(File, group_json["keep_file_id"]).hash_sha256 = hashlib.sha256(payload).hexdigest()
        db.commit()
    assert client.get(f"/api/proposals?session_id={sid}").json()["items"][0]["duplicate_evidence"]["exact_bytes"] is True
    with Session(get_engine()) as db:
        db.get(File, victim_id).hash_sha256 = hashlib.sha256(b"different indexed content").hexdigest()
        db.commit()
    assert client.get(f"/api/proposals?session_id={sid}").json()["items"][0]["duplicate_evidence"]["exact_bytes"] is False


def test_individual_review_is_session_bound_and_edit_cannot_escape_root(review_db):
    client, (one, two), (root_one, _) = review_db
    rename = _proposal(one, root_one, "rename.txt")
    move = _proposal(one, root_one, "move.txt", proposal_type=ProposalType.MOVE)
    folder = _proposal(one, root_one, "folder.txt", proposal_type=ProposalType.RENAME_FOLDER)

    for endpoint in ("approve", "reject", "edit"):
        payload = {"session_id": two, "proposed_value": "safe.txt"}
        assert client.post(f"/api/proposals/{rename}/{endpoint}", json=payload).status_code == 404
    for name in ("../escape.txt", r"..\escape.txt", "C:escape.txt", "nested/file.txt"):
        response = client.post(f"/api/proposals/{rename}/edit", json={"session_id": one, "proposed_value": name})
        assert response.status_code == 400, name
    for proposal_id in (move, folder):
        response = client.post(f"/api/proposals/{proposal_id}/edit", json={"session_id": one, "proposed_value": str(root_one.parent / "outside.txt")})
        assert response.status_code == 400
        response = client.post(f"/api/proposals/{proposal_id}/edit", json={"session_id": one, "proposed_value": str(root_one / "sub" / "inside.txt")})
        assert response.status_code == 200

    response = client.post(f"/api/proposals/{rename}/edit", json={"session_id": one, "proposed_value": "safe.txt"})
    assert response.status_code == 200
    assert response.json()["proposed_value"] == str(root_one / "safe.txt")


def test_preview_matches_reviewed_selection_and_stale_token_blocks_commit(review_db):
    client, (one, two), (root_one, root_two) = review_db
    approved = _proposal(one, root_one, "approved.txt", ProposalStatus.APPROVED)
    pending = _proposal(one, root_one, "pending.txt")
    foreign = _proposal(two, root_two, "foreign.txt", ProposalStatus.APPROVED)

    preview = client.get("/api/execute/preview", params={"session_id": one})
    assert preview.status_code == 200
    snapshot = preview.json()
    assert snapshot["total"] == 1
    assert [item["id"] for item in snapshot["items"]] == [approved]
    assert snapshot["items"][0]["source"] == str(root_one / "approved.txt")

    assert client.post("/api/execute", json={"session_id": one, "dry_run": False}).status_code == 400
    assert client.post(f"/api/proposals/{pending}/approve", json={"session_id": one}).status_code == 200
    stale = client.post("/api/execute", json={"session_id": one, "dry_run": False, "preview_token": snapshot["token"]})
    assert stale.status_code == 409
    assert (root_one / "approved.txt").exists()

    fresh = client.get("/api/execute/preview", params={"session_id": one}).json()
    assert set(item["id"] for item in fresh["items"]) == {approved, pending}
    committed = client.post("/api/execute", json={"session_id": one, "dry_run": False, "preview_token": fresh["token"]})
    assert committed.status_code == 200, committed.text
    assert committed.json()["applied"] == 2
    assert (root_one / "new-approved.txt").exists()
    assert (root_one / "new-pending.txt").exists()
    assert (root_two / "foreign.txt").exists()
    assert _status(foreign) == ProposalStatus.APPROVED
    assert (root_one.parent / "journal" / f"undo_{one}.log").exists()


def test_empty_preview_cannot_commit(review_db):
    client, (one, _), _ = review_db
    preview = client.get("/api/execute/preview", params={"session_id": one}).json()
    assert preview["total"] == 0
    response = client.post("/api/execute", json={"session_id": one, "dry_run": False, "preview_token": preview["token"]})
    assert response.status_code == 400


def test_preview_uses_ordered_rename_then_move_destination(review_db):
    client, (sid, _), (root, _) = review_db
    rename_id = _proposal(sid, root, "a.txt", ProposalStatus.APPROVED,
                          ProposalType.RENAME, str(root / "b.txt"))
    with Session(get_engine()) as db:
        file_id = db.get(Proposal, rename_id).file_id
        move = Proposal(file_id=file_id, proposal_type=ProposalType.MOVE,
                        current_value=str(root / "a.txt"),
                        proposed_value=str(root / "sorted" / "a.txt"),
                        status=ProposalStatus.APPROVED, confidence=0.9)
        db.add(move)
        db.commit()
        move_id = move.id
    preview = client.get("/api/execute/preview", params={"session_id": sid}).json()
    assert preview["errors"] == 0
    by_id = {item["id"]: item for item in preview["items"]}
    assert by_id[rename_id]["destination"] == str(root / "b.txt")
    assert by_id[move_id]["source"] == str(root / "b.txt")
    assert by_id[move_id]["destination"] == str(root / "sorted" / "b.txt")
    committed = client.post("/api/execute", json={
        "session_id": sid, "dry_run": False, "preview_token": preview["token"],
    })
    assert committed.status_code == 200
    assert (root / "sorted" / "b.txt").is_file()


def test_preview_projects_folder_rename_into_child_move(review_db):
    client, (sid, _), (root, _) = review_db
    old = root / "old"
    old.mkdir()
    source = old / "one.txt"
    source.write_text("content", encoding="utf-8")
    with Session(get_engine()) as db:
        file = File(session_id=sid, path=str(source), filename=source.name)
        db.add(file)
        db.flush()
        folder = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                          current_value=str(old), proposed_value=str(root / "new"),
                          status=ProposalStatus.APPROVED, confidence=0.9)
        move = Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                        current_value=str(source), proposed_value=str(old / "final" / source.name),
                        status=ProposalStatus.APPROVED, confidence=0.9)
        db.add_all([folder, move])
        db.commit()
        move_id = move.id
    preview = client.get("/api/execute/preview", params={"session_id": sid}).json()
    by_id = {item["id"]: item for item in preview["items"]}
    assert preview["errors"] == 0
    assert by_id[move_id]["source"] == str(root / "new" / "one.txt")
    assert by_id[move_id]["destination"] == str(root / "new" / "final" / "one.txt")


def test_preview_flags_destination_swap_before_commit(review_db):
    client, (sid, _), (root, _) = review_db
    _proposal(sid, root, "a.txt", ProposalStatus.APPROVED,
              ProposalType.RENAME, str(root / "b.txt"))
    _proposal(sid, root, "b.txt", ProposalStatus.APPROVED,
              ProposalType.RENAME, str(root / "a.txt"))
    preview = client.get("/api/execute/preview", params={"session_id": sid}).json()
    assert preview["errors"] == 2
    assert all(item["error"] for item in preview["items"])
    committed = client.post("/api/execute", json={
        "session_id": sid, "dry_run": False, "preview_token": preview["token"],
    })
    assert committed.status_code == 409
    assert (root / "a.txt").is_file() and (root / "b.txt").is_file()


def test_edit_during_commit_revalidation_invalidates_old_preview(review_db, monkeypatch):
    from donedatahoarder.web.api import proposals as proposal_api
    from donedatahoarder import executor

    client, (one, _), (root_one, _) = review_db
    proposal_id = _proposal(one, root_one, "race.txt", ProposalStatus.APPROVED)
    token = client.get("/api/execute/preview", params={"session_id": one}).json()["token"]
    editing = Event()
    release_edit = Event()
    original_validate = proposal_api._validated_edit
    execute_called = []

    def hold_edit(*args):
        editing.set()
        assert release_edit.wait(5)
        return original_validate(*args)

    monkeypatch.setattr(proposal_api, "_validated_edit", hold_edit)
    monkeypatch.setattr(executor, "execute", lambda **kwargs: execute_called.append(kwargs))

    with ThreadPoolExecutor(max_workers=2) as pool:
        edited = pool.submit(client.post, f"/api/proposals/{proposal_id}/edit",
                             json={"session_id": one, "proposed_value": "changed.txt"})
        assert editing.wait(5)
        committed = pool.submit(client.post, "/api/execute",
                                json={"session_id": one, "dry_run": False, "preview_token": token})
        release_edit.set()
        assert edited.result(timeout=5).status_code == 200
        response = committed.result(timeout=5)

    assert response.status_code == 409
    assert execute_called == []
    assert (root_one / "race.txt").exists()


def test_parallel_commits_cannot_apply_same_preview_twice(review_db, monkeypatch):
    from donedatahoarder import executor

    client, (one, _), (root_one, _) = review_db
    proposal_id = _proposal(one, root_one, "once.txt", ProposalStatus.APPROVED)
    token = client.get("/api/execute/preview", params={"session_id": one}).json()["token"]
    executing = Event()
    release_execute = Event()
    calls = []

    def held_execute(**kwargs):
        calls.append(kwargs)
        executing.set()
        assert release_execute.wait(5)
        with Session(get_engine()) as db:
            db.get(Proposal, proposal_id).status = ProposalStatus.APPLIED
            db.commit()
        return {"applied": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(executor, "execute", held_execute)
    payload = {"session_id": one, "dry_run": False, "preview_token": token}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/api/execute", json=payload)
        assert executing.wait(5)
        second = pool.submit(client.post, "/api/execute", json=payload)
        release_execute.set()
        responses = (first.result(timeout=5), second.result(timeout=5))

    assert [response.status_code for response in responses] == [200, 409]
    assert len(calls) == 1


def test_cancelled_but_live_worker_blocks_commit(review_db, monkeypatch):
    from donedatahoarder.core import wake_lock
    from donedatahoarder.core.jobs import JobState, job_manager

    client, (one, _), (root_one, _) = review_db
    _proposal(one, root_one, "worker.txt", ProposalStatus.APPROVED)
    token = client.get("/api/execute/preview", params={"session_id": one}).json()["token"]
    monkeypatch.setattr(wake_lock, "acquire", lambda: None)
    monkeypatch.setattr(wake_lock, "release", lambda: None)
    running = Event()
    exit_worker = Event()
    job = job_manager._create_job("test", one)

    def worker():
        try:
            running.set()
            assert exit_worker.wait(5)
        finally:
            job_manager._finish_job(job, JobState.CANCELLED)

    try:
        job_manager._start_worker(job, worker)
        assert running.wait(5)
        job_manager.force_cancel(job.job_id)
        # Durable cancellation retains the active slot until the worker exits.
        assert job_manager.get_active().state.value == "cancelling"
        assert job_manager.has_live_workers()
        response = client.post("/api/execute", json={
            "session_id": one, "dry_run": False, "preview_token": token,
        })
        assert response.status_code == 409
        assert (root_one / "worker.txt").exists()
    finally:
        exit_worker.set()
        worker_thread = job_manager._worker_threads.get(job.job_id)
        if worker_thread is not None:
            worker_thread.join(timeout=5)
        assert not job_manager.has_live_workers()
        assert job_manager.get_active() is None


def test_keeper_change_invalidates_duplicate_commit_preview(review_db):
    client, (one, _), (root_one, _) = review_db
    victim_id = _proposal(
        one, root_one, "victim.txt", ProposalStatus.APPROVED,
        ProposalType.MARK_DUPLICATE, str(root_one / "keeper.txt"),
    )
    keeper_id = _proposal(one, root_one, "keeper.txt")
    alternate_id = _proposal(one, root_one, "alternate.txt")
    with Session(get_engine()) as db:
        victim_file = db.get(Proposal, victim_id).file_id
        keeper_file = db.get(Proposal, keeper_id).file_id
        alternate_file = db.get(Proposal, alternate_id).file_id
        group = DuplicateGroup(session_id=one, dupe_type=DupeType.EXACT,
                               group_hash="review-keeper", keep_file_id=keeper_file)
        db.add(group)
        db.flush()
        db.add_all([
            DuplicateMember(group_id=group.id, file_id=file_id)
            for file_id in (victim_file, keeper_file, alternate_file)
        ])
        db.commit()
        group_id = group.id
    token = client.get("/api/execute/preview", params={"session_id": one}).json()["token"]
    assert client.post(f"/api/duplicates/{group_id}/keeper",
                       json={"session_id": one, "keep_file_id": alternate_file}).status_code == 200
    changed = client.get("/api/execute/preview", params={"session_id": one}).json()
    assert changed["token"] != token
    assert client.post("/api/execute", json={
        "session_id": one, "dry_run": False, "preview_token": token,
    }).status_code == 409


def test_keeper_change_resets_linked_approval_for_re_review(review_db):
    client, (sid, _), (root, _) = review_db
    victim_id = _proposal(sid, root, "victim.txt", ProposalStatus.APPROVED,
                          ProposalType.MARK_DUPLICATE, str(root / "keeper.txt"))
    keeper_id = _proposal(sid, root, "keeper.txt")
    alternate_id = _proposal(sid, root, "alternate.txt")
    with Session(get_engine()) as db:
        victim = db.get(Proposal, victim_id)
        keeper = db.get(Proposal, keeper_id)
        alternate = db.get(Proposal, alternate_id)
        group = DuplicateGroup(session_id=sid, dupe_type=DupeType.EXACT,
                               group_hash="keeper-reset", keep_file_id=keeper.file_id)
        db.add(group)
        db.flush()
        victim.duplicate_group_id = group.id
        victim.review_kind = "individual"
        db.add_all([DuplicateMember(group_id=group.id, file_id=file_id)
                    for file_id in (victim.file_id, keeper.file_id, alternate.file_id)])
        db.commit()
        group_id, alternate_file_id = group.id, alternate.file_id
    response = client.post(f"/api/duplicates/{group_id}/keeper", json={
        "session_id": sid, "keep_file_id": alternate_file_id,
    })
    assert response.status_code == 200
    assert response.json()["review_reset"] >= 1
    with Session(get_engine()) as db:
        victim = db.get(Proposal, victim_id)
        assert victim.status == ProposalStatus.PENDING
        assert victim.review_kind is None
        assert victim.proposed_value == str(root / "alternate.txt")


def test_duplicate_keeper_edit_cannot_bypass_group_membership(review_db):
    client, (one, two), (root_one, _) = review_db
    victim_id = _proposal(
        one, root_one, "victim.txt", proposal_type=ProposalType.MARK_DUPLICATE,
        destination=str(root_one / "keeper.txt"),
    )
    keeper_id = _proposal(one, root_one, "keeper.txt")
    unrelated_id = _proposal(one, root_one, "unrelated.txt")
    victim_path = root_one / "victim.txt"
    keeper_path = root_one / "keeper.txt"
    unrelated_path = root_one / "unrelated.txt"
    victim_path.write_bytes(b"same content")
    keeper_path.write_bytes(b"same content")
    unrelated_path.write_bytes(b"different content")

    with Session(get_engine()) as db:
        victim = db.get(File, db.get(Proposal, victim_id).file_id)
        keeper = db.get(File, db.get(Proposal, keeper_id).file_id)
        unrelated = db.get(File, db.get(Proposal, unrelated_id).file_id)
        victim.hash_md5 = hashlib.md5(victim_path.read_bytes()).hexdigest()
        group = DuplicateGroup(session_id=one, dupe_type=DupeType.EXACT,
                               group_hash=victim.hash_md5, keep_file_id=keeper.id)
        db.add(group)
        db.flush()
        db.add_all([
            DuplicateMember(group_id=group.id, file_id=file_id)
            for file_id in (victim.id, keeper.id)
        ])
        db.commit()
        group_id, unrelated_file_id, keeper_file_id = group.id, unrelated.id, keeper.id

    # The generic editor is available in the review UI, but a duplicate's
    # destination is a keeper identity rather than a freely editable path.
    edited = client.post(f"/api/proposals/{victim_id}/edit", json={
        "session_id": one, "proposed_value": str(unrelated_path),
    })
    assert edited.status_code == 400
    with Session(get_engine()) as db:
        proposal = db.get(Proposal, victim_id)
        assert proposal.proposed_value == str(keeper_path)
        assert proposal.status == ProposalStatus.PENDING

    assert client.post(f"/api/duplicates/{group_id}/keeper", json={
        "session_id": one, "keep_file_id": unrelated_file_id,
    }).status_code == 400
    assert client.post(f"/api/duplicates/{group_id}/keeper", json={
        "session_id": two, "keep_file_id": keeper_file_id,
    }).status_code == 404
    assert client.post(f"/api/duplicates/{group_id}/keeper", json={
        "keep_file_id": keeper_file_id,
    }).status_code == 422
    assert victim_path.exists()


def test_cancelled_nested_producer_still_blocks_commit_and_new_job(review_db, monkeypatch):
    from donedatahoarder.core.jobs import job_manager
    from donedatahoarder.proposals.namer import core as namer_core

    client, (one, _), (root_one, _) = review_db
    _proposal(one, root_one, "nested.txt", ProposalStatus.APPROVED)
    token = client.get("/api/execute/preview", params={"session_id": one}).json()["token"]
    running = Event()
    exit_worker = Event()
    cancelled = Event()

    def slow_propose(**kwargs):
        running.set()
        assert exit_worker.wait(10)
        return {"rename": 0}

    monkeypatch.setattr(namer_core, "generate_proposals", slow_propose)
    progress = namer_core.generate_proposals_with_progress(
        session_id=one, cancel_check=cancelled.is_set,
    )
    assert next(progress)["phase"] == "starting"
    try:
        assert next(progress)["heartbeat"] is True
        assert running.wait(5)
        cancelled.set()
        assert next(progress)["cancelled"] is True
        progress.close()
        assert job_manager.get_active() is None
        assert job_manager.has_live_workers()
        response = client.post("/api/execute", json={
            "session_id": one, "dry_run": False, "preview_token": token,
        })
        assert response.status_code == 409
        with pytest.raises(RuntimeError, match="previous pipeline worker"):
            job_manager._create_job("test", one)
    finally:
        exit_worker.set()
        for worker in list(job_manager._worker_threads.values()):
            worker.join(timeout=5)
        assert not job_manager.has_live_workers()


def test_worker_registration_is_atomic_with_thread_start():
    from donedatahoarder.core.jobs import job_manager

    starting = Event()
    allow_start = Event()
    allow_exit = Event()

    class DelayedStart(Thread):
        def start(self):
            starting.set()
            assert allow_start.wait(5)
            super().start()

    worker = DelayedStart(target=lambda: allow_exit.wait(5), daemon=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(job_manager.start_tracked_worker, worker)
            assert starting.wait(5)
            check = pool.submit(job_manager.has_live_workers)
            with pytest.raises(FutureTimeout):
                check.result(timeout=0.05)
            allow_start.set()
            start.result(timeout=5)
            assert check.result(timeout=5) is True
    finally:
        allow_start.set()
        allow_exit.set()
        if worker.ident is not None:
            worker.join(timeout=5)
    assert not job_manager.has_live_workers()


def test_completed_job_allows_immediate_next_job_after_writes(monkeypatch):
    from donedatahoarder.core import wake_lock
    from donedatahoarder.core.jobs import JobState, job_manager

    monkeypatch.setattr(wake_lock, "acquire", lambda: None)
    monkeypatch.setattr(wake_lock, "release", lambda: None)
    published = Event()
    release_publish = Event()
    finished = Event()
    job = job_manager._create_job("test", "first")
    original_push = job.push_progress

    def slow_terminal_push(progress):
        original_push(progress)
        if progress.get("done"):
            published.set()
            assert release_publish.wait(5)

    job.push_progress = slow_terminal_push
    def finish_job():
        try:
            job_manager._finish_job(job, JobState.COMPLETED)
        finally:
            finished.set()

    job_manager._start_worker(job, finish_job)
    try:
        assert published.wait(5)
        assert job_manager.get_active() is None
        # The old worker is still finishing its terminal notification, but
        # has no remaining database writes or nested producer.
        assert not job_manager.has_live_workers()
        next_job = job_manager._create_job("test", "second")
        job_manager._finish_job(next_job, JobState.CANCELLED)
    finally:
        release_publish.set()
        assert finished.wait(5)


def test_pipeline_uses_saved_models_when_request_omits_model(review_db, monkeypatch):
    from donedatahoarder.core.jobs import job_manager

    client, (one, _), _ = review_db
    with Session(get_engine()) as db:
        user_session = db.get(UserSession, one)
        user_session.model = "gemma3:12b"  # legacy fallback must not win
        user_session.analyze_model = "gemma4:26b"
        user_session.propose_model = "gemma4:26b"
        db.commit()

    started = []
    monkeypatch.setattr(job_manager, "start_analyze",
                        lambda **kwargs: started.append(("analyze", kwargs)) or "analysis-job")
    monkeypatch.setattr(job_manager, "start_propose",
                        lambda **kwargs: started.append(("propose", kwargs)) or "proposal-job")

    for step in ("analyze", "propose"):
        response = client.post(f"/api/pipeline/{step}", json={"session_id": one})
        assert response.status_code == 200, response.text
    assert [(step, kwargs["model"]) for step, kwargs in started] == [
        ("analyze", "gemma4:26b"), ("propose", "gemma4:26b"),
    ]

    for step in ("analyze", "propose"):
        response = client.post(f"/api/pipeline/{step}", json={
            "session_id": one, "model": "gemma4:e4b",
        })
        assert response.status_code == 200, response.text
    assert [(step, kwargs["model"]) for step, kwargs in started[2:]] == [
        ("analyze", "gemma4:e4b"), ("propose", "gemma4:e4b"),
    ]

    with Session(get_engine()) as db:
        user_session = db.get(UserSession, one)
        user_session.analyze_model = None
        user_session.propose_model = None
        user_session.model = ""
        db.commit()
    response = client.post("/api/pipeline/analyze", json={"session_id": one})
    assert response.status_code == 200, response.text
    assert started[-1][1]["model"] == "gemma3:12b"

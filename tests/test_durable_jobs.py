"""Durable run checkpoints and cancellation lease tests, without model calls."""

import os
import threading
import time

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.core.job_store import process_started_at
from donedatahoarder.core import job_store
from donedatahoarder.core.jobs import JobManager, JobState
from donedatahoarder.db.models import BackgroundJob, File, RunPlan, UserSession
from donedatahoarder.db.session import get_engine, init_db


@pytest.fixture
def manager(tmp_path, monkeypatch):
    init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(JobManager, "_instance", None)
    return JobManager()


def _session(root):
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root))
        db.add(user)
        db.commit()
        return user.id


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def test_plan_scan_checkpoint_survives_manager_restart(manager, tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")
    sid = _session(root)
    plan_id = manager.create_run_plan(sid, ["scan"], {"root_path": str(root), "workers": 1})
    job_id = manager.advance_run_plan(plan_id)
    _wait_until(lambda: manager.get_run_plan(plan_id)["state"] == "completed")
    plan = manager.get_run_plan(plan_id)
    assert plan["completed_steps"] == ["scan"]
    assert plan["current_index"] == 1
    assert manager.get_job(job_id).state == JobState.COMPLETED
    with Session(get_engine()) as db:
        assert db.query(File).filter_by(session_id=sid).count() == 1
        user = db.get(UserSession, sid)
        assert user.is_unsaved
        assert user.stats["completed_steps"] == ["scan"]
    monkeypatch.setattr(JobManager, "_instance", None)
    restarted = JobManager()
    assert restarted.get_run_plan(plan_id)["completed_steps"] == ["scan"]
    assert restarted.advance_run_plan(plan_id) is None
    with Session(get_engine()) as db:
        assert db.query(BackgroundJob).filter_by(run_plan_id=plan_id).count() == 1


def test_reconcile_preserves_live_other_owner_then_marks_dead(manager, tmp_path):
    sid = _session(tmp_path)
    with Session(get_engine()) as db:
        db.add(BackgroundJob(id="external", session_id=sid, job_type="analyze",
                             state="running", progress_json="{}",
                             owner_pid=os.getpid(), owner_token="other-server",
                             owner_started_at=process_started_at(os.getpid())))
        db.commit()
    manager.reconcile_startup()
    with Session(get_engine()) as db:
        assert db.get(BackgroundJob, "external").state == "running"
        row = db.get(BackgroundJob, "external")
        row.owner_pid = 2_000_000_000
        db.commit()
    manager.reconcile_startup()
    with Session(get_engine()) as db:
        assert db.get(BackgroundJob, "external").state == "interrupted"


def test_cancel_keeps_live_lease_until_worker_exits(manager, tmp_path):
    sid = _session(tmp_path)
    job = manager._create_job("test", sid)
    release = threading.Event()

    def run():
        release.wait(timeout=5)
        manager._finish_job(job, JobState.CANCELLED)

    manager._start_worker(job, run)
    manager.force_cancel(job.job_id)
    assert manager.get_job(job.job_id).state == JobState.CANCELLING
    with Session(get_engine()) as db:
        assert db.get(BackgroundJob, job.job_id).state == "cancelling"
    with pytest.raises(RuntimeError):
        manager._create_job("next", sid)
    release.set()
    _wait_until(lambda: manager.get_job(job.job_id).state == JobState.CANCELLED)


def test_persisted_cancel_wins_finish_race_and_does_not_advance(manager, tmp_path):
    sid = _session(tmp_path)
    plan_id = manager.create_run_plan(sid, ["scan", "enrich"],
                                      {"root_path": str(tmp_path)})
    job = manager._create_job("scan", sid, run_plan_id=plan_id, expected_index=0)
    assert job_store.request_cancel(job.job_id)
    manager._finish_job(job, JobState.COMPLETED)
    plan = manager.get_run_plan(plan_id)
    assert job.state == JobState.CANCELLED
    assert plan["state"] == "cancelled"
    assert plan["current_index"] == 0
    assert plan["completed_steps"] == []
    with Session(get_engine()) as db:
        assert db.query(BackgroundJob).filter_by(run_plan_id=plan_id).count() == 1
        user = db.get(UserSession, sid)
        assert not user.is_unsaved
        assert user.stats.get("completed_steps", []) == []


def test_plan_requires_analysis_before_semantic_dedup_and_relate(manager, tmp_path):
    sid = _session(tmp_path)
    options = {"root_path": str(tmp_path)}
    with pytest.raises(ValueError, match="pipeline order"):
        manager.create_run_plan(sid, ["scan", "enrich", "dedup", "relate", "analyze"],
                                options)
    plan_id = manager.create_run_plan(
        sid, ["scan", "enrich", "analyze", "dedup", "relate", "propose"], options,
    )
    assert manager.get_run_plan(plan_id)["steps"] == [
        "scan", "enrich", "analyze", "dedup", "relate", "propose",
    ]


def test_failed_phase_does_not_mark_session_step_complete(manager, tmp_path):
    sid = _session(tmp_path)
    job = manager._create_job("analyze", sid)
    manager._finish_job(job, JobState.FAILED, "provider timeout")
    with Session(get_engine()) as db:
        user = db.get(UserSession, sid)
        assert not user.is_unsaved
        assert user.stats.get("completed_steps", []) == []

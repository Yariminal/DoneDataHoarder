"""Terminal checkpoints do not claim that worker threads have already exited."""
import threading
import time

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.core.jobs import JobManager, JobState
from donedatahoarder.db.models import UserSession
from donedatahoarder.db.session import init_db


@pytest.fixture
def manager(tmp_path, monkeypatch):
    engine = init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.commit()
        session_id = owner.id
    yield manager, session_id
    for thread in list(manager._worker_threads.values()):
        thread.join(timeout=5)
    assert not manager.has_live_workers()
    engine.dispose()


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_completed_worker_stays_live_during_final_progress(manager, monkeypatch):
    manager, session_id = manager
    job = manager._create_job("test", session_id)
    entered, release = threading.Event(), threading.Event()
    original_progress = job.push_progress

    def blocked_progress(progress):
        if progress.get("done"):
            entered.set()
            release.wait(timeout=5)
        original_progress(progress)

    monkeypatch.setattr(job, "push_progress", blocked_progress)
    manager._start_worker(job, lambda: manager._finish_job(job, JobState.COMPLETED))
    thread = manager._worker_threads[job.job_id]
    try:
        assert entered.wait(timeout=5)
        assert job.state == JobState.COMPLETED
        assert manager.get_active() is None
        assert manager.has_live_workers()
        assert manager.has_live_workers(session_id)
        with pytest.raises(RuntimeError, match="worker to exit"):
            manager._create_job("next", session_id)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not manager.has_live_workers(session_id)
    assert job.job_id not in manager._worker_threads


def test_worker_stays_live_between_phases_and_real_plan_continues(manager, tmp_path, monkeypatch):
    manager, session_id = manager
    root = tmp_path / "collection"
    root.mkdir()
    (root / "note.txt").write_text("A simple fixture", encoding="utf-8")
    plan_id = manager.create_run_plan(session_id, ["scan", "enrich"], {"root_path": str(root)})
    entered, release = threading.Event(), threading.Event()
    original_advance = manager.advance_run_plan

    def blocked_advance(identifier):
        if threading.current_thread().name.startswith("job-"):
            entered.set()
            release.wait(timeout=5)
        return original_advance(identifier)

    monkeypatch.setattr(manager, "advance_run_plan", blocked_advance)
    manager.advance_run_plan(plan_id)
    try:
        assert entered.wait(timeout=5)
        assert manager.get_run_plan(plan_id)["state"] == "ready"
        assert manager.get_active() is None
        assert manager.has_live_workers(session_id)
        # Another caller cannot bypass the finishing worker by advancing the
        # same plan; only its own synchronous continuation may do that.
        with pytest.raises(RuntimeError, match="worker to exit"):
            original_advance(plan_id)
    finally:
        release.set()
    wait_until(lambda: manager.get_run_plan(plan_id)["state"] == "completed"
               and not manager.has_live_workers())
    assert manager.get_run_plan(plan_id)["completed_steps"] == ["scan", "enrich"]


def test_fast_continuation_can_outlive_its_completed_parent_threads(manager, tmp_path, monkeypatch):
    manager, session_id = manager
    plan_id = manager.create_run_plan(session_id, ["scan", "enrich", "dedup"], {"root_path": str(tmp_path)})
    release_parents = threading.Event()
    entered_final = threading.Event()
    original_advance = manager.advance_run_plan

    def keep_parents_alive(identifier):
        result = original_advance(identifier)
        if threading.current_thread().name.startswith("job-"):
            release_parents.wait(timeout=5)
        return result

    def start_immediate(stage):
        def start(session_id, *args, run_plan_id=None, expected_index=None, **kwargs):
            job = manager._create_job(stage, session_id, run_plan_id=run_plan_id, expected_index=expected_index)
            def finish():
                manager._finish_job(job, JobState.COMPLETED)
                if stage == "dedup":
                    entered_final.set()
            manager._start_worker(job, finish)
            return job.job_id
        return start

    monkeypatch.setattr(manager, "advance_run_plan", keep_parents_alive)
    for stage in ("scan", "enrich", "dedup"):
        monkeypatch.setattr(manager, "start_" + stage, start_immediate(stage))
    manager.advance_run_plan(plan_id)
    try:
        assert entered_final.wait(timeout=5)
        assert manager.get_run_plan(plan_id)["state"] == "completed"
        assert manager.has_live_workers(session_id)
    finally:
        release_parents.set()
    wait_until(lambda: not manager.has_live_workers())

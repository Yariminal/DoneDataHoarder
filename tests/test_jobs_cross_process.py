"""Durable job ownership checks with a real second Python interpreter."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.core.jobs import JobState, job_manager
from donedatahoarder.db.models import UserSession
from donedatahoarder.db.session import get_engine, init_db


def _setup(tmp_path):
    db_path = tmp_path / "jobs.db"
    root = tmp_path / "files"
    root.mkdir()
    init_db(db_path)
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="cross-process")
        db.add(user)
        db.commit()
        return db_path, root, user.id


def _wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for cross-process job state")


def test_remote_cancel_retains_lease_until_live_worker_exits(tmp_path, monkeypatch):
    db_path, _root, sid = _setup(tmp_path)
    from donedatahoarder.core import wake_lock
    monkeypatch.setattr(wake_lock, "acquire", lambda: None)
    monkeypatch.setattr(wake_lock, "release", lambda: None)
    release = tmp_path / "release"
    child_code = """
import sys, time
from pathlib import Path
from donedatahoarder.db.session import init_db
from donedatahoarder.core.jobs import job_manager, JobState
from donedatahoarder.core import wake_lock
wake_lock.acquire = lambda: None
wake_lock.release = lambda: None
init_db(Path(sys.argv[1]))
release = Path(sys.argv[3])
job = job_manager._create_job('test', sys.argv[2])
def worker():
    while not release.exists():
        job_manager._cancel_requested(job)
        time.sleep(0.05)
    state = JobState.CANCELLED if job_manager._cancel_requested(job) else JobState.COMPLETED
    job_manager._finish_job(job, state)
job_manager._start_worker(job, worker)
print(job.job_id, flush=True)
while job.finished_at is None:
    time.sleep(0.05)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(db_path), sid, str(release)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        job_id = child.stdout.readline().strip()
        assert job_id, child.stderr.read()
        try:
            job_manager._create_job("test", sid)
        except RuntimeError as exc:
            assert "already" in str(exc)
        else:
            raise AssertionError("second interpreter started a conflicting job")
        job_manager.force_cancel(job_id)
        _wait_for(lambda: job_manager.get_job(job_id).state == JobState.CANCELLING)
        assert job_manager.get_active().job_id == job_id
        try:
            job_manager._create_job("test", sid)
        except RuntimeError as exc:
            assert "already" in str(exc)
        else:
            raise AssertionError("cancelling live worker released its lease")
        release.write_text("exit", encoding="utf-8")
        _wait_for(lambda: job_manager.get_job(job_id).state == JobState.CANCELLED)
        next_job = job_manager._create_job("test", sid)
        job_manager._finish_job(next_job, JobState.COMPLETED)
    finally:
        release.write_text("exit", encoding="utf-8")
        child.communicate(timeout=10)
    assert child.returncode == 0


def test_dead_owner_interrupted_then_explicit_resume_checkpoint(tmp_path, monkeypatch):
    db_path, root, sid = _setup(tmp_path)
    from donedatahoarder.core import wake_lock
    monkeypatch.setattr(wake_lock, "acquire", lambda: None)
    monkeypatch.setattr(wake_lock, "release", lambda: None)
    child_code = """
import sys, time
from pathlib import Path
from donedatahoarder.db.session import init_db
from donedatahoarder.core.jobs import job_manager
from donedatahoarder.core import scanner, wake_lock
wake_lock.acquire = lambda: None
wake_lock.release = lambda: None
init_db(Path(sys.argv[1]))
def blocked_scan(*args, **kwargs):
    while True:
        time.sleep(1)
scanner.scan = blocked_scan
plan = job_manager.create_run_plan(sys.argv[2], ['scan'],
                                   {'root_path': sys.argv[3], 'backend': 'ollama'})
job = job_manager.advance_run_plan(plan)
print(plan + ' ' + job, flush=True)
while True:
    time.sleep(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(db_path), sid, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        ids = child.stdout.readline().strip().split()
        assert len(ids) == 2, child.stderr.read()
        plan_id, original_job_id = ids
        assert job_manager.get_job(original_job_id).state == JobState.RUNNING
        child.terminate()
        child.wait(timeout=10)
        job_manager.reconcile_startup()
        interrupted = job_manager.get_run_plan(plan_id)
        assert interrupted["state"] == "interrupted"
        assert interrupted["current_index"] == 0
        assert interrupted["completed_steps"] == []
        assert job_manager.get_job(original_job_id).state == JobState.INTERRUPTED
        # The resume proof is about ownership/checkpoint, not Rich's Windows
        # console encoding in a captured pytest worker.
        from donedatahoarder.core import scanner
        monkeypatch.setattr(scanner, "scan", lambda *args, **kwargs: {"scanned": 0})
        resumed_id = job_manager.resume_run_plan(plan_id)
        assert resumed_id and resumed_id != original_job_id
        completed = _wait_for(
            lambda: (plan if (plan := job_manager.get_run_plan(plan_id))["state"]
                     == "completed" else None)
        )
        assert completed["current_index"] == 1
        assert completed["completed_steps"] == ["scan"]
        assert "scan" in completed["checkpoint"]
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)

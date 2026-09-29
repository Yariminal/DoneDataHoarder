"""
Background job manager for long-running pipeline operations.

Runs jobs in a background thread so they survive browser disconnects.
SSE endpoints become thin read-only observers of job state.
"""
from __future__ import annotations

import enum
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from donedatahoarder.timeutils import utcnow
from typing import Callable, Generator, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from donedatahoarder.core import job_store
from donedatahoarder.db.models import BackgroundJob, File, FileStatus, RunPlan, SessionStatus, UserSession
from donedatahoarder.db.session import get_engine

logger = logging.getLogger(__name__)


class JobState(str, enum.Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"
    INTERRUPTED = "interrupted"


def _error_category(error: str | None) -> str | None:
    if not error:
        return None
    lower = error.lower()
    if "timed out" in lower or "timeout" in lower:
        return "provider_timeout"
    if "analysis file(s) failed" in lower:
        return "analysis_errors"
    if "output token limit" in lower:
        return "provider_output_limit"
    return "phase_error"


@dataclass
class JobInfo:
    job_id: str
    job_type: str  # "analyze" or "enrich"
    session_id: str
    state: JobState = JobState.RUNNING
    progress: dict = field(default_factory=dict)
    error: Optional[str] = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    run_plan_id: Optional[str] = None
    owner_token: Optional[str] = None

    # Threading controls
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_flag: bool = False

    # Subscriber queues for SSE streaming
    _subscribers: list[queue.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _persist: Optional[Callable[["JobInfo"], None]] = None
    _heartbeat_stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        # Start unpaused
        self.pause_event.set()

    def push_progress(self, data: dict):
        """Update progress and notify all subscribers."""
        self.progress = data
        if self._persist:
            self._persist(self)
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # Drop oldest message to make room
                    try:
                        q.get_nowait()
                        q.put_nowait(data)
                    except (queue.Empty, queue.Full):
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def add_subscriber(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def remove_subscriber(self, q: queue.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "session_id": self.session_id,
            "state": self.state.value,
            "progress": self.progress,
            "error": self.error,
            "error_category": _error_category(self.error),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "run_plan_id": self.run_plan_id,
            "cancel_requested": self.cancel_flag,
        }


class JobManager:
    """
    Singleton that manages background pipeline jobs.

    At most one job runs at a time. Jobs survive browser disconnects
    because they run in a dedicated background thread.
    """

    _instance: Optional["JobManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._jobs: dict[str, JobInfo] = {}
        self._active_job_id: Optional[str] = None
        self._lock = threading.Lock()
        self._worker_threads: dict[str, threading.Thread] = {}
        self._owner_token = str(uuid.uuid4())

    # Finished jobs kept for status queries; older ones are evicted so the
    # registry doesn't grow unboundedly in a long-lived server process.
    MAX_FINISHED_JOBS = 20

    def reconcile_startup(self) -> None:
        """Reconcile only jobs whose owning process is confirmed dead."""
        job_store.reconcile(self._owner_token)

    def _persist_job(self, job: JobInfo) -> None:
        with Session(get_engine()) as db:
            row = db.get(BackgroundJob, job.job_id)
            if row:
                row.state = job.state.value
                row.progress_json = json.dumps(job.progress)
                row.error = job.error
                row.finished_at = job.finished_at
                row.cancel_requested = job.cancel_flag or row.cancel_requested
                row.heartbeat_at = utcnow()
                db.commit()

    def _cancel_requested(self, job: JobInfo) -> bool:
        if job.cancel_flag:
            return True
        if job_store.cancel_requested(job.job_id):
            job.cancel_flag = True
            job.state = JobState.CANCELLING
            job.pause_event.set()
            return True
        return False

    def _heartbeat_worker(self, job: JobInfo) -> None:
        while not job._heartbeat_stop.wait(2):
            try:
                job_store.heartbeat(job.job_id, self._owner_token)
                if job_store.cancel_requested(job.job_id):
                    job.cancel_flag = True
                    job.state = JobState.CANCELLING
                    job.pause_event.set()
            except Exception:
                logger.exception("Could not persist job heartbeat")

    def _prune_workers_locked(self) -> None:
        self._worker_threads = {
            key: worker for key, worker in self._worker_threads.items()
            if worker.is_alive()
        }

    def start_tracked_worker(self, thread: threading.Thread, key: str | None = None) -> None:
        """Start a worker without exposing an untracked liveness window."""
        with self._lock:
            self._prune_workers_locked()
            worker_key = key or str(uuid.uuid4())
            self._worker_threads[worker_key] = thread
            try:
                thread.start()
            except BaseException:
                del self._worker_threads[worker_key]
                raise

    def _start_worker(self, job: JobInfo, run) -> None:
        """Keep a reference after force-cancel until the worker really exits."""
        thread = threading.Thread(target=run, daemon=True, name=f"job-{job.job_id}")
        self.start_tracked_worker(thread, key=job.job_id)
        threading.Thread(target=self._heartbeat_worker, args=(job,), daemon=True,
                         name=f"heartbeat-{job.job_id}").start()

    def has_live_workers(self) -> bool:
        """True while any pipeline worker can still mutate the database."""
        with self._lock:
            self._prune_workers_locked()
            return bool(self._worker_threads)

    def _create_job_unlocked(
        self, job_type: str, session_id: str, *, run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> JobInfo:
        self.reconcile_startup()
        with self._lock:
            # Prevent starting a new job while one is active
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active.state in (JobState.RUNNING, JobState.PAUSED, JobState.CANCELLING):
                    raise RuntimeError(
                        f"A {active.job_type} job is already {active.state.value}. "
                        "Pause or cancel it first."
                    )
            self._prune_workers_locked()
            if self._worker_threads:
                raise RuntimeError("Wait for the previous pipeline worker to exit")
            with Session(get_engine()) as db:
                remote_live = db.query(BackgroundJob).filter(
                    BackgroundJob.state.in_(job_store.LIVE_STATES),
                ).first()
                if remote_live:
                    raise RuntimeError(
                        f"A {remote_live.job_type} job is already {remote_live.state} "
                        f"in process {remote_live.owner_pid}"
                    )

            # Evict oldest finished jobs beyond the retention cap
            finished = sorted(
                (j for j in self._jobs.values() if j.finished_at is not None),
                key=lambda j: j.finished_at,
            )
            for old in finished[: max(0, len(finished) - self.MAX_FINISHED_JOBS)]:
                del self._jobs[old.job_id]

            job_id = str(uuid.uuid4())
            job = JobInfo(job_id=job_id, job_type=job_type, session_id=session_id,
                          run_plan_id=run_plan_id, owner_token=self._owner_token)
            job._persist = self._persist_job
            with Session(get_engine()) as db:
                if run_plan_id:
                    claimed = db.execute(update(RunPlan).where(
                        RunPlan.id == run_plan_id,
                        RunPlan.session_id == session_id,
                        RunPlan.active_job_id.is_(None),
                        RunPlan.current_index == expected_index,
                        RunPlan.state == "ready",
                    ).values(active_job_id=job_id, state="running", updated_at=utcnow()))
                    if claimed.rowcount != 1:
                        db.rollback()
                        raise RuntimeError("Run plan was already advanced by another process")
                db.add(BackgroundJob(
                    id=job_id, job_type=job_type, session_id=session_id,
                    state=JobState.RUNNING.value, progress_json="{}",
                    owner_pid=os.getpid(), owner_token=self._owner_token,
                    owner_started_at=job_store.process_started_at(os.getpid()),
                    heartbeat_at=utcnow(), run_plan_id=run_plan_id,
                ))
                db.commit()
            self._jobs[job_id] = job
            self._active_job_id = job_id

        # Outside the lock: hold a system wake lock for the lifetime of the job
        # so unattended runs survive the OS idle-sleep timer.
        from donedatahoarder.core import wake_lock
        wake_lock.acquire()
        return job

    def _create_job(
        self, job_type: str, session_id: str, *, run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> JobInfo:
        from donedatahoarder.core.process_lock import operation_lock

        # The short dispatch lock makes the remote-live check and insert one
        # cross-process critical section; the worker later holds its phase lock.
        with operation_lock("job_dispatch"):
            return self._create_job_unlocked(
                job_type, session_id, run_plan_id=run_plan_id,
                expected_index=expected_index,
            )

    def _finish_job(self, job: JobInfo, state: JobState, error: str | None = None):
        # Terminal state is published only after the producer and all nested
        # workers have exited, so cancellation never releases the lease early.
        continue_plan = False
        with self._lock:
            if job.finished_at is not None:
                return
            finished = utcnow()
            # Job terminal status and run checkpoint commit in one transaction.
            # A crash can leave either both old states or both new states.
            with Session(get_engine()) as db:
                row = db.get(BackgroundJob, job.job_id)
                plan = db.get(RunPlan, job.run_plan_id) if job.run_plan_id else None
                if state == JobState.COMPLETED and (
                    (row and row.cancel_requested) or (plan and plan.state == "cancelling")
                ):
                    state = JobState.CANCELLED
                    job.cancel_flag = True
                if row:
                    row.state = state.value
                    row.progress_json = json.dumps(job.progress)
                    row.error = error
                    row.cancel_requested = job.cancel_flag or row.cancel_requested
                    row.finished_at = finished
                    row.heartbeat_at = finished
                if plan:
                    if plan.active_job_id == job.job_id:
                        plan.active_job_id = None
                        if state == JobState.COMPLETED:
                            steps = json.loads(plan.steps_json or "[]")
                            completed = json.loads(plan.completed_steps_json or "[]")
                            if plan.current_index < len(steps):
                                completed.append(steps[plan.current_index])
                                plan.current_index += 1
                            plan.completed_steps_json = json.dumps(completed)
                            checkpoint = json.loads(plan.checkpoint_json or "{}")
                            checkpoint[job.job_type] = job.progress
                            plan.checkpoint_json = json.dumps(checkpoint)
                            plan.state = "completed" if plan.current_index >= len(steps) else "ready"
                            continue_plan = plan.state == "ready"
                        else:
                            plan.state = state.value
                        plan.updated_at = finished
                if state == JobState.COMPLETED:
                    user_session = db.get(UserSession, job.session_id)
                    if user_session:
                        user_session.is_unsaved = True
                        user_session.updated_at = finished
                        if user_session.status == SessionStatus.NEW:
                            user_session.status = SessionStatus.ACTIVE
                        step = "execute" if job.job_type == "execute_dry" else job.job_type
                        stats = user_session.stats
                        completed_steps = stats.get("completed_steps", [])
                        if step not in completed_steps:
                            completed_steps.append(step)
                        stats["completed_steps"] = completed_steps
                        user_session.stats = stats
                db.commit()
            job.finished_at = finished
            job.state = state
            job.error = error
            job._heartbeat_stop.set()
            if self._active_job_id == job.job_id:
                self._active_job_id = None
            if self._worker_threads.get(job.job_id) is threading.current_thread():
                del self._worker_threads[job.job_id]
        # Release the system wake lock acquired in _create_job.
        from donedatahoarder.core import wake_lock
        wake_lock.release()
        # Push final progress to subscribers
        final = {**job.progress, "done": True, "state": state.value}
        if error:
            final["error"] = error
        job.push_progress(final)
        if continue_plan:
            try:
                self.advance_run_plan(job.run_plan_id)
            except Exception:
                logger.exception("Could not advance run plan %s", job.run_plan_id)

    def start_analyze(
        self,
        session_id: str,
        backend: str = "ollama",
        model: str = "gemma3:12b",
        workers: int = 1,
        retry_errors: bool = False,
        sequence_sample_stride: int = 0,
        use_cache: bool = True,
        run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """Start an analyze job in a background thread. Returns job_id."""
        job = self._create_job("analyze", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            try:
                from donedatahoarder.ai.router import init_ai
                from donedatahoarder.analyzers.pipeline import analyze_with_progress

                init_ai(backend=backend, text_model=model, vision_model=model)

                with closing(analyze_with_progress(
                    workers=workers,
                    session_id=session_id,
                    retry_errors=retry_errors,
                    sequence_sample_stride=sequence_sample_stride,
                    use_cache=use_cache,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                )) as stream:
                    for progress in stream:
                        job.push_progress(progress)
                        if progress.get("done") is True or progress.get("cancelled") is True:
                            break

                if self._cancel_requested(job):
                    self._finish_job(job, JobState.CANCELLED)
                elif job.progress.get("errors", 0):
                    self._finish_job(job, JobState.FAILED,
                                     f"{job.progress['errors']} analysis file(s) failed")
                else:
                    self._finish_job(job, JobState.COMPLETED)

            except Exception as exc:
                self._finish_job(job, JobState.FAILED, str(exc))

        self._start_worker(job, run)
        return job.job_id

    def start_enrich(self, session_id: str, *, run_plan_id: str | None = None,
                     expected_index: int | None = None) -> str:
        """Start an enrich job in a background thread. Returns job_id."""
        job = self._create_job("enrich", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            try:
                from donedatahoarder.core.enricher import enrich_with_progress

                with closing(enrich_with_progress(
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                )) as stream:
                    for progress in stream:
                        job.push_progress(progress)
                        if progress.get("done") is True or progress.get("cancelled") is True:
                            break

                if self._cancel_requested(job):
                    self._finish_job(job, JobState.CANCELLED)
                elif job.progress.get("errors", 0):
                    self._finish_job(job, JobState.FAILED,
                                     f"{job.progress['errors']} enrichment file(s) failed")
                else:
                    self._finish_job(job, JobState.COMPLETED)

            except Exception as exc:
                self._finish_job(job, JobState.FAILED, str(exc))

        self._start_worker(job, run)
        return job.job_id

    # ------------------------------------------------------------------
    # New background jobs (dedup, relate, propose, organize, execute-dry)
    # ------------------------------------------------------------------

    def _generic_runner(
        self,
        job: JobInfo,
        gen_factory,  # callable returning a fresh generator
        step_name: str,
        init_ai_kwargs: Optional[dict] = None,
    ):
        """
        Shared worker for all background jobs that follow the
        `*_with_progress(pause_event, cancel_check)` contract.

        Mirrors the start_analyze/start_enrich pattern: drive the generator,
        push progress, and finalize state with its session checkpoint.
        """
        try:
            if init_ai_kwargs:
                from donedatahoarder.ai.router import init_ai
                init_ai(**init_ai_kwargs)

            with closing(gen_factory()) as stream:
                for progress in stream:
                    if progress.get("done") is True or progress.get("cancelled") is True:
                        # Push the terminal payload too so subscribers see counts
                        job.push_progress(progress)
                        break
                    job.push_progress(progress)

            if self._cancel_requested(job):
                self._finish_job(job, JobState.CANCELLED)
            elif (job.progress.get("error") or job.progress.get("errors", 0)
                  or job.progress.get("failed", 0)
                  or job.progress.get("status") == "failed"):
                detail = (job.progress.get("error") or
                          f"{job.progress.get('errors', 0)} errors, "
                          f"{job.progress.get('failed', 0)} failed")
                self._finish_job(job, JobState.FAILED, str(detail))
            else:
                self._finish_job(job, JobState.COMPLETED)

        except Exception as exc:
            self._finish_job(job, JobState.FAILED, str(exc))

    def start_dedup(self, session_id: str, *, run_plan_id: str | None = None,
                    expected_index: int | None = None) -> str:
        """Start a dedup job in a background thread. Returns job_id."""
        job = self._create_job("dedup", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            from donedatahoarder.core.dedup import dedup_with_progress
            self._generic_runner(
                job=job,
                gen_factory=lambda: dedup_with_progress(
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                ),
                step_name="dedup",
            )

        self._start_worker(job, run)
        return job.job_id

    def start_relate(
        self,
        session_id: str,
        backend: str = "ollama",
        model: str = "gemma3:12b",
        scope: str = "per_directory",
        run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """Start a relate job in a background thread. Returns job_id."""
        job = self._create_job("relate", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            from donedatahoarder.core.relate import relate_with_progress
            self._generic_runner(
                job=job,
                gen_factory=lambda: relate_with_progress(
                    session_id=session_id,
                    scope=scope,
                    model=model,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                ),
                step_name="relate",
                init_ai_kwargs={"backend": backend, "text_model": model, "vision_model": model},
            )

        self._start_worker(job, run)
        return job.job_id

    def start_propose(
        self,
        session_id: str,
        backend: str = "ollama",
        model: str = "gemma3:12b",
        run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """Start a propose job in a background thread. Returns job_id."""
        job = self._create_job("propose", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            from donedatahoarder.proposals.namer import generate_proposals_with_progress
            self._generic_runner(
                job=job,
                gen_factory=lambda: generate_proposals_with_progress(
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                ),
                step_name="propose",
                init_ai_kwargs={"backend": backend, "text_model": model, "vision_model": model},
            )

        self._start_worker(job, run)
        return job.job_id

    def start_organize(
        self,
        session_id: str,
        backend: str = "ollama",
        model: str = "gemma3:12b",
        run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """Start an organize job in a background thread. Returns job_id."""
        job = self._create_job("organize", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            from donedatahoarder.proposals.organizer import generate_reorg_proposals_with_progress
            self._generic_runner(
                job=job,
                gen_factory=lambda: generate_reorg_proposals_with_progress(
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                ),
                step_name="organize",
                init_ai_kwargs={"backend": backend, "text_model": model, "vision_model": model},
            )

        self._start_worker(job, run)
        return job.job_id

    def start_execute_dry(
        self,
        session_id: str,
        min_confidence: float = 0.7,
        run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """
        Start an execute --dry-run job in a background thread. Returns job_id.

        IMPORTANT: this is dry-run only. The destructive --commit path stays
        synchronous through the existing /execute endpoint to preserve the
        user-visible "Apply changes? y/N" confirmation flow.
        """
        job = self._create_job("execute_dry", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            from donedatahoarder.executor import execute_with_progress
            self._generic_runner(
                job=job,
                gen_factory=lambda: execute_with_progress(
                    min_confidence=min_confidence,
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: self._cancel_requested(job),
                ),
                step_name="execute",
            )

        self._start_worker(job, run)
        return job.job_id

    def start_scan(
        self, session_id: str, root_path: str, workers: int = 1,
        skip_dirs: list[str] | None = None, *, run_plan_id: str | None = None,
        expected_index: int | None = None,
    ) -> str:
        """Run a streamed scanner as a durable background phase."""
        job = self._create_job("scan", session_id, run_plan_id=run_plan_id,
                               expected_index=expected_index)

        def run():
            try:
                from donedatahoarder.core.scanner import scan
                counts = scan(Path(root_path), session_id=session_id, workers=workers,
                              extra_skip_dirs=set(skip_dirs or []),
                              cancel_check=lambda: self._cancel_requested(job))
                job.push_progress(counts)
                if counts.get("cancelled") or self._cancel_requested(job):
                    self._finish_job(job, JobState.CANCELLED)
                elif counts.get("errors", 0):
                    self._finish_job(job, JobState.FAILED,
                                     f"{counts['errors']} scan file(s) failed")
                else:
                    self._finish_job(job, JobState.COMPLETED)
            except Exception as exc:
                self._finish_job(job, JobState.FAILED, str(exc))

        self._start_worker(job, run)
        return job.job_id

    _PLAN_STEPS = ("scan", "enrich", "analyze", "dedup", "relate", "propose",
                   "organize", "execute_dry")

    def create_run_plan(self, session_id: str, steps: list[str], options: dict) -> str:
        """Persist a reviewable unattended plan; no filesystem commit is allowed."""
        self.reconcile_startup()
        if not steps or len(steps) != len(set(steps)) or any(
            step not in self._PLAN_STEPS for step in steps
        ):
            raise ValueError("run plan steps must be unique supported phases")
        if list(steps) != [step for step in self._PLAN_STEPS if step in steps]:
            raise ValueError("run plan steps must follow pipeline order")
        if "scan" in steps and not options.get("root_path"):
            raise ValueError("scan run plan requires root_path")
        if options.get("backend", "ollama") not in ("ollama", "gemini"):
            raise ValueError("run plan backend must be explicitly selected")
        with Session(get_engine()) as db:
            if not db.get(UserSession, session_id):
                raise ValueError("run plan session does not exist")
            existing = db.query(RunPlan).filter(
                RunPlan.session_id == session_id,
                RunPlan.state.in_(("ready", "running", "paused", "cancelling")),
            ).first()
            if existing:
                raise RuntimeError("session already has an active run plan")
            plan_id = str(uuid.uuid4())
            db.add(RunPlan(id=plan_id, session_id=session_id, state="ready",
                           steps_json=json.dumps(steps), options_json=json.dumps(options),
                           completed_steps_json="[]", checkpoint_json="{}"))
            db.commit()
        return plan_id

    def get_run_plan(self, plan_id: str) -> dict | None:
        self.reconcile_startup()
        with Session(get_engine()) as db:
            plan = db.get(RunPlan, plan_id)
            return job_store.plan_dict(plan) if plan else None

    def get_active_run_plan(self, session_id: str | None = None) -> dict | None:
        self.reconcile_startup()
        with Session(get_engine()) as db:
            query = db.query(RunPlan).filter(RunPlan.state.in_(
                ("ready", "running", "paused", "cancelling")))
            if session_id:
                query = query.filter(RunPlan.session_id == session_id)
            plan = query.order_by(RunPlan.created_at.desc()).first()
            return job_store.plan_dict(plan) if plan else None

    def advance_run_plan(self, plan_id: str) -> str | None:
        """Start only the next incomplete phase; claim is atomic in _create_job."""
        plan = self.get_run_plan(plan_id)
        if not plan:
            raise KeyError(f"Run plan {plan_id} not found")
        if plan["active_job_id"]:
            return plan["active_job_id"]
        if plan["state"] == "completed":
            return None
        if plan["state"] != "ready":
            raise RuntimeError(f"Run plan is {plan['state']}; resume it explicitly")
        index = plan["current_index"]
        steps = plan["steps"]
        if index >= len(steps):
            return None
        step = steps[index]
        options = plan["options"]
        common = {"run_plan_id": plan_id, "expected_index": index}
        sid = plan["session_id"]
        if step == "scan":
            return self.start_scan(sid, options["root_path"],
                                   workers=options.get("workers", 1),
                                   skip_dirs=options.get("skip_dirs"), **common)
        if step == "enrich":
            return self.start_enrich(sid, **common)
        if step == "dedup":
            return self.start_dedup(sid, **common)
        if step == "relate":
            return self.start_relate(sid, backend=options.get("backend", "ollama"),
                                     model=options.get("propose_model", "gemma3:12b"),
                                     scope=options.get("relate_scope", "per_directory"),
                                     **common)
        if step == "analyze":
            return self.start_analyze(sid, backend=options.get("backend", "ollama"),
                                      model=options.get("analyze_model", "gemma3:12b"),
                                      workers=options.get("workers", 1),
                                      retry_errors=options.get("retry_errors", False),
                                      sequence_sample_stride=options.get("sequence_sample_stride", 0),
                                      use_cache=options.get("use_cache", True),
                                      **common)
        if step == "propose":
            return self.start_propose(sid, backend=options.get("backend", "ollama"),
                                      model=options.get("propose_model", "gemma3:12b"),
                                      **common)
        if step == "organize":
            return self.start_organize(sid, backend=options.get("backend", "ollama"),
                                       model=options.get("propose_model", "gemma3:12b"),
                                       **common)
        return self.start_execute_dry(sid, **common)

    def resume_run_plan(self, plan_id: str, *, retry_errors: bool = False) -> str | None:
        """Explicitly resume the first incomplete phase after an interruption."""
        self.reconcile_startup()
        with Session(get_engine()) as db:
            plan = db.get(RunPlan, plan_id)
            if not plan:
                raise KeyError(f"Run plan {plan_id} not found")
            if plan.active_job_id or plan.state not in ("interrupted", "failed", "cancelled"):
                raise RuntimeError("Run plan is not resumable")
            steps = json.loads(plan.steps_json or "[]")
            if plan.current_index < len(steps) and steps[plan.current_index] == "analyze":
                unresolved = db.query(File).filter(
                    File.session_id == plan.session_id, File.status == FileStatus.ERROR,
                    File.analysis_reason.startswith("provider_"),
                ).count()
                if unresolved and not retry_errors:
                    raise RuntimeError(
                        f"{unresolved} provider errors require explicit retry_errors=True"
                    )
            options = json.loads(plan.options_json or "{}")
            if retry_errors:
                options["retry_errors"] = True
                plan.options_json = json.dumps(options)
            plan.state = "ready"
            plan.updated_at = utcnow()
            db.commit()
        return self.advance_run_plan(plan_id)

    def cancel_run_plan(self, plan_id: str) -> None:
        self.reconcile_startup()
        with Session(get_engine()) as db:
            plan = db.get(RunPlan, plan_id)
            if not plan:
                raise KeyError(f"Run plan {plan_id} not found")
            if plan.state == "completed":
                return
            active = plan.active_job_id
            if not active:
                plan.state = "cancelled"
                plan.updated_at = utcnow()
                db.commit()
                return
            plan.state = "cancelling"
            plan.updated_at = utcnow()
            db.commit()
        self.force_cancel(active)

    def pause(self, job_id: str):
        job = self._get_job(job_id)
        if job.state != JobState.RUNNING:
            raise RuntimeError(f"Cannot pause job in state {job.state.value}")
        job.pause_event.clear()
        job.state = JobState.PAUSED
        job.push_progress({**job.progress, "state": "paused"})

    def resume(self, job_id: str):
        job = self._get_job(job_id)
        if job.state != JobState.PAUSED:
            raise RuntimeError(f"Cannot resume job in state {job.state.value}")
        job.state = JobState.RUNNING
        job.pause_event.set()
        job.push_progress({**job.progress, "state": "running"})


    def force_cancel(self, job_id: str):
        """Request cancellation; terminal state waits for the worker to exit."""
        self.reconcile_startup()
        job_store.request_cancel(job_id)
        job = self._jobs.get(job_id)
        if not job:
            return
        job.cancel_flag = True
        job.pause_event.set()
        if job.state in (JobState.RUNNING, JobState.PAUSED):
            job.state = JobState.CANCELLING
            job.push_progress({**job.progress, "state": "cancelling"})

    def cancel_session_jobs(self, session_id: str):
        """Force-cancel all jobs for a given session (used when session is deleted)."""
        for job in list(self._jobs.values()):
            if job.session_id == session_id and job.state in (
                JobState.RUNNING, JobState.PAUSED, JobState.CANCELLING,
            ):
                logger.info(f"Force-cancelling job {job.job_id} for deleted session {session_id}")
                self.force_cancel(job.job_id)

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        self.reconcile_startup()
        local = self._jobs.get(job_id)
        if local:
            return local
        with Session(get_engine()) as db:
            row = db.get(BackgroundJob, job_id)
            if not row:
                return None
            info = JobInfo(job_id=row.id, job_type=row.job_type,
                           session_id=row.session_id, state=JobState(row.state),
                           progress=json.loads(row.progress_json or "{}"),
                           error=row.error, started_at=row.started_at,
                           finished_at=row.finished_at, run_plan_id=row.run_plan_id,
                           owner_token=row.owner_token, cancel_flag=row.cancel_requested)
            return info

    def get_active(self) -> Optional[JobInfo]:
        self.reconcile_startup()
        with self._lock:
            if self._active_job_id:
                job = self._jobs.get(self._active_job_id)
                if job and job.state in (JobState.RUNNING, JobState.PAUSED, JobState.CANCELLING):
                    return job
        with Session(get_engine()) as db:
            row = db.query(BackgroundJob).filter(
                BackgroundJob.state.in_(job_store.LIVE_STATES),
            ).order_by(BackgroundJob.started_at.desc()).first()
            if row:
                return self.get_job(row.id)
        return None

    def subscribe(self, job_id: str) -> Generator[dict, None, None]:
        """
        Yield progress dicts for SSE streaming. Blocks waiting for updates.
        Safe to call after page refresh — immediately yields current state.
        """
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found")
        if job_id not in self._jobs:
            # A second server can observe an existing owner through durable
            # progress polling. It must not emit done during cancellation.
            prior = None
            while True:
                current = self.get_job(job_id)
                if current is None:
                    raise KeyError(f"Job {job_id} disappeared")
                payload = {**current.progress, "state": current.state.value}
                if current.state not in (JobState.RUNNING, JobState.PAUSED,
                                         JobState.CANCELLING):
                    yield {**payload, "done": True, "error": current.error}
                    return
                if payload != prior:
                    yield payload
                    prior = payload
                else:
                    yield {"heartbeat": True, "state": current.state.value}
                time.sleep(2)
        sub_queue = job.add_subscriber()

        try:
            # Immediately yield current state
            if job.progress:
                yield job.progress.copy()

            while job.state in (JobState.RUNNING, JobState.PAUSED, JobState.CANCELLING):
                try:
                    msg = sub_queue.get(timeout=2.0)
                    yield msg
                    if msg.get("done") is True:
                        return
                except queue.Empty:
                    # Heartbeat to keep SSE alive
                    yield {"heartbeat": True}

            # Yield final state if we haven't already
            yield {
                **job.progress,
                "done": True,
                "state": job.state.value,
                "error": job.error,
            }
        finally:
            job.remove_subscriber(sub_queue)

    def _get_job(self, job_id: str) -> JobInfo:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"Job {job_id} not found")
        return job


# Module-level singleton
job_manager = JobManager()

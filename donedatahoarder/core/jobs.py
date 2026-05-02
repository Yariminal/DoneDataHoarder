"""
Background job manager for long-running pipeline operations.

Runs jobs in a background thread so they survive browser disconnects.
SSE endpoints become thin read-only observers of job state.
"""
from __future__ import annotations

import enum
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generator, Optional

logger = logging.getLogger(__name__)


class JobState(str, enum.Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobInfo:
    job_id: str
    job_type: str  # "analyze" or "enrich"
    session_id: str
    state: JobState = JobState.RUNNING
    progress: dict = field(default_factory=dict)
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    # Threading controls
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_flag: bool = False

    # Subscriber queues for SSE streaming
    _subscribers: list[queue.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        # Start unpaused
        self.pause_event.set()

    def push_progress(self, data: dict):
        """Update progress and notify all subscribers."""
        self.progress = data
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
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
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

    def _create_job(self, job_type: str, session_id: str) -> JobInfo:
        with self._lock:
            # Prevent starting a new job while one is active
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active.state in (JobState.RUNNING, JobState.PAUSED):
                    raise RuntimeError(
                        f"A {active.job_type} job is already {active.state.value}. "
                        "Pause or cancel it first."
                    )

            job_id = str(uuid.uuid4())[:8]
            job = JobInfo(job_id=job_id, job_type=job_type, session_id=session_id)
            self._jobs[job_id] = job
            self._active_job_id = job_id
            return job

    def _finish_job(self, job: JobInfo, state: JobState, error: str | None = None):
        job.state = state
        job.error = error
        job.finished_at = datetime.utcnow()
        # Clear active job ID so new jobs can start
        with self._lock:
            if self._active_job_id == job.job_id:
                self._active_job_id = None
        # Push final progress to subscribers
        final = {**job.progress, "done": True, "state": state.value}
        if error:
            final["error"] = error
        job.push_progress(final)

    def start_analyze(
        self,
        session_id: str,
        backend: str = "ollama",
        model: str = "gemma3:12b",
        workers: int = 1,
    ) -> str:
        """Start an analyze job in a background thread. Returns job_id."""
        job = self._create_job("analyze", session_id)

        def run():
            try:
                from donedatahoarder.ai.router import init_ai
                from donedatahoarder.analyzers.pipeline import analyze_with_progress

                init_ai(backend=backend, text_model=model, vision_model=model)

                for progress in analyze_with_progress(
                    workers=workers,
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: job.cancel_flag,
                ):
                    if progress.get("done") or progress.get("cancelled"):
                        break
                    job.push_progress(progress)

                if job.cancel_flag:
                    self._finish_job(job, JobState.CANCELLED)
                else:
                    self._finish_job(job, JobState.COMPLETED)

                # Mark session unsaved
                try:
                    from donedatahoarder.web.api import _mark_session_unsaved
                    _mark_session_unsaved(session_id, step="analyze")
                except Exception:
                    pass

            except Exception as exc:
                self._finish_job(job, JobState.FAILED, str(exc))

        thread = threading.Thread(target=run, daemon=True, name=f"job-{job.job_id}")
        thread.start()
        return job.job_id

    def start_enrich(self, session_id: str) -> str:
        """Start an enrich job in a background thread. Returns job_id."""
        job = self._create_job("enrich", session_id)

        def run():
            try:
                from donedatahoarder.core.enricher import enrich_with_progress

                for progress in enrich_with_progress(
                    session_id=session_id,
                    pause_event=job.pause_event,
                    cancel_check=lambda: job.cancel_flag,
                ):
                    if progress.get("done") or progress.get("cancelled"):
                        break
                    job.push_progress(progress)

                if job.cancel_flag:
                    self._finish_job(job, JobState.CANCELLED)
                else:
                    self._finish_job(job, JobState.COMPLETED)

                try:
                    from donedatahoarder.web.api import _mark_session_unsaved
                    _mark_session_unsaved(session_id, step="enrich")
                except Exception:
                    pass

            except Exception as exc:
                self._finish_job(job, JobState.FAILED, str(exc))

        thread = threading.Thread(target=run, daemon=True, name=f"job-{job.job_id}")
        thread.start()
        return job.job_id

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
        """
        Force-cancel a job immediately, even if the worker thread is stuck.

        Sets the cancel flag AND immediately transitions the job to CANCELLED
        state so that new jobs can start and the UI stops showing it as active.
        The worker thread may still be running in the background but will
        eventually exit (daemon thread dies with the process).
        """
        job = self._jobs.get(job_id)
        if not job:
            return
        job.cancel_flag = True
        job.pause_event.set()
        if job.state in (JobState.RUNNING, JobState.PAUSED):
            self._finish_job(job, JobState.CANCELLED, "Force-cancelled")

    def cancel_session_jobs(self, session_id: str):
        """Force-cancel all jobs for a given session (used when session is deleted)."""
        for job in list(self._jobs.values()):
            if job.session_id == session_id and job.state in (JobState.RUNNING, JobState.PAUSED):
                logger.info(f"Force-cancelling job {job.job_id} for deleted session {session_id}")
                self.force_cancel(job.job_id)

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        return self._jobs.get(job_id)

    def get_active(self) -> Optional[JobInfo]:
        with self._lock:
            if self._active_job_id:
                job = self._jobs.get(self._active_job_id)
                if job and job.state in (JobState.RUNNING, JobState.PAUSED):
                    return job
        return None

    def subscribe(self, job_id: str) -> Generator[dict, None, None]:
        """
        Yield progress dicts for SSE streaming. Blocks waiting for updates.
        Safe to call after page refresh — immediately yields current state.
        """
        job = self._get_job(job_id)
        sub_queue = job.add_subscriber()

        try:
            # Immediately yield current state
            if job.progress:
                yield job.progress.copy()

            while job.state in (JobState.RUNNING, JobState.PAUSED):
                try:
                    msg = sub_queue.get(timeout=2.0)
                    yield msg
                    if msg.get("done"):
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

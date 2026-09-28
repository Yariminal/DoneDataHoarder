"""
Shared helpers used across the API routers.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import RLock

from fastapi import HTTPException
from sqlalchemy.orm import Session

from donedatahoarder.db.models import SessionStatus, UserSession
from donedatahoarder.db.session import get_engine
from donedatahoarder.core.process_lock import OperationBusyError, operation_lock
from donedatahoarder.timeutils import utcnow


# Process-local serialization of web review writes, pipeline starts, and commit.
review_execute_lock = RLock()


@contextmanager
def review_operation(phase: str):
    """Keep review, keeper and commit decisions atomic across web/CLI peers."""
    with review_execute_lock:
        try:
            with operation_lock(phase):
                yield
        except OperationBusyError as exc:
            raise HTTPException(409, str(exc)) from exc


def _require_session_id(body_session_id: str = "") -> str:
    """Helper: resolve the active session_id from request body."""
    sid = body_session_id
    if not sid:
        raise HTTPException(400, "No active session. Create or load a session first.")
    return sid


def _resolve_model(body_model: str, session_id: str, step: str = "analyze", fallback: str = "gemma3:12b") -> str:
    """
    Resolve the model to use, with fallback priority:
      1. body_model (from request) — if non-empty
      2. session.analyze_model or session.propose_model (based on step)
      3. session.model (legacy fallback)
      4. fallback default
    This prevents empty-string model names from reaching Ollama.
    """
    if body_model:
        return body_model
    if session_id:
        from donedatahoarder.db.models import UserSession
        from sqlalchemy.orm import Session as _Sess
        try:
            with _Sess(get_engine()) as db:
                us = db.get(UserSession, session_id)
                if us:
                    # Step-specific model takes priority
                    if step == "analyze" and getattr(us, 'analyze_model', None):
                        return us.analyze_model
                    elif step == "propose" and getattr(us, 'propose_model', None):
                        return us.propose_model
                    # Fall through to legacy model field
                    if us.model:
                        return us.model
        except Exception:
            pass
    return fallback


def _mark_session_unsaved(session_id: str, step: str | None = None) -> None:
    """Helper: mark a session as unsaved and optionally record a completed step."""
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if user_session:
            user_session.is_unsaved = True
            user_session.updated_at = utcnow()
            if user_session.status == SessionStatus.NEW:
                user_session.status = SessionStatus.ACTIVE
            if step:
                stats = user_session.stats
                completed = stats.get("completed_steps", [])
                if step not in completed:
                    completed.append(step)
                stats["completed_steps"] = completed
                user_session.stats = stats
            session.commit()

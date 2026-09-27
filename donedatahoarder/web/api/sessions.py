"""
Session management endpoints (create / list / load / save / delete).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    DuplicateGroup,
    File,
    Proposal,
    SessionStatus,
    UserSession,
)
from donedatahoarder.db.session import get_engine
from donedatahoarder.timeutils import utcnow

from .deps import review_operation
from .schemas import CreateSessionRequest, SaveSessionRequest

router = APIRouter()


@router.post("/sessions")
def create_session(body: CreateSessionRequest):
    """Create a new session and return its ID."""
    engine = get_engine()
    with Session(engine) as session:
        user_session = UserSession(
            root_path=body.root_path,
            backend=body.backend,
            model=body.model,
            workers=body.workers,
            preferred_language=body.preferred_language,
            status=SessionStatus.NEW,
            is_unsaved=False,
        )
        # Set new fields if ORM supports them (migration may not have run yet)
        if hasattr(user_session, 'analyze_model'):
            user_session.analyze_model = body.analyze_model or None
        if hasattr(user_session, 'propose_model'):
            user_session.propose_model = body.propose_model or None
        if hasattr(user_session, 'relate_scope') and body.relate_scope:
            user_session.relate_scope = body.relate_scope
        session.add(user_session)
        session.commit()

        return {
            "id": user_session.id,
            "name": user_session.name,
            "created_at": user_session.created_at.isoformat(),
            "status": user_session.status.value,
            "preferred_language": user_session.preferred_language,
        }


@router.get("/sessions")
def list_sessions():
    """List all sessions with preview stats."""
    engine = get_engine()
    with Session(engine) as session:
        sessions = (
            session.query(UserSession)
            .order_by(UserSession.updated_at.desc())
            .all()
        )
        items = []
        for s in sessions:
            # Count files in this session
            file_count = (
                session.query(func.count(File.id))
                .filter(File.session_id == s.id)
                .scalar() or 0
            )
            # Count proposals
            proposal_count = (
                session.query(func.count(Proposal.id))
                .join(File)
                .filter(File.session_id == s.id)
                .scalar() or 0
            )
            # Count duplicate groups
            dupe_count = (
                session.query(func.count(DuplicateGroup.id))
                .filter(DuplicateGroup.session_id == s.id)
                .scalar() or 0
            )

            # Determine completed pipeline steps
            stats = s.stats
            completed_steps = stats.get("completed_steps", [])

            items.append({
                "id": s.id,
                "name": s.name,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat(),
                "last_saved_at": s.last_saved_at.isoformat() if s.last_saved_at else None,
                "root_path": s.root_path,
                "status": s.status.value,
                "is_unsaved": s.is_unsaved,
                "file_count": file_count,
                "proposal_count": proposal_count,
                "duplicate_count": dupe_count,
                "completed_steps": completed_steps,
                "backend": s.backend,
                "model": s.model,
                "analyze_model": getattr(s, 'analyze_model', '') or "",
                "propose_model": getattr(s, 'propose_model', '') or "",
                "workers": s.workers,
                "preferred_language": s.preferred_language,
            })

    return {"items": items, "total": len(items)}


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: str):
    """Load a specific session with full details."""
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        file_count = (
            session.query(func.count(File.id))
            .filter(File.session_id == session_id)
            .scalar() or 0
        )
        proposal_count = (
            session.query(func.count(Proposal.id))
            .join(File)
            .filter(File.session_id == session_id)
            .scalar() or 0
        )
        dupe_count = (
            session.query(func.count(DuplicateGroup.id))
            .filter(DuplicateGroup.session_id == session_id)
            .scalar() or 0
        )

        return {
            "id": user_session.id,
            "name": user_session.name,
            "created_at": user_session.created_at.isoformat(),
            "updated_at": user_session.updated_at.isoformat(),
            "last_saved_at": user_session.last_saved_at.isoformat() if user_session.last_saved_at else None,
            "root_path": user_session.root_path,
            "backend": user_session.backend,
            "model": user_session.model,
            "analyze_model": getattr(user_session, 'analyze_model', '') or "",
            "propose_model": getattr(user_session, 'propose_model', '') or "",
            "workers": user_session.workers,
            "preferred_language": user_session.preferred_language,
            "relate_scope": getattr(user_session, 'relate_scope', 'per_directory'),
            "status": user_session.status.value,
            "is_unsaved": user_session.is_unsaved,
            "stats": user_session.stats,
            "file_count": file_count,
            "proposal_count": proposal_count,
            "duplicate_count": dupe_count,
        }


class UpdateSessionSettingsRequest(BaseModel):
    root_path: Optional[str] = None
    backend: Optional[str] = None
    model: Optional[str] = None
    analyze_model: Optional[str] = None
    propose_model: Optional[str] = None
    workers: Optional[int] = None
    preferred_language: Optional[str] = None
    relate_scope: Optional[str] = None


@router.patch("/sessions/{session_id}")
def update_session_settings(session_id: str, body: UpdateSessionSettingsRequest):
    """Update session settings (models, backend, etc.)."""
    with review_operation("update session settings"):
        return _update_session_settings_locked(session_id, body)


def _update_session_settings_locked(session_id: str, body: UpdateSessionSettingsRequest):
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        if body.root_path is not None:
            user_session.root_path = body.root_path
        if body.backend is not None:
            user_session.backend = body.backend
        if body.model is not None:
            user_session.model = body.model
        if body.analyze_model is not None and hasattr(user_session, 'analyze_model'):
            user_session.analyze_model = body.analyze_model or None
        if body.propose_model is not None and hasattr(user_session, 'propose_model'):
            user_session.propose_model = body.propose_model or None
        if body.workers is not None:
            user_session.workers = body.workers
        if body.preferred_language is not None:
            user_session.preferred_language = body.preferred_language
        if body.relate_scope is not None and hasattr(user_session, 'relate_scope'):
            user_session.relate_scope = body.relate_scope

        user_session.is_unsaved = True
        user_session.updated_at = utcnow()
        session.commit()

        return {"status": "ok"}


@router.post("/sessions/{session_id}/save")
def save_session(session_id: str, body: SaveSessionRequest):
    """Save session with a user-provided name."""
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        # Check for duplicate names (exclude this session)
        if body.name and body.name.strip():
            existing = (
                session.query(UserSession)
                .filter(UserSession.name == body.name.strip())
                .filter(UserSession.id != session_id)
                .first()
            )
            if existing:
                raise HTTPException(409, f"Session name '{body.name}' already exists")
            user_session.name = body.name.strip()

        user_session.is_unsaved = False
        user_session.last_saved_at = utcnow()
        user_session.updated_at = utcnow()
        session.commit()

        return {
            "id": user_session.id,
            "name": user_session.name,
            "is_unsaved": False,
            "last_saved_at": user_session.last_saved_at.isoformat(),
        }


@router.patch("/sessions/{session_id}")
def mark_session_dirty(session_id: str):
    """Mark a session as having unsaved changes."""
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        user_session.is_unsaved = True
        user_session.updated_at = utcnow()
        session.commit()

    return {"id": session_id, "is_unsaved": True}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a session and all its associated data."""
    with review_operation("delete session"):
        return _delete_session_locked(session_id)


def _delete_session_locked(session_id: str):
    # A second web process may own a paused or just-dispatched worker that
    # is absent from this process's in-memory registry. Its persisted lease
    # remains live until that worker really exits.
    from donedatahoarder.core.jobs import job_manager
    from donedatahoarder.core.job_store import LIVE_STATES
    from donedatahoarder.db.models import BackgroundJob

    job_manager.cancel_session_jobs(session_id)
    job_manager.reconcile_startup()
    with Session(get_engine()) as db:
        live = (db.query(BackgroundJob).filter(
            BackgroundJob.session_id == session_id,
            BackgroundJob.state.in_(LIVE_STATES),
        ).first())
    if live is not None:
        job_manager.force_cancel(live.id)
        raise HTTPException(409, "Wait for the session's pipeline worker to exit before deleting it")

    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        session.delete(user_session)
        session.commit()

    return {"status": "deleted", "id": session_id}

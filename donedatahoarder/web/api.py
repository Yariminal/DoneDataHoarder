"""
REST API endpoints for the DoneDataHoarder web UI.
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import string
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from starlette.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    DuplicateGroup,
    File,
    FileStatus,
    Proposal,
    ProposalStatus,
    ProposalType,
    SessionStatus,
    UserSession,
)
from donedatahoarder.db.session import get_engine

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class StatsResponse(BaseModel):
    total_files: int = 0
    total_size_bytes: int = 0
    by_status: dict[str, int] = {}
    by_extension: list[dict] = []
    by_mime_category: list[dict] = []
    proposal_counts: dict[str, int] = {}
    duplicate_groups: int = 0
    duplicate_wasted_bytes: int = 0


class FileResponse(BaseModel):
    id: int
    path: str
    filename: str
    extension: Optional[str] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    status: str
    date_best: Optional[str] = None
    ai_description: Optional[str] = None
    ai_tags: Optional[list[str]] = None
    ai_confidence: Optional[float] = None
    ai_model: Optional[str] = None
    proposals: list[dict] = []


class ProposalResponse(BaseModel):
    id: int
    file_id: int
    filename: str
    proposal_type: str
    current_value: Optional[str] = None
    proposed_value: Optional[str] = None
    reasoning: Optional[str] = None
    confidence: Optional[float] = None
    status: str
    mime_type: Optional[str] = None


class DuplicateGroupResponse(BaseModel):
    id: int
    dupe_type: str
    count: int
    keep_file_id: Optional[int] = None
    wasted_bytes: int = 0
    files: list[dict] = []


class BulkApproveRequest(BaseModel):
    min_confidence: float = 0.8
    proposal_type: Optional[str] = None


class EditProposalRequest(BaseModel):
    proposed_value: str


class SetKeeperRequest(BaseModel):
    keep_file_id: int


class PipelineRequest(BaseModel):
    root_path: str = ""
    backend: str = "ollama"
    model: str = "gemma3:12b"
    workers: int = 1
    session_id: str = ""
    skip_dirs: list[str] = []


class CreateSessionRequest(BaseModel):
    root_path: str = ""
    backend: str = "ollama"
    model: str = "gemma3:12b"
    analyze_model: str = ""
    propose_model: str = ""
    workers: int = 1
    preferred_language: str = "leave_as_is"
    # "per_directory" (default, cheap) or "cross_directory" (whole-tree relate).
    relate_scope: str = "per_directory"


class SaveSessionRequest(BaseModel):
    name: str


class ExecuteRequest(BaseModel):
    session_id: str = ""
    dry_run: bool = True
    min_confidence: float = 0.7


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------

@router.get("/info")
def get_info():
    """Get app version and info."""
    try:
        from importlib.metadata import version
        app_version = version("donedatahoarder")
    except Exception:
        app_version = "0.3.0"

    return {
        "name": "DoneDataHoarder",
        "version": app_version,
        "description": "AI-powered file organization for data hoarders",
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

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
        user_session.updated_at = datetime.utcnow()
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
        user_session.last_saved_at = datetime.utcnow()
        user_session.updated_at = datetime.utcnow()
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
        user_session.updated_at = datetime.utcnow()
        session.commit()

    return {"id": session_id, "is_unsaved": True}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a session and all its associated data."""
    # Force-cancel any running jobs for this session first
    from donedatahoarder.core.jobs import job_manager
    job_manager.cancel_session_jobs(session_id)

    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if not user_session:
            raise HTTPException(404, "Session not found")

        session.delete(user_session)
        session.commit()

    return {"status": "deleted", "id": session_id}


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
            user_session.updated_at = datetime.utcnow()
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


# ---------------------------------------------------------------------------
# Dashboard / Stats
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=StatsResponse)
def get_stats(session_id: Optional[str] = None):
    sid = session_id
    engine = get_engine()
    with Session(engine) as session:
        file_q = session.query(File)
        if sid:
            file_q = file_q.filter(File.session_id == sid)

        total_files = file_q.count()
        total_size = file_q.with_entities(func.sum(File.size_bytes)).scalar() or 0

        # By status
        status_q = session.query(File.status, func.count(File.id))
        if sid:
            status_q = status_q.filter(File.session_id == sid)
        status_rows = status_q.group_by(File.status).all()
        by_status = {s.value: c for s, c in status_rows}

        # Top extensions
        ext_q = session.query(File.extension, func.count(File.id)).filter(File.extension.isnot(None))
        if sid:
            ext_q = ext_q.filter(File.session_id == sid)
        ext_rows = ext_q.group_by(File.extension).order_by(func.count(File.id).desc()).limit(15).all()
        by_extension = [{"ext": e, "count": c} for e, c in ext_rows]

        # By MIME category
        mime_q = session.query(
            func.substr(File.mime_type, 1, func.instr(File.mime_type, "/") - 1),
            func.count(File.id),
        ).filter(File.mime_type.isnot(None))
        if sid:
            mime_q = mime_q.filter(File.session_id == sid)
        mime_rows = mime_q.group_by(func.substr(File.mime_type, 1, func.instr(File.mime_type, "/") - 1)).order_by(func.count(File.id).desc()).all()
        by_mime = [{"category": m or "unknown", "count": c} for m, c in mime_rows]

        # Proposals
        prop_q = session.query(Proposal.status, func.count(Proposal.id))
        if sid:
            prop_q = prop_q.join(File).filter(File.session_id == sid)
        prop_rows = prop_q.group_by(Proposal.status).all()
        proposal_counts = {s.value: c for s, c in prop_rows}

        # Duplicates
        dupe_q = session.query(func.count(DuplicateGroup.id))
        if sid:
            dupe_q = dupe_q.filter(DuplicateGroup.session_id == sid)
        dupe_count = dupe_q.scalar() or 0

        # Wasted bytes in duplicate groups
        dupe_wasted = 0
        grp_q = session.query(DuplicateGroup)
        if sid:
            grp_q = grp_q.filter(DuplicateGroup.session_id == sid)
        groups = grp_q.all()
        for g in groups:
            for m in g.members:
                if m.file_id != g.keep_file_id:
                    f = session.get(File, m.file_id)
                    if f:
                        dupe_wasted += f.size_bytes or 0

    return StatsResponse(
        total_files=total_files,
        total_size_bytes=total_size,
        by_status=by_status,
        by_extension=by_extension,
        by_mime_category=by_mime,
        proposal_counts=proposal_counts,
        duplicate_groups=dupe_count,
        duplicate_wasted_bytes=dupe_wasted,
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@router.get("/files")
def list_files(
    status: Optional[str] = None,
    mime_prefix: Optional[str] = None,
    extension: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "filename",
    order: str = "asc",
    page: int = 1,
    per_page: int = 50,
    session_id: Optional[str] = None,
):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(File)
        if session_id:
            query = query.filter(File.session_id == session_id)

        if status:
            try:
                query = query.filter(File.status == FileStatus(status))
            except ValueError:
                pass
        if mime_prefix:
            query = query.filter(File.mime_type.like(f"{mime_prefix}%"))
        if extension:
            query = query.filter(File.extension == extension.lower())
        if search:
            term = f"%{search}%"
            query = query.filter(
                File.filename.ilike(term)
                | File.path.ilike(term)
                | File.ai_description.ilike(term)
            )

        # Sorting
        sort_col = getattr(File, sort, File.filename)
        if order == "desc":
            sort_col = sort_col.desc()
        query = query.order_by(sort_col)

        total = query.count()
        files = query.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        for f in files:
            tags = []
            if f.ai_tags:
                try:
                    tags = json.loads(f.ai_tags)
                except (json.JSONDecodeError, TypeError):
                    pass
            items.append({
                "id": f.id,
                "path": f.path,
                "filename": f.filename,
                "extension": f.extension,
                "size_bytes": f.size_bytes,
                "mime_type": f.mime_type,
                "status": f.status.value,
                "date_best": f.date_best.isoformat() if f.date_best else None,
                "ai_description": f.ai_description,
                "ai_tags": tags,
                "ai_confidence": f.ai_confidence,
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/files/{file_id}")
def get_file(file_id: int):
    engine = get_engine()
    with Session(engine) as session:
        f = session.get(File, file_id)
        if not f:
            raise HTTPException(404, "File not found")

        tags = []
        if f.ai_tags:
            try:
                tags = json.loads(f.ai_tags)
            except (json.JSONDecodeError, TypeError):
                pass

        proposals = [
            {
                "id": p.id,
                "type": p.proposal_type.value,
                "current_value": p.current_value,
                "proposed_value": p.proposed_value,
                "reasoning": p.reasoning,
                "confidence": p.confidence,
                "status": p.status.value,
            }
            for p in f.proposals
        ]

        return {
            "id": f.id,
            "path": f.path,
            "filename": f.filename,
            "extension": f.extension,
            "size_bytes": f.size_bytes,
            "mime_type": f.mime_type,
            "hash_md5": f.hash_md5,
            "status": f.status.value,
            "date_modified": f.date_modified.isoformat() if f.date_modified else None,
            "date_created": f.date_created.isoformat() if f.date_created else None,
            "date_exif": f.date_exif.isoformat() if f.date_exif else None,
            "date_best": f.date_best.isoformat() if f.date_best else None,
            "ai_description": f.ai_description,
            "ai_tags": tags,
            "ai_confidence": f.ai_confidence,
            "ai_model": f.ai_model,
            "ai_transcript": f.ai_transcript,
            "proposals": proposals,
        }


@router.get("/files/{file_id}/thumbnail")
def get_thumbnail(file_id: int, size: int = 200):
    """Serve a resized thumbnail for image files."""
    engine = get_engine()
    with Session(engine) as session:
        f = session.get(File, file_id)
        if not f:
            raise HTTPException(404, "File not found")

    path = Path(f.path)
    mime = f.mime_type or ""

    if not mime.startswith("image/") and path.suffix.lower() not in (
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"
    ):
        raise HTTPException(415, "Not an image file")

    if not path.exists():
        raise HTTPException(404, "File not on disk")

    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return Response(
                content=buf.getvalue(),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except Exception as exc:
        raise HTTPException(500, f"Thumbnail generation failed: {exc}")


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

@router.get("/proposals")
def list_proposals(
    status: Optional[str] = None,
    proposal_type: Optional[str] = None,
    min_confidence: float = 0.0,
    search: Optional[str] = None,
    sort: str = "confidence",
    order: str = "desc",
    page: int = 1,
    per_page: int = 50,
    session_id: Optional[str] = None,
):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(Proposal).join(File)
        if session_id:
            query = query.filter(File.session_id == session_id)

        if status:
            try:
                query = query.filter(Proposal.status == ProposalStatus(status))
            except ValueError:
                pass
        else:
            # Default: show pending
            query = query.filter(Proposal.status == ProposalStatus.PENDING)

        if proposal_type:
            try:
                query = query.filter(Proposal.proposal_type == ProposalType(proposal_type))
            except ValueError:
                pass
        if min_confidence > 0:
            query = query.filter(Proposal.confidence >= min_confidence)
        if search:
            term = f"%{search}%"
            query = query.filter(
                File.filename.ilike(term)
                | Proposal.proposed_value.ilike(term)
                | Proposal.reasoning.ilike(term)
            )

        # Sort
        if sort == "confidence":
            sort_col = Proposal.confidence.desc() if order == "desc" else Proposal.confidence
        elif sort == "filename":
            sort_col = File.filename.desc() if order == "desc" else File.filename
        else:
            sort_col = Proposal.id.desc() if order == "desc" else Proposal.id
        query = query.order_by(sort_col)

        total = query.count()
        proposals = query.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        for p in proposals:
            f = session.get(File, p.file_id)
            current_name = Path(p.current_value).name if p.current_value else (f.filename if f else "")
            proposed_name = Path(p.proposed_value).name if p.proposed_value and p.proposal_type == ProposalType.RENAME else p.proposed_value
            items.append({
                "id": p.id,
                "file_id": p.file_id,
                "filename": f.filename if f else "",
                "proposal_type": p.proposal_type.value,
                "current_value": current_name,
                "proposed_value": proposed_name,
                "reasoning": p.reasoning,
                "confidence": p.confidence,
                "status": p.status.value,
                "mime_type": f.mime_type if f else None,
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")
        p.status = ProposalStatus.APPROVED
        session.commit()
    return {"status": "approved", "id": proposal_id}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")
        p.status = ProposalStatus.REJECTED
        session.commit()
    return {"status": "rejected", "id": proposal_id}


@router.post("/proposals/{proposal_id}/edit")
def edit_proposal(proposal_id: int, body: EditProposalRequest):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")

        if p.proposal_type == ProposalType.RENAME and p.current_value:
            # Replace just the filename, keep the directory
            old_dir = str(Path(p.current_value).parent)
            p.proposed_value = str(Path(old_dir) / body.proposed_value)
        else:
            p.proposed_value = body.proposed_value

        p.status = ProposalStatus.MODIFIED
        session.commit()
    return {"status": "modified", "id": proposal_id, "proposed_value": p.proposed_value}


@router.post("/proposals/bulk-approve")
def bulk_approve(body: BulkApproveRequest):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(Proposal).filter(
            Proposal.status == ProposalStatus.PENDING,
            Proposal.confidence >= body.min_confidence,
        )
        if body.proposal_type:
            try:
                query = query.filter(Proposal.proposal_type == ProposalType(body.proposal_type))
            except ValueError:
                pass
        count = query.update({"status": ProposalStatus.APPROVED})
        session.commit()
    return {"approved": count}


@router.post("/proposals/bulk-reject")
def bulk_reject():
    engine = get_engine()
    with Session(engine) as session:
        count = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .update({"status": ProposalStatus.REJECTED})
        )
        session.commit()
    return {"rejected": count}


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@router.get("/duplicates")
def list_duplicates(page: int = 1, per_page: int = 20, session_id: str = Query(...)):
    _require_session_id(session_id)
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(DuplicateGroup).filter(DuplicateGroup.session_id == session_id)
        total = q.count()
        groups = q.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        for g in groups:
            files = []
            wasted = 0
            for m in g.members:
                f = session.get(File, m.file_id)
                if f:
                    is_keeper = f.id == g.keep_file_id
                    if not is_keeper:
                        wasted += f.size_bytes or 0
                    files.append({
                        "id": f.id,
                        "path": f.path,
                        "filename": f.filename,
                        "size_bytes": f.size_bytes,
                        "date_best": f.date_best.isoformat() if f.date_best else None,
                        "is_keeper": is_keeper,
                        "mime_type": f.mime_type,
                    })
            items.append({
                "id": g.id,
                "dupe_type": g.dupe_type.value,
                "count": len(files),
                "keep_file_id": g.keep_file_id,
                "wasted_bytes": wasted,
                "files": files,
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/duplicates/{group_id}/keeper")
def set_keeper(group_id: int, body: SetKeeperRequest):
    engine = get_engine()
    with Session(engine) as session:
        g = session.get(DuplicateGroup, group_id)
        if not g:
            raise HTTPException(404, "Group not found")
        g.keep_file_id = body.keep_file_id
        session.commit()
    return {"status": "ok", "keep_file_id": body.keep_file_id}


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

@router.post("/execute")
def execute_proposals(body: ExecuteRequest):
    from donedatahoarder.executor import execute as do_execute, _make_quiet_console

    sid = body.session_id
    if not sid:
        raise HTTPException(400, "No active session. Create or load a session first.")

    counts = do_execute(
        dry_run=body.dry_run,
        min_confidence=body.min_confidence,
        session_id=sid,
        _console=_make_quiet_console(),
    )
    _mark_session_unsaved(sid, step="execute")

    # Record completed folders after a real (non-dry) execute
    if not body.dry_run:
        try:
            from donedatahoarder.db.models import CompletedFolder as CF
            engine = get_engine()
            with Session(engine) as db:
                us = db.get(UserSession, sid)
                root_path = us.root_path if us else ""
                # Collect unique parent dirs of APPLIED files
                applied_parents = set(
                    str(Path(f.path).parent)
                    for f in db.query(File).filter(
                        File.session_id == sid,
                        File.status == FileStatus.APPLIED,
                    ).all()
                )
                # Also include the root itself
                if root_path:
                    applied_parents.add(root_path)
                # Upsert: avoid duplicate entries
                existing = {
                    r.folder_path
                    for r in db.query(CF.folder_path).filter(
                        CF.session_id == sid
                    ).all()
                }
                for folder_path in sorted(applied_parents):
                    if folder_path not in existing:
                        db.add(CF(
                            folder_path=folder_path,
                            session_id=sid,
                            root_path=root_path,
                        ))
                db.commit()
        except Exception:
            pass  # Non-fatal

    return counts


# ---------------------------------------------------------------------------
# Pipeline triggers (run steps on demand)
# ---------------------------------------------------------------------------

@router.post("/pipeline/scan")
def trigger_scan(body: PipelineRequest):
    from donedatahoarder.core.scanner import scan as do_scan
    import io
    import contextlib

    try:
        sid = _require_session_id(body.session_id)

        if not body.root_path or not body.root_path.strip():
            raise HTTPException(400, "Root path is required")

        root = Path(body.root_path)
        if not root.exists():
            raise HTTPException(400, f"Path does not exist: {root}")

        if not root.is_dir():
            raise HTTPException(400, f"Path is not a directory: {root}")

        # Update session root_path
        engine = get_engine()
        with Session(engine) as session:
            user_session = session.get(UserSession, sid)
            if user_session:
                user_session.root_path = str(root.resolve())
                session.commit()

        # Suppress stdout to prevent Rich progress bar Unicode errors in web context
        extra_skip = set(body.skip_dirs) if body.skip_dirs else None
        with contextlib.redirect_stdout(io.StringIO()):
            counts = do_scan(root, session_id=sid, extra_skip_dirs=extra_skip)

        _mark_session_unsaved(sid, step="scan")
        return counts
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Scan failed: {str(exc)}")


@router.post("/pipeline/enrich")
def trigger_enrich(body: PipelineRequest = PipelineRequest()):
    """Start an enrich background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        job_id = job_manager.start_enrich(session_id=sid)
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Enrich failed: {str(exc)}")


@router.post("/pipeline/dedup")
def trigger_dedup(body: PipelineRequest = PipelineRequest()):
    from donedatahoarder.core.dedup import (
        find_exact_duplicates, find_perceptual_duplicates,
        find_semantic_duplicates, find_text_near_duplicates,
        generate_dedup_proposals,
    )
    import io
    import contextlib

    try:
        sid = _require_session_id(body.session_id)

        # Suppress stdout to prevent Rich progress bar Unicode errors in web context
        with contextlib.redirect_stdout(io.StringIO()):
            exact = find_exact_duplicates(session_id=sid)
            perc = find_perceptual_duplicates(session_id=sid)
            semantic = find_semantic_duplicates(session_id=sid)  # Stage 3: AI-based semantic duplicates
            text_near = find_text_near_duplicates(session_id=sid)  # Stage 4: byte-level fuzzy text match
            # Stage 5: turn detected groups into actionable MARK_DUPLICATE proposals
            # so the executor / approval UI actually has something to work with.
            proposals = generate_dedup_proposals(session_id=sid)

        _mark_session_unsaved(sid, step="dedup")
        return {
            "exact": exact,
            "perceptual": perc,
            "semantic": semantic,
            "text_near": text_near,
            "proposals": proposals,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Dedup failed: {str(exc)}")


@router.post("/pipeline/relate")
def trigger_relate(body: PipelineRequest = PipelineRequest()):
    """
    Run the Relate step: LLM groups conceptually-related files (e.g. a .dwg
    with its .bak backup and PDF exports), falling back to numeric-prefix
    regex clustering if the LLM is unavailable. Writes RelationGroups /
    RelationMembers. Idempotent — re-running wipes and rebuilds groups.

    Relate is filename-only reasoning (no vision), so it uses the session's
    `propose_model` (reasoning model) rather than `analyze_model` (vision).
    This is a hard init — previously the step inherited whatever model was
    last initialised by a prior step, which could be a tiny vision-tuned
    model incapable of producing valid JSON group output.
    """
    from donedatahoarder.ai.router import init_ai
    from donedatahoarder.core.relate import relate

    try:
        sid = _require_session_id(body.session_id)
        # Relate is a reasoning task — use propose_model (fallback chain in
        # _resolve_model covers empty / legacy cases).
        model = _resolve_model(body.model, sid, step="propose")
        init_ai(
            backend=body.backend,
            text_model=model,
            vision_model=model,
        )

        # Pull the session's relate_scope preference
        scope = "per_directory"
        with Session(get_engine()) as db:
            us = db.get(UserSession, sid)
            if us and getattr(us, "relate_scope", None):
                scope = us.relate_scope

        summary = relate(session_id=sid, scope=scope, model=model)
        _mark_session_unsaved(sid, step="relate")
        return {
            "scope": scope,
            "model": model,
            **summary,
        }
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Relate failed: {str(exc)}")


@router.get("/sessions/{session_id}/relations")
def list_relations(session_id: str):
    """List RelationGroups (with members) for a session — for UI display."""
    from donedatahoarder.db.models import RelationGroup
    engine = get_engine()
    with Session(engine) as session:
        groups = (
            session.query(RelationGroup)
            .filter(RelationGroup.session_id == session_id)
            .order_by(RelationGroup.dir_path, RelationGroup.label)
            .all()
        )
        # Prefetch filenames for every file referenced by any member
        all_file_ids = {m.file_id for g in groups for m in g.members}
        filename_by_id: dict[int, str] = {}
        if all_file_ids:
            for fid, fname in session.query(File.id, File.filename).filter(
                File.id.in_(all_file_ids)
            ):
                filename_by_id[fid] = fname

        out = []
        for g in groups:
            out.append({
                "id": g.id,
                "label": g.label,
                "reason": g.reason,
                "confidence": g.confidence,
                "scope": g.scope,
                "dir_path": g.dir_path,
                "members": [
                    {
                        "file_id": m.file_id,
                        "filename": filename_by_id.get(m.file_id, "?"),
                        "role": m.role.value,
                    }
                    for m in g.members
                ],
            })
        return {"session_id": session_id, "groups": out}


@router.post("/pipeline/analyze")
def trigger_analyze(body: PipelineRequest):
    """Start an analyze background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="analyze")
        # Persist the resolved model back to the session so later steps (Propose, Organize) can use it
        if model and body.model:
            with Session(get_engine()) as db:
                us = db.get(UserSession, sid)
                if us and not us.model:
                    us.model = model
                    db.commit()
        job_id = job_manager.start_analyze(
            session_id=sid,
            backend=body.backend,
            model=model,
            workers=body.workers,
        )
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Analyze failed: {str(exc)}")


# ---------------------------------------------------------------------------
# Job control endpoints (pause, resume, stream, active)
# ---------------------------------------------------------------------------

@router.get("/pipeline/jobs/active")
def get_active_job():
    """Return the currently active background job, if any."""
    from donedatahoarder.core.jobs import job_manager
    job = job_manager.get_active()
    if job:
        return job.to_dict()
    return {"job_id": None}


@router.get("/pipeline/jobs/{job_id}")
def get_job_status(job_id: str):
    """Return status of a specific job."""
    from donedatahoarder.core.jobs import job_manager
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job.to_dict()


@router.get("/pipeline/jobs/{job_id}/stream")
def stream_job_progress(job_id: str):
    """SSE stream of job progress. Reconnectable after page refresh."""
    from donedatahoarder.core.jobs import job_manager

    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    def generate():
        try:
            for progress in job_manager.subscribe(job_id):
                yield f"data: {json.dumps(progress)}\n\n"
        except KeyError:
            yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/pipeline/jobs/{job_id}/pause")
def pause_job(job_id: str):
    """Pause a running job."""
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.pause(job_id)
        return {"status": "paused", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/jobs/{job_id}/resume")
def resume_job(job_id: str):
    """Resume a paused job."""
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.resume(job_id)
        return {"status": "resumed", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Cancel a running or paused job.

    Sets the cooperative cancel flag first, then immediately force-finishes
    the job so the UI reflects the cancellation and new jobs can start.
    The worker thread (if stuck on an Ollama call) will eventually exit
    on its own since it's a daemon thread.
    """
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.force_cancel(job_id)
        return {"status": "cancelled", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/propose")
def trigger_propose(body: PipelineRequest = PipelineRequest()):
    from donedatahoarder.proposals.namer import generate_proposals
    import io
    import contextlib

    try:
        sid = _require_session_id(body.session_id)

        with contextlib.redirect_stdout(io.StringIO()):
            counts = generate_proposals(session_id=sid)

        _mark_session_unsaved(sid, step="propose")
        return counts
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Propose failed: {str(exc)}")


def _fs_walk_skeleton(root_path: str, max_depth: int = 3) -> dict:
    """
    Walk the actual filesystem under root_path and produce a skeletal tree
    that includes *every* folder and (up to N) file names — regardless of
    whether those files were analyzed / exist in the DB.

    This is what makes empty folders (GIF/, images/, _files/ companions) and
    un-analyzed / errored / pending root files (the 452 MB PDF, HTMLs) show
    up in the UI. Without it, the tree only ever reflects ANALYZED files
    and the user has no idea what's actually on disk.

    Depth is capped so we don't blow up on deep hierarchies; the UI tree
    only ever shows a few levels anyway.
    """
    root = Path(root_path)
    tree: dict = {}
    if not root.exists():
        return tree

    SAMPLE_CAP = 12  # per-folder cap on listed filenames

    def _walk(fs_path: Path, node: dict, depth: int) -> None:
        try:
            entries = list(fs_path.iterdir())
        except (OSError, PermissionError):
            return

        files: list[dict] = []
        total_size = 0
        for entry in entries:
            try:
                if entry.is_file():
                    size = 0
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass
                    total_size += size
                    files.append({"name": entry.name, "size": size})
                elif entry.is_dir() and depth < max_depth:
                    # Recurse into subdirs
                    sub: dict = {}
                    node[entry.name] = sub
                    _walk(entry, sub, depth + 1)
            except OSError:
                continue

        # Record file count (own directory, not including subfolders) and sample.
        # Sort files by size descending so the biggest show first — the 452 MB
        # PDF jumps to the top of the list where the user can't miss it.
        files.sort(key=lambda f: -f["size"])
        node["_files"] = len(files)
        node["_size"] = total_size
        if files:
            node["_sample_files"] = files[:SAMPLE_CAP]
            if len(files) > SAMPLE_CAP:
                node["_sample_truncated"] = len(files) - SAMPLE_CAP

    _walk(root, tree, 0)
    return tree


def _folder_summaries_to_tree(summaries, root_path: str) -> dict:
    """
    Build a nested dict tree for the frontend visualization.

    Starts from an actual filesystem walk so that *every* folder on disk is
    represented (even empty / un-analyzed ones like GIF/ or images/), then
    overlays analyzed-file metadata from the FolderSummary records. Without
    the fs walk, folders that had no analyzed files would silently disappear
    from the Current Structure panel, misleading the user about what's on disk.

    The overlay increments counters rather than overwriting them — that way
    the fs file counts include un-analyzed files too (so the 452 MB PDF at
    root shows up in the root's total even if it was never analyzed).
    """
    root = Path(root_path)

    # Phase 1: skeleton from actual filesystem — guarantees every folder and
    # file on disk is represented, regardless of analysis status.
    tree = _fs_walk_skeleton(str(root))

    # Phase 2: no overlay needed — the fs walk already populated _files,
    # _size, and _sample_files. FolderSummary metadata (tags, descriptions)
    # isn't used by the frontend tree renderer, so there's nothing to merge.
    # We keep `summaries` as a parameter for API compatibility and future use
    # (e.g. overlaying analysis-derived labels).
    _ = summaries

    return tree


def _build_proposed_tree(session_id: str, before_summaries, root_path: str) -> dict:
    """
    Build an 'after' tree reflecting pending MOVE and RENAME_FOLDER proposals.

    Starts from the filesystem-backed before-tree, then simulates the effect
    of applying every pending folder rename + file move. Unlike the previous
    implementation this also:

    - Decrements the source folder's counts (not just increments the target)
    - Moves the actual filename between source._sample_files and target._sample_files
      so the UI shows the file in its destination, not both places
    """
    tree = _folder_summaries_to_tree(before_summaries, root_path)
    root = Path(root_path)

    def _node_at(parts: tuple[str, ...]) -> dict | None:
        """Traverse the tree by path parts. Returns None if any segment missing."""
        node: dict = tree
        for part in parts:
            if part not in node or not isinstance(node[part], dict):
                return None
            node = node[part]
        return node

    def _ensure_node(parts: tuple[str, ...]) -> dict:
        """Like _node_at but creates missing segments as empty dicts."""
        node: dict = tree
        for part in parts:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {"_files": 0, "_size": 0}
            node = node[part]
        return node

    # Fetch each Proposal alongside its File's size_bytes so we can use the
    # DB as the authoritative source for move sizes. Relying on _sample_files
    # alone was buggy: when the moved file was past SAMPLE_CAP (12 entries
    # per folder), the destination node would end up with "1 files, 0 B"
    # because the fallback entry used size=0. DB size_bytes is always right.
    with Session(get_engine()) as db:
        rows = (
            db.query(Proposal, File.size_bytes)
            .join(File, Proposal.file_id == File.id)
            .filter(
                File.session_id == session_id,
                Proposal.status == ProposalStatus.PENDING,
                Proposal.proposal_type.in_([ProposalType.MOVE, ProposalType.RENAME_FOLDER]),
            )
            .all()
        )

    # Pass 1: apply folder renames (just a key rename in the parent node).
    # Done first so subsequent MOVE lookups resolve via post-rename paths.
    for p, _size in rows:
        if p.proposal_type == ProposalType.RENAME_FOLDER and p.current_value and p.proposed_value:
            try:
                old_rel = Path(p.current_value).relative_to(root)
                new_rel = Path(p.proposed_value).relative_to(root)
            except ValueError:
                continue
            old_parts = old_rel.parts
            new_name = new_rel.parts[-1] if new_rel.parts else None
            if old_parts and new_name:
                parent_node = _node_at(old_parts[:-1]) if len(old_parts) > 1 else tree
                if parent_node is None:
                    continue
                old_name = old_parts[-1]
                if old_name in parent_node:
                    parent_node[new_name] = parent_node.pop(old_name)

    # Pass 2: apply file MOVE proposals. Each MOVE updates both source and
    # target: decrement source counts / remove from source._sample_files,
    # increment target counts / add to target._sample_files. Size comes from
    # the DB (File.size_bytes) so the bookkeeping works even for files past
    # the per-folder sample cap.
    for p, db_size in rows:
        if p.proposal_type != ProposalType.MOVE or not p.proposed_value or not p.current_value:
            continue
        try:
            src_path = Path(p.current_value)
            dst_path = Path(p.proposed_value)
            src_parent_rel = src_path.parent.relative_to(root).parts
            dst_parent_rel = dst_path.parent.relative_to(root).parts
        except ValueError:
            continue

        file_size = int(db_size or 0)
        src_node = _node_at(src_parent_rel)
        dst_node = _ensure_node(dst_parent_rel)

        # Pop the entry from source._sample_files if present (so the UI
        # doesn't show the same file in two places). Absence is fine — the
        # file just wasn't among the 12 sampled names for that folder.
        src_name = src_path.name
        if src_node is not None:
            src_samples = src_node.get("_sample_files") or []
            for i, entry in enumerate(src_samples):
                if entry.get("name") == src_name:
                    src_samples.pop(i)
                    break
            # Always decrement source counts — the file is leaving regardless
            # of whether it was in the sample list.
            src_node["_files"] = max(0, src_node.get("_files", 1) - 1)
            src_node["_size"] = max(0, src_node.get("_size", 0) - file_size)

        # Place it into the destination with the authoritative DB size.
        dst_samples = dst_node.setdefault("_sample_files", [])
        dst_samples.append({"name": dst_path.name, "size": file_size})
        dst_node["_files"] = dst_node.get("_files", 0) + 1
        dst_node["_size"] = dst_node.get("_size", 0) + file_size

    return tree


@router.post("/pipeline/organize")
def trigger_organize(body: PipelineRequest = PipelineRequest()):
    """Use LLM to suggest folder reorganization based on file analysis."""
    from donedatahoarder.ai.router import init_ai
    from donedatahoarder.proposals.organizer import generate_reorg_proposals

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="propose")
        init_ai(
            backend=body.backend,
            text_model=model,
            vision_model=model,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))

    try:
        from donedatahoarder.proposals.organizer import build_folder_tree

        # Capture current folder structure BEFORE generating proposals
        with Session(get_engine()) as db:
            us = db.get(UserSession, sid)
            root_path = us.root_path if us else ""
        before_summaries = build_folder_tree(sid, root_path)
        before_tree = _folder_summaries_to_tree(before_summaries, root_path)

        counts = generate_reorg_proposals(session_id=sid)
        _mark_session_unsaved(sid, step="organize")

        # Build proposed "after" tree using the new MOVE/RENAME_FOLDER proposals
        after_tree = _build_proposed_tree(sid, before_summaries, root_path)

        return {**counts, "before_tree": before_tree, "after_tree": after_tree}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Organize failed: {str(exc)}")


# ---------------------------------------------------------------------------
# Filesystem browser (for folder selection)
# ---------------------------------------------------------------------------

class BrowseResponse(BaseModel):
    current: str
    parent: Optional[str] = None
    drives: list[dict] = []
    folders: list[dict] = []


@router.get("/browse")
def browse_filesystem(path: Optional[str] = None) -> BrowseResponse:
    """
    Browse directories for the folder picker.
    If no path given, returns available drives (Windows) or / (Unix).
    """
    # --- List drives (root level) ---
    if not path:
        if platform.system() == "Windows":
            drives = []
            # Check all drive letters
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    try:
                        total, used, free = shutil.disk_usage(drive)
                        drives.append({
                            "name": drive,
                            "label": f"{letter}: Drive",
                            "total_bytes": total,
                            "free_bytes": free,
                        })
                    except (PermissionError, OSError):
                        drives.append({"name": drive, "label": f"{letter}: Drive", "total_bytes": 0, "free_bytes": 0})
            return BrowseResponse(current="", drives=drives, folders=[])
        else:
            path = "/"

    # --- List folders at the given path ---
    target = Path(path)
    if not target.exists():
        raise HTTPException(400, f"Path does not exist: {path}")
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {path}")

    parent = str(target.parent) if target.parent != target else None

    folders = []
    try:
        for entry in sorted(target.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            # Skip hidden/system dirs
            if name.startswith(".") or name.startswith("$") or name in (
                "System Volume Information", "RECYCLER", "$RECYCLE.BIN",
            ):
                continue
            try:
                # Quick peek: count children and check accessibility
                children = sum(1 for c in entry.iterdir() if c.is_dir())
                folders.append({
                    "name": name,
                    "path": str(entry),
                    "has_children": children > 0,
                })
            except PermissionError:
                folders.append({"name": name, "path": str(entry), "has_children": False, "locked": True})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Cannot read directory: {path}")

    return BrowseResponse(
        current=str(target),
        parent=parent,
        folders=folders,
    )


# ---------------------------------------------------------------------------
# Subfolders — list immediate subdirs, flagging completed ones
# ---------------------------------------------------------------------------

@router.get("/subfolders")
def list_subfolders(root_path: str):
    """Return immediate subdirectories of root_path, marking completed ones."""
    from donedatahoarder.db.models import CompletedFolder as CF
    target = Path(root_path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(400, f"Invalid path: {root_path}")

    engine = get_engine()
    # Get set of completed folder paths for this root
    completed_paths: set[str] = set()
    try:
        with Session(engine) as db:
            if "completed_folders" in [t.name for t in db.get_bind().dialect.get_table_names(db.get_bind()) if False] or True:
                rows = db.query(CF.folder_path).filter(CF.root_path == root_path).all()
                completed_paths = {r.folder_path for r in rows}
    except Exception:
        pass

    folders = []
    try:
        for entry in sorted(target.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name.startswith("$"):
                continue
            folders.append({
                "name": name,
                "path": str(entry),
                "completed": str(entry) in completed_paths,
            })
    except PermissionError:
        raise HTTPException(403, f"Cannot read directory: {root_path}")

    return {"folders": folders}


# ---------------------------------------------------------------------------
# Completed folders log
# ---------------------------------------------------------------------------

@router.get("/completed-folders")
def list_completed_folders(root_path: Optional[str] = None):
    """Return all completed folder records, optionally filtered by root_path."""
    from donedatahoarder.db.models import CompletedFolder as CF
    engine = get_engine()
    with Session(engine) as db:
        q = db.query(CF)
        if root_path:
            q = q.filter(CF.root_path == root_path)
        rows = q.order_by(CF.completed_at.desc()).all()
        return {"items": [
            {"id": r.id, "folder_path": r.folder_path, "session_id": r.session_id,
             "completed_at": r.completed_at.isoformat(), "root_path": r.root_path}
            for r in rows
        ]}


# ---------------------------------------------------------------------------
# Database config
# ---------------------------------------------------------------------------

@router.get("/db-info")
def get_db_info():
    """Return current DB path."""
    import json
    config_file = Path.home() / ".datahoarder.json"
    db_path = ""
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            db_path = cfg.get("db_path", "")
        except Exception:
            pass
    if not db_path:
        import os
        db_path = os.environ.get("DDH_DB", "donedatahoarder.db")
    return {"db_path": db_path}


class DbInfoRequest(BaseModel):
    db_path: str


@router.post("/db-info")
def save_db_info(body: DbInfoRequest):
    """Save DB path to ~/.datahoarder.json. Requires server restart to take effect."""
    import json
    config_file = Path.home() / ".datahoarder.json"
    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["db_path"] = body.db_path
    config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return {"status": "saved", "db_path": body.db_path}


# ---------------------------------------------------------------------------
# Ollama management (status, models, pull)
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Recommended models for DoneDataHoarder
# Latest versions first: Gemma 4 is now available on Ollama!
RECOMMENDED_MODELS = [
    # Gemma 4 (Latest from Google - NOW AVAILABLE on Ollama)
    {"name": "gemma4:31b",  "desc": "Gemma 4 31B - Highest quality, dense architecture, multimodal, 256K context", "size": "20 GB", "vision": True, "latest": True},
    {"name": "gemma4:26b",  "desc": "Gemma 4 26B - Mixture of Experts, balanced quality/speed, multimodal, 256K context", "size": "18 GB", "vision": True, "latest": True},
    {"name": "gemma4:e4b",  "desc": "Gemma 4 E4B - Edge variant, multimodal+audio, 128K context", "size": "9.6 GB", "vision": True, "latest": True},
    {"name": "gemma4:e2b",  "desc": "Gemma 4 E2B - Lightweight edge variant, multimodal+audio, 128K context", "size": "7.2 GB", "vision": True, "latest": True},
    # Gemma 2 (stable, proven quality)
    {"name": "gemma2:27b",  "desc": "Gemma 2 27B - High quality, multimodal, needs 20GB+ RAM", "size": "16 GB", "vision": True},
    {"name": "gemma2:9b",   "desc": "Gemma 2 9B - Best balance of quality and speed", "size": "5.5 GB", "vision": True},
    # Gemma 3 (solid performers)
    {"name": "gemma3:12b",  "desc": "Gemma 3 12B - Good quality, multimodal", "size": "8.1 GB", "vision": True},
    {"name": "gemma3:4b",   "desc": "Gemma 3 4B - Fast and lightweight, multimodal", "size": "3.3 GB", "vision": True},
    # Vision specialists
    {"name": "llava:13b",   "desc": "LLaVA 13B - Specialized vision model", "size": "8.0 GB", "vision": True},
    {"name": "llava:7b",    "desc": "LLaVA 7B - Lightweight vision model", "size": "4.7 GB", "vision": True},
    # Lightweight text-only
    {"name": "llama3.2:3b", "desc": "Llama 3.2 3B - Fast text-only, only 2GB", "size": "2.0 GB", "vision": False},
]


@router.get("/ollama/status")
def ollama_status():
    """Check if Ollama is installed and running."""
    # Check if ollama binary exists
    ollama_path = shutil.which("ollama")
    installed = ollama_path is not None

    # Check if server is reachable
    running = False
    version = None
    if installed:
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=3)
            if resp.status_code == 200:
                running = True
                version = resp.json().get("version")
        except Exception:
            pass

    return {
        "installed": installed,
        "running": running,
        "version": version,
        "ollama_path": ollama_path,
        "host": OLLAMA_HOST,
        "download_url": "https://ollama.com/download",
    }


@router.get("/ollama/models")
def list_ollama_models():
    """List locally installed Ollama models."""
    try:
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except Exception:
        return {"models": [], "error": "Cannot reach Ollama server"}

    items = []
    for m in models:
        name = m.get("name", "")
        size = m.get("size", 0)
        # Check if this is a vision-capable model
        is_vision = any(v in name.lower() for v in ("llava", "gemma3", "gemma4", "bakllava", "moondream"))
        items.append({
            "name": name,
            "size_bytes": size,
            "modified_at": m.get("modified_at"),
            "vision": is_vision,
            "family": m.get("details", {}).get("family", ""),
            "parameters": m.get("details", {}).get("parameter_size", ""),
        })

    return {"models": items, "recommended": RECOMMENDED_MODELS}


class PullModelRequest(BaseModel):
    model: str


@router.post("/ollama/pull")
def pull_ollama_model(body: PullModelRequest):
    """
    Pull (download) an Ollama model with streaming progress updates.
    Returns Server-Sent Events (SSE) with progress information.
    """
    model = body.model.strip()
    if not model:
        raise HTTPException(400, "Model name required")

    def pull_stream():
        try:
            # Use separate connect/read timeouts: 30s to connect, 1 hour for reads
            # (large models can take a long time to download)
            timeout = httpx.Timeout(connect=30.0, read=3600.0, write=30.0, pool=30.0)
            with httpx.stream(
                "POST",
                f"{OLLAMA_HOST}/api/pull",
                json={"name": model, "stream": True},
                timeout=timeout,
            ) as resp:
                if resp.status_code != 200:
                    yield f"data: {json.dumps({'status': 'error', 'message': f'Ollama API returned {resp.status_code}'})}\n\n"
                    return

                # Track the largest layer to compute overall progress
                largest_total = 0
                largest_completed = 0
                pull_succeeded = False

                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            status = data.get("status", "")
                            total = data.get("total", 0)
                            completed = data.get("completed", 0)

                            # Track progress of the largest layer (the actual model weights)
                            if total > largest_total:
                                largest_total = total
                                largest_completed = completed
                            elif total == largest_total and total > 0:
                                largest_completed = completed

                            # Calculate progress based on largest layer
                            if largest_total > 0:
                                progress = min(99, int((largest_completed / largest_total * 100)))
                            else:
                                progress = 0

                            # Detect successful completion from Ollama
                            if status == "success":
                                pull_succeeded = True

                            yield f"data: {json.dumps({'status': status, 'progress': progress, 'completed': largest_completed, 'total': largest_total})}\n\n"
                        except json.JSONDecodeError:
                            pass

                # Only send success if Ollama actually reported success
                if pull_succeeded:
                    yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
                else:
                    # Verify by checking if model exists
                    try:
                        check = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
                        models = [m.get("name", "") for m in check.json().get("models", [])]
                        if any(model in m or m in model for m in models):
                            yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
                        else:
                            yield f"data: {json.dumps({'status': 'error', 'message': 'Download stream ended but model not found in Ollama.'})}\n\n"
                    except Exception:
                        yield f"data: {json.dumps({'status': 'success', 'progress': 100})}\n\n"
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'status': 'error', 'message': 'Pull timed out. Model may still be downloading. Check Ollama status with: ollama list'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'message': f'Pull failed: {str(exc)}'})}\n\n"

    return StreamingResponse(pull_stream(), media_type="text/event-stream")


@router.post("/ollama/delete")
def delete_ollama_model(body: PullModelRequest):
    """Delete an Ollama model via POST request."""
    model = body.model.strip()
    if not model:
        raise HTTPException(400, "Model name required")
    try:
        resp = httpx.request(
            "DELETE",
            f"{OLLAMA_HOST}/api/delete",
            json={"name": model},
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "deleted", "model": model}
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}")


class StartOllamaRequest(BaseModel):
    num_parallel: Optional[int] = None


def _start_ollama_process(ollama_path: str, num_parallel: int | None = None):
    """Start the Ollama server process with optional OLLAMA_NUM_PARALLEL."""
    env = os.environ.copy()
    if num_parallel and num_parallel > 1:
        env["OLLAMA_NUM_PARALLEL"] = str(num_parallel)

    if platform.system() == "Windows":
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(
            [ollama_path, "serve"],
            env=env,
            creationflags=create_no_window | detached_process,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [ollama_path, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


@router.post("/ollama/start")
def start_ollama(body: StartOllamaRequest = StartOllamaRequest()):
    """Attempt to start the Ollama server."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise HTTPException(404, "Ollama not found. Install from https://ollama.com/download")

    try:
        _start_ollama_process(ollama_path, body.num_parallel)

        # Give it a moment to start
        import time
        time.sleep(2)

        # Verify it started
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=3)
            if resp.status_code == 200:
                return {"status": "started", "version": resp.json().get("version")}
        except Exception:
            pass

        return {"status": "starting", "message": "Ollama is starting up..."}
    except Exception as exc:
        raise HTTPException(500, f"Failed to start Ollama: {exc}")


@router.post("/ollama/restart")
def restart_ollama(body: StartOllamaRequest = StartOllamaRequest()):
    """Stop and restart Ollama with updated settings (e.g. OLLAMA_NUM_PARALLEL)."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise HTTPException(404, "Ollama not found. Install from https://ollama.com/download")

    import time

    # Stop the running Ollama process
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/f", "/im", "ollama.exe"],
                           capture_output=True, timeout=10)
            # Also kill the runner process
            subprocess.run(["taskkill", "/f", "/im", "ollama_llama_server.exe"],
                           capture_output=True, timeout=10)
        else:
            subprocess.run(["pkill", "-f", "ollama serve"],
                           capture_output=True, timeout=10)
    except Exception:
        pass  # Process may not be running

    time.sleep(1)

    # Start with new settings
    try:
        _start_ollama_process(ollama_path, body.num_parallel)
        time.sleep(3)

        # Verify it started
        try:
            resp = httpx.get(f"{OLLAMA_HOST}/api/version", timeout=5)
            if resp.status_code == 200:
                return {
                    "status": "restarted",
                    "version": resp.json().get("version"),
                    "num_parallel": body.num_parallel,
                }
        except Exception:
            pass

        return {"status": "restarting", "message": "Ollama is restarting..."}
    except Exception as exc:
        raise HTTPException(500, f"Failed to restart Ollama: {exc}")


# ---------------------------------------------------------------------------
# Results management (save/load result snapshots)
# ---------------------------------------------------------------------------

@router.post("/results/save/{result_type}")
def save_results(result_type: str, name: Optional[str] = Query(None)):
    """Save current results to a file for later review."""
    from donedatahoarder.web.results_manager import save_results as manager_save

    if result_type not in ["files", "proposals", "duplicates"]:
        raise HTTPException(400, f"Invalid result type: {result_type}")

    # Get current data based on type
    if result_type == "files":
        response = list_files(page=1, per_page=10000)
        data = {"items": response["items"], "total": response["total"]}
    elif result_type == "proposals":
        response = list_proposals(page=1, per_page=10000, status=None)
        data = {"items": response["items"], "total": response["total"]}
    else:  # duplicates
        response = list_duplicates(page=1, per_page=10000)
        data = {"items": response["items"], "total": response["total"]}

    filename = manager_save(result_type, data, name)
    return {"success": True, "filename": filename, "message": f"Saved {data['total']} items"}


@router.get("/results/list")
def list_results():
    """List all saved result files."""
    from donedatahoarder.web.results_manager import list_saved_results

    return list_saved_results()


@router.get("/results/load/{filename}")
def load_results(filename: str):
    """Load a previously saved result file."""
    from donedatahoarder.web.results_manager import load_results as manager_load

    result = manager_load(filename)
    if not result:
        raise HTTPException(404, "Result file not found")

    return result


@router.delete("/results/{filename}")
def delete_results(filename: str):
    """Delete a saved result file."""
    from donedatahoarder.web.results_manager import delete_results as manager_delete

    if manager_delete(filename):
        return {"success": True, "message": "Result deleted"}
    else:
        raise HTTPException(404, "Result file not found")

"""
Info + dashboard stats endpoints.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from sqlalchemy import func
from sqlalchemy.orm import Session

from donedatahoarder.db.models import DuplicateGroup, File, Proposal
from donedatahoarder.db.session import get_engine

from .schemas import StatsResponse

router = APIRouter()


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

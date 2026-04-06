"""
REST API endpoints for the DataHoarder web UI.
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from datahoarder.db.models import (
    DuplicateGroup,
    DuplicateMember,
    DupeType,
    File,
    FileStatus,
    Proposal,
    ProposalStatus,
    ProposalType,
    ScanSession,
)
from datahoarder.db.session import get_engine

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


# ---------------------------------------------------------------------------
# Dashboard / Stats
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=StatsResponse)
def get_stats():
    engine = get_engine()
    with Session(engine) as session:
        total_files = session.query(func.count(File.id)).scalar() or 0
        total_size = session.query(func.sum(File.size_bytes)).scalar() or 0

        # By status
        status_rows = (
            session.query(File.status, func.count(File.id))
            .group_by(File.status)
            .all()
        )
        by_status = {s.value: c for s, c in status_rows}

        # Top extensions
        ext_rows = (
            session.query(File.extension, func.count(File.id))
            .filter(File.extension.isnot(None))
            .group_by(File.extension)
            .order_by(func.count(File.id).desc())
            .limit(15)
            .all()
        )
        by_extension = [{"ext": e, "count": c} for e, c in ext_rows]

        # By MIME category
        mime_rows = (
            session.query(
                func.substr(File.mime_type, 1, func.instr(File.mime_type, "/") - 1),
                func.count(File.id),
            )
            .filter(File.mime_type.isnot(None))
            .group_by(func.substr(File.mime_type, 1, func.instr(File.mime_type, "/") - 1))
            .order_by(func.count(File.id).desc())
            .all()
        )
        by_mime = [{"category": m or "unknown", "count": c} for m, c in mime_rows]

        # Proposals
        prop_rows = (
            session.query(Proposal.status, func.count(Proposal.id))
            .group_by(Proposal.status)
            .all()
        )
        proposal_counts = {s.value: c for s, c in prop_rows}

        # Duplicates
        dupe_count = session.query(func.count(DuplicateGroup.id)).scalar() or 0

        # Wasted bytes in duplicate groups
        dupe_wasted = 0
        groups = session.query(DuplicateGroup).all()
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
):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(File)

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
):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(Proposal).join(File)

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
def list_duplicates(page: int = 1, per_page: int = 20):
    engine = get_engine()
    with Session(engine) as session:
        total = session.query(func.count(DuplicateGroup.id)).scalar() or 0
        groups = (
            session.query(DuplicateGroup)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

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
def execute_proposals(dry_run: bool = True, min_confidence: float = 0.7):
    from datahoarder.executor import execute as do_execute

    counts = do_execute(dry_run=dry_run, min_confidence=min_confidence)
    return counts


# ---------------------------------------------------------------------------
# Pipeline triggers (run steps on demand)
# ---------------------------------------------------------------------------

@router.post("/pipeline/scan")
def trigger_scan(body: PipelineRequest):
    from datahoarder.core.scanner import scan as do_scan

    root = Path(body.root_path)
    if not root.exists():
        raise HTTPException(400, f"Path does not exist: {root}")
    counts = do_scan(root)
    return counts


@router.post("/pipeline/enrich")
def trigger_enrich():
    from datahoarder.core.enricher import enrich as do_enrich

    counts = do_enrich()
    return counts


@router.post("/pipeline/dedup")
def trigger_dedup():
    from datahoarder.core.dedup import find_exact_duplicates, find_perceptual_duplicates

    exact = find_exact_duplicates()
    perc = find_perceptual_duplicates()
    return {"exact": exact, "perceptual": perc}


@router.post("/pipeline/analyze")
def trigger_analyze(body: PipelineRequest):
    from datahoarder.ai.router import init_ai
    from datahoarder.analyzers.pipeline import analyze as do_analyze

    try:
        init_ai(
            backend=body.backend,
            text_model=body.model,
            vision_model=body.model,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))

    counts = do_analyze(workers=body.workers)
    return counts


@router.post("/pipeline/propose")
def trigger_propose():
    from datahoarder.proposals.namer import generate_proposals

    counts = generate_proposals()
    return counts

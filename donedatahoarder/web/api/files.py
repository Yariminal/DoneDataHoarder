"""
File listing / detail / thumbnail endpoints.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, FileStatus
from donedatahoarder.db.session import get_engine

router = APIRouter()


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
                "analysis_outcome": getattr(f, "analysis_outcome", None),
                "analysis_reason": getattr(f, "analysis_reason", None),
                "analysis_evidence_source": getattr(f, "analysis_evidence_source", None),
                "analysis_model_tag": getattr(f, "analysis_model_tag", None),
                "error_message": f.error_message,
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
            "analysis_outcome": getattr(f, "analysis_outcome", None),
            "analysis_reason": getattr(f, "analysis_reason", None),
            "analysis_evidence_source": getattr(f, "analysis_evidence_source", None),
            "analysis_model_tag": getattr(f, "analysis_model_tag", None),
            "analysis_model_digest": getattr(f, "analysis_model_digest", None),
            "analysis_prompt_version": getattr(f, "analysis_prompt_version", None),
            "analysis_extractor_version": getattr(f, "analysis_extractor_version", None),
            "analysis_content_chars": getattr(f, "analysis_content_chars", None),
            "error_message": f.error_message,
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

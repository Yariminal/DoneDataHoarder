"""
Duplicate group endpoints.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.db.models import DuplicateGroup, DuplicateMember, File, UserSession
from donedatahoarder.db.session import get_engine

from .deps import _require_session_id, review_operation
from .duplicate_evidence import indexed_md5_match, stored_sha256_match
from .schemas import SetKeeperRequest

router = APIRouter()


@router.get("/duplicates")
def list_duplicates(page: int = 1, per_page: int = 20, session_id: str = Query(...)):
    _require_session_id(session_id)
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if user_session is None:
            raise HTTPException(404, "Session not found")
        root = Path(user_session.root_path) if user_session.root_path else None
        protection = ProtectionIndex(root) if root and root.is_dir() else None
        q = session.query(DuplicateGroup).filter(DuplicateGroup.session_id == session_id)
        total = q.count()
        groups = q.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        for g in groups:
            files = []
            wasted = 0
            keeper = session.get(File, g.keep_file_id) if g.keep_file_id else None
            for m in g.members:
                f = session.get(File, m.file_id)
                if f:
                    is_keeper = f.id == g.keep_file_id
                    if not is_keeper:
                        wasted += f.size_bytes or 0
                    decision = protection.assess(Path(f.path)) if protection else None
                    files.append({
                        "id": f.id,
                        "path": f.path,
                        "filename": f.filename,
                        "size_bytes": f.size_bytes,
                        "date_best": f.date_best.isoformat() if f.date_best else None,
                        "is_keeper": is_keeper,
                        "mime_type": f.mime_type,
                        "ai_description": f.ai_description,
                        "analysis_outcome": getattr(f, "analysis_outcome", None),
                        "analysis_evidence_source": getattr(f, "analysis_evidence_source", None),
                        "similarity_score": m.similarity_score,
                        "distance_to_keeper": getattr(m, "distance_to_keeper", None),
                        "perceptual_bits": len(f.hash_perceptual) * 4 if f.hash_perceptual else None,
                        "exact_bytes_to_keeper": stored_sha256_match(f, keeper),
                        "matching_indexed_md5": indexed_md5_match(f, keeper),
                        "protected": bool(decision and decision.protected),
                        "protection_reason": decision.reason if decision else None,
                    })
            items.append({
                "id": g.id,
                "dupe_type": g.dupe_type.value,
                "count": len(files),
                "keep_file_id": g.keep_file_id,
                "wasted_bytes": wasted,
                "files": files,
                "evidence_label": {
                    "exact": "Grouped by indexed MD5; live SHA-256 checked before trash",
                    "perceptual": "Visual similarity; inspect both images",
                    "semantic": "Description similarity; inspect both files",
                    "content": "Text similarity; inspect both files",
                }.get(g.dupe_type.value, "Unverified similarity"),
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/duplicates/{group_id}/keeper")
def set_keeper(group_id: int, body: SetKeeperRequest):
    from donedatahoarder.core.dedup import refresh_group_proposals

    with review_operation("change duplicate keeper"):
        engine = get_engine()
        with Session(engine) as session:
            g = session.get(DuplicateGroup, group_id)
            if not g or g.session_id != body.session_id:
                raise HTTPException(404, "Group not found in this session")
            keeper = (
                session.query(File)
                .join(DuplicateMember, DuplicateMember.file_id == File.id)
                .filter(
                    DuplicateMember.group_id == group_id,
                    File.id == body.keep_file_id,
                    File.session_id == body.session_id,
                )
                .first()
            )
            if keeper is None:
                raise HTTPException(400, "Keeper must be a file in this duplicate group")
            if g.keep_file_id == body.keep_file_id:
                return {"status": "unchanged", "keep_file_id": body.keep_file_id,
                        "review_reset": 0}
            g.keep_file_id = body.keep_file_id
            try:
                refreshed = refresh_group_proposals(session, group_id)
            except ValueError as exc:
                session.rollback()
                raise HTTPException(409, str(exc)) from exc
            session.commit()
    return {"status": "ok", "keep_file_id": body.keep_file_id,
            "review_reset": refreshed["changed"], "created": refreshed["created"]}

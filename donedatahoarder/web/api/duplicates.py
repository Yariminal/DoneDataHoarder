"""
Duplicate group endpoints.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from donedatahoarder.core.dependency_protection import cached_protection_index
from donedatahoarder.core.dedup import closest_perceptual_peer, sequence_comparison_metadata
from donedatahoarder.core.photo_quality import compare_photos, photo_evidence
from donedatahoarder.core.review import ReviewError, change_keeper
from donedatahoarder.db.models import DuplicateGroup, DuplicateMember, File, UserSession
from donedatahoarder.db.session import get_engine

from .deps import _require_session_id, review_operation
from .duplicate_evidence import indexed_md5_match, stored_sha256_match
from .schemas import SetKeeperRequest

router = APIRouter()


@router.get("/duplicates")
def list_duplicates(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100),
                    session_id: str = Query(...)):
    _require_session_id(session_id)
    engine = get_engine()
    with Session(engine) as session:
        user_session = session.get(UserSession, session_id)
        if user_session is None:
            raise HTTPException(404, "Session not found")
        root = Path(user_session.root_path) if user_session.root_path else None
        protection = cached_protection_index(root) if root and root.is_dir() else None
        q = session.query(DuplicateGroup).filter(DuplicateGroup.session_id == session_id)
        total = q.count()
        groups = q.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        for g in groups:
            files = []
            member_query = (session.query(DuplicateMember)
                            .join(File, File.id == DuplicateMember.file_id)
                            .filter(DuplicateMember.group_id == g.id, File.session_id == session_id))
            member_count = member_query.count()
            visible_members = member_query.order_by(DuplicateMember.file_id).limit(100).all()
            if g.keep_file_id and all(m.file_id != g.keep_file_id for m in visible_members):
                keeper_member = member_query.filter(DuplicateMember.file_id == g.keep_file_id).first()
                if keeper_member:
                    visible_members.append(keeper_member)
            peer_rows = []
            if g.dupe_type.value == "perceptual":
                peer_rows = (session.query(File.id, File.path, File.hash_perceptual)
                             .join(DuplicateMember, DuplicateMember.file_id == File.id)
                             .filter(DuplicateMember.group_id == g.id, File.session_id == session_id)
                             .order_by(File.id).limit(256).all())
            keeper = session.get(File, g.keep_file_id) if g.keep_file_id else None
            if keeper and keeper.session_id != session_id:
                keeper = None
            for m in visible_members:
                f = session.get(File, m.file_id)
                if f and f.session_id == session_id:
                    is_keeper = f.id == g.keep_file_id
                    decision = protection.assess(Path(f.path)) if protection else None
                    files.append({
                        "id": f.id,
                        "path": f.path,
                        "filename": f.filename,
                        "size_bytes": f.size_bytes,
                        "date_best": f.date_best.isoformat() if f.date_best else None,
                        "is_keeper": is_keeper,
                        "mime_type": f.mime_type,
                        "photo_metadata": photo_evidence(f),
                        "photo_quality": compare_photos(f, keeper) if keeper else None,
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
                        "sequence_comparison": sequence_comparison_metadata(f, keeper)
                        if not is_keeper else None,
                        "closest_observed_peer": closest_perceptual_peer(
                            f.id, f.hash_perceptual, peer_rows,
                            total_members=member_count,
                        ) if peer_rows and not is_keeper else None,
                    })
            wasted = 0
            if g.dupe_type.value == "exact" and g.keep_file_id is not None:
                wasted = (session.query(func.coalesce(func.sum(File.size_bytes), 0))
                          .join(DuplicateMember, DuplicateMember.file_id == File.id)
                          .filter(DuplicateMember.group_id == g.id,
                                  File.session_id == session_id,
                                  File.id != g.keep_file_id).scalar() or 0)
            items.append({
                "id": g.id,
                "dupe_type": g.dupe_type.value,
                "count": member_count,
                "visible_count": len(files),
                "members_truncated": member_count > len(files),
                "keep_file_id": g.keep_file_id,
                "wasted_bytes": wasted,
                "files": files,
                "sequence_comparison_count": sum(
                    1 for entry in files if entry["sequence_comparison"] is not None
                ),
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
    with review_operation("change duplicate keeper"):
        engine = get_engine()
        with Session(engine) as session:
            try:
                result = change_keeper(session, body.session_id, group_id, body.keep_file_id,
                                       expected_keeper_id=body.expected_keeper_id)
            except ReviewError as exc:
                session.rollback()
                raise HTTPException(exc.status_code, str(exc)) from exc
            session.commit()
    return result

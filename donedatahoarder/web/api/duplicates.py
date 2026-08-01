"""
Duplicate group endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from donedatahoarder.db.models import DuplicateGroup, File
from donedatahoarder.db.session import get_engine

from .deps import _require_session_id
from .schemas import SetKeeperRequest

router = APIRouter()


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

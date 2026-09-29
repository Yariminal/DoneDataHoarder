"""
Results management endpoints (save/load result snapshots).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from donedatahoarder.db.models import UserSession
from donedatahoarder.db.session import get_engine

from .duplicates import list_duplicates
from .files import list_files
from .proposals import list_proposals

router = APIRouter()


def _saved_results_session(session_id: str) -> str:
    with Session(get_engine()) as db:
        if db.get(UserSession, session_id) is None:
            raise HTTPException(404, "Session not found")
    return session_id


def _owned_snapshot(result: dict, session_id: str, result_type: Optional[str] = None) -> dict:
    if not result.get("session_id"):
        raise HTTPException(409, "Legacy saved result has no session ownership and cannot be loaded")
    if result["session_id"] != session_id or (result_type and result.get("type") != result_type):
        raise HTTPException(404, "Saved result not found for this session and type")
    return result


@router.post("/results/save/{result_type}")
def save_results(result_type: str, session_id: str = Query(...), name: Optional[str] = Query(None)):
    """Save current results to a file for later review."""
    from donedatahoarder.web.results_manager import save_results as manager_save

    if result_type not in ["files", "proposals", "duplicates"]:
        raise HTTPException(400, f"Invalid result type: {result_type}")
    sid = _saved_results_session(session_id)

    # Get current data based on type
    if result_type == "files":
        response = list_files(page=1, per_page=10000, session_id=sid)
        data = {"items": response["items"], "total": response["total"]}
    elif result_type == "proposals":
        response = list_proposals(page=1, per_page=10000, status="pending", session_id=sid)
        data = {"items": response["items"], "total": response["total"]}
    else:  # duplicates
        response = list_duplicates(page=1, per_page=100, session_id=sid)
        data = {"items": response["items"], "total": response["total"]}

    filename = manager_save(result_type, data, name, session_id=sid)
    return {"success": True, "filename": filename,
            "message": f"Saved {len(data['items'])} of {data['total']} items"}


@router.get("/results/list")
def list_results(session_id: str = Query(...), result_type: str = Query(...)):
    """List all saved result files."""
    from donedatahoarder.web.results_manager import list_saved_results

    sid = _saved_results_session(session_id)
    if result_type not in ("files", "proposals", "duplicates"):
        raise HTTPException(400, "Invalid result type")
    return [item for item in list_saved_results()
            if item.get("session_id") == sid and item.get("type") == result_type]


@router.get("/results/load/{filename}")
def load_results(filename: str, session_id: str = Query(...), result_type: str = Query(...)):
    """Load a previously saved result file."""
    from donedatahoarder.web.results_manager import load_results as manager_load

    sid = _saved_results_session(session_id)
    result = manager_load(filename)
    if not result:
        raise HTTPException(404, "Result file not found")

    return _owned_snapshot(result, sid, result_type)


@router.delete("/results/{filename}")
def delete_results(filename: str, session_id: str = Query(...)):
    """Delete a saved result file."""
    from donedatahoarder.web.results_manager import delete_results as manager_delete
    from donedatahoarder.web.results_manager import load_results as manager_load

    sid = _saved_results_session(session_id)
    result = manager_load(filename)
    if not result:
        raise HTTPException(404, "Result file not found")
    _owned_snapshot(result, sid)

    if manager_delete(filename):
        return {"success": True, "message": "Result deleted"}
    else:
        raise HTTPException(404, "Result file not found")

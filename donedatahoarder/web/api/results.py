"""
Results management endpoints (save/load result snapshots).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .duplicates import list_duplicates
from .files import list_files
from .proposals import list_proposals

router = APIRouter()


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

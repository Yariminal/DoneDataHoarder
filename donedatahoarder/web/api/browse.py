"""
Filesystem browsing + database config endpoints.
"""
from __future__ import annotations

import os
import platform
import shutil
import string
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from donedatahoarder.db.session import get_engine

router = APIRouter()


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

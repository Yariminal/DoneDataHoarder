"""
Filesystem scanner — walks a directory tree and populates the file index.

Designed to be resumable: already-indexed files are skipped by default.
All writes are batched (BATCH_SIZE) to keep SQLite happy on large drives.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.db.models import File, FileStatus, ScanSession
from datahoarder.db.session import get_engine

BATCH_SIZE = 500

# Directories we never want to descend into
SKIP_DIRS: set[str] = {
    "System Volume Information",
    "$RECYCLE.BIN",
    "RECYCLER",
    ".git",
    ".svn",
    "__pycache__",
    "node_modules",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    "lost+found",
}

# File extensions that carry no useful content
SKIP_EXTENSIONS: set[str] = {
    ".lnk", ".url", ".tmp", ".part",
    ".sys", ".dll", ".exe", ".com",
    ".ini", ".dat", ".log",
    ".db", ".db-shm", ".db-wal",
}


def walk_files(root: Path, extra_skip_dirs: set[str] | None = None) -> Iterator[Path]:
    """Yield Path objects for every regular file under *root*."""
    skip = SKIP_DIRS | (extra_skip_dirs or set())

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune unwanted dirs in-place so os.walk won't descend into them
        dirnames[:] = [
            d for d in dirnames
            if d not in skip and not d.startswith(".")
        ]
        for name in filenames:
            if Path(name).suffix.lower() not in SKIP_EXTENSIONS:
                yield Path(dirpath) / name


def scan(
    root: Path,
    force_rescan: bool = False,
    extra_skip_dirs: set[str] | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Walk *root* and upsert File records into the database.

    Returns a summary dict with counts for new / skipped / error files.
    """
    engine = get_engine()

    with Session(engine) as session:
        sess_kwargs = dict(
            root_path=str(root.resolve()),
            started_at=datetime.utcnow(),
        )
        if session_id:
            sess_kwargs["session_id"] = session_id
        sess_record = ScanSession(**sess_kwargs)
        session.add(sess_record)
        session.commit()
        scan_session_id = sess_record.id

    counts = {"new": 0, "skipped": 0, "errors": 0}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        refresh_per_second=4,
    ) as progress:
        task = progress.add_task("Scanning…", total=None)
        batch: list[dict] = []

        def _flush(session: Session) -> None:
            if not batch:
                return
            for record in batch:
                path_str = record["path"]
                existing = session.query(File).filter_by(path=path_str).first()
                if existing and not force_rescan:
                    counts["skipped"] += 1
                elif existing:
                    # Update basic stat fields, reset status
                    for k, v in record.items():
                        setattr(existing, k, v)
                    existing.status = FileStatus.PENDING
                    counts["new"] += 1
                else:
                    session.add(File(**record))
                    counts["new"] += 1
            session.commit()
            batch.clear()

        with Session(engine) as session:
            for file_path in walk_files(root, extra_skip_dirs):
                progress.advance(task)
                path_str = str(file_path.resolve())

                if not force_rescan:
                    # Fast pre-check without loading the full object
                    exists = session.query(File.id).filter_by(path=path_str).scalar()
                    if exists is not None:
                        counts["skipped"] += 1
                        continue

                try:
                    stat = file_path.stat()
                    record = dict(
                        path=path_str,
                        filename=file_path.name,
                        extension=file_path.suffix.lower() or None,
                        size_bytes=stat.st_size,
                        date_modified=datetime.fromtimestamp(stat.st_mtime),
                        date_created=datetime.fromtimestamp(stat.st_ctime),
                        status=FileStatus.PENDING,
                        scanned_at=datetime.utcnow(),
                    )
                    if session_id:
                        record["session_id"] = session_id
                    batch.append(record)
                except (PermissionError, OSError) as exc:
                    counts["errors"] += 1
                    continue

                if len(batch) >= BATCH_SIZE:
                    _flush(session)
                    progress.update(
                        task,
                        description=f"Scanning… {counts['new']} new, {counts['skipped']} skipped",
                    )

            _flush(session)

            # Mark session complete
            sess_record = session.get(ScanSession, scan_session_id)
            if sess_record:
                sess_record.finished_at = datetime.utcnow()
                sess_record.files_new = counts["new"]
                sess_record.files_skipped = counts["skipped"]
                sess_record.files_error = counts["errors"]
                sess_record.files_found = counts["new"] + counts["skipped"]
                sess_record.completed = True
                session.commit()

    return counts

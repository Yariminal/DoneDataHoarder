"""
Filesystem scanner — walks a directory tree and populates the file index.

Designed to be resumable: already-indexed files are skipped by default.
All writes are batched (BATCH_SIZE) to keep SQLite happy on large drives.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.db.models import File, FileStatus, ScanSession
from datahoarder.db.session import get_engine
from datahoarder.logging import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 500

# Optional media-metadata imports for Linux birthtime fallback
try:
    import exifread
    _HAS_EXIFREAD = True
except ImportError:
    _HAS_EXIFREAD = False

try:
    import mutagen
    _HAS_MUTAGEN = True
except ImportError:
    _HAS_MUTAGEN = False

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


# ---------------------------------------------------------------------------
# Cross-platform date_created extraction
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS_FOR_EXIF = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".webp", ".heic", ".heif",
}
_AUDIO_VIDEO_EXTENSIONS_FOR_MUTAGEN = {
    ".mp3", ".m4a", ".flac", ".wav", ".ogg",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv",
    ".m4v", ".3gp",
}


def _exif_date_created(path: Path) -> Optional[datetime]:
    """Best-effort EXIF DateTimeOriginal extraction for birthtime fallback."""
    if not _HAS_EXIFREAD:
        return None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        for tag_key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            tag = tags.get(tag_key)
            if tag:
                raw = str(tag).strip()
                try:
                    return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
    except Exception:
        pass
    return None


def _mutagen_date_created(path: Path) -> Optional[datetime]:
    """Best-effort audio/video metadata date extraction for birthtime fallback."""
    if not _HAS_MUTAGEN:
        return None
    try:
        f = mutagen.File(str(path), easy=True)
        if not f:
            return None
        for key in ("date", "year", "tdrc"):
            val = f.get(key)
            if val:
                raw = str(val[0]).strip()
                for fmt in ("%Y-%m-%d", "%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        return datetime.strptime(raw[: len(fmt)], fmt)
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def _get_file_dates(
    file_path: Path, stat_result: os.stat_result
) -> tuple[datetime, datetime, Optional[str], Optional[str]]:
    """
    Return (date_modified, date_created, date_created_source, warning).

    Priority:
      1. macOS / BSD  -> st_birthtime
      2. Windows       -> st_ctime (creation time)
      3. Linux         -> st_birthtime if exposed by Python/os
      4. Linux media   -> EXIF (images) / mutagen (audio/video)
      5. All others    -> st_mtime with warning
    """
    date_modified = datetime.fromtimestamp(stat_result.st_mtime)
    warning: Optional[str] = None

    # 1. macOS / BSD birthtime
    if hasattr(stat_result, "st_birthtime"):
        return (
            date_modified,
            datetime.fromtimestamp(stat_result.st_birthtime),
            "birthtime",
            None,
        )

    # 2. Windows creation time
    if sys.platform == "win32" or os.name == "nt":
        return (
            date_modified,
            datetime.fromtimestamp(stat_result.st_ctime),
            "ctime_windows",
            None,
        )

    # 3. Linux: some Python builds / filesystems expose st_birthtime
    try:
        birth = stat_result.st_birthtime
        return date_modified, datetime.fromtimestamp(birth), "birthtime", None
    except AttributeError:
        pass

    # 4. Linux media fallbacks
    ext = file_path.suffix.lower()
    if ext in _IMAGE_EXTENSIONS_FOR_EXIF:
        exif_dt = _exif_date_created(file_path)
        if exif_dt:
            return date_modified, exif_dt, "exif_fallback", None
    if ext in _AUDIO_VIDEO_EXTENSIONS_FOR_MUTAGEN:
        mutagen_dt = _mutagen_date_created(file_path)
        if mutagen_dt:
            return date_modified, mutagen_dt, "mutagen_fallback", None

    # 5. Final fallback to mtime with warning
    warning = (
        f"date_created fell back to mtime for {file_path.name} "
        f"(birthtime unavailable on {sys.platform})"
    )
    return date_modified, date_modified, "mtime_fallback", warning


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
    logger.info(
        "Scan started",
        extra={
            "root": str(root.resolve()),
            "force_rescan": force_rescan,
            "session_id": session_id,
        },
    )

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
            logger.info(
                "Flushing batch",
                extra={"batch_size": len(batch), "counts": counts.copy()},
            )
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
                    date_modified, date_created, date_created_source, date_warning = _get_file_dates(
                        file_path, stat
                    )
                    record = dict(
                        path=path_str,
                        filename=file_path.name,
                        extension=file_path.suffix.lower() or None,
                        size_bytes=stat.st_size,
                        date_modified=date_modified,
                        date_created=date_created,
                        date_created_source=date_created_source,
                        status=FileStatus.PENDING,
                        scanned_at=datetime.utcnow(),
                    )
                    if date_warning:
                        record["error_message"] = date_warning
                    if session_id:
                        record["session_id"] = session_id
                    batch.append(record)
                except (PermissionError, OSError) as exc:
                    counts["errors"] += 1
                    logger.warning(
                        "Scan error for file",
                        extra={"path": path_str, "error": str(exc)},
                    )
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

    logger.info(
        "Scan complete",
        extra={
            "new": counts["new"],
            "skipped": counts["skipped"],
            "errors": counts["errors"],
        },
    )
    return counts

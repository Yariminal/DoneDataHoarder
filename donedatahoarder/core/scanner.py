"""
Filesystem scanner — walks a directory tree and populates the file index.

Designed to be resumable: already-indexed files are skipped by default.
All writes are batched (BATCH_SIZE) to keep SQLite happy on large drives.

Respects .ddhignore files in the root directory (gitignore-style patterns).
"""
import os
import stat
import sys
from datetime import datetime
from donedatahoarder.timeutils import utcnow
from pathlib import Path
from typing import Callable, Iterator, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ScanSession,
)
from donedatahoarder.db.session import get_engine
from donedatahoarder.logging import get_logger
from donedatahoarder.core.ignore import load_ddhignore

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

# Transient files are outside the collection index. Archived SQLite sidecars
# may be required to recover user data; only this app's active DB is excluded.
SKIP_EXTENSIONS: set[str] = {
    ".tmp", ".part",
}

# Filenames that should always be skipped (macOS/Windows metadata, etc.)
SKIP_FILENAMES: set[str] = {
    ".DS_Store", "Thumbs.db", "desktop.ini", "._.DS_Store",
}

# Cleanup, when explicitly invoked, must never treat CAD plot resources as junk.
JUNK_FILE_EXTENSIONS: set[str] = {".tmp", ".part"}

# Filename prefixes that indicate system/metadata files (macOS AppleDouble)
SKIP_FILENAME_PREFIXES: tuple[str, ...] = ("._",)


def _is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse entries without following."""
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _active_database_files() -> set[Path]:
    """Keep this application's live SQLite files out of a selected root."""
    try:
        database = get_engine().url.database
    except RuntimeError:
        database = os.environ.get("DDH_DB")
    if not database:
        return set()
    path = Path(database).resolve()
    return {path, Path(f"{path}-wal"), Path(f"{path}-shm"),
            Path(f"{path}-journal")}


def walk_files(root: Path, extra_skip_dirs: set[str] | None = None) -> Iterator[Path]:
    """
    Yield Path objects for every regular file under *root*.

    Respects:
    - Built-in skip lists (SKIP_DIRS, SKIP_EXTENSIONS, SKIP_FILENAMES)
    - .ddhignore file in root (gitignore-style patterns)
    - extra_skip_dirs parameter
    """
    skip = SKIP_DIRS | (extra_skip_dirs or set())
    active_database_files = _active_database_files()
    if _is_link_or_reparse(root):
        return
    ddhignore = load_ddhignore(root)

    def _walk_error(exc: OSError) -> None:
        logger.warning("Cannot read directory during scan", extra={
            "path": getattr(exc, "filename", None), "error": str(exc),
        })

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=_walk_error,
    ):
        dirpath_obj = Path(dirpath)

        # Prune unwanted dirs in-place so os.walk won't descend into them
        dirnames_filtered = []
        for d in dirnames:
            if d in skip or d.startswith("."):
                continue
            # Check .ddhignore patterns
            dir_path = dirpath_obj / d
            if _is_link_or_reparse(dir_path):
                continue
            if ddhignore.should_ignore(dir_path, is_dir=True):
                continue
            dirnames_filtered.append(d)

        dirnames[:] = dirnames_filtered

        for name in filenames:
            # Skip by filename pattern (system metadata, macOS AppleDouble, etc.)
            if name in SKIP_FILENAMES or name.startswith(SKIP_FILENAME_PREFIXES):
                continue
            # Skip by extension
            if Path(name).suffix.lower() in SKIP_EXTENSIONS:
                continue
            # Check .ddhignore patterns
            file_path = dirpath_obj / name
            if _is_link_or_reparse(file_path):
                continue
            if file_path.resolve() in active_database_files:
                continue
            if ddhignore.should_ignore(file_path, is_dir=False):
                continue
            yield file_path


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


def _collect_file_stat(
    file_path: Path,
    force_rescan: bool,
    session_id: str | None,
) -> dict | None:
    """Best-effort stat collection for a single file. Returns a record dict or None on skip/error."""
    path_str = str(file_path.resolve())
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
            scanned_at=utcnow(),
        )
        if date_warning:
            record["error_message"] = date_warning
        if session_id:
            record["session_id"] = session_id
        return record
    except (PermissionError, OSError) as exc:
        logger.warning(
            "Scan error for file",
            extra={"path": path_str, "error": str(exc)},
        )
        return {"_error": True, "path": path_str}


def _scan_unlocked(
    root: Path,
    force_rescan: bool = False,
    extra_skip_dirs: set[str] | None = None,
    session_id: str | None = None,
    workers: int = 1,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """
    Walk *root* and upsert File records into the database.

    Args:
        workers: Number of parallel threads for filesystem stat collection.
                 DB writes remain single-threaded to avoid SQLite locks.

    Returns a summary dict with counts for new / skipped / error files.
    """
    import concurrent.futures

    if _is_link_or_reparse(root):
        raise ValueError("Collection root is a symlink, junction, or unreadable")
    engine = get_engine()

    with Session(engine) as session:
        sess_kwargs = dict(
            root_path=str(root.resolve()),
            started_at=utcnow(),
        )
        if session_id:
            sess_kwargs["session_id"] = session_id
        sess_record = ScanSession(**sess_kwargs)
        session.add(sess_record)
        session.commit()
        scan_session_id = sess_record.id

    counts = {"new": 0, "skipped": 0, "errors": 0}
    cancelled = False
    logger.info(
        "Scan started",
        extra={
            "root": str(root.resolve()),
            "force_rescan": force_rescan,
            "session_id": session_id,
            "workers": workers,
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
        last_path: str | None = None

        def _flush(session: Session) -> None:
            nonlocal last_path
            if not batch:
                return
            logger.info(
                "Flushing batch",
                extra={"batch_size": len(batch), "counts": counts.copy()},
            )
            paths = [record["path"] for record in batch if not record.get("_error")]
            rows = (
                session.query(File)
                .filter_by(session_id=session_id)
                .filter(File.path.in_(paths))
                .all()
            ) if paths else []
            existing_by_path = {row.path: row for row in rows}
            reset_ids: list[int] = []
            for record in batch:
                if record.get("_error"):
                    counts["errors"] += 1
                    continue
                path_str = record["path"]
                last_path = path_str
                existing = existing_by_path.get(path_str)
                if existing is not None and force_rescan:
                    # Update basic stat fields, reset status
                    reset_ids.append(existing.id)
                    for k, v in record.items():
                        if k.startswith("_"):
                            continue
                        setattr(existing, k, v)
                    existing.status = FileStatus.PENDING
                    existing.hash_md5 = None
                    existing.hash_sha256 = None
                    existing.hash_perceptual = None
                    existing.date_exif = None
                    existing.date_best = None
                    existing.ai_description = None
                    existing.ai_suggested_name = None
                    existing.ai_tags = None
                    existing.ai_transcript = None
                    existing.ai_confidence = None
                    existing.ai_model = None
                    existing.analysis_outcome = None
                    existing.analysis_reason = None
                    existing.analysis_evidence_source = None
                    existing.analysis_model_tag = None
                    existing.analysis_model_digest = None
                    existing.analysis_prompt_version = None
                    existing.analysis_extractor_version = None
                    existing.analysis_content_chars = None
                    existing.analysis_context_hash = None
                    existing.analysis_detected_date = None
                    existing.analysis_cache_hit = False
                    existing.analyzed_at = None
                    existing.enriched_at = None
                    existing.error_message = record.get("error_message")
                    counts["new"] += 1
                elif existing is not None:
                    counts["skipped"] += 1
                else:
                    session.add(File(**{k: v for k, v in record.items() if not k.startswith("_")}))
                    counts["new"] += 1
            if reset_ids:
                session.query(Proposal).filter(
                    Proposal.file_id.in_(reset_ids),
                    Proposal.status.in_((ProposalStatus.PENDING, ProposalStatus.APPROVED,
                                         ProposalStatus.MODIFIED)),
                ).update({"status": ProposalStatus.REJECTED}, synchronize_session=False)
            session.commit()
            # Persist resume point
            if last_path and scan_session_id:
                sess_rec = session.get(ScanSession, scan_session_id)
                if sess_rec:
                    sess_rec.last_scanned_path = last_path
                    session.commit()
            batch.clear()

        if workers > 1:
            # Only a small number of stat futures may be outstanding. Paths
            # are yielded by os.walk and never collected into a full list.
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                pending: dict[concurrent.futures.Future, Path] = {}
                paths = iter(walk_files(root, extra_skip_dirs))
                exhausted = False
                with Session(engine) as session:
                    while pending or not exhausted:
                        if cancel_check and cancel_check():
                            exhausted = True
                            cancelled = True
                        while not exhausted and len(pending) < max(2, workers * 2):
                            try:
                                file_path = next(paths)
                            except StopIteration:
                                exhausted = True
                                break
                            future = pool.submit(
                                _collect_file_stat, file_path, force_rescan, session_id
                            )
                            pending[future] = file_path
                        if not pending:
                            continue
                        done, _ = concurrent.futures.wait(
                            pending, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        for future in done:
                            file_path = pending.pop(future)
                            progress.advance(task)
                            try:
                                record = future.result()
                            except Exception as exc:
                                counts["errors"] += 1
                                logger.warning("Scan error for file", extra={
                                    "path": str(file_path), "error": str(exc),
                                })
                                continue
                            if record is None:
                                counts["errors"] += 1
                                continue
                            batch.append(record)
                            if len(batch) >= BATCH_SIZE:
                                _flush(session)
                                progress.update(task, description=(
                                    f"Scanning… {counts['new']} new, {counts['skipped']} skipped"
                                ))
                    _flush(session)
        else:
            # Sequential path (original behaviour)
            with Session(engine) as session:
                for file_path in walk_files(root, extra_skip_dirs):
                    if cancel_check and cancel_check():
                        cancelled = True
                        break
                    progress.advance(task)
                    record = _collect_file_stat(file_path, force_rescan, session_id)
                    if record is None or record.get("_error"):
                        counts["errors"] += 1
                        continue
                    batch.append(record)

                    if len(batch) >= BATCH_SIZE:
                        _flush(session)
                        progress.update(
                            task,
                            description=f"Scanning… {counts['new']} new, {counts['skipped']} skipped",
                        )

                _flush(session)

    # Mark session complete (separate session to avoid lock contention)
    with Session(engine) as session:
        sess_record = session.get(ScanSession, scan_session_id)
        if sess_record:
            sess_record.finished_at = utcnow()
            sess_record.files_new = counts["new"]
            sess_record.files_skipped = counts["skipped"]
            sess_record.files_error = counts["errors"]
            sess_record.files_found = counts["new"] + counts["skipped"]
            sess_record.completed = not cancelled
            sess_record.last_scanned_path = None
            session.commit()

    logger.info(
        "Scan complete",
        extra={
            "new": counts["new"],
            "skipped": counts["skipped"],
            "errors": counts["errors"],
        },
    )
    if cancelled:
        counts["cancelled"] = True
    return counts


def scan(
    root: Path,
    force_rescan: bool = False,
    extra_skip_dirs: set[str] | None = None,
    session_id: str | None = None,
    workers: int = 1,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("scan"):
        return _scan_unlocked(root, force_rescan, extra_skip_dirs, session_id,
                              workers, cancel_check)

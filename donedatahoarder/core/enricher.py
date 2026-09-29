"""
Enricher — extracts metadata, hashes, and dates from scanned files.

Processes all File records in PENDING status and upgrades them to ENRICHED.
Safe to re-run; already-enriched files are skipped.
"""
import hashlib
import mimetypes
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from donedatahoarder.timeutils import utcnow
from pathlib import Path
from typing import Callable, Iterator, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, FileStatus
from donedatahoarder.db.session import get_engine
from donedatahoarder.logging import get_logger

logger = get_logger(__name__)

# Optional heavy imports
try:
    import magic  # python-magic or python-magic-bin
    _HAS_MAGIC = True
except ImportError:
    _HAS_MAGIC = False

try:
    import exifread
    _HAS_EXIFREAD = True
except ImportError:
    _HAS_EXIFREAD = False

try:
    from PIL import Image as PilImage
    import imagehash
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from donedatahoarder.phash import compute_phash as _compute_phash

try:
    import mutagen
    _HAS_MUTAGEN = True
except ImportError:
    _HAS_MUTAGEN = False

CHUNK = 65_536  # 64 KB read chunks for hashing
BATCH_SIZE = 200

# libmagic keeps one global cookie. from_file on that cookie is not safe
# to re-enter, so mime detection takes this lock and the heavy reads
# (md5, exif, phash) stay outside it.
_MIME_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _md5(path: Path) -> Optional[str]:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _content_hashes(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Compute both index hashes in one bounded streaming read."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                md5.update(chunk)
                sha256.update(chunk)
        return md5.hexdigest(), sha256.hexdigest()
    except OSError:
        return None, None


def _mime_type(path: Path) -> str:
    """Best-effort MIME type detection."""
    if _HAS_MAGIC:
        try:
            with _MIME_LOCK:
                result = magic.from_file(str(path), mime=True)
            # Guard: libmagic on Windows sometimes returns error messages
            # instead of raising exceptions (especially for Unicode paths).
            # Valid MIME types look like "type/subtype", never start with
            # error keywords.
            if result and "/" in result and not result.startswith(("cannot ", "error", "failed")):
                return result
        except Exception:
            pass
    # Fallback: stdlib mimetypes (extension-based)
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


def _exif_date(path: Path) -> Optional[datetime]:
    """Extract the most reliable date from EXIF (photos/videos)."""
    if not _HAS_EXIFREAD:
        return None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        for tag_key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            tag = tags.get(tag_key)
            if tag:
                raw = str(tag).strip()
                # EXIF format: "YYYY:MM:DD HH:MM:SS"
                try:
                    return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
    except Exception:
        pass
    return None


def _audio_date(path: Path) -> Optional[datetime]:
    """Extract date from audio/video metadata via mutagen."""
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
                        return datetime.strptime(raw[:len(fmt)], fmt)
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def _perceptual_hash(path: Path) -> Optional[str]:
    """Compute perceptual hash for images and videos (delegates to phash module).

    Hash the file as stored. Resizing before the hash changes the digest.
    """
    return _compute_phash(path)


def _disk_metadata(path: Path) -> dict:
    """Read mime, md5, exif, and perceptual hash. No database access."""
    if not path.exists():
        return {"missing": True}

    mime = _mime_type(path) or ""
    digest, sha256 = _content_hashes(path)
    date_exif = None
    perceptual = None
    have_exif = False
    have_phash = False

    if mime.startswith("image/"):
        date_exif = _exif_date(path)
        perceptual = _perceptual_hash(path)
        have_exif = True
        have_phash = True
    elif mime.startswith(("video/", "audio/")):
        date_exif = _audio_date(path)
        have_exif = True
        if mime.startswith("video/"):
            perceptual = _perceptual_hash(path)
            have_phash = True

    return {
        "mime_type": mime,
        "hash_md5": digest,
        "hash_sha256": sha256,
        "date_exif": date_exif,
        "have_exif": have_exif,
        "hash_perceptual": perceptual,
        "have_phash": have_phash,
    }


def _safe_disk(path_str: str) -> dict:
    """Worker entry point. Exceptions stay off the database thread."""
    try:
        return _disk_metadata(Path(path_str))
    except Exception as exc:
        return {"error": str(exc)[:500]}


def _apply_disk_result(file_rec: File, result: dict) -> str:
    """Write one disk result onto a File row. Caller owns the session."""
    if result.get("missing"):
        file_rec.status = FileStatus.ERROR
        file_rec.error_message = "File not found on disk"
        return "errors"
    if "error" in result:
        file_rec.status = FileStatus.ERROR
        file_rec.error_message = result["error"]
        logger.warning(
            "Enrichment failed",
            extra={"path": str(file_rec.path), "error": result["error"]},
        )
        return "errors"

    file_rec.mime_type = result["mime_type"]
    file_rec.hash_md5 = result["hash_md5"]
    file_rec.hash_sha256 = result["hash_sha256"]
    if result["have_exif"]:
        file_rec.date_exif = result["date_exif"]
    if result["have_phash"]:
        file_rec.hash_perceptual = result["hash_perceptual"]

    exif = result["date_exif"] if result["have_exif"] else file_rec.date_exif
    file_rec.date_best = _best_date(
        exif,
        file_rec.date_modified,
        file_rec.date_created,
    )
    file_rec.status = FileStatus.ENRICHED
    file_rec.enriched_at = utcnow()
    return "enriched"


def _pending_snapshot(engine, session_id: str | None, take: int) -> list[dict]:
    """Copy id and path for a pending batch. The session closes before disk work."""
    with Session(engine) as session:
        query = session.query(File.id, File.path).filter(File.status == FileStatus.PENDING)
        if session_id:
            query = query.filter(File.session_id == session_id)
        rows = query.limit(take).all()
    return [{"id": row[0], "path": row[1]} for row in rows]


def _iter_disk(
    snapshots: list[dict],
    workers: int,
    pause_event: "threading.Event | None",
    cancel_check: "Callable[[], bool] | None",
) -> Iterator[tuple]:
    """Yield ('item', snap, result) as disk work finishes.

    At most `workers` reads run at once. Pause blocks before a new read
    starts. Cancel stops scheduling; reads already running still yield so
    the caller can write them.
    """
    worker_count = max(1, workers)
    cancelled = False

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        inflight: dict = {}
        pending = iter(snapshots)

        def pull() -> bool:
            nonlocal cancelled
            if cancel_check and cancel_check():
                cancelled = True
                return False
            if pause_event is not None:
                pause_event.wait()
            # Cancel during a pause must not start another file once we wake.
            if cancel_check and cancel_check():
                cancelled = True
                return False
            try:
                snap = next(pending)
            except StopIteration:
                return False
            inflight[pool.submit(_safe_disk, snap["path"])] = snap
            return True

        for _ in range(worker_count):
            if not pull():
                break

        while inflight:
            done, _ = wait(tuple(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                snap = inflight.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {"error": str(exc)[:500]}
                yield ("item", snap, result)
            if cancelled:
                continue
            while len(inflight) < worker_count:
                if not pull():
                    break

    if cancelled:
        yield ("cancelled", None, None)


def _write_disk_results(
    engine,
    snapshots: list[dict],
    workers: int,
    counts: dict,
    pause_event: "threading.Event | None" = None,
    cancel_check: "Callable[[], bool] | None" = None,
) -> Iterator[str]:
    """Apply disk results on this thread. Yields 'file' or 'cancelled'."""
    if not snapshots:
        return
    stream = _iter_disk(snapshots, workers, pause_event, cancel_check)
    try:
        with Session(engine) as session:
            for kind, snap, result in stream:
                if kind == "cancelled":
                    session.commit()
                    yield "cancelled"
                    return
                file_rec = session.get(File, snap["id"])
                if file_rec is None:
                    counts["errors"] += 1
                else:
                    counts[_apply_disk_result(file_rec, result)] += 1
                # Consumers persist durable job progress on another connection.
                # Never yield with pending writes: the next file's autoflush
                # would otherwise hold SQLite's writer lock across that yield.
                session.commit()
                yield "file"
    finally:
        stream.close()


def _best_date(
    exif: Optional[datetime],
    modified: Optional[datetime],
    created: Optional[datetime],
) -> Optional[datetime]:
    return exif or modified or created


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def _take(counts: dict, limit: Optional[int]) -> Optional[int]:
    """How many pending rows to pull this batch. None means stop."""
    take = BATCH_SIZE
    if limit:
        remaining = limit - (counts["enriched"] + counts["errors"])
        if remaining <= 0:
            return None
        take = min(BATCH_SIZE, remaining)
    return take


def _enrich_unlocked(workers: int = 1, limit: Optional[int] = None, session_id: str | None = None) -> dict:
    """
    Enrich all PENDING File records with metadata and hashes.

    Args:
        workers: parallel threads for disk work (mime, md5, exif, phash).
                 Database writes stay on the caller thread.
        limit:   process at most this many files (useful for testing)
        session_id: if set, only enrich files belonging to this session

    Returns:
        Summary dict with counts.
    """
    engine = get_engine()
    counts = {"enriched": 0, "errors": 0, "skipped": 0}

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.PENDING)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if limit:
            query = query.limit(limit)
        total = query.count()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold green]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        refresh_per_second=4,
    ) as progress:
        task = progress.add_task("Enriching…", total=total)

        while True:
            # Always query from offset 0: processed files change status
            # and no longer match the PENDING filter.
            take = _take(counts, limit)
            if take is None:
                break
            snapshots = _pending_snapshot(engine, session_id, take)
            if not snapshots:
                break

            for event in _write_disk_results(engine, snapshots, workers, counts):
                if event == "file":
                    progress.advance(task)

            if limit and (counts["enriched"] + counts["errors"]) >= limit:
                break

    logger.info(
        "Enrichment complete",
        extra={
            "enriched": counts["enriched"],
            "errors": counts["errors"],
            "workers": workers,
        },
    )
    return counts


def _enrich_with_progress_unlocked(
    workers: int = 1,
    limit: int | None = None,
    session_id: str | None = None,
    pause_event: "threading.Event | None" = None,
    cancel_check: "Callable[[], bool] | None" = None,
):
    """
    Like enrich() but yields progress dicts for SSE streaming.

    `workers` threads read mime, md5, exif, and perceptual hashes.
    Database writes stay on the caller thread. Pause and cancel are
    checked before a file's disk work is started.
    """
    engine = get_engine()
    counts = {"enriched": 0, "errors": 0, "skipped": 0}

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.PENDING)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if limit:
            query = query.limit(limit)
        total = query.count()

    if total == 0:
        yield {"current": 0, "total": 0, "enriched": 0, "errors": 0, "skipped": 0, "done": True}
        return

    current = 0
    while True:
        if cancel_check and cancel_check():
            yield {"cancelled": True, **counts}
            return

        # Always query from offset 0: processed files change status
        # and no longer match the PENDING filter.
        take = _take(counts, limit)
        if take is None:
            break
        snapshots = _pending_snapshot(engine, session_id, take)
        if not snapshots:
            break

        for event in _write_disk_results(
            engine,
            snapshots,
            workers,
            counts,
            pause_event=pause_event,
            cancel_check=cancel_check,
        ):
            if event == "cancelled":
                yield {"cancelled": True, **counts}
                return
            current += 1
            yield {"current": current, "total": total, **counts}

        if limit and (counts["enriched"] + counts["errors"]) >= limit:
            break

    yield {"current": current, "total": total, **counts, "done": True}


def enrich(workers: int = 1, limit: Optional[int] = None, session_id: str | None = None) -> dict:
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("enrich"):
        return _enrich_unlocked(workers=workers, limit=limit, session_id=session_id)


def enrich_with_progress(
    workers: int = 1,
    limit: int | None = None,
    session_id: str | None = None,
    pause_event: "threading.Event | None" = None,
    cancel_check: "Callable[[], bool] | None" = None,
):
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("enrich"):
        yield from _enrich_with_progress_unlocked(
            workers=workers, limit=limit, session_id=session_id,
            pause_event=pause_event, cancel_check=cancel_check,
        )

"""
Enricher — extracts metadata, hashes, and dates from scanned files.

Processes all File records in PENDING status and upgrades them to ENRICHED.
Safe to re-run; already-enriched files are skipped.
"""
import hashlib
import mimetypes
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.db.models import File, FileStatus
from datahoarder.db.session import get_engine
from datahoarder.logging import get_logger

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

try:
    import mutagen
    _HAS_MUTAGEN = True
except ImportError:
    _HAS_MUTAGEN = False

CHUNK = 65_536  # 64 KB read chunks for hashing
BATCH_SIZE = 200


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


def _mime_type(path: Path) -> str:
    """Best-effort MIME type detection."""
    if _HAS_MAGIC:
        try:
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
    """Compute perceptual hash for images (for near-duplicate detection)."""
    if not _HAS_PIL:
        return None
    try:
        with PilImage.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def _best_date(
    exif: Optional[datetime],
    modified: Optional[datetime],
    created: Optional[datetime],
) -> Optional[datetime]:
    return exif or modified or created


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich(workers: int = 1, limit: Optional[int] = None, session_id: str | None = None) -> dict:
    """
    Enrich all PENDING File records with metadata and hashes.

    Args:
        workers: reserved for future async implementation (currently sequential)
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
            with Session(engine) as session:
                # Always query from offset 0: processed files change status
                # and no longer match the PENDING filter.
                enrich_q = session.query(File).filter(File.status == FileStatus.PENDING)
                if session_id:
                    enrich_q = enrich_q.filter(File.session_id == session_id)
                batch = enrich_q.limit(BATCH_SIZE).all()
                if not batch:
                    break

                for file_rec in batch:
                    path = Path(file_rec.path)

                    if not path.exists():
                        file_rec.status = FileStatus.ERROR
                        file_rec.error_message = "File not found on disk"
                        counts["errors"] += 1
                        progress.advance(task)
                        continue

                    try:
                        # MIME type
                        file_rec.mime_type = _mime_type(path)
                        mime = file_rec.mime_type or ""

                        # Hashes
                        file_rec.hash_md5 = _md5(path)

                        # EXIF / audio dates
                        if mime.startswith("image/"):
                            file_rec.date_exif = _exif_date(path)
                            file_rec.hash_perceptual = _perceptual_hash(path)
                        elif mime.startswith(("video/", "audio/")):
                            file_rec.date_exif = _audio_date(path)

                        file_rec.date_best = _best_date(
                            file_rec.date_exif,
                            file_rec.date_modified,
                            file_rec.date_created,
                        )

                        file_rec.status = FileStatus.ENRICHED
                        file_rec.enriched_at = datetime.utcnow()
                        counts["enriched"] += 1

                    except Exception as exc:
                        file_rec.status = FileStatus.ERROR
                        file_rec.error_message = str(exc)[:500]
                        counts["errors"] += 1
                        logger.warning(
                            "Enrichment failed",
                            extra={
                                "path": str(path),
                                "error": str(exc),
                            },
                        )

                    progress.advance(task)

                session.commit()

            if limit and (counts["enriched"] + counts["errors"]) >= limit:
                break

    logger.info(
        "Enrichment complete",
        extra={
            "enriched": counts["enriched"],
            "errors": counts["errors"],
        },
    )
    return counts


def enrich_with_progress(
    workers: int = 1,
    limit: int | None = None,
    session_id: str | None = None,
    pause_event: "threading.Event | None" = None,
    cancel_check: "Callable[[], bool] | None" = None,
):
    """
    Like enrich() but yields progress dicts for SSE streaming.
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
        with Session(engine) as session:
            # Always query from offset 0: processed files change status
            # and no longer match the PENDING filter.
            enrich_q = session.query(File).filter(File.status == FileStatus.PENDING)
            if session_id:
                enrich_q = enrich_q.filter(File.session_id == session_id)
            batch = enrich_q.limit(BATCH_SIZE).all()
            if not batch:
                break

            for file_rec in batch:
                # Check for cancel
                if cancel_check and cancel_check():
                    session.commit()
                    yield {"cancelled": True, **counts}
                    return

                # Block if paused
                if pause_event:
                    pause_event.wait()

                path = Path(file_rec.path)
                current += 1

                if not path.exists():
                    file_rec.status = FileStatus.ERROR
                    file_rec.error_message = "File not found on disk"
                    counts["errors"] += 1
                    yield {"current": current, "total": total, **counts}
                    continue

                try:
                    file_rec.mime_type = _mime_type(path)
                    mime = file_rec.mime_type or ""
                    file_rec.hash_md5 = _md5(path)

                    if mime.startswith("image/"):
                        file_rec.date_exif = _exif_date(path)
                        file_rec.hash_perceptual = _perceptual_hash(path)
                    elif mime.startswith(("video/", "audio/")):
                        file_rec.date_exif = _audio_date(path)

                    file_rec.date_best = _best_date(
                        file_rec.date_exif,
                        file_rec.date_modified,
                        file_rec.date_created,
                    )

                    file_rec.status = FileStatus.ENRICHED
                    file_rec.enriched_at = datetime.utcnow()
                    counts["enriched"] += 1
                except Exception as exc:
                    file_rec.status = FileStatus.ERROR
                    file_rec.error_message = str(exc)[:500]
                    counts["errors"] += 1

                yield {"current": current, "total": total, **counts}

            session.commit()

        if cancel_check and cancel_check():
            yield {"cancelled": True, **counts}
            return
        if limit and (counts["enriched"] + counts["errors"]) >= limit:
            break

    yield {"current": current, "total": total, **counts, "done": True}

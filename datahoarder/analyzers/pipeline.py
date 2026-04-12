"""
Analysis pipeline — orchestrates all analyzers across ENRICHED files.

Picks the right analyzer per file, runs it, saves results.

Parallelism model:
  - Worker threads run _process_one_file() concurrently.
  - Pre-processing (text extraction, image resize, Whisper transcription)
    overlaps freely between workers.
  - The actual Ollama LLM call is serialised via _OLLAMA_REQUEST_LOCK in
    ollama_client.py — Ollama is sequential anyway, but the lock prevents
    connection pool exhaustion when workers > 1.
  - GPU safety: WhisperModel runs on CPU (fixed), so no GPU contention with Ollama.
"""
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.analyzers.archive import ArchiveAnalyzer
from datahoarder.analyzers.base import BaseAnalyzer, AnalysisResult
from datahoarder.analyzers.document import DocumentAnalyzer
from datahoarder.analyzers.image import ImageAnalyzer
from datahoarder.analyzers.threedmodel import ThreeDModelAnalyzer
from datahoarder.analyzers.video import VideoAnalyzer
from datahoarder.core.context import build_context
from datahoarder.db.models import File, FileStatus
from datahoarder.db.session import get_engine

logger = logging.getLogger(__name__)

QUERY_BATCH = 50


def _get_analyzer(
    analyzers: list[BaseAnalyzer],
    mime_type: Optional[str],
    extension: Optional[str],
) -> Optional[BaseAnalyzer]:
    ext = (extension or "").lower()
    mime = mime_type or ""
    for a in analyzers:
        if a.can_handle(mime, ext):
            return a
    return None


def _process_one_file(
    file_id: int,
    engine,
    analyzers: list[BaseAnalyzer],
    client,
    skip_ext: set[str],
) -> tuple[int, str, Optional[str]]:
    """Analyze a single file. Returns (file_id, status, error_msg)."""
    with Session(engine) as session:
        file_rec = session.get(File, file_id)
        if not file_rec:
            return file_id, "error", "File not found in DB"

        logger.debug("Processing file %d: %s (%s)", file_id, file_rec.filename, file_rec.mime_type)

        ext = file_rec.extension or ""
        if ext in skip_ext:
            file_rec.status = FileStatus.SKIPPED
            session.commit()
            return file_id, "skipped", None

        analyzer = _get_analyzer(analyzers, file_rec.mime_type, ext)
        if not analyzer:
            file_rec.status = FileStatus.SKIPPED
            mime = file_rec.mime_type or ""
            if mime.startswith("video/") or ext in (
                ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
            ):
                file_rec.ai_description = (
                    "Skipped: install ffmpeg to analyze video files"
                )
            else:
                file_rec.ai_description = (
                    "No analyzer available for this file type"
                )
            session.commit()
            return file_id, "skipped", None

        ctx = build_context(file_rec)
        try:
            result: AnalysisResult = analyzer.analyze(file_rec, ctx)
            analyzer.save_result(
                file_rec, result, model_name=str(type(client).__name__),
            )
            return file_id, "analyzed", None
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning("File %d failed: %s", file_id, exc)
            file_rec.status = FileStatus.ERROR
            file_rec.error_message = f"{exc}\n{tb}"[:1000]
            session.commit()
            return file_id, "error", str(exc)


def analyze(
    workers: int = 1,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
) -> dict:
    """
    Run AI analysis on all ENRICHED files (CLI version with Rich progress).

    Args:
        workers:          Ignored (kept for API compat). Processing is sequential.
        limit:            Process at most this many files.
        min_size_kb:      Skip files smaller than this.
        skip_extensions:  Set of extensions to skip (e.g. {'.db', '.iso'}).

    Returns:
        Summary dict with counts.
    """
    from datahoarder.ai.router import get_client

    client = get_client()
    analyzer_list: list[BaseAnalyzer] = [
        ImageAnalyzer(client),
        VideoAnalyzer(client),
        DocumentAnalyzer(client),
        ArchiveAnalyzer(client),
        ThreeDModelAnalyzer(client),
    ]

    engine = get_engine()
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}
    skip_ext = skip_extensions or set()

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.ENRICHED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if min_size_kb:
            query = query.filter(File.size_bytes >= min_size_kb * 1024)
        if limit:
            query = query.limit(limit)
        total = query.count()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        refresh_per_second=2,
    ) as progress:
        task = progress.add_task("Analyzing...", total=total)
        processed = 0

        while True:
            with Session(engine) as session:
                batch = (
                    session.query(File.id)
                    .filter(File.status == FileStatus.ENRICHED)
                    .filter(File.session_id == session_id)
                    .limit(QUERY_BATCH)
                    .all()
                ) if session_id else (
                    session.query(File.id)
                    .filter(File.status == FileStatus.ENRICHED)
                    .limit(QUERY_BATCH)
                    .all()
                )
            if not batch:
                break

            for (file_id,) in batch:
                fid, status, error = _process_one_file(
                    file_id, engine, analyzer_list, client, skip_ext,
                )
                counts[status if status in counts else "errors"] += 1
                progress.advance(task)
                progress.update(
                    task,
                    description=(
                        f"Analyzing... done:{counts['analyzed']} "
                        f"skip:{counts['skipped']} err:{counts['errors']}"
                    ),
                )
                processed += 1
                if limit and processed >= limit:
                    break

            if limit and processed >= limit:
                break

    return counts


def analyze_with_progress(
    workers: int = 1,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
    pause_event: threading.Event | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """
    Analyze files, yielding progress dicts for SSE streaming.

    When workers > 1, files are processed in parallel using a ThreadPoolExecutor.
    Pre-processing (text extraction, image resize, Whisper transcription) overlaps
    between workers; LLM calls are serialised by _OLLAMA_REQUEST_LOCK in the client.

    Yields:
        {"current": N, "total": M, "analyzed": A, "skipped": S, "errors": E}
        ...
        {"done": true, "analyzed": A, "skipped": S, "errors": E}  (final)
    """
    from datahoarder.ai.router import get_client

    client = get_client()
    analyzer_list: list[BaseAnalyzer] = [
        ImageAnalyzer(client),
        VideoAnalyzer(client),
        DocumentAnalyzer(client),
        ArchiveAnalyzer(client),
        ThreeDModelAnalyzer(client),
    ]

    engine = get_engine()
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}
    skip_ext = skip_extensions or set()
    effective_workers = max(1, workers)

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.ENRICHED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if min_size_kb:
            query = query.filter(File.size_bytes >= min_size_kb * 1024)
        if limit:
            query = query.limit(limit)
        total = query.count()

    if total == 0:
        yield {"done": True, **counts}
        return

    yield {"current": 0, "total": total, **counts}

    processed = 0

    while True:
        if cancel_check and cancel_check():
            yield {"cancelled": True, **counts}
            return

        with Session(engine) as db:
            awp_q = db.query(File.id).filter(File.status == FileStatus.ENRICHED)
            if session_id:
                awp_q = awp_q.filter(File.session_id == session_id)
            batch = awp_q.limit(QUERY_BATCH).all()
        if not batch:
            break

        if effective_workers <= 1:
            # ----- Sequential path -----
            for (file_id,) in batch:
                if cancel_check and cancel_check():
                    yield {"cancelled": True, **counts}
                    return
                if pause_event:
                    pause_event.wait()

                fid, status, error = _process_one_file(
                    file_id, engine, analyzer_list, client, skip_ext,
                )
                if error:
                    logger.warning("File %d failed: %s", fid, error)
                counts[status if status in counts else "errors"] += 1
                processed += 1
                yield {"current": processed, "total": total, **counts}
                if limit and processed >= limit:
                    break
        else:
            # ----- Parallel path -----
            # Check pause before submitting the batch
            if pause_event:
                pause_event.wait()

            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = {
                    pool.submit(
                        _process_one_file, file_id, engine, analyzer_list, client, skip_ext,
                    ): file_id
                    for (file_id,) in batch
                }
                for future in as_completed(futures):
                    if cancel_check and cancel_check():
                        pool.shutdown(wait=False, cancel_futures=True)
                        yield {"cancelled": True, **counts}
                        return
                    try:
                        fid, status, error = future.result()
                        if error:
                            logger.warning("File %d failed: %s", fid, error)
                        counts[status if status in counts else "errors"] += 1
                    except Exception as exc:
                        logger.error("Worker raised: %s", exc)
                        counts["errors"] += 1
                    processed += 1
                    yield {"current": processed, "total": total, **counts}
                    if limit and processed >= limit:
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

        if limit and processed >= limit:
            break

    yield {"done": True, **counts}

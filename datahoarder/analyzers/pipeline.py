"""
Analysis pipeline — orchestrates all analyzers across ENRICHED files.

Picks the right analyzer per file, runs it, saves results.
Supports batched parallel processing via ThreadPoolExecutor.
"""
import threading
import traceback
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.analyzers.base import BaseAnalyzer, AnalysisResult
from datahoarder.analyzers.document import DocumentAnalyzer
from datahoarder.analyzers.image import ImageAnalyzer
from datahoarder.analyzers.video import VideoAnalyzer
from datahoarder.core.context import build_context
from datahoarder.db.models import File, FileStatus
from datahoarder.db.session import get_engine

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


def analyze(
    workers: int = 2,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
) -> dict:
    """
    Run AI analysis on all ENRICHED files.

    Args:
        workers:          Number of parallel threads (keep low to avoid OOM with local models).
        limit:            Process at most this many files.
        min_size_kb:      Skip files smaller than this (avoids analyzing empty files).
        skip_extensions:  Set of extensions to skip (e.g. {'.db', '.iso'}).

    Returns:
        Summary dict with counts.
    """
    from datahoarder.ai.router import get_client

    client = get_client()
    analyzers: list[BaseAnalyzer] = [
        ImageAnalyzer(client),
        VideoAnalyzer(client),
        DocumentAnalyzer(client),
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

    def process_file(file_id: int) -> tuple[int, str, Optional[str]]:
        """Worker function: analyze one file. Returns (id, status, error)."""
        with Session(engine) as session:
            file_rec = session.get(File, file_id)
            if not file_rec:
                return file_id, "error", "File not found in DB"

            ext = file_rec.extension or ""
            if ext in skip_ext:
                file_rec.status = FileStatus.SKIPPED
                session.commit()
                return file_id, "skipped", None

            analyzer = _get_analyzer(analyzers, file_rec.mime_type, ext)
            if not analyzer:
                file_rec.status = FileStatus.SKIPPED
                mime = file_rec.mime_type or ""
                if mime.startswith("video/") or ext in (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"):
                    file_rec.ai_description = "Skipped: install ffmpeg to analyze video files"
                else:
                    file_rec.ai_description = "No analyzer available for this file type"
                session.commit()
                return file_id, "skipped", None

            ctx = build_context(file_rec)
            try:
                result: AnalysisResult = analyzer.analyze(file_rec, ctx)
                analyzer.save_result(file_rec, result, model_name=str(type(client).__name__))
                return file_id, "analyzed", None
            except Exception as exc:
                tb = traceback.format_exc()
                file_rec.status = FileStatus.ERROR
                file_rec.error_message = f"{exc}\n{tb}"[:1000]
                session.commit()
                return file_id, "error", str(exc)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        refresh_per_second=2,
    ) as progress:
        task = progress.add_task("Analyzing…", total=total)

        offset = 0
        processed = 0

        while True:
            with Session(engine) as session:
                # Always query from offset 0: processed files change status
                # and no longer match the ENRICHED filter, so the result set
                # naturally shrinks each iteration.
                a_q = session.query(File.id).filter(File.status == FileStatus.ENRICHED)
                if session_id:
                    a_q = a_q.filter(File.session_id == session_id)
                batch = a_q.limit(QUERY_BATCH).all()
            if not batch:
                break

            file_ids = [row[0] for row in batch]

            with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
                futures = {pool.submit(process_file, fid): fid for fid in file_ids}
                for future in as_completed(futures):
                    try:
                        # Add timeout to prevent blocking indefinitely
                        fid, status, error = future.result(timeout=120)
                    except concurrent.futures.TimeoutError:
                        # File analysis took too long, mark as error
                        fid = futures.get(future, "unknown")
                        status = "errors"
                        error = "Analysis timeout (>120s)"
                    counts[status if status in counts else "errors"] += 1
                    progress.advance(task)
                    progress.update(
                        task,
                        description=(
                            f"Analyzing… ✓{counts['analyzed']} "
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
    workers: int = 2,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
    pause_event: threading.Event | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """
    Same as analyze() but yields progress dicts for SSE streaming.

    Yields:
        {"current": N, "total": M, "analyzed": A, "skipped": S, "errors": E}
        ...
        {"done": true, "analyzed": A, "skipped": S, "errors": E}  (final)
    """
    from datahoarder.ai.router import get_client

    client = get_client()
    analyzers_list: list[BaseAnalyzer] = [
        ImageAnalyzer(client),
        VideoAnalyzer(client),
        DocumentAnalyzer(client),
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

    if total == 0:
        yield {"done": True, **counts}
        return

    # Yield initial state
    yield {"current": 0, "total": total, **counts}

    def process_file_inner(file_id: int) -> tuple[int, str, Optional[str]]:
        with Session(engine) as session:
            file_rec = session.get(File, file_id)
            if not file_rec:
                return file_id, "error", "File not found in DB"

            ext = file_rec.extension or ""
            if ext in skip_ext:
                file_rec.status = FileStatus.SKIPPED
                session.commit()
                return file_id, "skipped", None

            analyzer = _get_analyzer(analyzers_list, file_rec.mime_type, ext)
            if not analyzer:
                file_rec.status = FileStatus.SKIPPED
                mime = file_rec.mime_type or ""
                if mime.startswith("video/") or ext in (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"):
                    file_rec.ai_description = "Skipped: install ffmpeg to analyze video files"
                else:
                    file_rec.ai_description = "No analyzer available for this file type"
                session.commit()
                return file_id, "skipped", None

            ctx = build_context(file_rec)
            try:
                result: AnalysisResult = analyzer.analyze(file_rec, ctx)
                analyzer.save_result(file_rec, result, model_name=str(type(client).__name__))
                return file_id, "analyzed", None
            except Exception as exc:
                tb = traceback.format_exc()
                file_rec.status = FileStatus.ERROR
                file_rec.error_message = f"{exc}\n{tb}"[:1000]
                session.commit()
                return file_id, "error", str(exc)

    processed = 0

    while True:
        with Session(engine) as session:
            # Always query from offset 0: processed files change status
            # and no longer match the ENRICHED filter.
            awp_q = session.query(File.id).filter(File.status == FileStatus.ENRICHED)
            if session_id:
                awp_q = awp_q.filter(File.session_id == session_id)
            batch = awp_q.limit(QUERY_BATCH).all()
        if not batch:
            break

        file_ids = [row[0] for row in batch]

        with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
            futures = {pool.submit(process_file_inner, fid): fid for fid in file_ids}
            for future in as_completed(futures):
                # Check for cancel before processing result
                if cancel_check and cancel_check():
                    pool.shutdown(wait=False, cancel_futures=True)
                    yield {"cancelled": True, **counts}
                    return

                try:
                    # Add timeout to prevent blocking indefinitely on stuck file analysis
                    fid, status, error = future.result(timeout=120)
                except concurrent.futures.TimeoutError:
                    # File analysis took too long (120+ seconds), mark as error and continue
                    # This allows cancel checks to run periodically instead of blocking forever
                    fid = futures.get(future, "unknown")
                    status = "errors"
                    error = "Analysis timeout (>120s)"
                    if db_session and fid != "unknown":
                        f = db_session.query(File).filter(File.id == fid).first()
                        if f:
                            f.error_message = error

                counts[status if status in counts else "errors"] += 1
                processed += 1
                yield {"current": processed, "total": total, **counts}

                # Block if paused (between files)
                if pause_event:
                    pause_event.wait()

                if limit and processed >= limit:
                    break

        # Check cancel between batches
        if cancel_check and cancel_check():
            yield {"cancelled": True, **counts}
            return

        if limit and processed >= limit:
            break

    yield {"done": True, **counts}

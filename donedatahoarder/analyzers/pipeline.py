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
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from donedatahoarder.analyzers.archive import ArchiveAnalyzer
from donedatahoarder.analyzers.base import BaseAnalyzer, AnalysisResult
from donedatahoarder.analyzers.document import DocumentAnalyzer
from donedatahoarder.analyzers.image import ImageAnalyzer
from donedatahoarder.analyzers.threedmodel import ThreeDModelAnalyzer
from donedatahoarder.analyzers.video import VideoAnalyzer
from donedatahoarder.core.context import build_context
from donedatahoarder.db.models import File, FileStatus
from donedatahoarder.db.session import get_engine
from donedatahoarder.logging import get_logger

logger = get_logger(__name__)

QUERY_BATCH = 50
AI_INFERENCE_FAILED_PREFIX = "AI inference failed:"


def _wait_ready(
    pause_event: threading.Event | None,
    cancel_check: Callable[[], bool] | None,
) -> bool:
    """Return False if cancellation arrived while a pipeline was paused."""
    while pause_event is not None and not pause_event.wait(timeout=0.25):
        if cancel_check and cancel_check():
            return False
    return not (cancel_check and cancel_check())


def _provider_reason(message: str) -> str:
    detail = message.lower()
    if "timed out" in detail or "timeout" in detail:
        return "provider_timeout"
    if "output token limit" in detail or "done_reason=length" in detail:
        return "provider_output_limit"
    if "json" in detail or "invalid response" in detail:
        return "provider_invalid_response"
    if "http" in detail or "status code" in detail or "connection" in detail:
        return "provider_http_error"
    return "provider_failure"


def _eligible_for_analysis(retry_errors: bool):
    eligible = File.status == FileStatus.ENRICHED
    if retry_errors:
        eligible = or_(
            eligible,
            and_(
                File.status == FileStatus.ERROR,
                or_(
                    File.analysis_reason.startswith("provider_"),
                    File.error_message.startswith(AI_INFERENCE_FAILED_PREFIX),
                ),
            ),
        )
    return eligible


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

        logger.debug(
            "Processing file",
            extra={
                "file_id": file_id,
                "file_name": file_rec.filename,
                "mime_type": file_rec.mime_type,
            },
        )

        ext = file_rec.extension or ""
        if ext in skip_ext:
            file_rec.status = FileStatus.SKIPPED
            file_rec.analysis_outcome = "skipped"
            file_rec.analysis_reason = "excluded_extension"
            file_rec.analysis_evidence_source = "none"
            session.commit()
            return file_id, "skipped", None

        analyzer = _get_analyzer(analyzers, file_rec.mime_type, ext)
        if not analyzer:
            file_rec.status = FileStatus.SKIPPED
            file_rec.analysis_outcome = "skipped"
            file_rec.analysis_reason = "unsupported_type"
            file_rec.analysis_evidence_source = "none"
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
            if result.description.startswith(AI_INFERENCE_FAILED_PREFIX):
                # Existing analyzers use this sentinel for provider failures.
                raise RuntimeError(result.description)
            uses_vision = result.evidence_source == "vision"
            model_name = (
                getattr(client, "vision_model" if uses_vision else "text_model", None)
                or getattr(client, "model_name", None)
                or type(client).__name__
            )
            model_digest = None
            if result.outcome != "skipped" and hasattr(client, "model_digest"):
                model_digest = client.model_digest(model_name)
            analyzer.save_result(file_rec, result, model_name, model_digest)
            logger.info(
                "AI analysis complete",
                extra={
                    "file_id": file_id,
                    "file_name": file_rec.filename,
                    "model": model_name,
                },
            )
            return file_id, "skipped" if result.outcome == "skipped" else "analyzed", None
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning(
                "AI analysis failed",
                extra={
                    "file_id": file_id,
                    "file_name": file_rec.filename,
                    "error": str(exc),
                },
            )
            file_rec.status = FileStatus.ERROR
            file_rec.error_message = f"{exc}\n{tb}"[:1000]
            file_rec.analysis_outcome = "failed"
            file_rec.analysis_reason = _provider_reason(str(exc)) if (
                str(exc).startswith(AI_INFERENCE_FAILED_PREFIX)
            ) else "analyzer_error"
            session.commit()
            return file_id, "error", str(exc)


def analyze(
    workers: int = 1,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
    retry_errors: bool = False,
) -> dict:
    """
    Run AI analysis on all ENRICHED files (CLI version with Rich progress).

    Delegates to analyze_with_progress so ``workers`` actually parallelizes.
    The batch query there skips files smaller than ``min_size_kb``.

    Returns:
        Summary dict with counts.
    """
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        refresh_per_second=2,
    ) as progress:
        task = progress.add_task("Analyzing...", total=0)
        for event in analyze_with_progress(
            workers=workers,
            limit=limit,
            min_size_kb=min_size_kb,
            skip_extensions=skip_extensions,
            session_id=session_id,
            retry_errors=retry_errors,
        ):
            counts["analyzed"] = event.get("analyzed", counts["analyzed"])
            counts["skipped"] = event.get("skipped", counts["skipped"])
            counts["errors"] = event.get("errors", counts["errors"])
            if event.get("total") is not None:
                progress.update(task, total=event["total"])
            current = event.get("current")
            if current:
                progress.update(
                    task,
                    completed=current,
                    description=(
                        f"Analyzing... done:{counts['analyzed']} "
                        f"skip:{counts['skipped']} err:{counts['errors']}"
                    ),
                )
            if event.get("done") or event.get("cancelled"):
                break

    return counts


def _analyze_with_progress_unlocked(
    workers: int = 1,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
    retry_errors: bool = False,
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
    from donedatahoarder.ai.router import get_client

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
        query = session.query(File).filter(_eligible_for_analysis(retry_errors))
        if session_id:
            query = query.filter(File.session_id == session_id)
        if min_size_kb:
            query = query.filter(File.size_bytes >= min_size_kb * 1024)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            query = query.limit(limit)
        total = query.count()

    if total == 0:
        yield {"done": True, **counts}
        return

    # Resolve tag digests once before workers fan out. This is a snapshot of
    # installed Ollama metadata, not a per-response attestation.
    if hasattr(client, "model_digest"):
        for tag in {getattr(client, "text_model", None),
                    getattr(client, "vision_model", None)} - {None}:
            client.model_digest(tag)

    yield {"current": 0, "total": total, **counts}

    processed = 0
    last_seen_id = 0

    while True:
        if cancel_check and cancel_check():
            yield {"cancelled": True, **counts}
            return

        with Session(engine) as db:
            awp_q = db.query(File.id).filter(
                _eligible_for_analysis(retry_errors), File.id > last_seen_id,
            )
            if session_id:
                awp_q = awp_q.filter(File.session_id == session_id)
            if min_size_kb:
                awp_q = awp_q.filter(File.size_bytes >= min_size_kb * 1024)
            remaining = total - processed
            queue_bound = min(QUERY_BATCH, max(1, effective_workers * 2), remaining)
            batch = awp_q.order_by(File.id).limit(queue_bound).all()
        if not batch:
            break
        # Failed retries remain ERROR; advance so each ID is tried once per run.
        last_seen_id = batch[-1][0]

        if effective_workers <= 1:
            # ----- Sequential path -----
            for (file_id,) in batch:
                if cancel_check and cancel_check():
                    yield {"cancelled": True, **counts}
                    return
                if not _wait_ready(pause_event, cancel_check):
                    yield {"cancelled": True, **counts}
                    return

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
            if not _wait_ready(pause_event, cancel_check):
                yield {"cancelled": True, **counts}
                return

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


def analyze_with_progress(
    workers: int = 1,
    limit: Optional[int] = None,
    min_size_kb: int = 0,
    skip_extensions: Optional[set[str]] = None,
    session_id: str | None = None,
    retry_errors: bool = False,
    pause_event: threading.Event | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("analyze"):
        yield from _analyze_with_progress_unlocked(
            workers=workers, limit=limit, min_size_kb=min_size_kb,
            skip_extensions=skip_extensions, session_id=session_id,
            retry_errors=retry_errors, pause_event=pause_event,
            cancel_check=cancel_check,
        )

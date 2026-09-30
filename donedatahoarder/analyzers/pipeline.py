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
from pathlib import Path
from typing import Callable, Optional

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from donedatahoarder.analyzers.archive import ArchiveAnalyzer
from donedatahoarder.analyzers.base import BaseAnalyzer, AnalysisResult
from donedatahoarder.analyzers.cad_text import CadTextAnalyzer, has_cad_plot_signature
from donedatahoarder.analyzers.document import DOC_MIMES, DocumentAnalyzer
from donedatahoarder.analyzers.dxf import DxfAnalyzer
from donedatahoarder.analyzers.format_policy import OPAQUE_RESOURCE_EXTENSIONS
from donedatahoarder.analyzers.image import ImageAnalyzer
from donedatahoarder.analyzers.threedmodel import ThreeDModelAnalyzer
from donedatahoarder.analyzers.video import VideoAnalyzer
from donedatahoarder.analyzers.cache import (
    context_hash, eligible_hash, remember, restore,
)
from donedatahoarder.core.context import build_context, clear_context_cache
from donedatahoarder.core.scanner import _is_link_or_reparse
from donedatahoarder.db.models import File, FileStatus
from donedatahoarder.db.session import get_engine
from donedatahoarder.logging import get_logger
from donedatahoarder.proposals.sequence_identity import (
    numbered_frame_identity, is_confirmed_frame,
)

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


def _eligible_for_analysis(retry_errors: bool, include_sampled: bool = False):
    eligible = File.status == FileStatus.ENRICHED
    if include_sampled:
        eligible = or_(eligible, and_(File.status == FileStatus.SKIPPED,
                                      File.analysis_outcome == "sampled"))
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


def _sample_sequence_frame(file_rec: File, stride: int) -> bool:
    """Choose only clearly adjacent numbered visual files for opt-in sampling.

    False means analyze normally. A sampled frame receives no borrowed result.
    """
    if stride <= 1 or not (file_rec.mime_type or "").startswith("image/"):
        return False
    path = Path(file_rec.path)
    identity = numbered_frame_identity(path)
    if identity is None:
        return False
    prefix, number, width = identity
    # Preserve the boundaries of every confirmed contiguous run. An absolute
    # modulo alone can skip an entire short run (frame_0011..frame_0014).
    previous = path.with_name(f"{prefix}{number - 1:0{width}d}{path.suffix}")
    following = path.with_name(f"{prefix}{number + 1:0{width}d}{path.suffix}")
    if number <= 1 or number % stride == 0 or not previous.is_file() or not following.is_file():
        return False
    return is_confirmed_frame(path)


def _get_analyzer(
    analyzers: list[BaseAnalyzer],
    mime_type: Optional[str],
    extension: Optional[str],
    path: Optional[str] = None,
) -> Optional[BaseAnalyzer]:
    ext = (extension or "").lower()
    mime = (mime_type or "").lower()
    # These are preserved resource files, even if a MIME detector happens to
    # misclassify one as an image or text document. No content is invented.
    if ext in OPAQUE_RESOURCE_EXTENSIONS:
        return None
    # A CAD file must not reach ImageAnalyzer merely because a MIME detector
    # calls it image/vnd.dwg. Backups are generic: block only recognized CAD
    # MIME or DWG header, leaving unrelated readable .bak files routable.
    if ext == ".dwg" or ext == ".bak" and _is_cad_backup(mime, path):
        return None
    if ext == ".dxf":
        return next((a for a in analyzers if isinstance(a, DxfAnalyzer)), None)
    if ext == ".shp":
        return next((a for a in analyzers if isinstance(a, CadTextAnalyzer)), None)
    if ext == ".log":
        if (mime not in DOC_MIMES or path and not _has_reparse_component(Path(path))
                and has_cad_plot_signature(Path(path))):
            return next((a for a in analyzers if isinstance(a, CadTextAnalyzer)), None)
        # Ordinary readable logs retain their existing MIME-based text route.
    for a in analyzers:
        if ext == ".log" and isinstance(a, CadTextAnalyzer):
            continue
        if a.can_handle(mime, ext):
            return a
    return None


def _is_cad_backup(mime: str, path: Optional[str]) -> bool:
    if mime in {"image/vnd.dwg", "application/acad", "application/x-acad",
                "application/autocad_dwg", "image/x-dwg"}:
        return True
    if path:
        try:
            if _has_reparse_component(Path(path)):
                return False
            with Path(path).open("rb") as source:
                return source.read(6) in {
                    b"AC1009", b"AC1012", b"AC1014", b"AC1015", b"AC1018",
                    b"AC1021", b"AC1024", b"AC1027", b"AC1032",
                }
        except OSError:
            pass
    return False


def _has_reparse_component(path: Path) -> bool:
    """Check a bounded lexical path chain before reading a backup header.

    This closes the ordinary post-index symlink/junction swap path. It is not
    an atomic no-follow open; the later content hash remains the staleness gate.
    """
    current = path
    for _ in range(64):
        if _is_link_or_reparse(current):
            return True
        if current == current.parent:
            return False
        current = current.parent
    return True


def _process_one_file(
    file_id: int,
    engine,
    analyzers: list[BaseAnalyzer],
    client,
    skip_ext: set[str],
    sequence_sample_stride: int = 0,
    use_cache: bool = True,
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
        if ext.lower() in {".bak", ".log", ".shp"} and _has_reparse_component(Path(file_rec.path)):
            file_rec.status = FileStatus.ERROR
            file_rec.analysis_outcome = "failed"
            file_rec.analysis_reason = "stale_enrichment"
            file_rec.error_message = "File path changed or includes a reparse point; rescan before analysis"
            session.commit()
            return file_id, "error", file_rec.error_message
        if ext in skip_ext:
            file_rec.status = FileStatus.SKIPPED
            file_rec.analysis_outcome = "skipped"
            file_rec.analysis_reason = "excluded_extension"
            file_rec.analysis_evidence_source = "none"
            session.commit()
            return file_id, "skipped", None

        if _sample_sequence_frame(file_rec, sequence_sample_stride):
            file_rec.status = FileStatus.SKIPPED
            file_rec.analysis_outcome = "sampled"
            file_rec.analysis_reason = f"sequence_stride_{sequence_sample_stride}"
            file_rec.analysis_evidence_source = "none"
            file_rec.ai_description = None
            file_rec.ai_suggested_name = None
            file_rec.ai_tags = None
            file_rec.ai_transcript = None
            file_rec.ai_confidence = None
            file_rec.ai_model = None
            file_rec.analysis_model_tag = None
            file_rec.analysis_model_digest = None
            file_rec.analysis_prompt_version = None
            file_rec.analysis_extractor_version = None
            file_rec.analysis_content_chars = None
            file_rec.analysis_context_hash = None
            file_rec.analysis_detected_date = None
            file_rec.analysis_cache_hit = False
            session.commit()
            return file_id, "sampled", None

        analyzer = _get_analyzer(analyzers, file_rec.mime_type, ext, file_rec.path)
        if not analyzer:
            file_rec.status = FileStatus.SKIPPED
            file_rec.analysis_outcome = "skipped"
            file_rec.analysis_reason = "unsupported_type"
            file_rec.analysis_evidence_source = "none"
            file_rec.ai_suggested_name = None
            file_rec.ai_tags = None
            file_rec.ai_transcript = None
            file_rec.ai_confidence = None
            file_rec.ai_model = None
            file_rec.analysis_model_tag = None
            file_rec.analysis_model_digest = None
            file_rec.analysis_prompt_version = None
            file_rec.analysis_extractor_version = None
            file_rec.analysis_content_chars = None
            file_rec.analysis_context_hash = None
            file_rec.analysis_detected_date = None
            file_rec.analysis_cache_hit = False
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

        # PDF text extraction can fall back to rendered-page vision depending
        # on installed local extractors. Until that route is known, neither a
        # prior text nor vision result is safe to restore. AI/PDF containers
        # share extraction dependencies, so conservatively treat both alike.
        cache_route_stable = not (
            isinstance(analyzer, VideoAnalyzer)
            or isinstance(analyzer, DxfAnalyzer)
            or isinstance(analyzer, CadTextAnalyzer)
            or isinstance(analyzer, DocumentAnalyzer) and ext.lower() in {".pdf", ".ai"}
        )
        ctx = build_context(file_rec)
        ctx_digest = context_hash(ctx)
        # Cache policy must not disable the indexed-byte staleness gate.
        content_digest = eligible_hash(file_rec)
        if file_rec.hash_sha256 and content_digest is None:
            file_rec.status = FileStatus.ERROR
            file_rec.analysis_outcome = "failed"
            file_rec.analysis_reason = "stale_enrichment"
            file_rec.error_message = "File bytes changed after enrichment; rescan before analysis"
            session.commit()
            return file_id, "error", file_rec.error_message
        try:
            if use_cache and cache_route_stable and content_digest and hasattr(client, "model_digest"):
                if isinstance(analyzer, ImageAnalyzer):
                    source, tag = "vision", getattr(client, "vision_model", None)
                elif isinstance(analyzer, (ArchiveAnalyzer, ThreeDModelAnalyzer)):
                    source, tag = "metadata", getattr(client, "text_model", None)
                else:
                    source, tag = "text", getattr(client, "text_model", None)
                model_keys = [(tag, client.model_digest(tag), source)] if tag else []
                if restore(session, file_rec, content_hash=content_digest,
                           context_digest=ctx_digest, model_keys=model_keys,
                           analyzer=analyzer):
                    return file_id, "cached", None
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
            if result.model_called and result.outcome != "skipped" and hasattr(client, "model_digest"):
                model_digest = client.model_digest(model_name)
            previous_date_best = file_rec.date_best
            analyzer.save_result(file_rec, result, model_name, model_digest)
            if content_digest:
                saved = session.get(File, file_id)
                session.refresh(saved)
                if eligible_hash(saved) != content_digest:
                    saved.status = FileStatus.ERROR
                    saved.analysis_outcome = "failed"
                    saved.analysis_reason = "stale_enrichment"
                    saved.error_message = "File bytes changed during analysis; rescan before retry"
                    saved.ai_description = None
                    saved.ai_suggested_name = None
                    saved.ai_tags = None
                    saved.ai_transcript = None
                    saved.ai_confidence = None
                    saved.ai_model = None
                    saved.analysis_model_tag = None
                    saved.analysis_model_digest = None
                    saved.analysis_prompt_version = None
                    saved.analysis_extractor_version = None
                    saved.analysis_content_chars = None
                    saved.analysis_context_hash = None
                    saved.analysis_detected_date = None
                    saved.analysis_cache_hit = False
                    saved.date_best = previous_date_best
                    session.commit()
                    return file_id, "error", saved.error_message
                saved.analysis_context_hash = ctx_digest
                if use_cache and cache_route_stable:
                    remember(session, saved, content_hash=content_digest,
                             context_digest=ctx_digest, analyzer=analyzer)
            logger.info(
                "File analysis complete",
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
    sequence_sample_stride: int = 0,
    use_cache: bool = True,
) -> dict:
    """
    Run AI analysis on all ENRICHED files (CLI version with Rich progress).

    Delegates to analyze_with_progress so ``workers`` actually parallelizes.
    The batch query there skips files smaller than ``min_size_kb``.

    Returns:
        Summary dict with counts.
    """
    counts = {"analyzed": 0, "cached": 0, "sampled": 0,
              "skipped": 0, "errors": 0}

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
            sequence_sample_stride=sequence_sample_stride,
            use_cache=use_cache,
        ):
            counts["analyzed"] = event.get("analyzed", counts["analyzed"])
            counts["cached"] = event.get("cached", counts["cached"])
            counts["sampled"] = event.get("sampled", counts["sampled"])
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
                        f"cache:{counts['cached']} sample:{counts['sampled']} "
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
    sequence_sample_stride: int = 0,
    use_cache: bool = True,
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

    clear_context_cache()

    client = get_client()
    analyzer_list: list[BaseAnalyzer] = [
        DxfAnalyzer(),
        CadTextAnalyzer(),
        ImageAnalyzer(client),
        VideoAnalyzer(client),
        DocumentAnalyzer(client),
        ArchiveAnalyzer(client),
        ThreeDModelAnalyzer(client),
    ]

    engine = get_engine()
    if sequence_sample_stride < 0:
        raise ValueError("sequence_sample_stride must be non-negative")
    counts = {"analyzed": 0, "cached": 0, "sampled": 0,
              "skipped": 0, "errors": 0}
    skip_ext = skip_extensions or set()
    effective_workers = max(1, workers)

    with Session(engine) as session:
        query = session.query(File).filter(_eligible_for_analysis(
            retry_errors, include_sampled=sequence_sample_stride == 0))
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
                _eligible_for_analysis(retry_errors,
                    include_sampled=sequence_sample_stride == 0),
                File.id > last_seen_id,
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
                    sequence_sample_stride, use_cache,
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
                        _process_one_file, file_id, engine, analyzer_list, client,
                        skip_ext, sequence_sample_stride, use_cache,
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
    sequence_sample_stride: int = 0,
    use_cache: bool = True,
):
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("analyze"):
        yield from _analyze_with_progress_unlocked(
            workers=workers, limit=limit, min_size_kb=min_size_kb,
            skip_extensions=skip_extensions, session_id=session_id,
            retry_errors=retry_errors, pause_event=pause_event,
            cancel_check=cancel_check, sequence_sample_stride=sequence_sample_stride,
            use_cache=use_cache,
        )

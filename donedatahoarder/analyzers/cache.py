"""Conservative persistent cache for local file analysis results.

The cache reuses a result only for identical bytes, prompt context, model tag
and digest, and extractor/prompt versions. A missing model digest disables it.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from donedatahoarder.analyzers.base import EXTRACTOR_VERSION, PROMPT_VERSION
from donedatahoarder.db.models import AnalysisCache, File, FileStatus
from donedatahoarder.core.scanner import _is_link_or_reparse
from donedatahoarder.timeutils import utcnow


def context_hash(context: str) -> str:
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def extractor_key(analyzer) -> str:
    return f"{type(analyzer).__name__}/{EXTRACTOR_VERSION}"


def eligible_hash(file_rec: File) -> str | None:
    """Verify indexed bytes before cache lookup or admission."""
    digest = file_rec.hash_sha256
    if not digest or len(digest) != 64:
        return None
    try:
        if _is_link_or_reparse(Path(file_rec.path)):
            return None
        stat = Path(file_rec.path).stat()
        if stat.st_size != file_rec.size_bytes:
            return None
        if file_rec.date_modified and abs(
            stat.st_mtime - file_rec.date_modified.timestamp()
        ) > 1:
            return None
        actual = hashlib.sha256()
        with Path(file_rec.path).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                actual.update(block)
        if actual.hexdigest() != digest:
            return None
    except OSError:
        return None
    return digest


_RESULT_FIELDS = (
    "ai_description", "ai_suggested_name", "ai_tags", "ai_transcript",
    "ai_confidence", "ai_model", "analysis_outcome", "analysis_reason",
    "analysis_evidence_source", "analysis_model_tag", "analysis_model_digest",
    "analysis_prompt_version", "analysis_extractor_version",
    "analysis_content_chars",
)


def restore(session: Session, file_rec: File, *, content_hash: str,
            context_digest: str, model_keys: list[tuple[str, str, str]],
            analyzer) -> bool:
    """Apply a verified result from another run, retaining its provenance."""
    for tag, digest, source in model_keys:
        if not digest:
            continue
        row = session.scalar(select(AnalysisCache).where(
            AnalysisCache.content_sha256 == content_hash,
            AnalysisCache.context_sha256 == context_digest,
            AnalysisCache.model_tag == tag,
            AnalysisCache.model_digest == digest,
            AnalysisCache.prompt_version == PROMPT_VERSION,
            AnalysisCache.extractor_version == extractor_key(analyzer),
        ).limit(1))
        if row is None:
            continue
        payload = json.loads(row.payload_json)
        if (payload.get("analysis_outcome") != "content_verified"
                or payload.get("analysis_evidence_source") != source):
            continue
        for name in _RESULT_FIELDS:
            setattr(file_rec, name, payload.get(name))
        detected = payload.get("analysis_detected_date")
        file_rec.analysis_detected_date = datetime.fromisoformat(detected) if detected else None
        if file_rec.analysis_detected_date and not file_rec.date_exif and not file_rec.date_best:
            file_rec.date_best = file_rec.analysis_detected_date
        file_rec.analysis_context_hash = context_digest
        file_rec.analysis_cache_hit = True
        file_rec.status = FileStatus.ANALYZED
        file_rec.analyzed_at = utcnow()
        file_rec.error_message = None
        session.commit()
        return True
    return False


def remember(session: Session, file_rec: File, *, content_hash: str,
             context_digest: str, analyzer) -> None:
    """Cache only content-backed, digest-identified successes."""
    if (file_rec.analysis_outcome != "content_verified"
            or not file_rec.analysis_model_digest
            or not file_rec.analysis_model_tag):
        return
    payload = {name: getattr(file_rec, name) for name in _RESULT_FIELDS}
    payload["analysis_detected_date"] = (
        file_rec.analysis_detected_date.isoformat()
        if file_rec.analysis_detected_date else None
    )
    values = dict(
        content_sha256=content_hash, context_sha256=context_digest,
        model_tag=file_rec.analysis_model_tag,
        model_digest=file_rec.analysis_model_digest,
        prompt_version=PROMPT_VERSION,
        extractor_version=extractor_key(analyzer),
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    stmt = insert(AnalysisCache).values(**values).on_conflict_do_nothing(
        index_elements=[
            "content_sha256", "context_sha256", "model_tag", "model_digest",
            "prompt_version", "extractor_version",
        ]
    )
    session.execute(stmt)
    session.commit()

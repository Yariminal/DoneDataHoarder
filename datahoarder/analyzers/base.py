"""Base class for all file analyzers."""
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path as _Path
from typing import Optional

from datahoarder.db.models import File, FileStatus
from datahoarder.db.session import get_engine
from sqlalchemy.orm import Session

SYSTEM_PROMPT = """\
You are a file analysis assistant helping to organize a personal archive.
Your job is to analyze files and return structured metadata to help rename and categorize them.
Be concise, factual, and consistent. Always respond in valid JSON.
"""

# Tags the LLM commonly emits that carry no information for organisation.
# Lower-cased, with spaces normalised to underscores.
_GENERIC_TAGS = {
    "file", "files", "document", "documents", "image", "images", "photo", "photos",
    "picture", "pictures", "media", "content", "data", "stuff", "thing", "object",
    "item", "items", "misc", "other", "unknown", "n_a", "na", "none", "general",
    "various", "untitled", "default", "test", "sample",
    # Compound generics the analyzer prompts already discourage but still leak through:
    "photo_object", "photo_place", "photo_scene", "image_file", "picture_file",
}


def _clean_tags(tags: list[str], filename: str | None, folder: str | None) -> list[str]:
    """
    Filter and deduplicate AI-emitted tags so they actually add information.

    Drops:
    - empty / non-string entries
    - tags shorter than 3 chars after normalisation (avoid "a", "of", "x")
    - generic tags from _GENERIC_TAGS
    - tags that are substrings of the filename stem (filename-echo tags)
    - tags that are substrings of the parent folder name (folder-echo tags)
    - case-insensitive duplicates (preserves first-seen casing/order)
    - tags longer than 40 chars (likely a description leaked into the tag list)
    """
    if not tags:
        return []

    # Build the echo-blocklist from filename stem and folder name.
    echo_block: set[str] = set()
    if filename:
        stem = _Path(filename).stem.lower()
        # Tokenise on common separators so "solar_dekathlon_2018" blocks each part
        for tok in re.split(r"[\s_\-\.]+", stem):
            if len(tok) >= 3:
                echo_block.add(tok)
    if folder:
        for tok in re.split(r"[\s_\-\.]+", folder.lower()):
            if len(tok) >= 3:
                echo_block.add(tok)

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or len(tag) > 40:
            continue
        norm = re.sub(r"\s+", "_", tag.lower())
        if len(norm) < 3:
            continue
        if norm in _GENERIC_TAGS:
            continue
        if norm in seen:
            continue
        # Echo filter: drop the tag if its normalised form is a token already in
        # the filename or folder name. Substring check on each block-token avoids
        # letting "solar_panel" through when the folder is "solar_dekathlon_2018".
        if any(norm == blk or norm in blk or blk in norm for blk in echo_block if len(blk) >= 4):
            continue
        seen.add(norm)
        cleaned.append(tag)

    return cleaned[:15]  # keep a reasonable cap


class AnalysisResult:
    """Structured output from an analyzer."""

    # Sentinel prefix added to descriptions when the analyzer could not actually
    # read the file's content (binary 3D scenes, encrypted PDFs, .hdr without
    # Pillow support, etc.) and is inferring from filename/folder context only.
    UNVERIFIED_PREFIX = "[UNVERIFIED — inferred from filename/folder only] "
    UNVERIFIED_CONFIDENCE_CAP = 0.4

    def __init__(
        self,
        description: str = "",
        tags: list[str] | None = None,
        suggested_name: str = "",  # stem only, no extension
        confidence: float = 0.5,
        transcript: str = "",
        detected_date: Optional[datetime] = None,
        raw: dict | None = None,
        content_available: bool = True,  # set False when only filename/folder seen
    ):
        self.description = description
        self.tags = tags or []
        self.suggested_name = suggested_name
        self.confidence = confidence
        self.transcript = transcript
        self.detected_date = detected_date
        self.raw = raw or {}
        self.content_available = content_available

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "tags": self.tags,
            "suggested_name": self.suggested_name,
            "confidence": self.confidence,
            "transcript": self.transcript,
            "detected_date": self.detected_date.isoformat() if self.detected_date else None,
        }

    @classmethod
    def from_ai_response(cls, data: dict) -> "AnalysisResult":
        """Parse AI JSON response into AnalysisResult."""
        detected_date = None
        raw_date = data.get("detected_date") or data.get("date")
        if raw_date:
            for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    detected_date = datetime.strptime(str(raw_date).strip()[:10], fmt)
                    break
                except ValueError:
                    continue

        tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        return cls(
            description=str(data.get("description", "")).strip(),
            tags=tags[:20],  # cap tags
            suggested_name=_sanitise_stem(str(data.get("suggested_name", ""))),
            confidence=float(data.get("confidence", 0.5)),
            transcript=str(data.get("transcript", "")).strip(),
            detected_date=detected_date,
            raw=data,
        )


def _sanitise_stem(stem: str) -> str:
    """Clean a proposed filename stem for cross-platform safety."""
    import re
    if not stem:
        return ""
    # Remove chars not safe on Windows/Mac/Linux
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", "_", stem.strip())
    stem = re.sub(r"_+", "_", stem)
    stem = stem.strip("._")
    return stem[:80]  # max length


class BaseAnalyzer(ABC):
    """Abstract base analyzer."""

    @abstractmethod
    def can_handle(self, mime_type: str, extension: str) -> bool:
        """Return True if this analyzer handles the given file type."""
        ...

    @abstractmethod
    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        """Analyze a file and return structured results."""
        ...

    def save_result(self, file_rec: File, result: AnalysisResult, model_name: str) -> None:
        """Persist analysis results back to the database."""
        engine = get_engine()
        with Session(engine) as session:
            f = session.get(File, file_rec.id)
            if not f:
                return

            # If the analyzer reported it had no real content access, mark the
            # description as unverified and cap the confidence so downstream
            # consumers (UI, organizer, namer) can treat it differently from
            # descriptions backed by actual content.
            description = result.description or ""
            confidence = result.confidence
            if not result.content_available and description:
                if not description.startswith(AnalysisResult.UNVERIFIED_PREFIX):
                    description = AnalysisResult.UNVERIFIED_PREFIX + description
                confidence = min(confidence, AnalysisResult.UNVERIFIED_CONFIDENCE_CAP)

            f.ai_description = description
            f.ai_suggested_name = result.suggested_name or None
            # Filter LLM-emitted tags through the quality cleaner: drops generic
            # noise, filename/folder echoes, near-duplicates, and overlong text.
            folder_name = _Path(f.path).parent.name if f.path else None
            cleaned_tags = _clean_tags(result.tags, f.filename, folder_name)
            f.ai_tags = json.dumps(cleaned_tags)
            f.ai_confidence = confidence
            f.ai_model = model_name
            f.ai_transcript = result.transcript or None
            # AI-detected dates are hints only — never overwrite real EXIF dates,
            # and only use as date_best if no real filesystem date exists either.
            if result.detected_date and not f.date_exif and not f.date_best:
                f.date_best = result.detected_date
                # Do NOT set date_exif — that column is reserved for real EXIF metadata
            f.status = FileStatus.ANALYZED
            f.analyzed_at = datetime.utcnow()
            session.commit()

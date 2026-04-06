"""Base class for all file analyzers."""
import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

from datahoarder.db.models import File, FileStatus
from datahoarder.db.session import get_engine
from sqlalchemy.orm import Session

SYSTEM_PROMPT = """\
You are a file analysis assistant helping to organize a personal archive.
Your job is to analyze files and return structured metadata to help rename and categorize them.
Be concise, factual, and consistent. Always respond in valid JSON.
"""


class AnalysisResult:
    """Structured output from an analyzer."""

    def __init__(
        self,
        description: str = "",
        tags: list[str] | None = None,
        suggested_name: str = "",  # stem only, no extension
        confidence: float = 0.5,
        transcript: str = "",
        detected_date: Optional[datetime] = None,
        raw: dict | None = None,
    ):
        self.description = description
        self.tags = tags or []
        self.suggested_name = suggested_name
        self.confidence = confidence
        self.transcript = transcript
        self.detected_date = detected_date
        self.raw = raw or {}

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
            f.ai_description = result.description
            f.ai_tags = json.dumps(result.tags)
            f.ai_confidence = result.confidence
            f.ai_model = model_name
            f.ai_transcript = result.transcript or None
            if result.detected_date and not f.date_exif:
                f.date_exif = result.detected_date
                f.date_best = result.detected_date
            f.status = FileStatus.ANALYZED
            f.analyzed_at = datetime.utcnow()
            session.commit()

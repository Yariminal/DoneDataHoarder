"""
Image analyzer — uses a vision LLM to describe and tag photos.

Resizes images before sending to keep token/bandwidth usage reasonable.
Reads EXIF GPS, camera info, and passes it as additional context.
"""
import io
from pathlib import Path

from datahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from datahoarder.db.models import File

try:
    from PIL import Image as PilImage, ExifTags
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

MAX_SIDE = 1024     # max width/height sent to model
JPEG_QUALITY = 85   # compression for resized image

IMAGE_MIME_PREFIXES = ("image/",)
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".avif", ".cr2", ".nef", ".arw",
    ".dng", ".orf", ".rw2",
    ".hdr",  # High Dynamic Range — RGBE format; Pillow may open it, fallback if not
}


def _resize_for_inference(img: "PilImage.Image") -> bytes:
    """Resize image to fit within MAX_SIDE × MAX_SIDE, return JPEG bytes."""
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        ratio = MAX_SIDE / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), PilImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _exif_extra(path: Path) -> str:
    """Extract readable EXIF fields as extra context string."""
    if not _HAS_PIL:
        return ""
    try:
        with PilImage.open(path) as img:
            raw = img._getexif()  # type: ignore[attr-defined]
            if not raw:
                return ""
        readable = {}
        for tag_id, value in raw.items():
            tag = ExifTags.TAGS.get(tag_id, tag_id)
            if tag in (
                "Make", "Model", "Software", "DateTimeOriginal",
                "GPSInfo", "ImageDescription", "UserComment",
                "LensModel", "Flash",
            ):
                readable[tag] = str(value)[:100]
        if not readable:
            return ""
        lines = [f"{k}: {v}" for k, v in readable.items()]
        return "EXIF data:\n" + "\n".join(lines)
    except Exception:
        return ""


VISION_PROMPT = """\
Analyze this image carefully.

Context about the file:
{context}

{exif_extra}

Return a JSON object with these fields:
{{
  "description": "2-3 sentence description of what is in the image",
  "suggested_name": "a concise, meaningful filename stem — MUST preserve specific proper nouns (people, places, events) from the original filename. Translate to English if not already. No extension, no date prefix, use_underscores, max 50 chars",
  "tags": ["tag1", "tag2", ...],
  "category": "one of: photo_person, photo_group, photo_place, photo_event, photo_document, photo_object, screenshot, artwork, other",
  "detected_date": "YYYY-MM-DD if a date is visible or inferable, else null",
  "confidence": 0.0-1.0
}}

For suggested_name: be specific and meaningful. Examples:
- "birthday_party_garden" not "party"
- "tax_return_documents_2019" not "documents"
- "family_holiday_beach_sunset" not "vacation"
"""


class ImageAnalyzer(BaseAnalyzer):
    def __init__(self, ai_client):
        self._client = ai_client

    def can_handle(self, mime_type: str, extension: str) -> bool:
        if mime_type and any(mime_type.startswith(p) for p in IMAGE_MIME_PREFIXES):
            return True
        return extension.lower() in IMAGE_EXTENSIONS

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        if not _HAS_PIL:
            return AnalysisResult(
                description="PIL not installed — cannot analyze image",
                confidence=0.0,
            )

        path = Path(file_rec.path)
        image_bytes = None
        try:
            with PilImage.open(path) as img:
                image_bytes = _resize_for_inference(img)
        except Exception:
            # HDR / exotic formats Pillow can't decode — fall back to text-only LLM call
            pass

        if image_bytes is None:
            # No visual content available; ask LLM to infer from filename + context alone
            text_prompt = VISION_PROMPT.format(context=context, exif_extra="")
            text_prompt += "\n(Note: image could not be decoded — infer from filename and folder context only.)"
            try:
                data = self._client.generate_json(text_prompt, system=SYSTEM_PROMPT)
            except Exception as exc:
                return AnalysisResult(
                    description=f"AI inference failed: {exc}",
                    confidence=0.0,
                )
            result = AnalysisResult.from_ai_response(data)
            result.confidence = min(result.confidence, 0.4)  # cap confidence for blind guesses
            category = data.get("category", "")
            if category and category not in result.tags:
                result.tags.insert(0, category)
            return result

        exif_extra = _exif_extra(path)
        prompt = VISION_PROMPT.format(context=context, exif_extra=exif_extra)

        try:
            data = self._client.generate_json(
                prompt,
                image_bytes=image_bytes,
                system=SYSTEM_PROMPT,
            )
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
        # Add category as a tag
        category = data.get("category", "")
        if category and category not in result.tags:
            result.tags.insert(0, category)

        return result

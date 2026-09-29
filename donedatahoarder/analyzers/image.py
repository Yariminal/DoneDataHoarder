"""
Image analyzer — uses a vision LLM to describe and tag photos.

Resizes images before sending to keep token/bandwidth usage reasonable.
Reads EXIF GPS, camera info, and passes it as additional context.
"""
import io
from pathlib import Path

from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from donedatahoarder.analyzers.response_schemas import ImageAnalysisResponse
from donedatahoarder.db.models import File

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

Return a JSON object in this shape, replacing the example values:
{{
  "description": "A concise description of the visible image content.",
  "suggested_name": "visible_subject_descriptive_detail",
  "tags": ["specific_subject", "visible_attribute"],
  "category": "photo_object",
  "detected_date": null,
  "confidence": 0.8
}}

Write 2-3 sentences for description. For suggested_name, describe the actual
content, not its folder or project; preserve meaningful proper nouns, translate
to English, omit extension and date prefix, use_underscores, max 50 chars.
Use 4-8 specific lowercase tags when supported, each adding information not
already in the filename or folder. Prefer concrete subjects, settings, styles,
techniques, colours and materials; avoid generic or duplicate tags, and skip
uncertain ones. category must be one of photo_person, photo_group, photo_place,
photo_event, photo_document, photo_object, screenshot, artwork, other.
Use YYYY-MM-DD for detected_date only when visible in the image itself, never
from the filename or folder; otherwise use null. confidence is a number from 0 to 1.

For suggested_name: describe what the image actually shows. Examples:
- "team_group_photo_outdoor" not "project_name_team_photo"
- "brick_texture_herringbone_pattern" not "solar_project_texture"
- "menu_cover_event_branding" not "2018_event_menu"
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
                content_available=False,
                outcome="skipped",
                reason="missing_dependency",
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
                data = self._client.generate_json(text_prompt, system=SYSTEM_PROMPT,
                                                  model_cls=ImageAnalysisResponse)
            except Exception as exc:
                return AnalysisResult(
                    description=f"AI inference failed: {exc}",
                    confidence=0.0,
                )
            result = AnalysisResult.from_ai_response(data)
            result.confidence = min(result.confidence, 0.4)  # cap confidence for blind guesses
            # Mark this as a content-unavailable inference so save_result()
            # prefixes the description with [UNVERIFIED ...] downstream.
            result.content_available = False
            result.evidence_source = "filename_only"
            result.reason = "unreadable_content"
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
                model_cls=ImageAnalysisResponse,
            )
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
        result.evidence_source = "vision"
        result.extractor = "pillow_resize"
        # Add category as a tag
        category = data.get("category", "")
        if category and category not in result.tags:
            result.tags.insert(0, category)

        return result

"""
3D model analyzer — handles 3D scene and mesh files.

Extraction strategy by format:
  .obj  — Text-based Wavefront OBJ: extract comment lines (#), object/group
          names (o/g), and material references (mtllib/usemtl). Rich signal.
  .fbx  — ASCII variant: read header comments. Binary variant: filename only.
  .max  — Autodesk 3ds Max binary: filename + folder context only.
  .3ds  — Legacy 3D Studio binary: filename + folder context only.
  .3dm  — Rhino 3D binary: filename + folder context only.

For binary formats the LLM still produces a useful tag set and suggested name
from the filename alone — much better than silently skipping the file.
"""
from pathlib import Path

from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from donedatahoarder.db.models import File

THREED_EXTENSIONS = {
    ".obj",  # Wavefront OBJ — text, extractable
    ".fbx",  # Filmbox — ASCII or binary
    ".max",  # 3ds Max scene — binary
    ".3ds",  # Legacy 3D Studio — binary
    ".3dm",  # Rhino 3D — binary
}

MAX_HEADER_LINES = 80   # OBJ comment / group lines to extract
MAX_FBX_CHARS = 2000    # characters from FBX ASCII header

THREED_PROMPT = """\
You are analyzing a 3D model or scene file to help rename and categorize it in a personal archive.

Context about the file:
{context}

Extracted metadata / header (empty means binary format with no readable text):
---
{header}
---

Based on the filename, folder, file extension, and any extracted text, return a JSON object:
{{
  "description": "1-2 sentences describing what this 3D asset likely is \
(scene, character, prop, environment, vehicle, architectural model, etc.)",
  "suggested_name": "meaningful filename stem — MUST preserve specific proper nouns \
(asset names, project names, character names) from the original filename. \
No extension, no date prefix, use_underscores, max 60 chars",
  "tags": ["3d-model", "tag2", ...],
  "asset_type": "one of: 3d_scene, 3d_character, 3d_prop, 3d_environment, \
3d_vehicle, 3d_architecture, 3d_texture_set, other",
  "software": "likely authoring software: 3ds_max, blender, rhino, maya, cinema4d, \
generic — infer from file extension and filename clues, or null if unknown",
  "confidence": 0.0-1.0
}}
"""


# ---------------------------------------------------------------------------
# Format-specific header extractors
# ---------------------------------------------------------------------------

def _extract_obj_header(path: Path) -> str:
    """
    Extract metadata-rich lines from a Wavefront OBJ file.
    OBJ is plain text; the first few hundred lines typically contain
    comment headers (#), object names (o), group names (g), and
    material library references (mtllib / usemtl) before the mass of
    numeric vertex/face data begins.
    """
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for i, raw in enumerate(fh):
                if i > 500:
                    break
                stripped = raw.strip()
                if stripped.startswith(("#", "mtllib", "usemtl", "o ", "g ")):
                    lines.append(stripped)
    except OSError:
        pass
    return "\n".join(lines[:MAX_HEADER_LINES])


def _extract_fbx_header(path: Path) -> str:
    """
    Try to read an ASCII FBX header.
    ASCII FBX files start with a '; FBX …' comment block.
    Binary FBX files start with 'Kaydara FBX Binary' — we detect and skip those.
    """
    try:
        with open(path, "rb") as fh:
            magic = fh.read(23)
        if magic.startswith(b"Kaydara FBX Binary"):
            return ""  # binary — nothing useful to extract
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read(MAX_FBX_CHARS)
    except OSError:
        return ""


def _extract_header(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".obj":
        return _extract_obj_header(path)
    if ext == ".fbx":
        return _extract_fbx_header(path)
    # .max / .3ds / .3dm — proprietary binary, nothing extractable
    return ""


# ---------------------------------------------------------------------------
# Analyzer class
# ---------------------------------------------------------------------------

class ThreeDModelAnalyzer(BaseAnalyzer):
    def __init__(self, ai_client):
        self._client = ai_client

    def can_handle(self, mime_type: str, extension: str) -> bool:
        return extension.lower() in THREED_EXTENSIONS

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        path = Path(file_rec.path)
        header = _extract_header(path)

        prompt = THREED_PROMPT.format(
            context=context,
            header=header or "(binary format — no text extractable, use filename/folder context)",
        )

        try:
            data = self._client.generate_json(prompt, system=SYSTEM_PROMPT)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                tags=["3d-model"],
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)

        # Ensure 3d-model tag is always present
        if "3d-model" not in result.tags:
            result.tags.insert(0, "3d-model")

        # Add asset_type and software as tags if present
        asset_type = data.get("asset_type", "")
        if asset_type and asset_type not in result.tags:
            result.tags.append(asset_type)
        software = data.get("software", "")
        if software and software not in ("null", "generic", None) and software not in result.tags:
            result.tags.append(software)

        # Cap confidence for binary formats — we're guessing from filename only.
        # Also flag content_available=False so save_result() prefixes the
        # description with [UNVERIFIED ...] and applies the lower confidence cap.
        if not header:
            result.confidence = min(result.confidence, 0.45)
            result.content_available = False

        return result

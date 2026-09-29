"""
Archive analyzer — lists contents of ZIP files and uses LLM to infer purpose.

Strategy: read the file manifest (entry names), pass to LLM as text context.
The list of paths inside an archive is usually enough to infer what it is
(project backup, photo album, asset pack, installer, etc.).
"""
import zipfile
from pathlib import Path

from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from donedatahoarder.analyzers.response_schemas import ArchiveAnalysisResponse
from donedatahoarder.db.models import File

ARCHIVE_EXTENSIONS = {".zip"}
ARCHIVE_MIMES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "multipart/x-zip",
}
MAX_ENTRIES = 120   # entries to include in manifest before truncating

ARCHIVE_PROMPT = """\
You are analyzing an archive (zip file) to help rename and categorize it in a personal file archive.

Context about the file:
{context}

Archive contents ({count} entries{truncated}):
---
{manifest}
---

Based on the archive name, folder context, and its contents, return a JSON
object in this shape, replacing the example values:
{{
  "description": "A concise description of the archive contents and purpose.",
  "suggested_name": "specific_archive_contents",
  "tags": ["specific_project", "archive_contents"],
  "archive_type": "assets_pack",
  "detected_date": null,
  "confidence": 0.8
}}
Describe contents and likely purpose in 1-2 sentences. In suggested_name,
preserve specific project, product or event names from filename and contents;
omit extension and date prefix, use_underscores, max 60 chars. archive_type
must be one of project_backup, software_installer, assets_pack, photos_album,
documents_bundle, source_code, game_files, fonts_pack, plugins_pack, other.
Use YYYY-MM-DD for detected_date only if clearly present in filenames or paths,
otherwise null. confidence is 0 to 1.
"""


class ArchiveAnalyzer(BaseAnalyzer):
    def __init__(self, ai_client):
        self._client = ai_client

    def can_handle(self, mime_type: str, extension: str) -> bool:
        if mime_type and mime_type in ARCHIVE_MIMES:
            return True
        return extension.lower() in ARCHIVE_EXTENSIONS

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        path = Path(file_rec.path)

        manifest_lines: list[str] = []
        total_entries = 0
        truncated = False

        try:
            with zipfile.ZipFile(path, "r") as zf:
                total_entries = len(zf.filelist)
                for entry in zf.filelist[:MAX_ENTRIES]:
                    manifest_lines.append(entry.filename[:512])
                if total_entries > MAX_ENTRIES:
                    truncated = True
        except zipfile.BadZipFile:
            manifest_lines = ["(corrupt or invalid zip file)"]
        except Exception as exc:
            manifest_lines = [f"(error reading archive: {exc})"]

        manifest = "\n".join(manifest_lines)
        truncated_str = f", showing first {MAX_ENTRIES}" if truncated else ""

        prompt = ARCHIVE_PROMPT.format(
            context=context,
            count=total_entries,
            truncated=truncated_str,
            manifest=manifest or "(empty archive)",
        )

        try:
            data = self._client.generate_json(prompt, system=SYSTEM_PROMPT,
                                              model_cls=ArchiveAnalysisResponse)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
        result.evidence_source = "metadata" if total_entries > 0 else "filename_only"
        result.extractor = "zip_manifest"
        result.content_chars = len(manifest)
        archive_type = data.get("archive_type", "")
        if archive_type and archive_type not in result.tags:
            result.tags.insert(0, archive_type)

        # If we couldn't read the manifest at all (corrupt zip, IO error,
        # or empty archive), the LLM was guessing from filename only —
        # mark it accordingly.
        if not manifest_lines or total_entries == 0 or (
            len(manifest_lines) == 1 and manifest_lines[0].startswith("(")
        ):
            result.content_available = False
            result.confidence = min(result.confidence, 0.4)
            result.reason = "unreadable_content"

        return result

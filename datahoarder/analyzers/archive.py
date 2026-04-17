"""
Archive analyzer — lists contents of ZIP files and uses LLM to infer purpose.

Strategy: read the file manifest (entry names), pass to LLM as text context.
The list of paths inside an archive is usually enough to infer what it is
(project backup, photo album, asset pack, installer, etc.).
"""
import zipfile
from pathlib import Path

from datahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from datahoarder.db.models import File

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

Based on the archive name, folder context, and its contents, return a JSON object:
{{
  "description": "1-2 sentences describing what this archive contains and its likely purpose",
  "suggested_name": "meaningful filename stem — MUST preserve specific proper nouns (project names, \
product names, event names) from the filename and contents. \
No extension, no date prefix, use_underscores, max 60 chars",
  "tags": ["tag1", "tag2", ...],
  "archive_type": "one of: project_backup, software_installer, assets_pack, photos_album, \
documents_bundle, source_code, game_files, fonts_pack, plugins_pack, other",
  "detected_date": "YYYY-MM-DD if a date is clearly present in filenames or paths, else null",
  "confidence": 0.0-1.0
}}
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
                entries = zf.namelist()
                total_entries = len(entries)
                for entry in entries[:MAX_ENTRIES]:
                    manifest_lines.append(entry)
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
            data = self._client.generate_json(prompt, system=SYSTEM_PROMPT)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
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

        return result

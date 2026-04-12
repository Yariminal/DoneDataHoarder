"""
Document analyzer — extracts text from PDFs and Office files, then summarises with LLM.

Supported formats: PDF, DOCX, XLSX, TXT, CSV, and other text-based files.
Only sends a limited excerpt to the AI to avoid context-window issues.
"""
from pathlib import Path
from typing import Optional

from datahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, SYSTEM_PROMPT
from datahoarder.db.models import File

MAX_CHARS = 3000   # max text chars to send to AI
MAX_PAGES = 3      # max PDF pages to read

DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
    ".txt", ".md", ".rtf",
    ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm",
}
DOC_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain", "text/csv", "text/html",
    "application/json", "application/xml", "text/xml",
    "application/rtf", "text/rtf",
}


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_pdf(path: Path) -> str:
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:MAX_PAGES]:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n\n".join(text_parts)
    except ImportError:
        # Fallback: try PyPDF2 or just give up
        return ""
    except Exception:
        return ""


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        return ""
    except Exception:
        return ""


def _extract_xlsx(path: Path) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = []
        for sheet in wb.worksheets[:2]:           # first 2 sheets
            for row in sheet.iter_rows(max_row=20, values_only=True):
                cell_vals = [str(c) for c in row if c is not None]
                if cell_vals:
                    rows.append(", ".join(cell_vals))
            if len(rows) > 30:
                break
        wb.close()
        return "\n".join(rows)
    except ImportError:
        return ""
    except Exception:
        return ""


def _extract_text(path: Path) -> str:
    """Read plain-text files with encoding fallbacks."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    return ""


def extract_text(path: Path, mime_type: Optional[str] = None) -> str:
    """Extract readable text from a document, up to MAX_CHARS."""
    ext = path.suffix.lower()
    mime = mime_type or ""

    if ext == ".pdf" or "pdf" in mime:
        text = _extract_pdf(path)
    elif ext in (".docx", ".doc") or "word" in mime:
        text = _extract_docx(path)
    elif ext in (".xlsx", ".xls") or "spreadsheet" in mime or "excel" in mime:
        text = _extract_xlsx(path)
    elif ext in (".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                  ".html", ".htm", ".rtf"):
        text = _extract_text(path)
    else:
        text = _extract_text(path)  # try anyway

    return text[:MAX_CHARS].strip()


# ---------------------------------------------------------------------------
# Analyzer class
# ---------------------------------------------------------------------------

DOC_PROMPT = """\
You are analyzing a document to help rename and categorize it in a personal file archive.

Context about the file:
{context}

Extracted text (first {max_chars} characters):
---
{text}
---

Based on the filename, folder context, and document content, return a JSON object:
{{
  "description": "1-2 sentences describing what this document is about",
  "suggested_name": "meaningful filename stem — MUST preserve specific proper nouns (names, places, organizations, project titles) from the original filename and content. Translate to English if not already. No extension, no date prefix, use_underscores, max 60 chars",
  "tags": ["tag1", "tag2", ...],
  "document_type": "one of: invoice, receipt, contract, report, letter, cv_resume, photo, presentation, spreadsheet, notes, form, certificate, manual, other",
  "detected_date": "YYYY-MM-DD if a date is clearly present in the content, else null",
  "language": "ISO 639-1 language code (e.g. en, he, fr)",
  "confidence": 0.0-1.0
}}

For suggested_name: reflect the actual content.
Examples:
- "invoice_amazon_order_123" not "invoice"
- "employment_contract_2021" not "contract"
- "project_proposal_client_name" not "proposal"

If the text is empty or unreadable, use the filename and folder context to make your best guess.
"""


class DocumentAnalyzer(BaseAnalyzer):
    def __init__(self, ai_client):
        self._client = ai_client

    def can_handle(self, mime_type: str, extension: str) -> bool:
        if mime_type and mime_type in DOC_MIMES:
            return True
        return extension.lower() in DOC_EXTENSIONS

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        path = Path(file_rec.path)
        text = extract_text(path, file_rec.mime_type)

        prompt = DOC_PROMPT.format(
            context=context,
            text=text or "(no readable text extracted)",
            max_chars=MAX_CHARS,
        )

        try:
            data = self._client.generate_json(prompt, system=SYSTEM_PROMPT)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
            )

        result = AnalysisResult.from_ai_response(data)
        doc_type = data.get("document_type", "")
        if doc_type and doc_type not in result.tags:
            result.tags.insert(0, doc_type)
        lang = data.get("language", "")
        if lang and lang != "en":
            result.tags.append(f"lang:{lang}")

        return result

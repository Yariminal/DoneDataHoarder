"""
Document analyzer — extracts text from PDFs and Office files, then summarises with LLM.

Supported formats: PDF, DOCX, XLSX, TXT, CSV, and other text-based files.
Only sends a limited excerpt to the AI to avoid context-window issues.

For PDFs whose text extraction yields nothing (image-only / scanned docs /
design-heavy brochures like event menus), this module falls back to
rendering the first page as a JPEG and sending it to the *vision* model —
so we still get a meaningful name and tags instead of a blind filename guess.
"""
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from donedatahoarder.analyzers.base import (
    AnalysisResult, BaseAnalyzer, EXTRACTOR_VERSION, SYSTEM_PROMPT,
)
from donedatahoarder.db.models import File

MAX_CHARS = 3000   # max text chars to send to AI
MIN_VERIFIED_TEXT_CHARS = 40
MAX_PAGES = 3      # max PDF pages to read

# Hard size cap on document *text* extraction (pdfplumber, openpyxl, etc.).
# Files larger than this skip text extraction entirely and the analyzer
# falls back to the rendered-page vision path (for PDFs) or to filename /
# folder context.
#
# Why: pdfplumber can take minutes / OOM on very large PDFs (the Solar
# Dekathlon test has a 452 MB PDF that hung the analyzer entirely),
# openpyxl / python-docx have similar pathological cases. We'd rather get
# a degraded analysis than no analysis at all.
MAX_DOC_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB

# Size cap for *rendering* a PDF via pypdfium2. Rendering is per-page and
# lazy (Chromium PDFium streams pages rather than loading the whole file
# into memory), so it tolerates far larger files than text extraction:
# the 452 MB test PDF opens in 10 ms and renders page 0 in 120 ms with
# only +30 MB RSS. We keep a generous 2 GB ceiling as a safety rail for
# truly pathological files (backups accidentally named .pdf, etc.).
MAX_PDF_RENDER_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Number of pages to render + send to the vision model for rich PDFs.
# Matches MAX_PAGES on the text side for symmetry. Three pages is enough
# to cover cover + table-of-contents + first content page for most
# brochures / reports, at ~120 KB per JPEG → ~360 KB total upload.
MAX_VISION_PAGES = 3
MAX_PPTX_XML_ENTRY_BYTES = 8 * 1024 * 1024
MAX_PPTX_XML_TOTAL_BYTES = 16 * 1024 * 1024
MAX_DOCX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_DOCX_PACKAGE_ENTRIES = 1024
MAX_DOCX_BLOCKS = 2000
MAX_DOCX_TABLE_DEPTH = 4
DOCX_EXTRACTOR_VERSION = "extractors-v5-2026-09-28"

DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
    ".txt", ".md", ".rtf",
    ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm",
    ".ai",   # Adobe Illustrator; only PDF-backed files can be decoded here
    ".mtl",  # Wavefront material library — plain text
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


@dataclass(frozen=True)
class ExtractionResult:
    text: str = ""
    reason: str | None = None
    extractor: str = "none"


_OFFICE_OPENXML = {".docx", ".xlsx", ".pptx"}
_LEGACY_UNSUPPORTED = {".doc", ".xls", ".ppt", ".odt", ".ods", ".odp"}


class ExtractionTooLarge(ValueError):
    pass


def _extract_pptx(path: Path) -> str:
    """Read visible slide text from the OpenXML package, in slide order."""
    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: info for info in archive.infolist()}
        slides = [n for n in entries if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        slides.sort(key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
        presentation = entries.get("ppt/presentation.xml")
        relationships = entries.get("ppt/_rels/presentation.xml.rels")
        if presentation and relationships:
            if (presentation.file_size > MAX_PPTX_XML_ENTRY_BYTES
                    or relationships.file_size > MAX_PPTX_XML_ENTRY_BYTES):
                raise ExtractionTooLarge("PPTX presentation XML exceeds extraction cap")
            pres_xml = ElementTree.fromstring(archive.read(presentation))
            rels_xml = ElementTree.fromstring(archive.read(relationships))
            rel_targets = {
                rel.get("Id"): rel.get("Target", "")
                for rel in rels_xml.iter() if rel.tag.endswith("}Relationship")
                and rel.get("TargetMode") != "External"
            }
            rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ordered = []
            for node in pres_xml.iter():
                if not node.tag.endswith("}sldId"):
                    continue
                target = rel_targets.get(node.get(rel_ns), "")
                candidate = (target.lstrip("/") if target.startswith("/") else
                             posixpath.normpath(posixpath.join("ppt", target)))
                if candidate in entries and candidate in slides:
                    ordered.append(candidate)
            if ordered:
                slides = list(dict.fromkeys(ordered))
        if not slides:
            return ""
        chunks: list[str] = []
        expanded = 0
        for name in slides[:20]:
            size = entries[name].file_size
            expanded += size
            if size > MAX_PPTX_XML_ENTRY_BYTES or expanded > MAX_PPTX_XML_TOTAL_BYTES:
                raise ExtractionTooLarge("PPTX slide XML exceeds extraction cap")
            root = ElementTree.fromstring(archive.read(name))
            words = [node.text.strip() for node in root.iter()
                     if node.tag.endswith("}t") and node.text and node.text.strip()]
            if words:
                chunks.append(" ".join(words))
            if sum(map(len, chunks)) >= MAX_CHARS:
                break
        return "\n".join(chunks)[:MAX_CHARS]


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _file_too_big(path: Path) -> bool:
    """True if *text* extracting this file is likely to OOM / hang."""
    try:
        return path.stat().st_size > MAX_DOC_SIZE_BYTES
    except OSError:
        return False


def _file_too_big_for_render(path: Path) -> bool:
    """True only for truly pathological PDFs (multi-GB). Rendering via
    pypdfium2 is lazy and page-scoped, so the 100 MB text-extraction
    gate is far too conservative for it."""
    try:
        return path.stat().st_size > MAX_PDF_RENDER_BYTES
    except OSError:
        return False


def _extract_pdf(path: Path) -> str:
    if _file_too_big(path):
        # Skip PDFs over 100 MB — pdfplumber's per-page parsing scales badly
        # on huge files (especially scanned docs and embedded-image-heavy PDFs).
        # The analyzer will fall back to filename/folder inference.
        return ""
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


# Vision-fallback render settings. Scale 2.0 ~= 144 DPI for typical PDFs —
# enough resolution for the vision model to read headings and small text
# while keeping the image small. We then downsize to PDF_VISION_MAX_SIDE
# before JPEG-encoding so the upload to the model stays cheap.
PDF_RENDER_SCALE = 2.0
PDF_VISION_MAX_SIDE = 1024
PDF_VISION_JPEG_QUALITY = 85


def _render_pdf_pages_as_jpegs(
    path: Path,
    max_pages: int = MAX_VISION_PAGES,
) -> list[bytes]:
    """
    Render up to `max_pages` pages of a PDF as JPEG byte blobs suitable
    for a vision model. Returns an empty list on any failure (missing
    library, encrypted / corrupt PDF, zero pages, over-size file) so the
    caller can gracefully degrade. Never raises.

    Size gate is MAX_PDF_RENDER_BYTES (2 GB), much higher than the
    text-extraction cap — pypdfium2 handles a 452 MB PDF in 120 ms / +30
    MB RSS because it streams one page at a time. The text-extraction
    cap is specifically about pdfplumber's appetite for the whole file.

    Returning a list (rather than the old single-page helper) lets the
    caller send the cover + first 2 content pages together via the
    `images_list` kwarg on the vision client — much richer context for
    content-dense PDFs like multi-page brochures, reports, and project
    documents (see the Solar Dekathlon 15.12.pdf case: 452 MB, 33 pages
    of text and 3D renderings that need more than a cover to name well).
    """
    if _file_too_big_for_render(path):
        return []
    try:
        import pypdfium2 as pdfium
        from PIL import Image as PilImage
    except ImportError:
        return []

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return []

    jpegs: list[bytes] = []
    try:
        total = len(pdf)
        if total == 0:
            return []
        # Render the first N pages — simpler and more predictable than
        # sampling e.g. first/middle/last. Can revisit if vision quality
        # plateaus. Page indices are 0-based.
        for idx in range(min(max_pages, total)):
            try:
                page = pdf[idx]
                pil = page.render(scale=PDF_RENDER_SCALE).to_pil()
            except Exception:
                # One bad page shouldn't nuke the whole render — skip it
                # and try the next. If every page fails we return [].
                continue
            try:
                pil = pil.convert("RGB")
                w, h = pil.size
                if max(w, h) > PDF_VISION_MAX_SIDE:
                    ratio = PDF_VISION_MAX_SIDE / max(w, h)
                    pil = pil.resize(
                        (int(w * ratio), int(h * ratio)),
                        PilImage.LANCZOS,
                    )
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=PDF_VISION_JPEG_QUALITY)
                jpegs.append(buf.getvalue())
            except Exception:
                continue
    finally:
        # PdfDocument holds a native PDFium handle; release it promptly.
        try:
            pdf.close()
        except Exception:
            pass

    return jpegs


def _render_pdf_first_page_as_jpeg(path: Path) -> bytes | None:
    """Thin compatibility wrapper — returns just the first rendered
    page's JPEG bytes, or None. Retained so existing callers / tests
    that expect the single-page helper keep working."""
    pages = _render_pdf_pages_as_jpegs(path, max_pages=1)
    return pages[0] if pages else None


def _extract_docx(path: Path) -> str:
    if _file_too_big(path):
        raise ExtractionTooLarge("DOCX compressed package exceeds extraction cap")
    # python-docx expands the OPC package in memory. Reject unusually large
    # declared parts before it reads them, even when the ZIP itself is small.
    with zipfile.ZipFile(path) as package:
        entries = package.infolist()
        document_xml = package.getinfo("word/document.xml")
        if (len(entries) > MAX_DOCX_PACKAGE_ENTRIES
                or document_xml.file_size > MAX_DOCX_DOCUMENT_XML_BYTES
                or sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED_BYTES):
            raise ExtractionTooLarge("DOCX expanded package exceeds extraction cap")

    from docx import Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    visited = 0

    def blocks(parent, depth: int):
        nonlocal visited
        if depth > MAX_DOCX_TABLE_DEPTH:
            raise ExtractionTooLarge("DOCX nested table depth exceeds extraction cap")
        element = parent.element if hasattr(parent, "element") else parent._tc
        body = element.body if hasattr(element, "body") else element
        for child in body.iterchildren():
            if not isinstance(child, (CT_P, CT_Tbl)):
                continue
            visited += 1
            if visited > MAX_DOCX_BLOCKS:
                raise ExtractionTooLarge("DOCX visible blocks exceed extraction cap")
            if isinstance(child, CT_P):
                value = Paragraph(child, parent).text.strip()
                if value:
                    yield value
                continue
            table = Table(child, parent)
            # Keep XML cell objects alive while traversing the table. Using
            # id() alone lets Python recycle wrappers between successive rows.
            seen_cells: set[object] = set()
            for row in table.rows:
                visited += 1
                if visited > MAX_DOCX_BLOCKS:
                    raise ExtractionTooLarge("DOCX table rows exceed extraction cap")
                values = []
                for cell in row.cells:
                    key = cell._tc
                    if key in seen_cells:
                        continue  # merged cell repeated in row/cross-row views
                    seen_cells.add(key)
                    value = " / ".join(blocks(cell, depth + 1)).strip()
                    if value:
                        values.append(value)
                if values:
                    yield " | ".join(values)

    parts = []
    used = 0
    for value in blocks(doc, 0):
        remaining = MAX_CHARS - used
        if remaining <= 0:
            break
        piece = value[:remaining]
        parts.append(piece)
        used += len(piece) + 1
    return "\n".join(parts)[:MAX_CHARS]


def _extract_xlsx(path: Path) -> str:
    if _file_too_big(path):
        return ""
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
    """Read a bounded plain-text excerpt after rejecting binary/control data."""
    if _file_too_big(path):
        # 1 GB log files / massive CSV exports — skip rather than load into RAM.
        # We only need MAX_CHARS worth anyway; future improvement would be a
        # streaming read of the first MAX_CHARS bytes, but for now skip.
        return ""
    try:
        with path.open("rb") as stream:
            sample = stream.read(MAX_CHARS * 4)
    except OSError:
        return ""
    if not sample or b"\x00" in sample or sample.startswith((b"PK\x03\x04", b"%PDF")):
        return ""
    controls = sum(byte < 32 and byte not in (9, 10, 13) for byte in sample)
    if controls / len(sample) > 0.01:
        return ""
    for enc in ("utf-8", "cp1252"):
        try:
            decoded = sample.decode(enc)
            if sum(ch.isprintable() or ch.isspace() for ch in decoded) / len(decoded) < 0.9:
                return ""
            return decoded[:MAX_CHARS]
        except UnicodeDecodeError:
            continue
    return ""


def _is_pdf_backed_illustrator(path: Path) -> bool:
    """Recognize only Illustrator files with a PDF header, using a bounded read."""
    try:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def extract_document(path: Path, mime_type: Optional[str] = None) -> ExtractionResult:
    """Extract a bounded excerpt with an explicit no-content reason."""
    ext = path.suffix.lower()
    mime = mime_type or ""
    if ext in _LEGACY_UNSUPPORTED:
        return ExtractionResult(reason="unsupported_type")
    if ext == ".ai" and not _is_pdf_backed_illustrator(path):
        return ExtractionResult(reason="unsupported_type")
    if ext in {".pdf", ".ai"} or "pdf" in mime:
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            return ExtractionResult(reason="missing_dependency", extractor="pdfplumber")
        text, extractor = _extract_pdf(path), "pdfplumber"
    elif ext == ".pptx":
        try:
            text = _extract_pptx(path)
        except ExtractionTooLarge:
            return ExtractionResult(reason="oversized_content", extractor="pptx_openxml")
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError):
            return ExtractionResult(reason="unreadable_content", extractor="pptx_openxml")
        extractor = "pptx_openxml"
    elif ext == ".docx":
        try:
            import docx  # noqa: F401
        except ImportError:
            return ExtractionResult(reason="missing_dependency", extractor="python-docx")
        try:
            text = _extract_docx(path)
        except ExtractionTooLarge:
            return ExtractionResult(reason="oversized_content", extractor="python-docx")
        except Exception:
            return ExtractionResult(reason="unreadable_content", extractor="python-docx")
        extractor = "python-docx"
    elif ext == ".xlsx":
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return ExtractionResult(reason="missing_dependency", extractor="openpyxl")
        text, extractor = _extract_xlsx(path), "openpyxl"
    else:
        text, extractor = _extract_text(path), "bounded_plaintext"
    text = text[:MAX_CHARS].strip()
    return ExtractionResult(text=text, reason=None if text else "unreadable_content", extractor=extractor)


def extract_text(path: Path, mime_type: Optional[str] = None) -> str:
    """Compatibility wrapper for callers needing only the text excerpt."""
    return extract_document(path, mime_type).text


# ---------------------------------------------------------------------------
# Analyzer class
# ---------------------------------------------------------------------------

PDF_VISION_PROMPT = """\
You are analyzing a PDF. Its first {page_count} page(s) have been
rendered and attached as images. The PDF's extractable text was empty
or too short to be useful — it's most likely a scanned document, a
design-heavy brochure, a flyer, a menu, a poster, a certificate, a
report with embedded 3D renderings, or a graphic cover. Describe what
you see (layout, imagery, visible headings, logos, colours, subject
matter) and propose a meaningful filename based on that.

If multiple pages are attached, treat them as an ordered excerpt — the
first is usually a cover / title, later pages show actual content.
Synthesise across all pages to produce a single description and name
rather than summarising each page separately.

Context about the file:
{context}

Return a JSON object in this shape, replacing the example values:
{{
  "description": "A concise description of the visible document pages.",
  "suggested_name": "visible_document_subject",
  "tags": ["specific_subject", "visible_attribute"],
  "document_type": "brochure",
  "detected_date": null,
  "language": null,
  "confidence": 0.8
}}

Describe the attached pages together in 1-2 sentences. Name their actual
content without repeating the folder name; preserve uniquely identifying
visible proper nouns, translate to English, omit extension and date prefix,
use_underscores, max 60 chars. Use 4-8 specific lowercase underscore_separated
tags for concrete visible elements; skip generic words and uncertain tags.
document_type must be one of invoice, receipt, contract, report, letter,
cv_resume, photo, presentation, spreadsheet, notes, form, certificate, manual,
menu, flyer, brochure, poster, cover, other. Use YYYY-MM-DD for detected_date
only when clearly visible on a page, otherwise null. language is an ISO 639-1
code for visible text, or null for purely graphical pages. confidence is 0 to 1.

For suggested_name: reflect what the document ACTUALLY shows. Examples:
- "event_menu_cactus_pattern" for a menu card with cactus illustrations
- "architecture_project_report_renderings" for a multi-page project report with 3D renderings
- "award_certificate_first_place" for a visible certificate layout
"""


DOC_PROMPT = """\
You are analyzing a document to help rename and categorize it in a personal file archive.

Context about the file:
{context}

Extracted text (first {max_chars} characters):
---
{text}
---

Based on the filename, folder context, and document content, return a JSON
object in this shape, replacing the example values:
{{
  "description": "A concise description of the document content.",
  "suggested_name": "document_subject_purpose",
  "tags": ["specific_subject", "document_subtype"],
  "document_type": "report",
  "detected_date": null,
  "language": "en",
  "confidence": 0.8
}}

Describe the content in 1-2 sentences. Name its actual purpose without
repeating the folder name; preserve uniquely identifying proper nouns,
translate to English, omit extension and date prefix, use_underscores, max
60 chars. Use 4-8 specific lowercase tags that add information beyond the
filename or folder; prefer concrete entities and skip generic or uncertain
tags. document_type must be one of invoice, receipt, contract, report, letter,
cv_resume, photo, presentation, spreadsheet, notes, form, certificate, manual,
other. Use YYYY-MM-DD for detected_date only for an explicit date in the text,
otherwise null. language is an ISO 639-1 code. confidence is 0 to 1.

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

    def extractor_version_for(self, file_rec: File) -> str:
        # Only DOCX body extraction changed. Keep existing PDF, spreadsheet,
        # plain-text and other analyzer cache identities reusable.
        return (DOCX_EXTRACTOR_VERSION if Path(file_rec.path).suffix.lower() == ".docx"
                else EXTRACTOR_VERSION)

    def can_handle(self, mime_type: str, extension: str) -> bool:
        if mime_type and mime_type in DOC_MIMES:
            return True
        return extension.lower() in DOC_EXTENSIONS

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        path = Path(file_rec.path)
        extraction = extract_document(path, file_rec.mime_type)
        text = extraction.text
        ext = path.suffix.lower()

        # Vision fallback: PDFs and PDF-backed Illustrator files that yield
        # near-zero extractable text are often image-based (scans, menus,
        # flyers, design covers)
        # OR over-large files where pdfplumber refused to parse (like the
        # 452 MB Solar Dekathlon 15.12.pdf report with 33 pages of text
        # and 3D renderings). Instead of letting the text-only LLM guess
        # from the filename, we rasterise up to MAX_VISION_PAGES pages and
        # ask the vision model what it sees — a dramatically better
        # signal for naming. _render returns [] on any failure (missing
        # pypdfium2, encrypted / corrupt PDF, render OOM, etc.).
        is_text_empty = not text or len(text.strip()) < 20
        if is_text_empty and (ext == ".pdf" or (
            ext == ".ai" and _is_pdf_backed_illustrator(path)
        )):
            pdf_pages = _render_pdf_pages_as_jpegs(path)
            if pdf_pages:
                vision_result = self._analyze_rendered_pdf(
                    file_rec, context, pdf_pages
                )
                if vision_result is not None:
                    vision_result.evidence_source = "vision"
                    vision_result.extractor = "pdfium_render"
                    vision_result.content_chars = 0
                    return vision_result
        if not text:
            return AnalysisResult(
                description="No readable document content was available",
                confidence=0.0,
                content_available=False,
                evidence_source="none",
                outcome="skipped",
                reason=extraction.reason or "unreadable_content",
                content_chars=0,
                extractor=extraction.extractor,
            )

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
        result.evidence_source = "text"
        result.content_chars = len(text)
        result.extractor = extraction.extractor
        if len(text) < MIN_VERIFIED_TEXT_CHARS:
            result.outcome = "context_only"
            result.reason = "limited_content"
            result.confidence = min(result.confidence, 0.4)
        doc_type = data.get("document_type", "")
        if doc_type and doc_type not in result.tags:
            result.tags.insert(0, doc_type)
        lang = data.get("language", "")
        if lang and lang != "en":
            result.tags.append(f"lang:{lang}")

        # If text extraction yielded nothing meaningful, the LLM was guessing
        # from filename + folder context only. Mark accordingly so the
        # description is prefixed [UNVERIFIED ...] and confidence is capped.
        return result

    def _analyze_rendered_pdf(
        self,
        file_rec: File,
        context: str,
        pages: list[bytes],
    ) -> AnalysisResult | None:
        """
        Vision path for text-empty PDFs, including PDF-backed Illustrator files.
        Sends up to MAX_VISION_PAGES
        rendered pages to the vision model and returns a full
        AnalysisResult. Provider failures return a failed result so the
        pipeline records ERROR without AI evidence.

        For single-page inputs we use `image_bytes=` (cheaper / simpler
        path in both the Ollama and Gemini clients). For multi-page we
        use `images_list=` and the prompt tells the model to synthesise
        across pages rather than describe each.

        NOTE: `content_available=True` because the model really did see
        the pages — unlike the filename-only guess, this isn't
        [UNVERIFIED].
        """
        if not pages:
            return None
        prompt = PDF_VISION_PROMPT.format(
            context=context, page_count=len(pages)
        )
        kwargs: dict = {"system": SYSTEM_PROMPT}
        if len(pages) == 1:
            kwargs["image_bytes"] = pages[0]
        else:
            kwargs["images_list"] = pages

        try:
            data = self._client.generate_json(prompt, **kwargs)
        except Exception as exc:
            return AnalysisResult(
                description=f"AI inference failed: {exc}",
                confidence=0.0,
                content_available=False,
                outcome="failed",
                reason="provider_failure",
            )

        result = AnalysisResult.from_ai_response(data)
        doc_type = data.get("document_type", "")
        if doc_type and doc_type not in result.tags:
            result.tags.insert(0, doc_type)
        lang = data.get("language")
        if lang and lang != "en":
            result.tags.append(f"lang:{lang}")
        # Add a tag so the user / organizer can tell this PDF was
        # analyzed via rendered-page vision — useful for "why is this
        # tagged?" debugging and for deliberately routing similar files
        # later.
        if "image_only_pdf" not in result.tags:
            result.tags.append("image_only_pdf")
        return result

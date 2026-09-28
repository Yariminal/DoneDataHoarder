"""Bounded, deterministic structure inspection for ASCII DXF drawings.

DXF group values can include coordinates and arbitrary user text. This reader
checks only group-code syntax and counts known structural markers; it never
passes values, geometry, or filenames to a language model.
"""
from pathlib import Path

from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer
from donedatahoarder.db.models import File


MAX_DXF_PREFIX_BYTES = 256 * 1024
_ENTITY_MARKERS = {
    b"3DFACE", b"ARC", b"CIRCLE", b"DIMENSION", b"ELLIPSE", b"HATCH",
    b"INSERT", b"LINE", b"LWPOLYLINE", b"MTEXT", b"POINT", b"POLYLINE",
    b"SPLINE", b"TEXT", b"VERTEX",
}


class DxfAnalyzer(BaseAnalyzer):
    def can_handle(self, mime_type: str, extension: str) -> bool:
        return extension.lower() == ".dxf"

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        try:
            with Path(file_rec.path).open("rb") as source:
                data = source.read(MAX_DXF_PREFIX_BYTES + 1)
        except OSError:
            return _unsupported("unreadable_content")

        truncated = len(data) > MAX_DXF_PREFIX_BYTES
        if truncated:
            data = data[:MAX_DXF_PREFIX_BYTES]
        if not data or b"\x00" in data or data.startswith(b"AutoCAD Binary DXF"):
            return _unsupported("unsupported_type")

        lines = data.split(b"\n")
        if truncated:
            # The bounded read may stop in the middle of a group value.
            lines.pop()
            if len(lines) % 2:
                lines.pop()
        elif lines and lines[-1] == b"":
            lines.pop()
        lines = [line.rstrip(b"\r") for line in lines]
        if (len(lines) < 4 or len(lines) % 2
                or lines[0].strip() != b"0" or lines[1].strip() != b"SECTION"):
            return _unsupported("unreadable_content")

        pairs = 0
        sections = 0
        closed_sections = 0
        entities = 0
        saw_section = False
        section_open = False
        in_entities = False
        expecting_section_name = False
        last_pair = None
        for offset in range(0, len(lines), 2):
            code_bytes = lines[offset].strip()
            numeric_code = (code_bytes.isdigit() or
                            code_bytes.startswith(b"-") and code_bytes[1:].isdigit())
            if (not numeric_code or len(code_bytes) > 5
                    or not -5 <= int(code_bytes) <= 1071):
                return _unsupported("unreadable_content")
            code = int(code_bytes)
            value = lines[offset + 1].strip()
            last_pair = (code, value)
            pairs += 1
            if code == 0:
                expecting_section_name = value == b"SECTION"
                if expecting_section_name:
                    if section_open:
                        return _unsupported("unreadable_content")
                    saw_section = True
                    sections += 1
                    section_open = True
                    in_entities = False
                elif value == b"ENDSEC":
                    if not section_open:
                        return _unsupported("unreadable_content")
                    closed_sections += 1
                    section_open = False
                    in_entities = False
                elif in_entities and value in _ENTITY_MARKERS:
                    entities += 1
            elif expecting_section_name and code == 2:
                in_entities = value == b"ENTITIES"
                expecting_section_name = False

        if (not saw_section or pairs < 2 or not truncated and
                (section_open or closed_sections != sections
                 or last_pair != (0, b"EOF"))):
            return _unsupported("unreadable_content")

        scope = (f"first {MAX_DXF_PREFIX_BYTES // 1024} KiB of file"
                 if truncated else "complete file")
        description = (
            f"ASCII DXF structure inspected in {scope}: {pairs} group-code pairs, "
            f"{sections} section starts, {entities} recognized entity markers."
        )
        if truncated:
            description += " Counts describe the inspected prefix only."
        return AnalysisResult(
            description=description,
            confidence=1.0,
            suggested_name="",
            tags=[],
            content_available=True,
            evidence_source="metadata",
            outcome="metadata_only",
            reason="prefix_only" if truncated else None,
            content_chars=0,
            extractor="ascii_dxf_structure",
            model_called=False,
        )


def _unsupported(reason: str) -> AnalysisResult:
    return AnalysisResult(
        outcome="skipped", reason=reason, content_available=False,
        evidence_source="none", extractor="ascii_dxf_structure",
        model_called=False,
    )

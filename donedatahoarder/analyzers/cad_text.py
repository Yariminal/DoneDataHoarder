"""Bounded structural metadata from two signature-checked CAD text formats.

Neither format is sent to a language model. Plot paths, people, timestamps,
shape labels and opcodes are never used as filename or subject suggestions.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer
from donedatahoarder.db.models import File


MAX_CAD_TEXT_PREFIX_BYTES = 64 * 1024
_PLOT_DATE_FORMATS = ("%d/%m/%y %H:%M:%S", "%d/%m/%Y %H:%M:%S")
_PLOT_SCALE = re.compile(r"^[0-9]{1,5}:[0-9]{1,5}$")
_SHAPE_DECLARATION = re.compile(r"^\*(\d{1,5}),(\d{1,6}),([A-Za-z0-9_. ()-]{1,80})$")
_OPCODE = r"(?:[+-]?[0-9][0-9A-Fa-f]*|\([+-]?\d{1,6},[+-]?\d{1,6}\))"
_SHAPE_OPCODES = re.compile(rf"^{_OPCODE}(?:,{_OPCODE})*,?$")
_SHAPE_BODY_CHARS = re.compile(r"^[0-9A-Fa-f(),+\-]+$")


def _unsupported(extractor: str, reason: str = "unsupported_type") -> AnalysisResult:
    return AnalysisResult(
        outcome="skipped", reason=reason, content_available=False,
        evidence_source="none", extractor=extractor, model_called=False,
    )


def _read_bounded_lines(path: Path) -> tuple[list[bytes], bool] | None:
    with path.open("rb") as source:
        data = source.read(MAX_CAD_TEXT_PREFIX_BYTES + 1)
    truncated = len(data) > MAX_CAD_TEXT_PREFIX_BYTES
    if truncated:
        # The final prefix line may be incomplete. Do not count or parse it.
        prefix, separator, _ = data[:MAX_CAD_TEXT_PREFIX_BYTES].rpartition(b"\n")
        if not separator:
            return None
        data = prefix
    if not data or any(byte < 32 and byte not in (9, 10, 13) or byte == 127
                       for byte in data):
        return None
    lines = [line.rstrip(b"\r") for line in data.split(b"\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines, truncated


def has_cad_plot_signature(path: Path) -> bool:
    """Bounded first-row check before overriding a readable text log route."""
    try:
        with path.open("rb") as source:
            first = source.readline(4097)
    except OSError:
        return False
    return len(first) <= 4096 and _parse_plot_row(first.rstrip(b"\r\n")) is not None


class CadTextAnalyzer(BaseAnalyzer):
    def can_handle(self, mime_type: str, extension: str) -> bool:
        return extension.lower() in {".log", ".shp"}

    def analyze(self, file_rec: File, context: str) -> AnalysisResult:
        ext = Path(file_rec.path).suffix.lower()
        extractor = "cad_plot_log_structure" if ext == ".log" else "autocad_shp_structure"
        try:
            inspected = _read_bounded_lines(Path(file_rec.path))
        except OSError:
            return _unsupported(extractor, "unreadable_content")
        if inspected is None:
            return _unsupported(extractor)
        lines, truncated = inspected
        if ext == ".log":
            return _analyze_plot_log(lines, truncated)
        if ext == ".shp":
            return _analyze_shape_source(lines, truncated)
        return _unsupported(extractor)


def _analyze_plot_log(lines: list[bytes], truncated: bool) -> AnalysisResult:
    extractor = "cad_plot_log_structure"
    dates: set[str] = set()
    devices: set[str] = set()
    events = 0
    for raw in lines:
        parsed = _parse_plot_row(raw)
        if parsed is None:
            return _unsupported(extractor)
        date, device = parsed
        dates.add(date)
        devices.add(device)
        events += 1
    if not events:
        return _unsupported(extractor)
    scope = (f"first {MAX_CAD_TEXT_PREFIX_BYTES // 1024} KiB of file"
             if truncated else "complete file")
    description = (
        f"CAD plot-event log structure inspected in {scope}: "
        f"{events} event {'row' if events == 1 else 'rows'}, "
        f"{len(dates)} distinct recorded plot {'date' if len(dates) == 1 else 'dates'}, "
        f"{len(devices)} plot {'device' if len(devices) == 1 else 'devices'}."
    )
    if truncated:
        description += " Counts describe the inspected prefix only."
    return AnalysisResult(
        description=description, confidence=1.0, suggested_name="", tags=[],
        content_available=True, evidence_source="metadata", outcome="metadata_only",
        reason="prefix_only" if truncated else "limited_content", content_chars=0,
        extractor=extractor, model_called=False,
    )


def _normalized_plot_date(value: str) -> str | None:
    for fmt in _PLOT_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_plot_row(raw: bytes) -> tuple[str, str] | None:
    if (not raw or len(raw) > 4096 or any(
            byte < 32 and byte != 9 or byte == 127 for byte in raw)):
        return None
    try:
        fields = next(csv.reader([raw.decode("cp1255", errors="strict")], strict=True))
    except (UnicodeDecodeError, csv.Error, StopIteration):
        return None
    if (len(fields) not in (7, 8) or len(fields) == 8 and fields[7].strip()
            or not fields[0].strip().lower().endswith(".dwg")
            or not all(fields[index].strip() for index in (1, 2, 3, 4, 5, 6))
            or not _PLOT_SCALE.fullmatch(fields[6].strip())):
        return None
    date = _normalized_plot_date(fields[2].strip())
    if date is None:
        return None
    return date, fields[4].strip()


def _analyze_shape_source(lines: list[bytes], truncated: bool) -> AnalysisResult:
    extractor = "autocad_shp_structure"
    try:
        text_lines = [line.decode("ascii", errors="strict").strip() for line in lines]
    except UnicodeDecodeError:
        return _unsupported(extractor)
    if not text_lines or not text_lines[0].startswith("*0,"):
        # A GIS shapefile or an arbitrary text file is not AutoCAD SHP source.
        return _unsupported(extractor)

    seen_ids: set[int] = set()
    current_id: int | None = None
    body_parts: list[str] = []
    shape_declarations = 0
    opcode_lines = 0

    def close_declaration() -> bool:
        nonlocal shape_declarations, opcode_lines
        body = "".join(body_parts)
        # Some real shape-source exports omit a comma between adjacent
        # coordinate tuples. This normalization is solely for recognizing
        # their token boundary; it does not change or expose source opcodes.
        if not body_parts or not _SHAPE_OPCODES.fullmatch(body.replace(")(", "),(")):
            return False
        if current_id:
            shape_declarations += 1
        opcode_lines += len(body_parts)
        return True

    for line in text_lines:
        declaration = _SHAPE_DECLARATION.fullmatch(line)
        if declaration:
            if current_id is not None and not close_declaration():
                return _unsupported(extractor, "unreadable_content")
            shape_id, declared_bytes = int(declaration[1]), int(declaration[2])
            if (shape_id in seen_ids or not 0 < declared_bytes <= 65535
                    or current_id is None and shape_id != 0
                    or current_id is not None and shape_id == 0):
                return _unsupported(extractor, "unreadable_content")
            seen_ids.add(shape_id)
            current_id = shape_id
            body_parts = []
        elif current_id is not None and _SHAPE_BODY_CHARS.fullmatch(line):
            # AutoCAD source wraps long opcode streams across physical lines,
            # sometimes in the middle of a coordinate. Validate the complete
            # declaration body only when the next declaration closes it.
            body_parts.append(line)
        else:
            return _unsupported(extractor, "unreadable_content")
    if not truncated and (current_id is None or not close_declaration()):
        return _unsupported(extractor, "unreadable_content")
    if opcode_lines < 2 or shape_declarations < 1:
        return _unsupported(extractor, "unreadable_content")
    scope = (f"first {MAX_CAD_TEXT_PREFIX_BYTES // 1024} KiB of file"
             if truncated else "complete file")
    description = (
        f"AutoCAD SHP shape/font source structure inspected in {scope}: "
        f"{shape_declarations} shape declarations with opcode lines "
        f"(plus one *0 font header), "
        f"{opcode_lines} opcode lines inspected. No geometry was interpreted."
    )
    if truncated:
        description += " Counts describe the inspected prefix only."
    return AnalysisResult(
        description=description, confidence=1.0, suggested_name="", tags=[],
        content_available=True, evidence_source="metadata", outcome="metadata_only",
        reason="prefix_only" if truncated else "limited_content", content_chars=0,
        extractor=extractor, model_called=False,
    )

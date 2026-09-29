"""Bounded, hash-bound photo preservation evidence and keeper recommendations.

Pixel dimensions measure resolution, not native detail or original quality.
These recommendations never authorize disposal of a visually similar file.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any


RAW_EXTENSIONS = frozenset({
    ".3fr", ".arw", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf",
    ".fff", ".iiq", ".k25", ".kdc", ".mef", ".mos", ".mrw", ".nef",
    ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2",
    ".srf", ".srw", ".x3f",
})
PHOTO_EXTENSIONS = RAW_EXTENSIONS | frozenset({
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp",
    ".gif", ".heic", ".heif", ".avif", ".jxl",
})
TEXT_FIELDS = frozenset({
    "camera_make", "camera_model", "lens_make", "lens_model", "camera_serial",
    "lens_serial", "artist", "copyright", "description",
})
DATE_FIELDS = frozenset({"capture_time", "digitized_time", "modified_time"})
OFFSET_FIELDS = frozenset({"capture_offset", "digitized_offset", "modified_offset"})
POSITIVE_FIELDS = frozenset({"exposure_time", "f_number", "focal_length", "iso"})
MEANINGFUL_FIELDS = (TEXT_FIELDS | DATE_FIELDS | OFFSET_FIELDS | POSITIVE_FIELDS
                     | {"orientation", "gps_latitude", "gps_longitude",
                        "gps_altitude", "capture_subseconds"})
MAX_METADATA_CHARS = 64 * 1024
MAX_FIELD_CHARS = 1024
MAX_DIMENSION = 1_000_000
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_EMPTY_TEXT = frozenset({"", "unknown", "undefined", "none", "null", "n/a", "not available", "unspecified"})


def _value(file: Any, name: str, default=None):
    return file.get(name, default) if isinstance(file, dict) else getattr(file, name, default)


def _extension(file: Any) -> str:
    extension = _value(file, "extension")
    return (extension or PurePosixPath(str(_value(file, "path", "")).replace("\\", "/")).suffix).lower()


def is_photo(file: Any) -> bool:
    return bool(file is not None and (
        str(_value(file, "mime_type", "") or "").lower().startswith("image/")
        or _extension(file) in PHOTO_EXTENSIONS
    ))


def _clean_text(value: Any, limit=MAX_FIELD_CHARS) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    result = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())
    return result or None


def _valid_field(name: str, value: Any):
    if name in TEXT_FIELDS:
        if (not isinstance(value, str) or len(value) > MAX_FIELD_CHARS
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        return value.strip() or None
    if name in DATE_FIELDS:
        if not isinstance(value, str) or len(value) > 32:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.isoformat() if parsed.year > 1 else None
    if name in OFFSET_FIELDS:
        if not isinstance(value, str) or not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", value):
            return None
        return value if value[1:3] != "14" or value[4:] == "00" else None
    if name == "capture_subseconds":
        return (value.rstrip("0") or "0") if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value) else None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        return None
    if name in POSITIVE_FIELDS:
        upper = {"exposure_time": 86400, "f_number": 1024, "focal_length": 100000, "iso": 1000000}[name]
        if value <= 0 or value > upper:
            return None
        if name == "iso":
            return int(value) if value == int(value) else None
        return value
    if name == "orientation":
        return int(value) if value == int(value) and 1 <= value <= 8 else None
    if name == "gps_latitude":
        return value if -90 <= value <= 90 else None
    if name == "gps_longitude":
        return value if -180 <= value <= 180 else None
    if name == "gps_altitude":
        return value if -100_000 <= value <= 100_000 else None
    return None


def photo_evidence(file: Any) -> dict:
    """Return a small, JSON-safe evidence record without touching source files.

    Absence is established only by a complete extraction. Legacy, stale,
    malformed or incomplete records cannot stand in for metadata-poor photos.
    """
    photo = is_photo(file)
    result = {
        "is_photo": photo, "status": "unknown" if photo else "not_photo",
        "width": None, "height": None, "display_width": None, "display_height": None,
        "pixels": None, "megapixels": None, "format": None, "mode": None,
        "fields": {}, "meaningful_field_count": 0, "warnings": [],
    }
    if not photo:
        return result
    raw = _value(file, "photo_metadata")
    if not isinstance(raw, str) or not raw:
        result["warnings"] = ["Photo evidence has not been extracted."]
        return result
    if len(raw) > MAX_METADATA_CHARS:
        result["warnings"] = ["Photo evidence exceeds the supported size."]
        return result
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        result["warnings"] = ["Photo evidence is malformed."]
        return result
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        result["warnings"] = ["Photo evidence version is unavailable or unsupported."]
        return result
    status = data.get("status")
    if status not in {"complete", "partial", "unavailable", "unsupported"}:
        result["warnings"] = ["Photo evidence status is invalid."]
        return result
    warnings = data.get("warnings", [])
    result["warnings"] = [text for item in warnings[:8]
                          if (text := _clean_text(item, 240))] if isinstance(warnings, list) else []
    if status in {"unavailable", "unsupported"}:
        result["status"] = status
        return result
    indexed_hash, source_hash = _value(file, "hash_sha256"), data.get("source_sha256")
    if not (isinstance(indexed_hash, str) and _SHA256.fullmatch(indexed_hash)
            and isinstance(source_hash, str) and source_hash.lower() == indexed_hash.lower()):
        result["warnings"] = ["Photo evidence is stale or is not bound to the indexed file hash."]
        return result
    result["status"] = status
    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        result["status"] = "partial"
        result["warnings"].append("The metadata inventory is malformed.")
    invalid = False
    for name in sorted(MEANINGFUL_FIELDS):
        if name in fields:
            if name in TEXT_FIELDS and isinstance(fields[name], str) and fields[name].strip().casefold() in _EMPTY_TEXT:
                continue
            value = _valid_field(name, fields[name])
            if value is None:
                invalid = True
            else:
                result["fields"][name] = value
    if invalid:
        result["status"] = "partial"
        result["warnings"].append("Some metadata values are invalid or unbounded.")
    result["meaningful_field_count"] = len(result["fields"])
    width, height = data.get("width"), data.get("height")
    if (type(width) is int and type(height) is int
            and 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        result.update(width=width, height=height, display_width=width, display_height=height,
                      pixels=width * height, megapixels=round(width * height / 1_000_000, 3))
        if result["fields"].get("orientation") in {5, 6, 7, 8}:
            result["display_width"], result["display_height"] = height, width
    else:
        result["status"] = "partial"
        result["warnings"].append("Pixel dimensions are unavailable or invalid.")
    result["format"] = _clean_text(data.get("format"), 32)
    result["mode"] = _clean_text(data.get("mode"), 32)
    if result["format"]:
        result["format"] = result["format"].upper()
    if not result["format"] or not result["mode"]:
        result["status"] = "partial"
        result["warnings"].append("Image format or color mode is unavailable.")
    result["warnings"] = result["warnings"][:10]
    return result


def keeper_sort_key(file: Any) -> tuple:
    """Choose a reference in linear work; comparison explains any tradeoff.

    Resolution and valid metadata replace dates, paths and byte size for
    photos. A reference with more pixels may still lack unique metadata held
    by another member; it is not permission to discard that member.
    """
    path = str(_value(file, "path", "") or "")
    stable_path = path.replace("\\", "/")
    tie = (stable_path.casefold(), stable_path, _value(file, "id", 0) or 0)
    evidence = photo_evidence(file)
    if evidence["is_photo"]:
        return (0, -(evidence["pixels"] or 0), -evidence["meaningful_field_count"],
                0 if evidence["status"] == "complete" else 1, *tie)
    date = (_value(file, "date_best") or _value(file, "date_modified")
            or _value(file, "date_created") or datetime(9999, 1, 1))
    return (1, date, -len(path), -(_value(file, "size_bytes", 0) or 0), *tie)


def _same_value(left, right) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def compare_photos(candidate: Any, keeper: Any) -> dict | None:
    """Explain preservation evidence against the selected keeper, not similarity.

    ``recommended`` means the keeper dominates measured preservation evidence;
    it does not claim native quality, originality, or interchangeability.
    """
    left, right = photo_evidence(candidate), photo_evidence(keeper)
    if not left["is_photo"] and not right["is_photo"]:
        return None
    left_fields, right_fields = left["fields"], right["fields"]
    candidate_unique = sorted(left_fields.keys() - right_fields.keys())
    keeper_unique = sorted(right_fields.keys() - left_fields.keys())
    conflicts = sorted(name for name in left_fields.keys() & right_fields.keys()
                       if not _same_value(left_fields[name], right_fields[name]))
    result = {
        "status": "unknown", "candidate": left, "keeper": right, "reasons": [],
        "candidate_unique_fields": candidate_unique, "keeper_unique_fields": keeper_unique,
        "conflicting_fields": conflicts, "requires_review": True,
    }
    reasons = result["reasons"]
    left_hash, right_hash = _value(candidate, "hash_sha256"), _value(keeper, "hash_sha256")
    if (isinstance(left_hash, str) and _SHA256.fullmatch(left_hash)
            and isinstance(right_hash, str) and left_hash.lower() == right_hash.lower()):
        result.update(status="equivalent", requires_review=False)
        reasons.append("Indexed SHA-256 hashes match: image bytes and embedded metadata are identical.")
        return result
    if (_extension(candidate) in RAW_EXTENSIONS or _extension(keeper) in RAW_EXTENSIONS
            or left["is_photo"] != right["is_photo"]):
        result["status"] = "variant"
        reasons.append("RAW files, exports, and different media types may preserve distinct information; retain both for review.")
        return result
    if left["status"] != "complete" or right["status"] != "complete":
        reasons.append("Complete, current photo evidence is unavailable for one or both files; missing metadata is not proven absent.")
        return result
    if conflicts:
        result["status"] = "tradeoff"
        reasons.append("Metadata values conflict: " + ", ".join(conflicts) + ". Retain both for review.")
    left_ratio = left["display_width"] / left["display_height"]
    right_ratio = right["display_width"] / right["display_height"]
    variants = []
    if not math.isclose(left_ratio, right_ratio, rel_tol=0.015):
        variants.append("Different oriented aspect ratios may indicate a crop or different composition.")
    if left["format"] != right["format"] or left["mode"] != right["mode"]:
        variants.append("Different image formats or color modes may preserve distinct rendering information.")
    left_phash, right_phash = _value(candidate, "hash_perceptual"), _value(keeper, "hash_perceptual")
    if left_phash and right_phash and left_phash != right_phash:
        variants.append("Perceptual fingerprints differ; edits, compression, or different content need visual review.")
    if variants:
        result["status"] = "variant"
        reasons.extend(variants)
    higher = right["pixels"] > left["pixels"]
    lower = right["pixels"] < left["pixels"]
    reasons.append(
        f"Keeper: {right['display_width']} x {right['display_height']} ({right['megapixels']:g} MP); "
        f"candidate: {left['display_width']} x {left['display_height']} ({left['megapixels']:g} MP)."
    )
    if candidate_unique:
        reasons.append("Only the candidate retains: " + ", ".join(candidate_unique) + ".")
    if keeper_unique:
        reasons.append("Only the keeper retains: " + ", ".join(keeper_unique) + ".")
    if not variants and not conflicts:
        if candidate_unique or lower:
            result["status"] = "tradeoff"
            reasons.append("The selected keeper does not preserve all measured advantages; retain both for review.")
        elif higher or keeper_unique:
            result["status"] = "recommended"
            reasons.append("The keeper retains at least as much resolution and all recorded candidate metadata with matching values.")
        else:
            result["status"] = "equivalent"
            reasons.append("Measured resolution and recorded metadata match; image interchangeability still needs visual review.")
    reasons.append("Higher resolution does not prove more original detail; similarity does not authorize disposal.")
    return result


def photo_comparison_reason(candidate: Any, keeper: Any) -> str:
    comparison = compare_photos(candidate, keeper)
    if comparison is None:
        return ""
    return " Photo preservation " + comparison["status"] + ": " + " ".join(comparison["reasons"])

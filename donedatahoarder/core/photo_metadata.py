"""Read bounded, structured photo evidence without decoding pixels or editing files.

This records preservation evidence, not a quality score or proof that two photos
are interchangeable. A complete result with no fields means inspected absence;
partial/unavailable/unsupported results never mean a metadata-poor original.
"""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re
import stat
import struct
import warnings
import zlib

from PIL import Image, UnidentifiedImageError


VERSION = 1
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 1024
MAX_WARNINGS = 8
# Windows can expose cloud placeholders as offline or recall-on-open/data-access.
# Inspecting stat attributes does not open file content or request hydration.
CLOUD_RECALL_MASK = 0x1000 | 0x40000 | 0x400000
RAW_EXTENSIONS = frozenset({
    ".3fr", ".arw", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf",
    ".fff", ".iiq", ".k25", ".kdc", ".mef", ".mos", ".mrw", ".nef",
    ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2",
    ".srf", ".srw", ".x3f",
})
PHOTO_EXTENSIONS = RAW_EXTENSIONS | frozenset({
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp",
    ".heic", ".heif", ".avif", ".gif",
})
SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "TIFF", "BMP"})


def is_cloud_placeholder(info) -> bool:
    """Recognized offline/recall content must be made local by its owner first."""
    return bool(getattr(info, "st_file_attributes", 0) & CLOUD_RECALL_MASK)


def source_identity(info) -> tuple:
    """Best-effort read boundary identity; timestamps do not rank keeper quality."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def unavailable_metadata(reason: str, *, source_sha256: str | None = None) -> dict:
    return {
        "version": VERSION, "status": "unavailable", "width": None,
        "height": None, "format": None, "mode": None, "fields": {},
        "warnings": [reason[:160]], "source_sha256": source_sha256,
    }


class _ReadLimit(OSError):
    pass


class _BoundedReader:
    """Bound aggregate header/IFD reads, including seeks to out-of-line values."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_READ_BYTES

    def read(self, count=-1):
        if count < 0 or count > self.remaining:
            raise _ReadLimit("Photo header metadata exceeds the read limit")
        value = self.stream.read(count)
        self.remaining -= len(value)
        return value

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()

    def readline(self, count=-1):
        count = self.remaining + 1 if count < 0 else min(count, self.remaining + 1)
        value = self.stream.readline(count)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise _ReadLimit("Photo header metadata exceeds the read limit")
        return value


def _warn(result: dict, message: str) -> None:
    result["status"] = "partial"
    if len(result["warnings"]) < MAX_WARNINGS and message not in result["warnings"]:
        result["warnings"].append(message[:160])


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValueError("not text")
    value = value.strip("\x00 \t\r\n")
    if not value or value.casefold() in {"unknown", "undefined", "none", "null", "n/a", "not available", "unspecified"}:
        return None
    if len(value) > MAX_TEXT_CHARS or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("unbounded text or control characters")
    return value


def _number(value, *, low=0.0, high=1_000_000.0, zero=False) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (str, bytes, bool)):
        raise ValueError("not a number")
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError("not a scalar")
        value = value[0]
    number = float(value)
    if not math.isfinite(number) or number < low or number > high or (number == 0 and not zero):
        raise ValueError("out of range")
    return float(format(number, ".12g"))


def _integer(value, *, low=1, high=1_000_000):
    number = _number(value, low=low, high=high, zero=(low == 0))
    if number is None:
        return None
    if number != int(number):
        raise ValueError("not an integer")
    return int(number)


def _date(value) -> str | None:
    text = _text(value)
    return datetime.strptime(text, "%Y:%m:%d %H:%M:%S").isoformat() if text else None


def _offset(value) -> str | None:
    text = _text(value)
    if text and not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", text):
        raise ValueError("invalid UTC offset")
    if text and text[1:3] == "14" and text[4:] != "00":
        raise ValueError("invalid UTC offset")
    return text


def _subseconds(value) -> str | None:
    text = _text(value)
    if text and not re.fullmatch(r"\d{1,9}", text):
        raise ValueError("invalid capture subseconds")
    return text.rstrip("0") or "0" if text else None


_FIELD_TAGS = {
    36867: ("capture_time", _date), 36868: ("digitized_time", _date),
    306: ("modified_time", _date), 36881: ("capture_offset", _offset),
    36882: ("digitized_offset", _offset), 36880: ("modified_offset", _offset),
    37521: ("capture_subseconds", _subseconds),
    271: ("camera_make", _text), 272: ("camera_model", _text),
    42035: ("lens_make", _text), 42036: ("lens_model", _text),
    42033: ("camera_serial", _text), 42037: ("lens_serial", _text),
    33434: ("exposure_time", lambda value: _number(value, high=86400)),
    33437: ("f_number", lambda value: _number(value, high=1024)),
    34855: ("iso", _integer),
    37386: ("focal_length", lambda value: _number(value, high=100000)),
    274: ("orientation", lambda value: _integer(value, high=8)),
    315: ("artist", _text), 33432: ("copyright", _text),
    270: ("description", _text),
}


def _coordinate(raw, ref, *, latitude: bool) -> float:
    if not isinstance(raw, (tuple, list)) or len(raw) != 3:
        raise ValueError("invalid GPS coordinate")
    degrees, minutes, seconds = (
        _number(value, high=180, zero=True) for value in raw
    )
    if None in (degrees, minutes, seconds) or minutes >= 60 or seconds >= 60:
        raise ValueError("invalid GPS coordinate")
    sign = _text(ref)
    if sign not in (("N", "S") if latitude else ("E", "W")):
        raise ValueError("missing GPS reference")
    result = degrees + minutes / 60 + seconds / 3600
    if result > (90 if latitude else 180):
        raise ValueError("out of range GPS coordinate")
    return round(-result if sign in ("S", "W") else result, 8)


def _extract_fields(exif, result: dict) -> None:
    fields = result["fields"]
    sources = [exif]
    if 34665 in exif:
        try:
            sources.append(exif.get_ifd(34665))
        except Exception:
            _warn(result, "EXIF capture metadata could not be fully read")
    for source in sources:
        for tag, (key, normalize) in _FIELD_TAGS.items():
            try:
                if tag not in source:
                    continue
                value = normalize(source[tag])
                if value is not None:
                    if key in fields and fields[key] != value:
                        _warn(result, f"Conflicting EXIF {key}")
                    else:
                        fields[key] = value
            except Exception:
                _warn(result, f"Invalid or oversized EXIF {key}")
    if 34853 not in exif:
        return
    try:
        gps = exif.get_ifd(34853)
        for tag, ref_tag, key, latitude in (
            (2, 1, "gps_latitude", True), (4, 3, "gps_longitude", False),
        ):
            if tag in gps or ref_tag in gps:
                try:
                    fields[key] = _coordinate(gps.get(tag), gps.get(ref_tag), latitude=latitude)
                except Exception:
                    _warn(result, f"Invalid EXIF {key}")
        if 6 in gps:
            try:
                altitude = _number(gps[6], high=100000, zero=True)
                ref = gps.get(5, 0)
                if isinstance(ref, bytes) and len(ref) == 1:
                    ref = ref[0]
                if ref not in (0, 1) or altitude is None:
                    raise ValueError("invalid altitude")
                fields["gps_altitude"] = -altitude if ref == 1 else altitude
            except Exception:
                _warn(result, "Invalid EXIF gps_altitude")
    except Exception:
        _warn(result, "EXIF GPS metadata could not be fully read")


def _exif_bytes(payload: bytes, result: dict) -> None:
    try:
        exif = Image.Exif()
        exif.load(payload)
        _extract_fields(exif, result)
    except Exception:
        _warn(result, "EXIF metadata could not be fully read")


def _png_metadata(reader: _BoundedReader, size: int, result: dict) -> None:
    """Inspect PNG chunks (www.w3.org/TR/png-3/), seeking past pixel data."""
    reader.seek(8)
    result.update(format="PNG")
    have_pixels = False
    for chunk_index in range(4096):
        header = reader.read(8)
        if len(header) != 8:
            raise ValueError("truncated PNG chunk")
        length, kind = struct.unpack(">I4s", header)
        end = reader.tell() + length + 4
        if end > size:
            raise ValueError("truncated PNG chunk")
        if chunk_index == 0 and kind != b"IHDR":
            raise ValueError("PNG header must be first")
        if kind == b"IHDR":
            if length != 13 or result["width"] is not None:
                raise ValueError("invalid PNG header")
            payload = reader.read(13)
            crc = reader.read(4)
            if len(crc) != 4 or zlib.crc32(kind + payload) != int.from_bytes(crc, "big"):
                raise ValueError("invalid PNG header checksum")
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            modes = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}
            if not width or not height or color not in modes or bits not in (1, 2, 4, 8, 16):
                raise ValueError("invalid PNG dimensions or mode")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise ValueError("invalid PNG format")
            if (color in (2, 4, 6) and bits not in (8, 16)) or (color == 3 and bits == 16):
                raise ValueError("invalid PNG color depth")
            mode = "I;16" if color == 0 and bits == 16 else modes[color]
            result.update(width=width, height=height, mode=mode)
        elif kind == b"eXIf":
            payload = reader.read(length)
            crc = reader.read(4)
            if len(crc) != 4 or zlib.crc32(kind + payload) != int.from_bytes(crc, "big"):
                _warn(result, "PNG EXIF checksum is invalid; capture metadata was not accepted")
            else:
                _exif_bytes(payload, result)
        elif kind == b"IDAT":
            have_pixels = have_pixels or length > 0
        elif kind in {b"tEXt", b"zTXt", b"iTXt"}:
            # Text may hold capture information or XMP beyond EXIF.
            _warn(result, "Additional PNG text/XMP metadata has not been compared")
        elif kind == b"acTL":
            _warn(result, "Multiple image frames require individual review")
        elif kind == b"IEND":
            if not result["width"] or not have_pixels or length != 0:
                raise ValueError("invalid PNG ending")
            if end < size:
                _warn(result, "Additional data after the PNG image has not been compared")
            return
        reader.seek(end)
    raise _ReadLimit("Too many PNG metadata chunks")


def _webp_metadata(reader: _BoundedReader, size: int, result: dict) -> None:
    """Inspect developers.google.com/speed/webp/docs/riff_container headers."""
    reader.seek(0)
    header = reader.read(12)
    total = struct.unpack("<I", header[4:8])[0] + 8
    if total > size or total < 12:
        raise ValueError("invalid WebP container")
    if total < size:
        _warn(result, "Additional data after the WebP container has not been compared")
    result.update(format="WEBP", mode="RGB")
    have_pixels = False
    for _ in range(4096):
        if reader.tell() == total:
            if not result["width"] or not result["height"] or not have_pixels:
                raise ValueError("missing WebP dimensions")
            return
        header = reader.read(8)
        if len(header) != 8:
            raise ValueError("truncated WebP chunk")
        kind, length = struct.unpack("<4sI", header)
        end = reader.tell() + length + (length % 2)
        if end > total:
            raise ValueError("truncated WebP chunk")
        if kind in {b"VP8 ", b"VP8L", b"ANMF"}:
            have_pixels = have_pixels or length > 0
        if kind == b"VP8X":
            data = reader.read(min(length, 10))
            if len(data) != 10:
                raise ValueError("invalid WebP extended header")
            result.update(width=1 + int.from_bytes(data[4:7], "little"),
                          height=1 + int.from_bytes(data[7:10], "little"),
                          mode="RGBA" if data[0] & 0x10 else "RGB")
            if data[0] & 2:
                _warn(result, "Multiple image frames require individual review")
        elif kind == b"VP8 " and not result["width"]:
            data = reader.read(min(length, 10))
            if len(data) != 10 or data[3:6] != b"\x9d\x01\x2a":
                raise ValueError("invalid WebP lossy header")
            result.update(width=int.from_bytes(data[6:8], "little") & 0x3fff,
                          height=int.from_bytes(data[8:10], "little") & 0x3fff)
        elif kind == b"VP8L" and not result["width"]:
            data = reader.read(min(length, 5))
            if len(data) != 5 or data[0] != 0x2f:
                raise ValueError("invalid WebP lossless header")
            packed = int.from_bytes(data[1:], "little")
            result.update(width=(packed & 0x3fff) + 1,
                          height=((packed >> 14) & 0x3fff) + 1,
                          mode="RGBA" if packed & (1 << 28) else "RGB")
        elif kind == b"EXIF":
            _exif_bytes(reader.read(length), result)
        elif kind == b"XMP ":
            _warn(result, "Additional XMP metadata has not been compared")
        elif kind in {b"ANIM", b"ANMF"}:
            _warn(result, "Multiple image frames require individual review")
        reader.seek(end)
    raise _ReadLimit("Too many WebP metadata chunks")


def extract_photo_metadata(path: Path, *, source_sha256: str | None = None) -> dict:
    """Inspect header dimensions and a validated EXIF inventory, without load().

    The caller binds ``source_sha256`` to its hash read and checks stat identity
    around that whole operation. We also check identity around our own reads.
    RAW/developed equivalence, XMP/IPTC and sidecar merging are not inferred.
    """
    path = Path(path)
    result = unavailable_metadata("Photo metadata unavailable", source_sha256=source_sha256)
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return unavailable_metadata("Source is not a regular file")
        if is_cloud_placeholder(before):
            return unavailable_metadata("Cloud content is not local; make it available offline to inspect")
        if path.suffix.lower() in RAW_EXTENSIONS:
            result.update(status="unsupported", warnings=["RAW capture metadata requires a supported RAW reader"])
            return result
        if path.suffix.lower() in {".avif", ".heif", ".heic"}:
            result.update(status="unsupported", warnings=["This format needs a supported header-only photo metadata reader"])
            return result
        result.update(status="complete", warnings=[])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with path.open("rb") as stream:
                reader = _BoundedReader(stream)
                signature = reader.read(12)
                reader.seek(0)
                if signature.startswith(b"\x89PNG\r\n\x1a\n"):
                    _png_metadata(reader, before.st_size, result)
                elif signature.startswith(b"RIFF") and signature[8:] == b"WEBP":
                    _webp_metadata(reader, before.st_size, result)
                else:
                    _pillow_metadata(reader, result)
            if caught:
                _warn(result, "Image header or EXIF parser reported incomplete or malformed metadata")
        if (result["width"] and result["height"] and Image.MAX_IMAGE_PIXELS
                and result["width"] * result["height"] > Image.MAX_IMAGE_PIXELS):
            _warn(result, "Image dimensions exceed the normal inspection limit")
        if source_identity(before) != source_identity(path.stat()):
            return unavailable_metadata("File changed while reading photo metadata")
        return result
    except _ReadLimit:
        _warn(result, "Photo header metadata exceeds the bounded read limit")
        return result
    except UnidentifiedImageError:
        return unavailable_metadata("Image header is unavailable or corrupt", source_sha256=source_sha256)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return unavailable_metadata("Image header or EXIF could not be read", source_sha256=source_sha256)


def _pillow_metadata(reader: _BoundedReader, result: dict) -> None:
    with Image.open(reader) as image:
        result.update(width=image.width, height=image.height, format=image.format, mode=image.mode)
        if image.format not in SUPPORTED_FORMATS:
            result.update(status="unsupported", warnings=["This image format is not supported for photo metadata comparison"])
            return
        exif = image.getexif()
        _extract_fields(exif, result)
        # Known independent metadata stores must not masquerade as complete
        # absence when this EXIF-only reader misses them.
        if (any(key.lower() in {"xmp", "xml:com.adobe.xmp", "iptc"} for key in image.info)
                or any(tag in exif for tag in (700, 33723))):
            _warn(result, "Additional XMP/IPTC metadata has not been compared")
        if image.format == "JPEG" and any(marker == "APP13" for marker, _ in image.applist):
            _warn(result, "Additional Photoshop/IPTC metadata has not been compared")
        if getattr(image, "is_animated", False):
            _warn(result, "Multiple image frames require individual review")

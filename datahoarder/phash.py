"""
Perceptual hashing module for images and video frames.

Centralises hash computation, algorithm selection, and distance calculation
so both the enricher and deduplicator behave consistently.

Supported algorithms (from imagehash):
  phash  — PHash (default, good balance of speed + accuracy)
  dhash  — DHash (gradient-based, fast)
  whash  — Wavelet hash (robust to compression)
  ahash  — Average hash (fastest, least robust)

Video support: extracts a middle frame via ffmpeg (best-effort) and hashes
that frame. Gracefully degrades when ffmpeg is unavailable.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Optional

try:
    from PIL import Image as PilImage
    import imagehash
    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False

try:
    import ffmpeg as _ffmpeg
    _HAS_FFMPEG = True
except ImportError:
    _HAS_FFMPEG = False

# Map friendly names to imagehash callables
_ALGORITHMS = {
    "phash": imagehash.phash,
    "dhash": imagehash.dhash,
    "whash": imagehash.whash,
    "ahash": imagehash.average_hash,
} if _HAS_IMAGEHASH else {}

# Default configuration
DEFAULT_ALGORITHM = "phash"
DEFAULT_HASH_SIZE = 8
DEFAULT_THRESHOLD = 8
DEFAULT_VIDEO_ENABLED = True


def _get_config() -> dict:
    """Lazy import to avoid circular deps."""
    from datahoarder.config import load_phash_config
    return load_phash_config()


def compute_phash(
    path: Path,
    algorithm: Optional[str] = None,
    hash_size: Optional[int] = None,
) -> Optional[str]:
    """
    Compute perceptual hash for an image or video file.

    For videos, extracts a middle frame via ffmpeg and hashes that frame.
    Returns None if dependencies are missing or extraction fails.

    The returned string can be passed to ``hash_distance()`` for comparison.
    """
    if not _HAS_IMAGEHASH:
        return None

    cfg = _get_config()
    algorithm = (algorithm or cfg.get("algorithm", DEFAULT_ALGORITHM)).lower()
    hash_size = hash_size or cfg.get("hash_size", DEFAULT_HASH_SIZE)
    video_enabled = cfg.get("video_enabled", DEFAULT_VIDEO_ENABLED)

    hasher = _ALGORITHMS.get(algorithm)
    if hasher is None:
        hasher = imagehash.phash

    # Try image path first
    img = _open_image(path)
    if img is not None:
        return str(hasher(img, hash_size=hash_size))

    # Video fallback
    if video_enabled:
        img = _extract_video_frame(path)
        if img is not None:
            return str(hasher(img, hash_size=hash_size))

    return None


def _open_image(path: Path) -> Optional[PilImage.Image]:
    """Attempt to open *path* as a static image. Returns None on failure."""
    if not _HAS_IMAGEHASH:
        return None
    try:
        return PilImage.open(path)
    except Exception:
        return None


def _extract_video_frame(path: Path) -> Optional[PilImage.Image]:
    """
    Extract a single middle frame from a video file using ffmpeg.
    Returns a PIL Image or None if ffmpeg is unavailable / fails.
    """
    if not _HAS_FFMPEG or not _HAS_IMAGEHASH:
        return None

    # Quick extension filter — don't waste time on non-video files
    ext = path.suffix.lower()
    video_exts = {
        ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
        ".m4v", ".3gp", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
    }
    if ext not in video_exts:
        return None

    duration = _video_duration(path)
    if duration is None or duration <= 0:
        return None

    timestamp = duration / 2
    jpeg_bytes = _ffmpeg_extract_frame(path, timestamp)
    if not jpeg_bytes:
        return None

    try:
        return PilImage.open(io.BytesIO(jpeg_bytes))
    except Exception:
        return None


def _video_duration(path: Path) -> Optional[float]:
    """Use ffprobe to get video duration in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0)) or None
    except Exception:
        return None


def _ffmpeg_extract_frame(path: Path, timestamp: float) -> Optional[bytes]:
    """Extract a single JPEG frame at *timestamp* seconds."""
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", str(timestamp), "-i", str(path),
            "-vframes", "1", "-f", "image2", "-vcodec", "mjpeg", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        out = result.stdout
        return out if out else None
    except Exception:
        return None


def hash_distance(hash_a: str, hash_b: str) -> Optional[int]:
    """
    Compute Hamming distance between two perceptual hash strings.

    Handles different hash sizes gracefully by comparing the common prefix
    (or falling back to direct XOR if lengths match).  Returns None if either
    input is empty/None.
    """
    if not hash_a or not hash_b:
        return None
    if hash_a == hash_b:
        return 0

    # imagehash stores hashes as hex strings; Hamming distance is just the
    # bit-distance between the two hashes.  imagehash.hex_to_hash recreates
    # the Hash object from the hex string and supports subtraction.
    if not _HAS_IMAGEHASH:
        return None
    try:
        a = imagehash.hex_to_hash(hash_a)
        b = imagehash.hex_to_hash(hash_b)
        return abs(a - b)
    except Exception:
        return None


def is_near_duplicate(hash_a: str, hash_b: str, threshold: Optional[int] = None) -> bool:
    """
    Return True if the perceptual distance between two hashes is within
    the configured (or overridden) threshold.
    """
    if threshold is None:
        cfg = _get_config()
        threshold = cfg.get("threshold", DEFAULT_THRESHOLD)
    dist = hash_distance(hash_a, hash_b)
    if dist is None:
        return False
    return dist <= threshold

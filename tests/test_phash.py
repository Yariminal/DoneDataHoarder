"""
Tests for datahoarder.phash module.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from datahoarder import config, phash


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def test_load_phash_config_defaults(monkeypatch, tmp_path):
    """When file is missing, load_phash_config returns validated defaults."""
    fake_path = tmp_path / "phash_config.json"
    monkeypatch.setattr(config, "_DEFAULT_PHASH_CONFIG_FILE", fake_path)
    cfg = config.load_phash_config(fake_path)
    assert cfg["algorithm"] == "phash"
    assert cfg["hash_size"] == 8
    assert cfg["threshold"] == 8
    assert cfg["video_enabled"] is True


def test_load_phash_config_invalid_algorithm(monkeypatch, tmp_path):
    """Invalid algorithm falls back to phash."""
    fake_path = tmp_path / "phash_config.json"
    fake_path.write_text(json.dumps({"algorithm": "bogus", "hash_size": 16}))
    cfg = config.load_phash_config(fake_path)
    assert cfg["algorithm"] == "phash"
    assert cfg["hash_size"] == 16


def test_load_phash_config_invalid_threshold(monkeypatch, tmp_path):
    """Non-int threshold falls back to 8."""
    fake_path = tmp_path / "phash_config.json"
    fake_path.write_text(json.dumps({"threshold": "abc"}))
    cfg = config.load_phash_config(fake_path)
    assert cfg["threshold"] == 8


def test_save_and_roundtrip_phash_config(tmp_path):
    """Save + reload preserves values."""
    fake_path = tmp_path / "phash_config.json"
    data = {"algorithm": "dhash", "hash_size": 16, "threshold": 12, "video_enabled": False}
    config.save_phash_config(data, fake_path)
    cfg = config.load_phash_config(fake_path)
    assert cfg == data


# ---------------------------------------------------------------------------
# Hash distance
# ---------------------------------------------------------------------------

def test_hash_distance_identical():
    """Identical hex strings have distance 0."""
    h = "aaaaaaaaaaaaaaaa"
    assert phash.hash_distance(h, h) == 0


def test_hash_distance_none_input():
    """None / empty inputs return None."""
    assert phash.hash_distance(None, "aaaa") is None
    assert phash.hash_distance("aaaa", "") is None
    assert phash.hash_distance("", "") is None


def test_hash_distance_different_hashes():
    """Two different valid hashes should return a positive integer distance."""
    # These are two random 64-bit phash hex strings (hash_size=8 → 64 bits → 16 hex chars)
    a = "0000000000000000"
    b = "ffffffffffffffff"
    dist = phash.hash_distance(a, b)
    assert isinstance(dist, int)
    assert dist > 0


def test_is_near_duplicate_threshold():
    """is_near_duplicate respects the threshold override."""
    a = "0000000000000000"
    b = "0000000000000001"
    # Distance between these two is 1
    assert phash.is_near_duplicate(a, b, threshold=1) is True
    assert phash.is_near_duplicate(a, b, threshold=0) is False


# ---------------------------------------------------------------------------
# compute_phash (image path)
# ---------------------------------------------------------------------------

def test_compute_phash_missing_deps(monkeypatch):
    """When imagehash is missing, compute_phash returns None."""
    monkeypatch.setattr(phash, "_HAS_IMAGEHASH", False)
    assert phash.compute_phash(Path("/fake/img.jpg")) is None


def test_compute_phash_image_success(monkeypatch, tmp_path):
    """A real PIL image yields a non-None hash string."""
    from PIL import Image

    img_path = tmp_path / "test.png"
    img = Image.new("RGB", (64, 64), color="red")
    img.save(img_path)

    # Ensure _HAS_IMAGEHASH is True
    monkeypatch.setattr(phash, "_HAS_IMAGEHASH", True)
    # Ensure _get_config returns defaults
    monkeypatch.setattr(phash, "_get_config", lambda: {
        "algorithm": "phash", "hash_size": 8, "threshold": 8, "video_enabled": True,
    })

    h = phash.compute_phash(img_path)
    assert h is not None
    assert isinstance(h, str)
    assert len(h) == 16  # 8x8 phash → 64 bits → 16 hex chars


def test_compute_phash_algorithm_override(monkeypatch, tmp_path):
    """Override algorithm via kwarg."""
    from PIL import Image

    img_path = tmp_path / "test.png"
    Image.new("RGB", (64, 64), color="blue").save(img_path)

    monkeypatch.setattr(phash, "_HAS_IMAGEHASH", True)
    monkeypatch.setattr(phash, "_get_config", lambda: {
        "algorithm": "phash", "hash_size": 8, "threshold": 8, "video_enabled": True,
    })

    h = phash.compute_phash(img_path, algorithm="ahash")
    assert h is not None


# ---------------------------------------------------------------------------
# Video frame extraction (best-effort)
# ---------------------------------------------------------------------------

def test_extract_video_frame_skips_non_video(tmp_path):
    """Non-video extensions are rejected early."""
    txt = tmp_path / "not_a_video.txt"
    txt.write_text("hello")
    assert phash._extract_video_frame(txt) is None


def test_extract_video_frame_no_ffmpeg(monkeypatch):
    """When ffmpeg-python is not installed, returns None."""
    monkeypatch.setattr(phash, "_HAS_FFMPEG", False)
    fake_video = Path("/fake/video.mp4")
    assert phash._extract_video_frame(fake_video) is None


@patch("datahoarder.phash._video_duration", return_value=120.0)
@patch("datahoarder.phash._ffmpeg_extract_frame", return_value=b"\xff\xd8\xff\xe0")
def test_extract_video_frame_success(mock_extract, mock_duration, monkeypatch, tmp_path):
    """Happy path: duration found, frame extracted, image opened."""
    from PIL import Image

    monkeypatch.setattr(phash, "_HAS_FFMPEG", True)
    monkeypatch.setattr(phash, "_HAS_IMAGEHASH", True)

    fake_video = tmp_path / "clip.mp4"
    fake_video.write_text("fake")

    # The returned JPEG bytes are too short to be a real image, so _extract_video_frame
    # will fail at the PIL.open step.  Let's instead patch PIL.open.
    fake_img = MagicMock()
    with patch("datahoarder.phash.PilImage.open", return_value=fake_img):
        result = phash._extract_video_frame(fake_video)
        assert result is fake_img


# ---------------------------------------------------------------------------
# _open_image
# ---------------------------------------------------------------------------

def test_open_image_valid(tmp_path):
    """Valid image opens successfully."""
    from PIL import Image
    img_path = tmp_path / "test.png"
    Image.new("RGB", (32, 32), color="green").save(img_path)
    result = phash._open_image(img_path)
    assert result is not None


def test_open_image_failure():
    """Non-existent path returns None."""
    assert phash._open_image(Path("/nonexistent/image.jpg")) is None

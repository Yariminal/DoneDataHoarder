"""
Configuration manager for DataHoarder user preferences.

Reads/writes JSON config files under ~/.datahoarder/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_DEFAULT_CONFIG_DIR = Path.home() / ".datahoarder"
_DEFAULT_NAMING_RULES_FILE = _DEFAULT_CONFIG_DIR / "naming_rules.json"
_DEFAULT_PHASH_CONFIG_FILE = _DEFAULT_CONFIG_DIR / "phash_config.json"

# Built-in default naming rules (matches former namer.py hard-coded values)
_DEFAULT_USELESS_STEM_PATTERNS = [
    r"^\d+$",                    # 1, 2, 99
    r"^\d+([._-]\d+)+$",         # 15.12, 2018-01, 1_2_3
    r"^[a-z]$",                  # a, b, c (single char)
    r"^untitled[\s_-]*\d*$",     # untitled, untitled1, untitled_2
    r"^new[\s_-]*(file|document|image|folder)?[\s_-]*\d*$",
    r"^document[\s_-]*\d*$",     # document, document1
    r"^copy([\s_-]*of[\s_-]*)?$",  # copy, copy of
    r"^scan[\s_-]*\d*$",         # scan, scan_001
    r"^img[\s_-]*\d*$",          # img, IMG_1234, img1
    r"^image[\s_-]*\d*$",        # image1
    r"^dsc[\s_-]*\d*$",          # DSC_1234, DSC0001 (camera default)
    r"^p\d+$",                   # P1010234 (Panasonic camera)
    r"^photo[\s_-]*\d*$",
    r"^picture[\s_-]*\d*$",
    r"^file[\s_-]*\d*$",
    r"^pdf[\s_-]*\d*$",
    r"^doc[\s_-]*\d*$",
    r"^temp[\s_-]*\d*$",
    r"^tmp[\s_-]*\d*$",
    r"^[a-z]{1,3}\d{1,4}$",      # IMG1234-style 3-letter prefixes
]

_DEFAULT_HYGIENE_CONFIG = {
    "illegal_chars_regex": r'[<>:"|?*\\/\x00-\x1f]',
    "noisy_chars_regex": r'[()\[\]{}#&%!;,@$=+`~^]',
}


def _ensure_dir() -> None:
    _DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_naming_rules(config_path: Optional[Path] = None) -> dict[str, Any]:
    """
    Load naming rules from ~/.datahoarder/naming_rules.json.

    Returns a dict with:
      - useless_stem_patterns: list[str]  (regex strings)
      - hygiene: dict with illegal_chars_regex, noisy_chars_regex
      - user_patterns: list[str]  (user-added custom patterns)
    """
    path = config_path or _DEFAULT_NAMING_RULES_FILE
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError):
            pass

    # Return defaults if file missing or corrupt
    return {
        "useless_stem_patterns": _DEFAULT_USELESS_STEM_PATTERNS,
        "hygiene": _DEFAULT_HYGIENE_CONFIG,
        "user_patterns": [],
    }


def save_naming_rules(data: dict[str, Any], config_path: Optional[Path] = None) -> None:
    """Persist naming rules to ~/.datahoarder/naming_rules.json."""
    _ensure_dir()
    path = config_path or _DEFAULT_NAMING_RULES_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_compiled_useless_patterns(config_path: Optional[Path] = None) -> list[re.Pattern]:
    """Return compiled regex patterns for useless stems (built-in + user)."""
    rules = load_naming_rules(config_path)
    all_patterns = list(rules.get("useless_stem_patterns", []))
    all_patterns.extend(rules.get("user_patterns", []))
    compiled: list[re.Pattern] = []
    for pat in all_patterns:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error:
            # Skip invalid user regexes rather than crashing
            continue
    return compiled


def get_hygiene_config(config_path: Optional[Path] = None) -> dict[str, str]:
    """Return hygiene character regexes."""
    rules = load_naming_rules(config_path)
    return rules.get("hygiene", _DEFAULT_HYGIENE_CONFIG)


# ---------------------------------------------------------------------------
# Perceptual hash configuration
# ---------------------------------------------------------------------------

_DEFAULT_PHASH_CONFIG = {
    "algorithm": "phash",
    "hash_size": 8,
    "threshold": 8,
    "video_enabled": True,
}

_VALID_ALGORITHMS = {"phash", "dhash", "whash", "ahash"}


def load_phash_config(config_path: Optional[Path] = None) -> dict[str, Any]:
    """
    Load perceptual hash config from ~/.datahoarder/phash_config.json.

    Returns dict with:
      - algorithm:   one of phash|dhash|whash|ahash
      - hash_size:   int (must be power of 2, >= 4)
      - threshold:   int (max Hamming distance to consider "near-duplicate")
      - video_enabled: bool (whether to extract video frame thumbnails for hashing)
    """
    path = config_path or _DEFAULT_PHASH_CONFIG_FILE
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    # Merge with defaults and validate
    result = dict(_DEFAULT_PHASH_CONFIG)
    result.update(data)

    # Coerce / validate
    algo = str(result.get("algorithm", "phash")).lower()
    if algo not in _VALID_ALGORITHMS:
        algo = "phash"
    result["algorithm"] = algo

    try:
        result["hash_size"] = int(result["hash_size"])
        if result["hash_size"] < 4:
            result["hash_size"] = 4
    except (ValueError, TypeError):
        result["hash_size"] = 8

    try:
        result["threshold"] = int(result["threshold"])
        if result["threshold"] < 0:
            result["threshold"] = 0
    except (ValueError, TypeError):
        result["threshold"] = 8

    result["video_enabled"] = bool(result.get("video_enabled", True))
    return result


def save_phash_config(data: dict[str, Any], config_path: Optional[Path] = None) -> None:
    """Persist perceptual hash config to ~/.datahoarder/phash_config.json."""
    _ensure_dir()
    path = config_path or _DEFAULT_PHASH_CONFIG_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

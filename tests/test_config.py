"""
Unit tests for donedatahoarder.config — naming rules loading and hygiene.
"""
import json
from pathlib import Path

import pytest

from donedatahoarder.config import (
    load_naming_rules,
    save_naming_rules,
    get_compiled_useless_patterns,
    get_hygiene_config,
    _DEFAULT_USELESS_STEM_PATTERNS,
    _DEFAULT_HYGIENE_CONFIG,
)


class TestLoadNamingRules:
    def test_load_defaults_when_missing(self, tmp_path: Path):
        missing_file = tmp_path / "naming_rules.json"
        rules = load_naming_rules(missing_file)
        assert "useless_stem_patterns" in rules
        assert rules["useless_stem_patterns"] == _DEFAULT_USELESS_STEM_PATTERNS
        assert rules["hygiene"] == _DEFAULT_HYGIENE_CONFIG

    def test_load_existing_file(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        custom = {
            "useless_stem_patterns": [r"^test$"],
            "hygiene": {"illegal_chars_regex": r'[<>:"|?*\\/\x00-\x1f]', "noisy_chars_regex": r'[()\[\]{}#&%!;,@$=+`~^]'},
            "user_patterns": [r"^custom_\d+$"],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(custom, f)
        rules = load_naming_rules(path)
        assert rules["useless_stem_patterns"] == [r"^test$"]
        assert rules["user_patterns"] == [r"^custom_\d+$"]

    def test_load_corrupt_file_falls_back(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        path.write_text("not json")
        rules = load_naming_rules(path)
        assert "useless_stem_patterns" in rules  # defaults


class TestSaveNamingRules:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        custom = {
            "useless_stem_patterns": [r"^foo$"],
            "hygiene": _DEFAULT_HYGIENE_CONFIG,
            "user_patterns": [r"^bar$"],
        }
        save_naming_rules(custom, path)
        loaded = load_naming_rules(path)
        assert loaded == custom


class TestCompiledPatterns:
    def test_default_patterns_match(self):
        patterns = get_compiled_useless_patterns()
        assert any(p.match("IMG_1234") for p in patterns)
        assert any(p.match("untitled") for p in patterns)
        assert any(p.match("1") for p in patterns)
        assert not any(p.match("family_photo") for p in patterns)

    def test_user_patterns_included(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        save_naming_rules(
            {
                "useless_stem_patterns": [],
                "hygiene": _DEFAULT_HYGIENE_CONFIG,
                "user_patterns": [r"^acme_\d+$"],
            },
            path,
        )
        patterns = get_compiled_useless_patterns(path)
        assert any(p.match("acme_42") for p in patterns)

    def test_invalid_user_regex_skipped(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        save_naming_rules(
            {
                "useless_stem_patterns": [],
                "hygiene": _DEFAULT_HYGIENE_CONFIG,
                "user_patterns": [r"[invalid"],
            },
            path,
        )
        patterns = get_compiled_useless_patterns(path)
        assert len(patterns) == 0  # invalid regex skipped, no crash


class TestHygieneConfig:
    def test_default_hygiene(self):
        cfg = get_hygiene_config()
        assert "illegal_chars_regex" in cfg
        assert "noisy_chars_regex" in cfg

    def test_custom_hygiene(self, tmp_path: Path):
        path = tmp_path / "naming_rules.json"
        save_naming_rules(
            {
                "useless_stem_patterns": [],
                "hygiene": {"illegal_chars_regex": r'[<>]', "noisy_chars_regex": r'[!]'},
                "user_patterns": [],
            },
            path,
        )
        cfg = get_hygiene_config(path)
        assert cfg["illegal_chars_regex"] == r'[<>]'

"""
Unit tests for datahoarder.proposals.namer — naming heuristics.
"""
from pathlib import Path

import pytest

from datahoarder.proposals.namer import (
    _is_useless_stem,
    _hygienic_stem,
    _safe,
)


class TestIsUselessStem:
    def test_pure_digits(self):
        assert _is_useless_stem("1")
        assert _is_useless_stem("99")

    def test_camera_defaults(self):
        assert _is_useless_stem("IMG_1234")
        assert _is_useless_stem("DSC0001")
        assert _is_useless_stem("P1010234")

    def test_untitled(self):
        assert _is_useless_stem("untitled")
        assert _is_useless_stem("untitled_1")

    def test_meaningful_stems(self):
        assert not _is_useless_stem("family_photo")
        assert not _is_useless_stem("report_final")
        assert not _is_useless_stem("logo_vector")

    def test_empty_and_whitespace(self):
        assert _is_useless_stem("")
        assert _is_useless_stem("   ")


class TestHygienicStem:
    def test_removes_parens(self):
        assert _hygienic_stem("My Report (Final Draft)") == "My_Report_Final_Draft"

    def test_removes_noisy_chars(self):
        assert _hygienic_stem("file&with#bad!chars") == "file_with_bad_chars"

    def test_normalizes_whitespace(self):
        assert _hygienic_stem("too    many   spaces") == "too_many_spaces"

    def test_no_change_for_clean(self):
        assert _hygienic_stem("normal_file_name") == "normal_file_name"

    def test_preserves_hebrew(self):
        assert _hygienic_stem("תפריט אירוע") == "תפריט_אירוע"


class TestSafe:
    def test_lowercases_and_truncates(self):
        result = _safe("A" * 100)
        assert result == "a" * 60

    def test_strips_special(self):
        assert _safe("hello-world!!") == "hello-world"

    def test_preserves_hebrew(self):
        result = _safe("תנורים")
        assert "תנורים" in result

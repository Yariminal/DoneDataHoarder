"""
Tests for organizer module — Hebrew transliteration and folder renaming.
"""
import pytest

from donedatahoarder.proposals.organizer import _transliterate_hebrew


class TestHebrewTransliteration:
    """Test Hebrew-to-Latin transliteration for folder names."""

    def test_hebrew_french_word(self):
        """Transliterate Hebrew word (צרפתי = French)."""
        # צרפתי = Tzarfati (French language/nationality)
        result = _transliterate_hebrew("צרפתי")
        # Should contain transliterated Hebrew
        assert result  # Not empty
        assert "tz" in result  # tz for צ (tsade)
        assert "r" in result   # r for ר (resh)

    def test_hebrew_plans_folder(self):
        """Transliterate 'Updated Plans' folder name."""
        # תכניות עדכניות = Plans & Updated
        result = _transliterate_hebrew("תכניות עדכניות")
        # Should have underscore where space was
        assert "_" in result
        # Should have transliterated Hebrew characters
        assert result and not any(c in result for c in "תכניות עדכניות")

    def test_mixed_hebrew_english(self):
        """Transliterate mixed Hebrew and English text."""
        result = _transliterate_hebrew("project_צרפתי")
        # English part preserved, Hebrew transliterated
        assert result.startswith("project_")
        assert len(result) > len("project_")

    def test_hebrew_with_numbers(self):
        """Transliterate Hebrew with digits preserved."""
        result = _transliterate_hebrew("פרויקט3_2013")
        # Digits and underscore preserved
        assert "3" in result
        assert "2013" in result
        assert "_" in result

    def test_latin_passthrough(self):
        """Latin text passes through unchanged."""
        assert _transliterate_hebrew("English_Folder_2024") == "english_folder_2024"

    def test_empty_string(self):
        """Empty string returns empty."""
        assert _transliterate_hebrew("") == ""

    def test_spaces_to_underscores(self):
        """Spaces are converted to underscores."""
        result = _transliterate_hebrew("hello world")
        assert result == "hello_world"

    def test_hyphens_to_underscores(self):
        """Hyphens are converted to underscores."""
        result = _transliterate_hebrew("hello-world")
        assert result == "hello_world"

    def test_hebrew_aleph(self):
        """Test individual Hebrew letter Aleph."""
        assert _transliterate_hebrew("א") == "a"

    def test_hebrew_common_letters(self):
        """Test common Hebrew letters."""
        assert _transliterate_hebrew("ב") == "b"  # Bet
        assert _transliterate_hebrew("ג") == "g"  # Gimel
        assert _transliterate_hebrew("ד") == "d"  # Dalet
        assert _transliterate_hebrew("ר") == "r"  # Resh
        assert _transliterate_hebrew("ש") == "sh"  # Shin

    def test_hebrew_final_forms(self):
        """Test Hebrew final forms (sofit)."""
        assert _transliterate_hebrew("ך") == "k"   # Final Kaph
        assert _transliterate_hebrew("ם") == "m"   # Final Mem
        assert _transliterate_hebrew("ן") == "n"   # Final Nun
        assert _transliterate_hebrew("ף") == "p"   # Final Pe
        assert _transliterate_hebrew("ץ") == "tz"  # Final Tsade

    def test_mixed_hebrew_digits_spaces(self):
        """Complex case: Hebrew + digits + spaces."""
        result = _transliterate_hebrew("תכניות 2024")
        # Space becomes underscore
        assert "_" in result
        # Digits preserved
        assert "2024" in result
        # Result is not empty and doesn't contain Hebrew
        assert result and not any(c in result for c in "תכניות")

    def test_case_normalization(self):
        """Output should be lowercase."""
        result = _transliterate_hebrew("HELLO")
        assert result == result.lower()

    def test_tzarfati_folder_name(self):
        """Test realistic folder name: Tzarfati (designer folder)."""
        # This represents a real folder name from the test
        result = _transliterate_hebrew("צרפתי")
        # Just verify it's transliterated and usable as a folder name
        assert result  # Not empty
        assert not any(c in result for c in "צרפתי")  # No Hebrew chars left
        assert all(c.isalnum() or c == "_" for c in result)  # Valid folder chars

    def test_plans_folder_name(self):
        """Test realistic folder name: תכניות עדכניות (Updated Plans)."""
        result = _transliterate_hebrew("תכניות עדכניות")
        # Verify it's transliterated
        assert result
        assert not any(c in result for c in "תכניות עדכניות")  # No Hebrew chars
        assert all(c.isalnum() or c == "_" for c in result)  # Valid folder chars
        assert "_" in result  # Space preserved as underscore

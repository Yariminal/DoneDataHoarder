"""
Unit tests for datahoarder.core.scanner — especially cross-platform date_created logic.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from datahoarder.core.scanner import _get_file_dates, _exif_date_created, _mutagen_date_created


class TestGetFileDates:
    def test_macos_birthtime(self):
        """macOS/BSD: st_birthtime is available and used."""
        stat = MagicMock()
        stat.st_mtime = 1_700_000_000
        stat.st_birthtime = 1_600_000_000
        stat.st_ctime = 1_650_000_000

        mod, cre, source, warn = _get_file_dates(Path("/tmp/x.jpg"), stat)
        assert source == "birthtime"
        assert warn is None
        assert cre == datetime.fromtimestamp(1_600_000_000)
        assert mod == datetime.fromtimestamp(1_700_000_000)

    def test_windows_ctime(self):
        """Windows: st_ctime is creation time."""
        with patch.object(sys, "platform", "win32"):
            stat = MagicMock()
            stat.st_mtime = 1_700_000_000
            # Remove birthtime attr so macOS path doesn't trigger
            del stat.st_birthtime
            stat.st_ctime = 1_650_000_000

            mod, cre, source, warn = _get_file_dates(Path("C:\\tmp\\x.jpg"), stat)
            assert source == "ctime_windows"
            assert warn is None
            assert cre == datetime.fromtimestamp(1_650_000_000)

    def test_linux_no_birthtime_no_media(self):
        """Linux without birthtime, non-media file -> mtime fallback with warning."""
        with patch.object(sys, "platform", "linux"):
            with patch.object(os, "name", "posix"):
                stat = MagicMock()
                stat.st_mtime = 1_700_000_000
                # Ensure AttributeError on st_birthtime
                del stat.st_birthtime
                stat.st_ctime = 1_650_000_000

                mod, cre, source, warn = _get_file_dates(Path("/tmp/readme.txt"), stat)
                assert source == "mtime_fallback"
                assert warn is not None
                assert "mtime" in warn.lower()
                assert cre == mod

    @patch("datahoarder.core.scanner._HAS_EXIFREAD", True)
    @patch("datahoarder.core.scanner._exif_date_created")
    def test_linux_image_exif_fallback(self, mock_exif):
        """Linux image without birthtime -> EXIF fallback."""
        mock_exif.return_value = datetime(2020, 5, 15, 10, 30, 0)
        with patch.object(sys, "platform", "linux"):
            with patch.object(os, "name", "posix"):
                stat = MagicMock()
                stat.st_mtime = 1_700_000_000
                del stat.st_birthtime

                mod, cre, source, warn = _get_file_dates(Path("/tmp/photo.jpg"), stat)
                assert source == "exif_fallback"
                assert cre == datetime(2020, 5, 15, 10, 30, 0)
                assert warn is None

    @patch("datahoarder.core.scanner._HAS_MUTAGEN", True)
    @patch("datahoarder.core.scanner._mutagen_date_created")
    def test_linux_audio_mutagen_fallback(self, mock_mutagen):
        """Linux audio without birthtime -> mutagen fallback."""
        mock_mutagen.return_value = datetime(2019, 8, 1, 0, 0, 0)
        with patch.object(sys, "platform", "linux"):
            with patch.object(os, "name", "posix"):
                stat = MagicMock()
                stat.st_mtime = 1_700_000_000
                del stat.st_birthtime

                mod, cre, source, warn = _get_file_dates(Path("/tmp/song.mp3"), stat)
                assert source == "mutagen_fallback"
                assert cre == datetime(2019, 8, 1, 0, 0, 0)
                assert warn is None


def test_exif_date_created_missing():
    """_exif_date_created returns None when exifread is missing or fails."""
    with patch("datahoarder.core.scanner._HAS_EXIFREAD", False):
        assert _exif_date_created(Path("/tmp/x.jpg")) is None


def test_mutagen_date_created_missing():
    """_mutagen_date_created returns None when mutagen is missing or fails."""
    with patch("datahoarder.core.scanner._HAS_MUTAGEN", False):
        assert _mutagen_date_created(Path("/tmp/x.mp3")) is None

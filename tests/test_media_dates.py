"""Media metadata dates retain their full precision in both index stages."""
from datetime import datetime
from pathlib import Path

import pytest

from donedatahoarder.core import enricher, scanner


@pytest.mark.parametrize("module,reader", [
    (scanner, scanner._mutagen_date_created),
    (enricher, enricher._audio_date),
])
@pytest.mark.parametrize("raw,expected", [
    ("2020", datetime(2020, 1, 1)),
    ("2020-05-17", datetime(2020, 5, 17)),
    ("2020-05-17T13:14:15", datetime(2020, 5, 17, 13, 14, 15)),
    ("2020-05-17T13:14:15Z", datetime(2020, 5, 17, 13, 14, 15)),
    ("2020-05-17T13:14:15+01:00", datetime(2020, 5, 17, 13, 14, 15)),
    ("2020-05-17T13:14:15.1Z", datetime(2020, 5, 17, 13, 14, 15, 100000)),
    ("2020-05-17T13:14:15.1234", datetime(2020, 5, 17, 13, 14, 15, 123400)),
    ("2020-05-17T13:14:15.123456+01:00", datetime(2020, 5, 17, 13, 14, 15, 123456)),
    ("2020-02-31", None),
    ("2020-05-17T25:14:15", None),
    ("2020 corrupted", None),
])
def test_media_tag_dates(module, reader, raw, expected, monkeypatch, tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(module, "_HAS_MUTAGEN", True)
    monkeypatch.setattr(module.mutagen, "File", lambda *args, **kwargs: {"date": [raw]})
    assert reader(path) == expected

"""Photo ranking preserves pixels and capture information without auto-disposal."""
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from donedatahoarder.core import dedup
from donedatahoarder.core.photo_quality import compare_photos, keeper_sort_key, photo_evidence
from donedatahoarder.db.models import (
    Base, DuplicateGroup, DuplicateMember, DupeType, File, FileStatus, Proposal,
    ProposalStatus, UserSession,
)


def photo(file_id=1, *, width=6000, height=4000, fields=None, status="complete",
          path=None, format="JPEG", mode="RGB", **kwargs):
    file = File(id=file_id, session_id="photos", path=path or f"/photos/{file_id}.jpg",
                filename=f"{file_id}.jpg", extension=".jpg", mime_type="image/jpeg",
                hash_sha256=hashlib.sha256(str(file_id).encode()).hexdigest(),
                hash_perceptual="0123456789abcdef", status=FileStatus.ENRICHED)
    for name, value in kwargs.items():
        setattr(file, name, value)
    file.photo_metadata = json.dumps({
        "version": 1, "status": status, "width": width, "height": height,
        "format": format, "mode": mode, "source_sha256": file.hash_sha256,
        "fields": fields or {}, "warnings": [],
    })
    return file


CAPTURE = {"capture_time": "2020-06-01T12:34:56", "camera_make": "Nikon",
           "camera_model": "D750", "lens_model": "24-70mm", "exposure_time": 0.005,
           "f_number": 5.6, "iso": 100, "focal_length": 35.0}


def test_full_resolution_and_capture_metadata_beat_earlier_date_longer_path_and_bytes():
    original = photo(1, fields=CAPTURE, date_best=datetime(2020, 6, 1), size_bytes=1000)
    export = photo(2, width=3000, height=2000, path="/one/very/descriptive/long/path/export.jpg",
                   date_best=datetime(1990, 1, 1), size_bytes=50_000_000)
    assert dedup._pick_keeper([export, original]) == original.id
    comparison = compare_photos(export, original)
    assert comparison["status"] == "recommended"
    assert comparison["requires_review"] is True
    assert comparison["keeper"]["megapixels"] == 24
    assert comparison["candidate"]["megapixels"] == 6
    assert set(comparison["keeper_unique_fields"]) == CAPTURE.keys()
    assert "Higher resolution does not prove" in " ".join(comparison["reasons"])


def test_equal_resolution_prefers_valid_metadata_superset_not_padding():
    stripped = photo(2, fields={"software": "Editor", "padding": "x" * 1000}, size_bytes=100000)
    richer = photo(1, fields=CAPTURE, size_bytes=100)
    assert dedup._pick_keeper([stripped, richer]) == richer.id
    assert photo_evidence(stripped)["meaningful_field_count"] == 0
    assert compare_photos(stripped, richer)["status"] == "recommended"


def test_resolution_metadata_split_is_a_tradeoff_and_keeps_unique_values():
    largest = photo(1)
    richer = photo(2, width=3000, height=2000, fields=CAPTURE)
    assert dedup._pick_keeper([richer, largest]) == largest.id
    comparison = compare_photos(richer, largest)
    assert comparison["status"] == "tradeoff"
    assert set(comparison["candidate_unique_fields"]) == CAPTURE.keys()
    assert comparison["candidate"]["fields"]["capture_time"] == CAPTURE["capture_time"]
    assert comparison["requires_review"] is True


def test_conflicting_capture_values_never_claim_the_keeper_preserves_all_information():
    first = photo(1, fields=CAPTURE)
    second = photo(2, fields={**CAPTURE, "capture_time": "2021-06-01T12:34:56"})
    comparison = compare_photos(second, first)
    assert comparison["status"] == "tradeoff"
    assert comparison["conflicting_fields"] == ["capture_time"]
    assert comparison["requires_review"] is True


@pytest.mark.parametrize("kwargs", [
    {"width": 4000, "height": 4000}, {"format": "PNG"}, {"mode": "CMYK"},
    {"hash_perceptual": "0123456789abcdee"},
    {"extension": ".nef", "status": "unsupported"},
])
def test_crops_renderings_different_fingerprints_and_raw_exports_are_reviewable_variants(kwargs):
    candidate, keeper = photo(2, **kwargs), photo(1)
    comparison = compare_photos(candidate, keeper)
    assert comparison["status"] == "variant"
    assert comparison["requires_review"] is True


def test_equal_evidence_uses_stable_path_then_id_without_date_or_size():
    z = photo(1, path="/photos/z.jpg", date_best=datetime(1900, 1, 1), size_bytes=9999999)
    a = photo(2, path="/photos/a.jpg", date_best=datetime(2025, 1, 1), size_bytes=1)
    assert dedup._pick_keeper([z, a]) == a.id
    assert dedup._pick_keeper([a, z]) == a.id
    same_path = photo(3, path=a.path)
    assert dedup._pick_keeper([same_path, a]) == a.id
    assert compare_photos(a, z)["status"] == "equivalent"
    assert compare_photos(a, z)["requires_review"] is True


def test_orientation_dimensions_are_displayed_correctly_without_changing_pixel_count():
    portrait = photo(1, width=6000, height=4000, fields={"orientation": 6})
    evidence = photo_evidence(portrait)
    assert (evidence["width"], evidence["height"]) == (6000, 4000)
    assert (evidence["display_width"], evidence["display_height"]) == (4000, 6000)
    assert evidence["pixels"] == 24_000_000


@pytest.mark.parametrize("raw", [None, "", "{bad", "[]", '{"version": 9}', "x" * 70000,
                                  "[" * 2000 + "]" * 2000],
                         ids=["legacy", "empty", "malformed", "list", "future-version", "oversized", "deeply-nested"])
def test_legacy_corrupt_or_oversized_photo_evidence_is_unknown(raw):
    candidate, keeper = photo(2), photo(1)
    candidate.photo_metadata = raw
    evidence = photo_evidence(candidate)
    assert evidence["status"] == "unknown"
    assert evidence["fields"] == {}
    assert evidence["pixels"] is None
    assert compare_photos(candidate, keeper)["status"] == "unknown"


@pytest.mark.parametrize("status", ["unavailable", "unsupported", "partial"])
def test_incomplete_inventory_is_not_assumed_metadata_free(status):
    candidate, keeper = photo(2, status=status), photo(1)
    assert compare_photos(candidate, keeper)["status"] == "unknown"
    assert compare_photos(candidate, keeper)["requires_review"] is True


def test_stale_or_unbound_extraction_cannot_rank_as_known_high_quality():
    stale = photo(1, width=10000, height=10000, fields=CAPTURE)
    stale.hash_sha256 = "f" * 64
    known = photo(2, width=3000, height=2000)
    assert photo_evidence(stale)["pixels"] is None
    assert photo_evidence(stale)["status"] == "unknown"
    assert dedup._pick_keeper([stale, known]) == known.id
    assert compare_photos(stale, known)["status"] == "unknown"
    stale.hash_sha256 = None
    assert photo_evidence(stale)["status"] == "unknown"


def test_invalid_tags_do_not_count_and_make_absence_claim_incomplete():
    candidate = photo(2, fields={"iso": 0, "f_number": float("nan"), "orientation": 12,
                                "gps_latitude": 100, "capture_time": "0000:00:00 00:00:00",
                                "camera_model": "x" * 1025, "camera_make": "Nikon"})
    evidence = photo_evidence(candidate)
    assert evidence["status"] == "partial"
    assert evidence["fields"] == {"camera_make": "Nikon"}
    assert evidence["meaningful_field_count"] == 1
    json.dumps(evidence, allow_nan=False)


def test_numeric_overflow_and_placeholder_metadata_cannot_break_or_pad_evidence():
    candidate = photo(2, fields={"iso": 10 ** 1000, "camera_model": "Unknown"})
    evidence = photo_evidence(candidate)
    assert evidence["fields"] == {}
    assert evidence["meaningful_field_count"] == 0
    assert evidence["status"] == "partial"
    complete = photo(3, fields={"description": "x" * 1024, "camera_model": "Unknown"})
    assert photo_evidence(complete)["status"] == "complete"
    assert photo_evidence(complete)["meaningful_field_count"] == 1


def test_supported_empty_inventory_proves_absence_but_not_image_equivalence():
    comparison = compare_photos(photo(2), photo(1))
    assert comparison["candidate"]["status"] == "complete"
    assert comparison["candidate"]["fields"] == {}
    assert comparison["status"] == "equivalent"
    assert comparison["requires_review"] is True


def test_identical_sha256_preserves_image_and_metadata_even_if_extraction_is_unknown():
    keeper = photo(1)
    candidate = photo(2, hash_sha256=keeper.hash_sha256)
    candidate.photo_metadata = None
    comparison = compare_photos(candidate, keeper)
    assert comparison["status"] == "equivalent"
    assert comparison["requires_review"] is False
    assert "embedded metadata are identical" in comparison["reasons"][0]


def test_manually_selected_lower_resolution_keeper_is_visible_tradeoff():
    candidate, keeper = photo(1, fields=CAPTURE), photo(2, width=3000, height=2000)
    assert compare_photos(candidate, keeper)["status"] == "tradeoff"


def test_nonphoto_existing_date_path_size_order_is_preserved():
    first = File(id=1, path="/first.txt", extension=".txt", date_best=datetime(2000, 1, 1), size_bytes=1)
    second = File(id=2, path="/much/longer/second.txt", extension=".txt", date_best=datetime(2001, 1, 1), size_bytes=1000)
    assert dedup._pick_keeper([second, first]) == 1
    second.date_best = first.date_best
    assert dedup._pick_keeper([second, first]) == 2
    first.path = "/much/longer/first1.txt"
    assert len(first.path) == len(second.path)
    assert dedup._pick_keeper([second, first]) == 2
    assert compare_photos(first, second) is None


@pytest.fixture
def photo_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'photos.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dedup, "get_engine", lambda: engine)
    # This isolated database has one writer in the test.
    monkeypatch.setattr(dedup, "operation_lock", lambda *args: __import__("contextlib").nullcontext())
    with Session(engine) as db:
        db.add(UserSession(id="photos", root_path=str(tmp_path)))
        db.commit()
    yield engine
    engine.dispose()


def test_chunked_database_selection_matches_in_memory_and_is_bounded(photo_db, monkeypatch):
    monkeypatch.setattr(dedup, "KEEPER_QUERY_CHUNK", 7)
    files = [photo(number, width=1000 + number, height=1000, fields=CAPTURE if number == 24 else {})
             for number in range(1, 30)]
    files[-1].photo_metadata = None
    statements = []
    with Session(photo_db, expire_on_commit=False) as db:
        db.add_all(files)
        db.commit()
        event.listen(photo_db, "before_cursor_execute",
                     lambda connection, cursor, statement, parameters, context, many: statements.append(statement))
        expected = dedup._pick_keeper(list(reversed(files)))
        assert dedup._pick_keeper_ids(db, (file.id for file in files)) == expected == 28
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 5


def test_rerun_keeps_existing_explicit_keeper_but_new_groups_use_photo_policy(photo_db):
    with Session(photo_db) as db:
        db.add_all([photo(1, width=3000, height=2000), photo(2, fields=CAPTURE)])
        db.flush()
        old = DuplicateGroup(session_id="photos", dupe_type=DupeType.PERCEPTUAL,
                             group_hash="old", keep_file_id=1)
        db.add(old)
        db.flush()
        dedup._upsert_group(db, DupeType.PERCEPTUAL, "old", [1, 2], session_id="photos")
        dedup._upsert_group(db, DupeType.PERCEPTUAL, "new", [1, 2], session_id="photos")
        assert old.keep_file_id == 1
        assert db.query(DuplicateGroup).filter_by(group_hash="new").one().keep_file_id == 2


def test_generation_and_keeper_refresh_explain_tradeoffs_and_never_autoapprove_nearmatches(photo_db):
    with Session(photo_db) as db:
        db.add_all([photo(1), photo(2, width=3000, height=2000, fields=CAPTURE)])
        db.flush()
        dedup._upsert_group(db, DupeType.PERCEPTUAL, "family", [1, 2], session_id="photos")
        db.commit()
    counts = dedup.generate_dedup_proposals(session_id="photos")
    assert counts["created"] == 1
    with Session(photo_db) as db:
        proposal = db.query(Proposal).one()
        assert proposal.status == ProposalStatus.PENDING
        assert proposal.confidence is None
        assert "Photo preservation tradeoff" in proposal.reasoning
        assert "capture_time" in proposal.reasoning
        group = db.get(DuplicateGroup, proposal.duplicate_group_id)
        group.keep_file_id = 2
        dedup.refresh_group_proposals(db, group.id)
        db.flush()
        refreshed = db.query(Proposal).filter_by(file_id=1).one()
        assert refreshed.status == ProposalStatus.PENDING
        assert refreshed.confidence is None
        assert "Photo preservation tradeoff" in refreshed.reasoning
        assert db.get(Proposal, proposal.id).status == ProposalStatus.REJECTED


def test_real_jpeg_scan_enrich_group_and_propose_preserves_full_resolution_exif(photo_db, tmp_path, monkeypatch):
    from PIL import Image, TiffImagePlugin
    from donedatahoarder.core import enricher, scanner
    from donedatahoarder.db import session as db_module

    monkeypatch.setattr(scanner, "get_engine", lambda: photo_db)
    monkeypatch.setattr(enricher, "get_engine", lambda: photo_db)
    monkeypatch.setattr(db_module, "get_engine", lambda: photo_db)
    root = tmp_path / "library"
    root.mkdir()
    original_path = root / "original.jpg"
    stripped_path = root / "stripped.jpg"
    exif = Image.Exif()
    exif[271] = "Canon"
    exif[272] = "EOS R5"
    exif[34665] = {36867: "2024:02:29 12:34:56", 42036: "RF 50mm",
                   33434: TiffImagePlugin.IFDRational(1, 125), 34855: 200}
    image = Image.new("RGB", (800, 600), (37, 91, 153))
    image.save(original_path, exif=exif)
    image.resize((400, 300)).save(stripped_path)
    original_bytes, stripped_bytes = original_path.read_bytes(), stripped_path.read_bytes()

    scanner.scan(root, session_id="photos", show_progress=False)
    assert enricher.enrich(session_id="photos") == {"enriched": 2, "errors": 0, "skipped": 0}
    result = dedup.find_perceptual_duplicates(session_id="photos")
    assert result["groups"] == 1
    assert dedup.generate_dedup_proposals(session_id="photos")["created"] == 1
    with Session(photo_db) as db:
        group = db.query(DuplicateGroup).one()
        keeper = db.get(File, group.keep_file_id)
        proposal = db.query(Proposal).one()
        candidate = db.get(File, proposal.file_id)
        assert Path(keeper.path) == original_path
        assert Path(candidate.path) == stripped_path
        assert photo_evidence(keeper)["fields"]["capture_time"] == "2024-02-29T12:34:56"
        assert compare_photos(candidate, keeper)["status"] == "recommended"
        assert "Photo preservation recommended" in proposal.reasoning
        assert "capture_time" in proposal.reasoning
        assert proposal.confidence is None
        assert proposal.status == ProposalStatus.PENDING
    assert original_path.read_bytes() == original_bytes
    assert stripped_path.read_bytes() == stripped_bytes

"""Real photo evidence, persisted backfill, and unknown-content regressions."""
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, TiffImagePlugin
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from donedatahoarder.core import enricher, photo_metadata
from donedatahoarder.core.photo_metadata import extract_photo_metadata
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus, Proposal,
    ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import init_db


def _jpeg(path, size=(320, 240), *, exif=None):
    Image.new("RGB", size, (37, 91, 153)).save(path, exif=exif or Image.Exif())
    return path


def _capture_exif():
    rational = TiffImagePlugin.IFDRational
    exif = Image.Exif()
    exif[271] = "Canon"
    exif[272] = "EOS R5"
    exif[274] = 6
    exif[306] = "2025:01:01 00:00:00"
    exif[34665] = {
        36867: "2024:02:29 12:34:56", 36881: "+01:00", 37521: "1200",
        36868: "2024:03:01 10:00:00", 42035: "Canon", 42036: "RF 50mm",
        33434: rational(1, 125), 33437: rational(28, 10),
        34855: 200, 37386: rational(50),
    }
    exif[34853] = {
        1: "N", 2: (rational(51), rational(30), rational(0)),
        3: "W", 4: (rational(0), rational(7), rational(30)),
        5: b"\x01", 6: rational(5),
    }
    return exif


def test_real_jpeg_dimensions_nested_capture_and_gps_preserve_original(tmp_path):
    path = _jpeg(tmp_path / "original.jpg", exif=_capture_exif())
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    evidence = extract_photo_metadata(path, source_sha256=digest)
    assert evidence == {
        "version": 1, "status": "complete", "width": 320, "height": 240,
        "format": "JPEG", "mode": "RGB", "source_sha256": digest, "warnings": [],
        "fields": {
            "camera_make": "Canon", "camera_model": "EOS R5", "orientation": 6,
            "capture_time": "2024-02-29T12:34:56", "capture_offset": "+01:00",
            "capture_subseconds": "12", "digitized_time": "2024-03-01T10:00:00",
            "modified_time": "2025-01-01T00:00:00", "lens_make": "Canon",
            "lens_model": "RF 50mm", "exposure_time": 0.008, "f_number": 2.8,
            "iso": 200, "focal_length": 50.0, "gps_latitude": 51.5,
            "gps_longitude": -0.125, "gps_altitude": -5.0,
        },
    }
    assert path.read_bytes() == original


def test_stripped_resized_export_has_known_absence_and_own_dimensions(tmp_path):
    original = _jpeg(tmp_path / "original.jpg", size=(640, 480), exif=_capture_exif())
    export = _jpeg(tmp_path / "export.jpg", size=(160, 120))
    full, stripped = map(extract_photo_metadata, (original, export))
    assert full["width"] == 640 and full["fields"]["capture_time"]
    assert stripped["width"] == 160 and stripped["status"] == "complete"
    assert stripped["fields"] == {} and stripped["warnings"] == []


def test_digitized_and_modified_dates_do_not_become_capture_time(tmp_path):
    exif = Image.Exif()
    exif[306] = "2023:01:01 12:00:00"
    exif[34665] = {36868: "2022:01:01 12:00:00"}
    result = extract_photo_metadata(_jpeg(tmp_path / "copy.jpg", exif=exif))
    assert result["status"] == "complete"
    assert "capture_time" not in result["fields"]
    assert result["fields"]["digitized_time"] == "2022-01-01T12:00:00"


def test_invalid_empty_and_padding_tags_do_not_count_as_metadata(tmp_path):
    exif = Image.Exif()
    exif[271] = "   "
    exif[272] = "Unknown"
    exif[305] = "Export utility " * 100  # Software does not preserve capture information.
    exif[270] = "padding" * 500
    exif[274] = 0
    exif[34665] = {36867: "2023:02:31 01:00:00", 33437: 0.0, 34855: 0}
    result = extract_photo_metadata(_jpeg(tmp_path / "malformed.jpg", exif=exif))
    assert result["status"] == "partial"
    assert result["fields"] == {}
    assert len(result["warnings"]) <= photo_metadata.MAX_WARNINGS
    assert len(json.dumps(result)) < 2000


@pytest.mark.parametrize("format,suffix", [("PNG", ".png"), ("TIFF", ".tif"), ("WEBP", ".webp")])
def test_supported_formats_inspect_dimensions_without_pixel_decode(tmp_path, monkeypatch, format, suffix):
    path = tmp_path / ("image" + suffix)
    Image.new("RGB", (40, 30)).save(path, format=format)
    original_load = Image.Image.load

    def reject_pixel_load(self, *args, **kwargs):
        raise AssertionError("Extractor must not decode image pixels")

    monkeypatch.setattr(Image.Image, "load", reject_pixel_load)
    result = extract_photo_metadata(path)
    monkeypatch.setattr(Image.Image, "load", original_load)
    assert result["status"] == "complete"
    assert (result["width"], result["height"]) == (40, 30)


def test_unknown_content_is_distinct_from_confirmed_absence(tmp_path):
    corrupt = tmp_path / "broken.jpg"
    corrupt.write_bytes(b"not a jpeg")
    raw = tmp_path / "original.dng"
    raw.write_bytes(b"unsupported raw")
    assert extract_photo_metadata(corrupt)["status"] == "unavailable"
    assert extract_photo_metadata(tmp_path / "missing.jpg")["status"] == "unavailable"
    assert extract_photo_metadata(raw)["status"] == "unsupported"


def test_png_exif_after_pixels_is_inspected_without_loading_pixels(tmp_path, monkeypatch):
    path = tmp_path / "after-data.png"
    Image.new("RGB", (40, 30)).save(path, exif=_capture_exif())
    raw = path.read_bytes()
    chunks = []
    position = 8
    while position < len(raw):
        length = struct.unpack(">I", raw[position:position + 4])[0]
        chunks.append(raw[position:position + length + 12])
        position += length + 12
    exif = next(chunk for chunk in chunks if chunk[4:8] == b"eXIf")
    chunks.remove(exif)
    chunks.insert(-1, exif)
    path.write_bytes(raw[:8] + b"".join(chunks))
    monkeypatch.setattr(Image.Image, "load", lambda *args: pytest.fail("Pixels were decoded"))
    result = extract_photo_metadata(path)
    assert result["status"] == "complete"
    assert result["fields"]["capture_time"] == "2024-02-29T12:34:56"


@pytest.mark.parametrize("mode,lossless", [("RGB", False), ("RGBA", False), ("RGB", True), ("RGBA", True)])
def test_webp_header_dimensions_and_exif_without_pixel_decoder(tmp_path, monkeypatch, mode, lossless):
    path = tmp_path / "photo.webp"
    Image.new(mode, (37, 29), (10, 20, 30, 127) if mode == "RGBA" else (10, 20, 30)).save(
        path, lossless=lossless, exif=_capture_exif(),
    )
    monkeypatch.setattr(Image, "open", lambda *args: pytest.fail("WebP decoder was initialized"))
    result = extract_photo_metadata(path)
    assert result["status"] == "complete"
    assert (result["width"], result["height"], result["mode"]) == (37, 29, mode)
    assert result["fields"]["camera_model"] == "EOS R5"


def test_uncompared_xmp_is_partial_not_absent(tmp_path):
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", "<metadata>Capture information</metadata>")
    path = tmp_path / "with-xmp.png"
    Image.new("RGB", (30, 20)).save(path, pnginfo=info)
    result = extract_photo_metadata(path)
    assert result["status"] == "partial"
    assert "XMP" in result["warnings"][0]


@pytest.mark.parametrize("chunk_kind", [b"IHDR", b"eXIf"])
def test_png_bad_metadata_crc_never_supplies_complete_evidence(tmp_path, chunk_kind):
    path = tmp_path / "bad-crc.png"
    Image.new("RGB", (30, 20)).save(path, exif=_capture_exif())
    data = bytearray(path.read_bytes())
    start = data.index(chunk_kind)
    length = int.from_bytes(data[start - 4:start], "big")
    data[start + 4 + length] ^= 1
    path.write_bytes(data)
    result = extract_photo_metadata(path)
    assert result["status"] in {"partial", "unavailable"}
    assert "capture_time" not in result["fields"]


def test_png_repeated_header_is_not_valid_photo_evidence(tmp_path):
    path = tmp_path / "duplicate-header.png"
    Image.new("RGB", (30, 20)).save(path)
    data = path.read_bytes()
    path.write_bytes(data[:33] + data[8:33] + data[33:])
    assert extract_photo_metadata(path)["status"] == "unavailable"


def test_webp_trailing_data_is_not_silently_ignored(tmp_path):
    path = tmp_path / "trailing.webp"
    Image.new("RGB", (30, 20)).save(path)
    path.write_bytes(path.read_bytes() + b"Additional metadata")
    assert extract_photo_metadata(path)["status"] == "partial"


def test_read_budget_limits_large_headers_and_marks_partial(tmp_path, monkeypatch):
    path = _jpeg(tmp_path / "image.jpg")
    monkeypatch.setattr(photo_metadata, "MAX_READ_BYTES", 32)
    result = extract_photo_metadata(path)
    assert result["status"] == "partial"
    assert "limit" in result["warnings"][0]


@pytest.mark.parametrize("attribute", [0x1000, 0x40000, 0x400000])
def test_cloud_placeholder_never_reads_content(tmp_path, monkeypatch, attribute):
    path = tmp_path / "cloud.jpg"
    info = SimpleNamespace(st_file_attributes=attribute, st_mode=0o100644)
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: info)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Cloud content was opened"))
    monkeypatch.setattr(enricher, "_mime_type", lambda path: pytest.fail("MIME probe opened cloud content"))
    assert extract_photo_metadata(path)["status"] == "unavailable"
    result = enricher._disk_metadata(path)
    assert "Cloud" in result["error"]
    assert result["photo_metadata"]["status"] == "unavailable"


def test_enrichment_does_not_bind_metadata_to_changed_hash(tmp_path, monkeypatch):
    path = _jpeg(tmp_path / "changing.jpg")
    original_extract = enricher.extract_photo_metadata

    def mutate_then_extract(path, **kwargs):
        _jpeg(path, size=(80, 60))
        return original_extract(path, **kwargs)

    monkeypatch.setattr(enricher, "extract_photo_metadata", mutate_then_extract)
    result = enricher._disk_metadata(path)
    assert "changed" in result["error"]
    row = File(path=str(path), filename=path.name, session_id="example",
               photo_metadata="old", hash_md5="old", hash_sha256="old")
    assert enricher._apply_disk_result(row, result) == "errors"
    assert row.photo_metadata is None and row.hash_sha256 is None and row.hash_md5 is None


def test_enrichment_persists_evidence_and_clears_it_on_nonimage_or_error(tmp_path):
    path = _jpeg(tmp_path / "photo.jpg", exif=_capture_exif())
    row = File(path=str(path), filename=path.name, session_id="example")
    assert enricher._apply_disk_result(row, enricher._disk_metadata(path)) == "enriched"
    evidence = json.loads(row.photo_metadata)
    assert evidence["source_sha256"] == row.hash_sha256
    assert evidence["fields"]["capture_time"] == "2024-02-29T12:34:56"
    document = tmp_path / "document.txt"
    document.write_text("text")
    assert enricher._apply_disk_result(row, enricher._disk_metadata(document)) == "enriched"
    assert row.photo_metadata is None and row.hash_perceptual is None and row.date_exif is None
    row.photo_metadata = "stale"
    assert enricher._apply_disk_result(row, {"missing": True}) == "errors"
    assert row.photo_metadata is None


def test_additive_migration_preserves_old_rows_and_null_means_uninspected(tmp_path):
    path = tmp_path / "old.db"
    engine = init_db(path)
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        row = File(session_id=owner.id, filename="photo.jpg", path=str(tmp_path / "photo.jpg"),
                   ai_description="Original analysis", hash_sha256="a" * 64)
        db.add(row)
        db.commit()
        file_id = row.id
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE files DROP COLUMN photo_metadata"))
    engine.dispose()
    engine = init_db(path)
    assert "photo_metadata" in {column["name"] for column in inspect(engine).get_columns("files")}
    with Session(engine) as db:
        row = db.get(File, file_id)
        assert row.photo_metadata is None and row.ai_description == "Original analysis"
        assert row.hash_sha256 == "a" * 64


def _indexed_pair(tmp_path):
    engine = init_db(tmp_path / "index.db")
    paths = [_jpeg(tmp_path / "original.jpg", exif=_capture_exif()), _jpeg(tmp_path / "copy.jpg")]
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        files = []
        for path in paths:
            row = File(session_id=owner.id, filename=path.name, path=str(path),
                       extension=".jpg", mime_type="image/jpeg", status=FileStatus.ANALYZED,
                       hash_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), ai_description="Preserve AI")
            db.add(row)
            db.flush()
            files.append(row)
        group = DuplicateGroup(session_id=owner.id, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="test", keep_file_id=files[0].id)
        db.add(group)
        db.flush()
        for row in files:
            db.add(DuplicateMember(group_id=group.id, file_id=row.id))
        for status in (ProposalStatus.APPROVED, ProposalStatus.MODIFIED,
                       ProposalStatus.REJECTED, ProposalStatus.APPLIED):
            db.add(Proposal(file_id=files[1].id, duplicate_group_id=group.id,
                            proposal_type=ProposalType.MARK_DUPLICATE, status=status))
        db.commit()
        sid, ids = owner.id, [row.id for row in files]
    return engine, sid, ids, paths


def test_explicit_refresh_preserves_ai_status_and_resets_related_approvals(tmp_path):
    engine, sid, ids, paths = _indexed_pair(tmp_path)
    originals = [path.read_bytes() for path in paths]
    counts = enricher.refresh_photo_metadata(sid, workers=2)
    assert counts == {"updated": 2, "skipped": 0, "unknown": 0}
    with Session(engine) as db:
        for file_id in ids:
            row = db.get(File, file_id)
            assert row.status == FileStatus.ANALYZED and row.ai_description == "Preserve AI"
            assert json.loads(row.photo_metadata)["source_sha256"] == row.hash_sha256
        statuses = [row.status for row in db.query(Proposal).order_by(Proposal.id)]
        assert statuses == [ProposalStatus.PENDING, ProposalStatus.PENDING,
                            ProposalStatus.REJECTED, ProposalStatus.APPLIED]
        approved = db.query(Proposal).first()
        approved.status = ProposalStatus.APPROVED
        db.commit()
    assert enricher.refresh_photo_metadata(sid) == {"updated": 0, "skipped": 2, "unknown": 0}
    with Session(engine) as db:
        assert db.query(Proposal).first().status == ProposalStatus.APPROVED
    assert [path.read_bytes() for path in paths] == originals


def test_refresh_changed_keeper_never_overwrites_indexed_hash(tmp_path):
    engine, sid, ids, paths = _indexed_pair(tmp_path)
    enricher.refresh_photo_metadata(sid)
    with Session(engine) as db:
        original_hash = db.get(File, ids[0]).hash_sha256
        db.query(Proposal).first().status = ProposalStatus.APPROVED
        db.commit()
    _jpeg(paths[0], size=(40, 30))
    assert enricher.refresh_photo_metadata(sid) == {"updated": 1, "skipped": 1, "unknown": 1}
    with Session(engine) as db:
        original = db.get(File, ids[0])
        assert original.hash_sha256 == original_hash and original.status == FileStatus.ANALYZED
        assert json.loads(original.photo_metadata)["status"] == "unavailable"
        assert json.loads(original.photo_metadata)["source_sha256"] is None
        assert db.query(Proposal).first().status == ProposalStatus.PENDING


def test_refresh_missing_index_hash_does_not_hash_or_decode(tmp_path, monkeypatch):
    path = _jpeg(tmp_path / "photo.jpg")
    monkeypatch.setattr(enricher, "_content_hashes", lambda path: pytest.fail("No indexed hash to bind"))
    evidence = enricher._refresh_photo({"path": str(path), "hash_sha256": None})
    assert evidence["status"] == "unavailable"


def test_refresh_photos_cli_backfills_saved_session_preserving_keeper_and_analysis(tmp_path):
    from typer.testing import CliRunner
    from donedatahoarder.cli import app

    engine, sid, ids, paths = _indexed_pair(tmp_path)
    result = CliRunner().invoke(app, [
        "refresh-photos", "--db", str(tmp_path / "index.db"),
        "--session", sid, "--workers", "2",
    ])
    assert result.exit_code == 0, result.output
    assert "updated: 2" in result.output and "keepers are preserved" in result.output
    with Session(engine) as db:
        assert db.query(DuplicateGroup).one().keep_file_id == ids[0]
        row = db.get(File, ids[0])
        assert row.ai_description == "Preserve AI" and row.status == FileStatus.ANALYZED
        assert json.loads(row.photo_metadata)["fields"]["camera_model"] == "EOS R5"
        assert db.query(Proposal).first().status == ProposalStatus.PENDING

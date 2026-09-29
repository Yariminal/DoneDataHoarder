"""CAD routing uses bounded source structure, never filename-only vision."""
import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers.document import DocumentAnalyzer
from donedatahoarder.analyzers.dxf import DxfAnalyzer, MAX_DXF_PREFIX_BYTES
from donedatahoarder.analyzers.image import ImageAnalyzer
from donedatahoarder.core.preflight import estimate_collection
from donedatahoarder.db.models import File, FileStatus, UserSession
from donedatahoarder.db.session import get_engine, init_db


class NoModelClient:
    text_model = "must-not-call"
    vision_model = "must-not-call"

    def generate_json(self, *_args, **_kwargs):
        pytest.fail("CAD format inspection must not call a model")

    def model_digest(self, *_args, **_kwargs):
        pytest.fail("deterministic format inspection must not inspect model metadata")


def _record(tmp_path, name, data, mime):
    source = tmp_path / name
    source.write_bytes(data)
    return File(path=str(source), filename=name, extension=source.suffix,
                mime_type=mime, size_bytes=len(data),
                hash_sha256=hashlib.sha256(data).hexdigest(),
                status=FileStatus.ENRICHED)


def _run_one(tmp_path, monkeypatch, name, data, mime):
    init_db(tmp_path / "state.sqlite")
    record = _record(tmp_path, name, data, mime)
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path), name="format routing")
        db.add(user)
        db.flush()
        record.session_id = user.id
        db.add(record)
        db.commit()
        file_id = record.id
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "untrusted context")
    client = NoModelClient()
    analyzers = [DxfAnalyzer(), ImageAnalyzer(client), DocumentAnalyzer(client)]
    result = pipeline._process_one_file(file_id, get_engine(), analyzers,
                                        client, set())
    with Session(get_engine()) as db:
        saved = db.get(File, file_id)
        db.expunge(saved)
    return result, saved


_DXF = (b"0\nSECTION\n2\nENTITIES\n"
        b"0\nLINE\n10\n123.456\n20\n789.012\n"
        b"0\nTEXT\n1\nIGNORE_THIS_INJECTION\n"
        b"0\nENDSEC\n0\nEOF\n")


@pytest.mark.parametrize("mime", ["application/octet-stream", "text/plain",
                                   "image/vnd.dwg", None])
def test_ascii_dxf_routes_by_extension_without_model(tmp_path, monkeypatch, mime):
    result, saved = _run_one(tmp_path, monkeypatch, "drawing.dxf", _DXF, mime)
    assert result[1] == "analyzed"
    assert saved.analysis_outcome == "metadata_only"
    assert saved.analysis_evidence_source == "metadata"
    assert saved.analysis_extractor_version.startswith("ascii_dxf_structure/")
    assert saved.ai_model is None
    assert saved.analysis_model_tag is None
    assert saved.analysis_model_digest is None
    assert saved.analysis_prompt_version is None
    assert saved.ai_confidence is None
    assert saved.ai_suggested_name is None
    assert saved.ai_tags == "[]"
    assert "2 recognized entity markers" in saved.ai_description
    assert "IGNORE_THIS_INJECTION" not in saved.ai_description
    assert "123.456" not in saved.ai_description
    assert "drawing" not in saved.ai_description


@pytest.mark.parametrize("name,mime,data", [
    ("source.dwg", "image/vnd.dwg", b"AC1027more bytes"),
    ("source.dwg", "text/plain", b"AC1027more bytes"),
    ("source.bak", "image/vnd.dwg", b"backup bytes"),
    ("source.bak", "application/octet-stream", b"AC1027more bytes"),
])
def test_dwg_and_recognized_cad_backups_do_not_reach_image_analyzer(
        tmp_path, monkeypatch, name, mime, data):
    result, saved = _run_one(tmp_path, monkeypatch, name, data, mime)
    assert result[1] == "skipped"
    assert saved.analysis_outcome == "skipped"
    assert saved.analysis_reason == "unsupported_type"
    assert saved.ai_model is None


def test_generic_readable_backup_keeps_document_route(tmp_path):
    path = tmp_path / "notes.bak"
    path.write_text("A readable backup", encoding="utf-8")
    client = NoModelClient()
    analyzer = pipeline._get_analyzer(
        [ImageAnalyzer(client), DocumentAnalyzer(client)],
        "text/plain", ".bak", str(path),
    )
    assert isinstance(analyzer, DocumentAnalyzer)


def test_text_backup_with_ambiguous_ac10_prefix_keeps_document_route(tmp_path):
    path = tmp_path / "notes.bak"
    path.write_bytes(b"AC10AB is an ordinary text label\n")
    client = NoModelClient()
    analyzer = pipeline._get_analyzer(
        [ImageAnalyzer(client), DocumentAnalyzer(client)],
        "text/plain", ".bak", str(path),
    )
    assert isinstance(analyzer, DocumentAnalyzer)


def test_swapped_backup_symlink_is_rejected_before_header_read(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    collection.mkdir()
    source = collection / "notes.bak"
    source.write_bytes(b"Ordinary backup")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "outside.bak"
    target.write_bytes(b"AC1027outside")
    init_db(tmp_path / "state.sqlite")
    with Session(get_engine()) as db:
        owner = UserSession(root_path=str(collection))
        db.add(owner)
        db.flush()
        row = File(session_id=owner.id, path=str(source), filename=source.name,
                   extension=".bak", mime_type="text/plain",
                   size_bytes=len(b"Ordinary backup"), hash_sha256=original_hash,
                   status=FileStatus.ENRICHED)
        db.add(row)
        db.commit()
        file_id = row.id
    source.unlink()
    try:
        os.symlink(target, source)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable on this host")
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "")
    client = NoModelClient()
    analyzers = [ImageAnalyzer(client), DocumentAnalyzer(client)]
    assert pipeline._get_analyzer(analyzers, "text/plain", ".bak", str(source)) == analyzers[1]
    result = pipeline._process_one_file(file_id, get_engine(), analyzers,
                                        client, set())
    assert result[1] == "error"
    with Session(get_engine()) as db:
        saved = db.get(File, file_id)
        assert saved.analysis_reason == "stale_enrichment"
        assert saved.ai_model is None


def test_cad_backup_header_guard_never_opens_reparse_path(monkeypatch, tmp_path):
    parent = tmp_path / "swapped"
    path = parent / "backup.bak"
    monkeypatch.setattr(pipeline, "_is_link_or_reparse", lambda part: part == parent)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs:
                        pytest.fail("reparse path must not be opened"))
    assert pipeline._is_cad_backup("text/plain", str(path)) is False


@pytest.mark.parametrize("data", [
    b"AutoCAD Binary DXF\r\n\x1a\x00more data",
    b"0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n\x00binary\n",
    b"0\nSECTION\ninvalid-code\nENTITIES\n",
    b"0\nLINE\n10\n42\n",
    b"0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n42\n",
    b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n",
])
def test_binary_and_malformed_dxf_skip_without_model(tmp_path, monkeypatch, data):
    result, saved = _run_one(tmp_path, monkeypatch, "broken.dxf", data,
                                 "application/octet-stream")
    assert result[1] == "skipped"
    assert saved.analysis_outcome == "skipped"
    assert saved.analysis_reason in {"unsupported_type", "unreadable_content"}
    assert saved.ai_model is None
    assert saved.ai_description == ""


def test_large_dxf_reports_only_bounded_prefix(tmp_path, monkeypatch):
    # A coordinate-heavy prefix has no subject evidence. The marker beyond the
    # cap must never appear in the result, and counts must be prefix-qualified.
    data = b"0\nSECTION\n2\nENTITIES\n" + b"0\nVERTEX\n10\n1\n20\n2\n" * 25000
    data += b"0\nTEXT\n1\nSECRET_BEYOND_PREFIX\n"
    assert len(data) > MAX_DXF_PREFIX_BYTES
    result, saved = _run_one(tmp_path, monkeypatch, "large.dxf", data,
                                 "text/plain")
    assert result[1] == "analyzed"
    assert saved.analysis_reason == "prefix_only"
    assert "first 256 KiB" in saved.ai_description
    assert "Counts describe the inspected prefix only" in saved.ai_description
    assert "SECRET_BEYOND_PREFIX" not in saved.ai_description


def test_ascii_dxf_accepts_bounded_signed_group_codes(tmp_path):
    record = _record(tmp_path, "drawing.dxf",
                     b"0\nSECTION\n2\nHEADER\n-1\nhandle\n0\nENDSEC\n0\nEOF\n",
                     "application/octet-stream")
    result = DxfAnalyzer().analyze(record, "")
    assert result.outcome == "metadata_only"
    assert "complete file" in result.description


def test_preflight_counts_dxf_as_deterministic_candidate_not_ai(tmp_path):
    for name in ("drawing.dxf", "draft.dwg", "backup.bak", "notes.txt"):
        (tmp_path / name).write_bytes(b"sample")
    report = estimate_collection(tmp_path)
    assert report["ai_candidate_files_estimate"] == 1
    assert report["deterministic_metadata_candidates_estimate"] == 1
    assert report["unsupported_files_estimate"] == 2

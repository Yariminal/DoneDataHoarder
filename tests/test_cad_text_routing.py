"""Signature-gated CAD text metadata stays bounded and never invokes AI."""
import hashlib
import re
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers.cad_text import (
    CadTextAnalyzer, MAX_CAD_TEXT_PREFIX_BYTES,
)
from donedatahoarder.analyzers.document import DocumentAnalyzer, extract_document
from donedatahoarder.analyzers.format_policy import disposition
from donedatahoarder.analyzers.image import ImageAnalyzer
from donedatahoarder.core.preflight import estimate_collection
from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalType, UserSession
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.proposals.namer.core import (
    _content_verified_for_naming, generate_proposals,
)
from donedatahoarder.proposals.organizer.core import _organizer_move_allowed


class NoModelClient:
    text_model = "must-not-call"
    vision_model = "must-not-call"

    def generate_json(self, *_args, **_kwargs):
        pytest.fail("signature metadata must not call a model")

    def model_digest(self, *_args, **_kwargs):
        pytest.fail("signature metadata must not inspect model metadata")


def _run_one(tmp_path, monkeypatch, name, data, mime="application/octet-stream",
             before_process=None):
    init_db(tmp_path / "state.sqlite")
    source = tmp_path / name
    source.write_bytes(data)
    with Session(get_engine()) as db:
        owner = UserSession(root_path=str(tmp_path), name="format routing")
        db.add(owner)
        db.flush()
        record = File(
            session_id=owner.id, path=str(source), filename=name,
            extension=source.suffix, mime_type=mime, size_bytes=len(data),
            hash_sha256=hashlib.sha256(data).hexdigest(),
            status=FileStatus.ENRICHED,
        )
        db.add(record)
        db.commit()
        file_id = record.id
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "untrusted context")
    client = NoModelClient()
    analyzers = [CadTextAnalyzer(), ImageAnalyzer(client), DocumentAnalyzer(client)]
    if before_process is not None:
        before_process()
    result = pipeline._process_one_file(file_id, get_engine(), analyzers, client, set())
    with Session(get_engine()) as db:
        saved = db.get(File, file_id)
        db.expunge(saved)
    return result, saved


def _plot_row(date="03/08/17 15:42:43", device="DWG To PDF.pc3"):
    # Legacy single-byte Hebrew is present, but never copied into the result.
    return (rf"C:\\ארכיון\\drawing.dwg,Sheet A,{date},Operator,{device},"
            "A4 (210 x 297 mm),1:10,\r\n").encode("cp1255")


_SHAPE = (b"*0,4,shape_font\n21,7,0,0\n"
          b"*32,5,space\n2,8,(19,0),0\n"
          b"*33,4,mark\n2,8,(1,-2),01A,0\n")


@pytest.mark.parametrize("mime", ["application/octet-stream", "text/plain", "image/png"])
def test_cad_plot_log_is_metadata_only_without_model_or_naming(tmp_path, monkeypatch, mime):
    result, saved = _run_one(tmp_path, monkeypatch, "plot.log",
                             _plot_row() + _plot_row(device="Other Plotter"), mime)
    assert result[1] == "analyzed"
    assert saved.analysis_outcome == "metadata_only"
    assert saved.analysis_evidence_source == "metadata"
    assert saved.analysis_extractor_version.startswith("cad_plot_log_structure/")
    assert saved.analysis_reason == "limited_content"
    assert "2 event rows" in saved.ai_description
    assert "1 distinct recorded plot date" in saved.ai_description
    assert "2 plot devices" in saved.ai_description
    assert "ארכיון" not in saved.ai_description
    assert "Operator" not in saved.ai_description
    assert "03/08/17" not in saved.ai_description
    assert saved.ai_suggested_name is None
    assert saved.ai_tags == "[]"
    assert saved.analysis_detected_date is None
    assert saved.ai_model is None and saved.analysis_model_digest is None
    assert not _content_verified_for_naming(saved)


def test_ordinary_text_log_keeps_existing_document_route(tmp_path):
    source = tmp_path / "service.log"
    source.write_text("Service started and completed normally.\n", encoding="utf-8")
    client = NoModelClient()
    analyzer = pipeline._get_analyzer(
        [CadTextAnalyzer(), DocumentAnalyzer(client)],
        "text/plain", ".log", str(source),
    )
    assert isinstance(analyzer, DocumentAnalyzer)
    assert "Service started" in extract_document(source, "text/plain").text


def test_cad_signature_overrides_text_log_mime(tmp_path):
    source = tmp_path / "plot.log"
    source.write_bytes(_plot_row())
    client = NoModelClient()
    analyzer = pipeline._get_analyzer(
        [CadTextAnalyzer(), DocumentAnalyzer(client)],
        "text/plain", ".log", str(source),
    )
    assert isinstance(analyzer, CadTextAnalyzer)


def test_equivalent_plot_date_formats_count_as_one_calendar_day(tmp_path, monkeypatch):
    data = _plot_row(date="03/08/17 15:42:43") + _plot_row(
        date="03/08/2017 16:20:00")
    result, saved = _run_one(tmp_path, monkeypatch, "plot.log", data)
    assert result[1] == "analyzed"
    assert "2 event rows" in saved.ai_description
    assert "1 distinct recorded plot date" in saved.ai_description
    assert saved.analysis_detected_date is None


@pytest.mark.parametrize("name,data", [
    ("plot.log", _plot_row()),
    ("source.shp", _SHAPE),
])
def test_reparse_guard_rejects_cad_text_before_read_or_model(
        tmp_path, monkeypatch, name, data):
    def trap_reads():
        monkeypatch.setattr(pipeline, "_has_reparse_component", lambda _path: True)
        monkeypatch.setattr(pipeline, "eligible_hash", lambda _file:
                            pytest.fail("reparse path must be rejected before hashing"))
        monkeypatch.setattr(CadTextAnalyzer, "analyze", lambda *_args:
                            pytest.fail("reparse path must be rejected before reading"))

    result, saved = _run_one(tmp_path, monkeypatch, name, data,
                             before_process=trap_reads)
    assert result[1] == "error"
    assert saved.status == FileStatus.ERROR
    assert saved.analysis_outcome == "failed"
    assert saved.analysis_reason == "stale_enrichment"
    assert saved.ai_model is None


def test_metadata_only_result_cannot_generate_semantic_rename_or_project_move(
        tmp_path, monkeypatch):
    _, saved = _run_one(tmp_path, monkeypatch, "source.shp", _SHAPE)
    assert saved.analysis_outcome == "metadata_only"
    generated = generate_proposals(session_id=saved.session_id)
    assert generated["rename"] == 0
    with Session(get_engine()) as db:
        assert db.query(Proposal).filter(
            Proposal.proposal_type == ProposalType.RENAME
        ).count() == 0

    project = tmp_path / "Named_Project"
    project.mkdir()
    source = project / "source.shp"
    source.write_bytes(_SHAPE)
    project_row = SimpleNamespace(
        path=str(source), id=1, extension=".shp", mime_type="application/octet-stream",
        analysis_outcome="metadata_only", analysis_evidence_source="metadata",
        ai_description=saved.ai_description, ai_tags="[]", date_exif=None,
    )
    protection = SimpleNamespace(assess=lambda _path: SimpleNamespace(protected=False))
    assert not _organizer_move_allowed(
        project_row, project / "Invented_Subject" / "source.shp", tmp_path,
        protection, set(), {project},
    )


def test_autocad_shp_declarations_are_metadata_only_without_opcodes_in_description(
        tmp_path, monkeypatch):
    result, saved = _run_one(tmp_path, monkeypatch, "source.shp", _SHAPE, "text/plain")
    assert result[1] == "analyzed"
    assert saved.analysis_outcome == "metadata_only"
    assert saved.analysis_evidence_source == "metadata"
    assert saved.analysis_extractor_version.startswith("autocad_shp_structure/")
    assert "2 shape declarations" in saved.ai_description
    assert "plus one *0 font header" in saved.ai_description
    assert "3 opcode lines" in saved.ai_description
    assert "space" not in saved.ai_description
    assert "01A" not in saved.ai_description
    assert saved.ai_suggested_name is None and saved.ai_tags == "[]"
    assert saved.ai_model is None and saved.analysis_detected_date is None
    assert not _content_verified_for_naming(saved)


def test_shape_wrapped_coordinates_signed_offsets_and_trailing_blanks(
        tmp_path, monkeypatch):
    data = (b"*0,4,font\r\n21,7,0,0\r\n"
            b"*48,53,shape\r\n2,8,-7,-28,(1,1)\r\n(1,2),0\r\n"
            b"*250,469,(c)\r\n2,0\r\n\r\n")
    result, saved = _run_one(tmp_path, monkeypatch, "source.shp", data)
    assert result[1] == "analyzed"
    assert saved.analysis_outcome == "metadata_only"
    assert "2 shape declarations" in saved.ai_description
    assert "4 opcode lines" in saved.ai_description


@pytest.mark.parametrize("name,data", [
    ("unrelated.log", b"Build succeeded at 03/08/17 15:42:43\n"),
    ("random_legacy.log", "זה טקסט רגיל".encode("cp1255")),
    ("binary.log", b"drawing.dwg,Sheet,03/08/17 15:42:43\x00garbage"),
    ("bad_date.log", _plot_row(date="99/99/17 15:42:43")),
    ("gis.shp", b"\x00\x00\x27\x0a" + b"\x00" * 96),
    ("random.shp", b"*0,4,title\nHello world, not opcodes\n"),
    ("malformed.shp", b"*0,4,font\n21,7,0,0\n*32,5,space\n2,8,(1,-2\n"),
])
def test_unrelated_binary_and_malformed_files_remain_unsupported(
        tmp_path, monkeypatch, name, data):
    result, saved = _run_one(tmp_path, monkeypatch, name, data)
    assert result[1] == "skipped"
    assert saved.analysis_outcome == "skipped"
    assert saved.analysis_reason in {"unsupported_type", "unreadable_content"}
    assert saved.ai_model is None
    assert not saved.ai_suggested_name


def test_bounded_log_prefix_count_is_explicit(tmp_path, monkeypatch):
    rows = [_plot_row() for _ in range(1000)]
    data = b"".join(rows)
    assert len(data) > MAX_CAD_TEXT_PREFIX_BYTES
    result, saved = _run_one(tmp_path, monkeypatch, "large.log", data)
    assert result[1] == "analyzed"
    assert saved.analysis_reason == "prefix_only"
    assert "first 64 KiB" in saved.ai_description
    assert "inspected prefix only" in saved.ai_description
    count = int(re.search(r": (\d+) event rows", saved.ai_description).group(1))
    assert 0 < count < 1000


def test_bounded_shape_prefix_count_is_explicit(tmp_path, monkeypatch):
    data = b"*0,4,font\n21,7,0,0\n" + b"".join(
        f"*{index},5,shape{index}\n2,8,(1,-2),01A,0\n".encode("ascii")
        for index in range(1, 3000)
    )
    assert len(data) > MAX_CAD_TEXT_PREFIX_BYTES
    result, saved = _run_one(tmp_path, monkeypatch, "large.shp", data)
    assert result[1] == "analyzed"
    assert saved.analysis_reason == "prefix_only"
    assert "first 64 KiB" in saved.ai_description
    assert "inspected prefix only" in saved.ai_description
    count = int(re.search(r": (\d+) shape declarations", saved.ai_description).group(1))
    assert 0 < count < 2999


def test_preflight_and_inventory_hints_do_not_claim_signature_is_proven(tmp_path):
    for name in ("drawing.dxf", "history.log", "font.shp", "unknown.bin"):
        (tmp_path / name).write_bytes(b"unknown content")
    report = estimate_collection(tmp_path)
    assert report["deterministic_metadata_candidates_estimate"] == 2
    assert report["ai_candidate_files_estimate"] == 1
    assert report["unsupported_files_estimate"] == 1
    assert "may still be unsupported" in report["estimate_note"]
    assert disposition(tmp_path / "history.log") == (
        "cad_plot_metadata_or_existing_text_route_or_unsupported")
    assert disposition(tmp_path / "font.shp").endswith("metadata_or_unsupported")

"""Extraction, evidence, bounded submission, and additive schema regressions."""

import sqlite3
import threading
import zipfile

import pytest
from sqlalchemy.orm import Session

from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers import document
from donedatahoarder.analyzers import video
from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer
from donedatahoarder.analyzers.document import extract_document
from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import get_engine, init_db


def test_pptx_reads_slide_text_not_zip_bytes(tmp_path):
    path = tmp_path / "speaker_notes.pptx"
    slide = ('<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
             '<a:t>Quarterly</a:t><a:t>Revenue</a:t></p:sld>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
    result = extract_document(path)
    assert result.text == "Quarterly Revenue"
    assert result.extractor == "pptx_openxml"
    assert result.reason is None
    assert "PK" not in result.text


def test_binary_text_and_legacy_office_have_explicit_reasons(tmp_path):
    binary = tmp_path / "fake.txt"
    binary.write_bytes(b"PK\x03\x04\0\x01\x02data")
    assert extract_document(binary).reason == "unreadable_content"
    legacy = tmp_path / "slides.ppt"
    legacy.write_bytes(b"binary")
    assert extract_document(legacy).reason == "unsupported_type"


def test_pdf_backed_illustrator_uses_rendered_page_vision(tmp_path, monkeypatch):
    path = tmp_path / "illustration.ai"
    path.write_bytes(b"%PDF-1.7\nillustrator content")
    monkeypatch.setattr(document, "_extract_pdf", lambda _path: "")
    rendered = []

    def fake_render(given_path, max_pages=document.MAX_VISION_PAGES):
        rendered.append((given_path, max_pages))
        return [b"page image"]

    monkeypatch.setattr(document, "_render_pdf_pages_as_jpegs", fake_render)

    class Client:
        text_model = "gemma4:26b"
        vision_model = "gemma4:26b"

        def generate_json(self, prompt, **kwargs):
            assert kwargs["image_bytes"] == b"page image"
            assert "illustration context" in prompt
            return {"description": "Illustrated cover", "suggested_name": "illustrated_cover",
                    "tags": ["cover"], "document_type": "cover", "confidence": 0.8}

        def model_digest(self, model):
            assert model == self.vision_model
            return "sha256:vision-model"

    record = File(path=str(path), filename=path.name, extension=".ai",
                  mime_type="application/postscript")
    result = document.DocumentAnalyzer(Client()).analyze(record, "illustration context")
    assert rendered == [(path, document.MAX_VISION_PAGES)]
    assert result.evidence_source == "vision"
    assert result.extractor == "pdfium_render"
    assert result.description == "Illustrated cover"
    assert result.content_available is True

    monkeypatch.setattr(pipeline, "build_context", lambda _row: "illustration context")
    init_db(tmp_path / "index.db")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path), name="illustrator")
        db.add(user)
        db.flush()
        record.session_id = user.id
        record.status = FileStatus.ENRICHED
        record.size_bytes = path.stat().st_size
        db.add(record)
        db.commit()
        file_id = record.id

    client = Client()
    _, status, _ = pipeline._process_one_file(
        file_id, get_engine(), [document.DocumentAnalyzer(client)], client,
        set(), use_cache=False,
    )
    assert status == "analyzed"
    with Session(get_engine()) as db:
        saved = db.get(File, file_id)
        assert saved.status == FileStatus.ANALYZED
        assert saved.analysis_evidence_source == "vision"
        assert saved.analysis_extractor_version == "pdfium_render/extractors-v4-2026-09-28"
        assert saved.analysis_prompt_version == "analysis-v3-2026-09-28"
        assert saved.ai_model == "gemma4:26b"
        assert saved.analysis_cache_hit is False


def test_non_pdf_illustrator_remains_unsupported(tmp_path, monkeypatch):
    path = tmp_path / "legacy.ai"
    path.write_bytes(b"%!PS-Adobe-3.0\n%%Creator: Illustrator")
    monkeypatch.setattr(document, "_render_pdf_pages_as_jpegs",
                        lambda _path: pytest.fail("non-PDF Illustrator must not render"))

    class Client:
        def generate_json(self, *_args, **_kwargs):
            pytest.fail("unsupported Illustrator must not call a model")

    assert extract_document(path, "application/pdf").reason == "unsupported_type"
    record = File(path=str(path), filename=path.name, extension=".ai",
                  mime_type="application/pdf")
    result = document.DocumentAnalyzer(Client()).analyze(record, "context")
    assert result.outcome == "skipped"
    assert result.reason == "unsupported_type"
    assert result.evidence_source == "none"


def test_pdf_backed_illustrator_provider_failure_has_no_saved_evidence(tmp_path, monkeypatch):
    from donedatahoarder.ai.json_utils import LooseDict, generate_json_with_retry

    path = tmp_path / "failed.ai"
    path.write_bytes(b"%PDF-1.7\nillustrator content")
    monkeypatch.setattr(document, "_extract_pdf", lambda _path: "")
    monkeypatch.setattr(document, "_render_pdf_pages_as_jpegs",
                        lambda _path: [b"page image"])
    monkeypatch.setattr("donedatahoarder.ai.json_utils.time.sleep", lambda _: None)
    init_db(tmp_path / "index.db")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path), name="illustrator")
        db.add(user)
        db.flush()
        file = File(session_id=user.id, path=str(path), filename=path.name,
                    extension=".ai", mime_type="application/pdf",
                    size_bytes=path.stat().st_size, status=FileStatus.ENRICHED)
        db.add(file)
        db.commit()
        file_id = file.id

    attempts = []

    class Client:
        text_model = "gemma4:26b"
        vision_model = "gemma4:26b"

        def generate_json(self, prompt, **_kwargs):
            def malformed_response(**kwargs):
                attempts.append(kwargs)
                return '{"description" "missing colon"}'

            return generate_json_with_retry(malformed_response, prompt, LooseDict)

    _, status, _ = pipeline._process_one_file(
        file_id, get_engine(), [document.DocumentAnalyzer(Client())], Client(),
        set(), use_cache=False,
    )
    assert status == "error"
    assert len(attempts) == 3
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        assert file.status == FileStatus.ERROR
        assert file.analysis_outcome == "failed"
        assert file.ai_description is None
        assert file.ai_model is None
        assert file.analysis_cache_hit is False


def test_pptx_uses_presentation_order_and_caps_expanded_xml(tmp_path, monkeypatch):
    path = tmp_path / "reordered.pptx"
    presentation = ('<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<p:sldIdLst><p:sldId id="2" r:id="rId2"/>'
                    '<p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="slides/slide1.xml"/>'
            '<Relationship Id="rId2" Target="/ppt/slides/slide2.xml"/></Relationships>')
    slide = lambda word: ('<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                          f'<a:t>{word}</a:t></p:sld>')
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", rels)
        archive.writestr("ppt/slides/slide1.xml", slide("First"))
        archive.writestr("ppt/slides/slide2.xml", slide("Second"))
    assert extract_document(path).text == "Second\nFirst"
    monkeypatch.setattr(document, "MAX_PPTX_XML_ENTRY_BYTES", 100)
    assert extract_document(path).reason == "oversized_content"


def test_short_pptx_text_is_limited_evidence_not_confident_content(tmp_path):
    path = tmp_path / "milestone.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:t>Bee’s Health</a:t></p:sld>',
        )

    class Client:
        def generate_json(self, prompt, **_kwargs):
            assert "Bee’s Health" in prompt
            assert "PK\\x03" not in prompt
            return {"description": "Bee health slide", "suggested_name": "bee_health",
                    "confidence": 0.95, "tags": ["bees"]}

    record = File(path=str(path), filename=path.name, extension=".pptx")
    result = document.DocumentAnalyzer(Client()).analyze(record, "milestone")
    assert result.evidence_source == "text"
    assert result.content_chars == 12
    assert result.outcome == "context_only"
    assert result.reason == "limited_content"
    assert result.confidence <= 0.4


def test_video_reports_missing_decoder_executable_not_unreadable_media(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "_HAS_FFMPEG", True)  # Python wrapper exists
    monkeypatch.setattr(video.shutil, "which", lambda tool: "ffmpeg" if tool == "ffmpeg" else None)
    monkeypatch.setattr(video, "_transcribe_with_reason",
                        lambda path, model: ("", "missing_dependency"))

    class Client:
        def generate_json(self, prompt, **_kwargs):
            assert "ffprobe executable unavailable" in prompt
            return {"description": "Unknown video", "suggested_name": "unknown_video",
                    "confidence": 0.9, "tags": []}

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    result = video.VideoAnalyzer(Client()).analyze(
        File(path=str(path), filename=path.name, extension=".mp4", mime_type="video/mp4"),
        "clip",
    )
    assert not result.content_available
    assert result.reason == "missing_dependency"
    assert result.evidence_source == "filename_only"


def test_whisper_respects_offline_mode_and_classifies_absent_cache(tmp_path, monkeypatch):
    class LocalEntryNotFoundError(Exception):
        pass

    seen = {}

    def fake_model(*args, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(video, "WhisperModel", fake_model, raising=False)
    monkeypatch.setattr(video, "_whisper_model_instance", None)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    video._get_whisper_model("base")
    assert seen["local_files_only"] is True
    monkeypatch.delenv("HF_HUB_OFFLINE")
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(video, "_whisper_model_instance", None)
    video._get_whisper_model("base")
    assert seen["local_files_only"] is False

    def missing_model(_size):
        raise LocalEntryNotFoundError("not cached")

    monkeypatch.setattr(video, "_HAS_WHISPER", True)
    monkeypatch.setattr(video, "_get_whisper_model", missing_model)
    assert video._transcribe_with_reason(tmp_path / "clip.mp4") == ("", "missing_dependency")


def test_limit_bounds_submissions_before_parallel_workers(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        for number in range(8):
            db.add(File(session_id=user.id, path=str(tmp_path / f"{number}.txt"),
                        filename=f"{number}.txt", extension=".txt", size_bytes=10,
                        status=FileStatus.ENRICHED))
        db.commit()
        user_id = user.id
    submitted = []

    def fake_process(file_id, *_args):
        submitted.append(file_id)
        with Session(get_engine()) as db:
            db.get(File, file_id).status = FileStatus.ANALYZED
            db.commit()
        return file_id, "analyzed", None

    monkeypatch.setattr(pipeline, "_process_one_file", fake_process)
    from donedatahoarder.ai import router
    monkeypatch.setattr(router, "get_client", lambda: object())
    result = list(pipeline.analyze_with_progress(
        workers=4, limit=3, session_id=user_id,
    ))[-1]
    assert result["analyzed"] == 3
    assert len(submitted) == 3


def test_cancel_while_paused_submits_no_work(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        db.add(File(session_id=user.id, path=str(tmp_path / "a.txt"),
                    filename="a.txt", extension=".txt", size_bytes=10,
                    status=FileStatus.ENRICHED))
        db.commit()
        user_id = user.id
    from donedatahoarder.ai import router
    monkeypatch.setattr(router, "get_client", lambda: object())
    submitted = []
    monkeypatch.setattr(pipeline, "_process_one_file", lambda *args: submitted.append(args[0]))
    pause = threading.Event()  # clear means paused
    cancel = threading.Event()
    outcomes = []

    def consume():
        outcomes.extend(pipeline.analyze_with_progress(
            workers=2, session_id=user_id, pause_event=pause,
            cancel_check=cancel.is_set,
        ))

    worker = threading.Thread(target=consume)
    worker.start()
    cancel.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert outcomes[-1]["cancelled"] is True
    assert submitted == []


def test_additive_file_and_duplicate_columns_preserve_rows(tmp_path):
    db_path = tmp_path / "index.db"
    init_db(db_path)
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        db.add(File(session_id=user.id, path=str(tmp_path / "a.txt"),
                    filename="a.txt", status=FileStatus.PENDING))
        db.commit()
        user_id = user.id
    get_engine().dispose()
    # Simulate an older current-schema database by removing only v2 fields.
    with sqlite3.connect(db_path) as raw:
        raw.execute("ALTER TABLE files DROP COLUMN analysis_outcome")
        raw.execute("ALTER TABLE duplicate_members DROP COLUMN distance_to_keeper")
        raw.execute("ALTER TABLE proposals DROP COLUMN review_kind")
        raw.commit()
    init_db(db_path)
    with Session(get_engine()) as db:
        files = db.query(File).filter_by(session_id=user_id).all()
        assert len(files) == 1
        assert files[0].analysis_outcome is None
        assert files[0].path == str(tmp_path / "a.txt")
    with sqlite3.connect(db_path) as raw:
        assert "distance_to_keeper" in {r[1] for r in raw.execute("PRAGMA table_info(duplicate_members)")}
        assert "review_kind" in {r[1] for r in raw.execute("PRAGMA table_info(proposals)")}


def test_legacy_database_is_never_dropped(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
        raw.execute("INSERT INTO files VALUES (7, 'irreplaceable')")
    try:
        init_db(path)
    except RuntimeError as exc:
        assert "will not erase" in str(exc)
    else:
        assert False, "legacy schema must require an explicit migration"
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT path FROM files WHERE id=7").fetchone() == ("irreplaceable",)


def test_force_rescan_invalidates_stale_analysis_and_approval(tmp_path):
    from donedatahoarder.core.scanner import scan

    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    path = root / "document.txt"
    path.write_text("original", encoding="utf-8")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root))
        db.add(user)
        db.commit()
        sid = user.id
    scan(root, session_id=sid)
    with Session(get_engine()) as db:
        file = db.query(File).filter_by(session_id=sid).one()
        file.status = FileStatus.PROPOSED
        file.ai_description = "Stale evidence"
        file.analysis_outcome = "content_verified"
        file.analysis_evidence_source = "text"
        file.hash_sha256 = "0" * 64
        db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                        proposed_value="wrong.txt", status=ProposalStatus.APPROVED))
        db.commit()
        fid = file.id
    path.write_text("changed", encoding="utf-8")
    scan(root, session_id=sid, force_rescan=True)
    with Session(get_engine()) as db:
        file = db.get(File, fid)
        proposal = db.query(Proposal).filter_by(file_id=fid).one()
        assert file.status == FileStatus.PENDING
        assert file.ai_description is None
        assert file.analysis_outcome is None
        assert file.hash_sha256 is None
        assert proposal.status == ProposalStatus.REJECTED


def test_nullable_duplicate_rebuild_preserves_group_row(tmp_path):
    from donedatahoarder.db.models import DuplicateGroup, DuplicateMember, DupeType

    path = tmp_path / "index.db"
    init_db(path)
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        file = File(session_id=user.id, path=str(tmp_path / "a.txt"),
                    filename="a.txt", status=FileStatus.PENDING)
        db.add(file)
        db.commit()
        sid = user.id
        file_id = file.id
    get_engine().dispose()
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=OFF")
        schemas = {name: raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0] for name in ("duplicate_groups", "duplicate_members", "proposals")}
        for name in ("duplicate_members", "proposals", "duplicate_groups"):
            raw.execute(f"DROP TABLE {name}")
        group_schema = schemas["duplicate_groups"].replace(
            "session_id VARCHAR(36)", "session_id VARCHAR(36) NOT NULL")
        raw.execute(group_schema)
        raw.execute(schemas["duplicate_members"])
        raw.execute(schemas["proposals"])
        raw.execute("INSERT INTO duplicate_groups (id,session_id,dupe_type,group_hash,keep_file_id) "
                    "VALUES (11,?,?,?,?)", (sid, "EXACT", "abc", file_id))
        raw.execute("INSERT INTO duplicate_members (id,group_id,file_id,similarity_score) "
                    "VALUES (12,11,?,1.0)", (file_id,))
        raw.execute("INSERT INTO proposals (id,file_id,proposal_type,status,duplicate_group_id) "
                    "VALUES (13,?,'MARK_DUPLICATE','PENDING',11)", (file_id,))
        raw.commit()
    init_db(path)
    with Session(get_engine()) as db:
        group = db.query(DuplicateGroup).filter_by(session_id=sid).one()
        assert group.group_hash == "abc"
        assert group.id == 11
        assert db.query(DuplicateMember).filter_by(group_id=11, file_id=file_id).one().id == 12
        assert db.query(Proposal).filter_by(duplicate_group_id=11, file_id=file_id).one().id == 13
    with sqlite3.connect(path) as raw:
        columns = {row[1]: row for row in raw.execute("PRAGMA table_info(duplicate_groups)")}
        assert columns["session_id"][3] == 0
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        unique_keys = [
            [column[2] for column in raw.execute(f"PRAGMA index_info('{index[1]}')")]
            for index in raw.execute("PRAGMA index_list('duplicate_groups')")
            if index[2]
        ]
        assert ["session_id", "dupe_type", "group_hash"] in unique_keys
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0  # new raw connection default
    with get_engine().connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_actual_model_tag_digest_and_evidence_are_persisted(tmp_path):
    init_db(tmp_path / "index.db")
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        file = File(session_id=user.id, path=str(tmp_path / "note.txt"),
                    filename="note.txt", extension=".txt", size_bytes=100,
                    status=FileStatus.ENRICHED)
        db.add(file)
        db.commit()
        file_id = file.id

    class Analyzer(BaseAnalyzer):
        def can_handle(self, mime_type, extension):
            return True

        def analyze(self, file_rec, context):
            return AnalysisResult(description="Quarterly revenue report",
                                  suggested_name="quarterly_revenue_report",
                                  confidence=0.8, evidence_source="text",
                                  content_chars=120, extractor="synthetic_text")

    class Client:
        text_model = "gemma4:26b"
        vision_model = "gemma4:26b"

        def model_digest(self, tag):
            assert tag == self.text_model
            return "sha256:synthetic"

    fid, status, _ = pipeline._process_one_file(
        file_id, get_engine(), [Analyzer()], Client(), set(),
    )
    assert (fid, status) == (file_id, "analyzed")
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        assert file.ai_model == "gemma4:26b"
        assert file.analysis_model_tag == "gemma4:26b"
        assert file.analysis_model_digest == "sha256:synthetic"
        assert file.analysis_outcome == "content_verified"
        assert file.analysis_evidence_source == "text"
        assert file.analysis_content_chars == 120
        assert file.analysis_prompt_version
        assert file.analysis_extractor_version.startswith("synthetic_text/")

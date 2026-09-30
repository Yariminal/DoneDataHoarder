"""Cache identity and opt-in sampling must preserve per-file evidence truth."""
import hashlib
import os
from pathlib import Path
from datetime import datetime
import pytest
import zipfile

from sqlalchemy.orm import Session

from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers import cache
from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer, PROMPT_VERSION
from donedatahoarder.analyzers.document import DocumentAnalyzer
from donedatahoarder.analyzers.image import ImageAnalyzer
from donedatahoarder.analyzers.video import VideoAnalyzer
from donedatahoarder.db.models import AnalysisCache, File, FileStatus, UserSession
from donedatahoarder.db.session import get_engine, init_db
from scripts.bench_analysis_economy import select_frame_window


class StubClient:
    text_model = "local:test"
    vision_model = "local:test"

    def __init__(self, digest="digest-one", text_model="local:test", vision_model="local:test"):
        self.digest = digest
        self.text_model = text_model
        self.vision_model = vision_model

    def model_digest(self, _model):
        return self.digest


class StubAnalyzer(BaseAnalyzer):
    def __init__(self):
        self.calls = 0

    def can_handle(self, mime_type, extension):
        return True

    def analyze(self, file_rec, context):
        self.calls += 1
        return AnalysisResult(
            description="Verified content", suggested_name="verified_content",
            tags=["verified"], confidence=0.8, evidence_source="text",
            extractor="stub", content_chars=30, outcome="content_verified",
        )


def _add_file(root, session_id, name, data=b"same content"):
    path = root / name
    path.write_bytes(data)
    with Session(get_engine()) as db:
        row = File(
            session_id=session_id, path=str(path), filename=name,
            extension=path.suffix, mime_type="image/png", size_bytes=len(data),
            hash_sha256=hashlib.sha256(data).hexdigest(), status=FileStatus.ENRICHED,
        )
        db.add(row)
        db.commit()
        return row.id


def test_verified_cache_reuses_only_identical_identity(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="cache")
        db.add(us)
        db.commit()
        session_id = us.id
    first = _add_file(root, session_id, "one.png")
    analyzer = StubAnalyzer()
    client = StubClient()
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "fixed context")

    assert pipeline._process_one_file(first, get_engine(), [analyzer], client, set())[1] == "analyzed"
    assert analyzer.calls == 1
    second = _add_file(root, session_id, "two.png")
    assert pipeline._process_one_file(second, get_engine(), [analyzer], client, set())[1] == "cached"
    assert analyzer.calls == 1
    with Session(get_engine()) as db:
        assert db.get(File, second).ai_description == "Verified content"
        assert db.get(File, second).analysis_outcome == "content_verified"

    third = _add_file(root, session_id, "three.png")
    assert pipeline._process_one_file(third, get_engine(), [analyzer], StubClient("new-digest"), set())[1] == "analyzed"
    fourth = _add_file(root, session_id, "four.png")
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "different context")
    assert pipeline._process_one_file(fourth, get_engine(), [analyzer], client, set())[1] == "analyzed"
    fifth = _add_file(root, session_id, "five.png", b"different bytes")
    assert pipeline._process_one_file(fifth, get_engine(), [analyzer], client, set())[1] == "analyzed"
    sixth = _add_file(root, session_id, "six.png")
    monkeypatch.setattr(cache, "EXTRACTOR_VERSION", "changed-extractor")
    assert pipeline._process_one_file(sixth, get_engine(), [analyzer], client, set())[1] == "analyzed"
    seventh = _add_file(root, session_id, "seven.png")
    monkeypatch.setattr(cache, "PROMPT_VERSION", "changed-prompt")
    assert pipeline._process_one_file(seventh, get_engine(), [analyzer], client, set())[1] == "analyzed"
    assert analyzer.calls == 6


def test_pre_contract_cache_is_reanalyzed_then_current_cache_reused(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="contract-cache")
        db.add(user)
        db.commit()
        session_id = user.id
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "fixed context")
    analyzer = StubAnalyzer()
    client = StubClient()

    first = _add_file(root, session_id, "legacy.png")
    assert pipeline._process_one_file(first, get_engine(), [analyzer], client, set())[1] == "analyzed"
    with Session(get_engine()) as db:
        row = db.query(AnalysisCache).one()
        assert row.prompt_version == PROMPT_VERSION
        row.prompt_version = "analysis-v3-2026-09-28"
        db.commit()

    second = _add_file(root, session_id, "reanalyzed.png")
    assert pipeline._process_one_file(second, get_engine(), [analyzer], client, set())[1] == "analyzed"
    assert analyzer.calls == 2
    third = _add_file(root, session_id, "current.png")
    assert pipeline._process_one_file(third, get_engine(), [analyzer], client, set())[1] == "cached"
    assert analyzer.calls == 2
    with Session(get_engine()) as db:
        assert {row.prompt_version for row in db.query(AnalysisCache).all()} == {
            "analysis-v3-2026-09-28", PROMPT_VERSION,
        }
        assert db.get(File, second).analysis_prompt_version == PROMPT_VERSION
        assert db.get(File, third).analysis_prompt_version == PROMPT_VERSION


@pytest.mark.parametrize("use_cache", [True, False])
def test_analysis_rejects_same_size_timestamp_preserving_byte_edit(tmp_path, monkeypatch, use_cache):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="cache-edit")
        db.add(us)
        db.commit()
        sid = us.id
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "same context")
    analyzer = StubAnalyzer()
    first = _add_file(root, sid, "first.png")
    assert pipeline._process_one_file(first, get_engine(), [analyzer], StubClient(), set())[1] == "analyzed"
    second = _add_file(root, sid, "second.png")
    path = root / "second.png"
    before = path.stat()
    path.write_bytes(b"fake content")  # same length, unlike indexed bytes
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert pipeline._process_one_file(second, get_engine(), [analyzer], StubClient(), set(),
                                      use_cache=use_cache)[1] == "error"
    with Session(get_engine()) as db:
        assert db.get(File, second).analysis_reason == "stale_enrichment"
    assert analyzer.calls == 1


@pytest.mark.parametrize("use_cache", [True, False])
def test_analysis_rechecks_edits_during_inference(tmp_path, monkeypatch, use_cache):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="edit-during-inference")
        db.add(user)
        db.commit()
        sid = user.id
    file_id = _add_file(root, sid, "one.png")
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "fixed context")

    class EditingAnalyzer(StubAnalyzer):
        def analyze(self, file_rec, context):
            result = super().analyze(file_rec, context)
            result.transcript = "now stale transcript"
            result.detected_date = datetime(2020, 1, 1)
            Path(file_rec.path).write_bytes(b"fake content")
            return result

    analyzer = EditingAnalyzer()
    assert pipeline._process_one_file(file_id, get_engine(), [analyzer], StubClient(), set(),
                                      use_cache=use_cache)[1] == "error"
    with Session(get_engine()) as db:
        row = db.get(File, file_id)
        assert row.status == FileStatus.ERROR
        assert row.analysis_reason == "stale_enrichment"
        assert row.ai_description is None
        assert row.ai_transcript is None
        assert row.analysis_detected_date is None
        assert row.date_best is None
        assert db.query(AnalysisCache).count() == 0


def test_no_cache_preserves_analysis_checks_without_cache_reads_or_writes(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="no-cache")
        db.add(user)
        db.commit()
        sid = user.id
    analyzer = StubAnalyzer()
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "fixed context")
    first = _add_file(root, sid, "one.png")
    assert pipeline._process_one_file(first, get_engine(), [analyzer], StubClient(), set())[1] == "analyzed"
    second = _add_file(root, sid, "two.png")
    monkeypatch.setattr(pipeline, "restore", lambda *args, **kwargs: pytest.fail("no-cache restored a result"))
    monkeypatch.setattr(pipeline, "remember", lambda *args, **kwargs: pytest.fail("no-cache stored a result"))
    assert pipeline._process_one_file(second, get_engine(), [analyzer], StubClient(), set(),
                                      use_cache=False)[1] == "analyzed"
    assert analyzer.calls == 2
    with Session(get_engine()) as db:
        assert db.get(File, second).analysis_cache_hit is False


def test_cache_does_not_cross_text_and_vision_model_tags(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="channels")
        db.add(us)
        db.commit()
        sid = us.id
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "fixed context")
    analyzer = StubAnalyzer()
    first = _add_file(root, sid, "first.png")
    assert pipeline._process_one_file(first, get_engine(), [analyzer],
                                      StubClient(text_model="text-A", vision_model="vision-A"), set())[1] == "analyzed"
    second = _add_file(root, sid, "second.png")
    assert pipeline._process_one_file(second, get_engine(), [analyzer],
                                      StubClient(text_model="text-B", vision_model="text-A"), set())[1] == "analyzed"
    assert analyzer.calls == 2


def test_sampled_frame_is_explicit_and_later_full_analysis_eligible(tmp_path):
    init_db(tmp_path / "index.db")
    root = tmp_path / "frames"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="samples")
        db.add(us)
        db.commit()
        session_id = us.id
    _add_file(root, session_id, "frame_01.png")
    middle = _add_file(root, session_id, "frame_02.png")
    _add_file(root, session_id, "frame_03.png")
    _add_file(root, session_id, "frame_04.png")
    with Session(get_engine()) as db:
        row = db.get(File, middle)
        row.ai_transcript = "stale transcript"
        row.analysis_model_tag = "old-model"
        row.analysis_model_digest = "old-digest"
        row.analysis_prompt_version = "old-prompt"
        row.analysis_extractor_version = "old-extractor"
        row.analysis_content_chars = 100
        row.analysis_context_hash = "old-context"
        row.analysis_detected_date = datetime(2020, 1, 1)
        row.analysis_cache_hit = True
        db.commit()
    analyzer = StubAnalyzer()
    status = pipeline._process_one_file(middle, get_engine(), [analyzer],
                                        StubClient(), set(), sequence_sample_stride=10)[1]
    assert status == "sampled"
    assert analyzer.calls == 0
    with Session(get_engine()) as db:
        row = db.get(File, middle)
        assert row.status == FileStatus.SKIPPED
        assert row.analysis_outcome == "sampled"
        assert row.ai_description is None
        assert row.ai_transcript is None
        assert row.analysis_model_tag is None
        assert row.analysis_model_digest is None
        assert row.analysis_prompt_version is None
        assert row.analysis_extractor_version is None
        assert row.analysis_content_chars is None
        assert row.analysis_context_hash is None
        assert row.analysis_detected_date is None
        assert row.analysis_cache_hit is False
        assert db.query(File).filter(pipeline._eligible_for_analysis(False, True),
                                     File.id == middle).count() == 1
    assert pipeline._process_one_file(middle, get_engine(), [analyzer],
                                      StubClient(), set())[1] == "analyzed"
    assert analyzer.calls == 1


def test_short_offset_frame_family_keeps_first_and_last_representatives(tmp_path):
    init_db(tmp_path / "index.db")
    root = tmp_path / "frames"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="short-run")
        db.add(us)
        db.commit()
        session_id = us.id
    ids = [_add_file(root, session_id, f"frame_{number:04}.png")
           for number in range(11, 15)]
    analyzer = StubAnalyzer()
    statuses = [pipeline._process_one_file(file_id, get_engine(), [analyzer],
                                           StubClient(), set(),
                                           sequence_sample_stride=10,
                                           use_cache=False)[1]
                for file_id in ids]
    assert statuses == ["analyzed", "sampled", "sampled", "analyzed"]
    assert analyzer.calls == 2


@pytest.mark.parametrize("extension,base_class", [
    (".pdf", DocumentAnalyzer), (".ai", DocumentAnalyzer),
    (".mp4", VideoAnalyzer),
])
def test_dual_route_content_is_rechecked_without_cache_reuse(
    tmp_path, monkeypatch, extension, base_class,
):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="dual-route")
        db.add(us)
        db.commit()
        session_id = us.id

    class DualRouteAnalyzer(base_class):
        def __init__(self):
            self.calls = 0

        def analyze(self, file_rec, context):
            self.calls += 1
            return AnalysisResult(description="Content checked", tags=["checked"],
                                  evidence_source="text", outcome="content_verified")

    analyzer = DualRouteAnalyzer()
    monkeypatch.setattr(pipeline, "build_context", lambda _row: "same context")
    first = _add_file(root, session_id, "first" + extension)
    second = _add_file(root, session_id, "second" + extension)
    assert pipeline._process_one_file(first, get_engine(), [analyzer],
                                      StubClient(), set())[1] == "analyzed"
    assert pipeline._process_one_file(second, get_engine(), [analyzer],
                                      StubClient(), set())[1] == "analyzed"
    assert analyzer.calls == 2
    with Session(get_engine()) as db:
        from donedatahoarder.db.models import AnalysisCache
        assert db.query(AnalysisCache).count() == 0


def test_image_cache_uses_only_the_vision_model_key(tmp_path, monkeypatch):
    init_db(tmp_path / "index.db")
    root = tmp_path / "images"
    root.mkdir()
    with Session(get_engine()) as db:
        us = UserSession(root_path=str(root), name="vision-key")
        db.add(us)
        db.commit()
        session_id = us.id

    class VisionStub(ImageAnalyzer):
        def __init__(self):
            self.calls = 0

        def analyze(self, file_rec, context):
            self.calls += 1
            return AnalysisResult(description="Seen image", tags=["image"],
                                  evidence_source="vision", outcome="content_verified")

    monkeypatch.setattr(pipeline, "build_context", lambda _row: "same context")
    analyzer = VisionStub()
    first = _add_file(root, session_id, "first.png")
    second = _add_file(root, session_id, "second.png")
    third = _add_file(root, session_id, "third.png")
    client_a = StubClient(text_model="text-A", vision_model="vision-A")
    client_b = StubClient(text_model="text-B", vision_model="vision-A")
    client_c = StubClient(text_model="text-B", vision_model="vision-B")
    assert pipeline._process_one_file(first, get_engine(), [analyzer], client_a, set())[1] == "analyzed"
    assert pipeline._process_one_file(second, get_engine(), [analyzer], client_b, set())[1] == "cached"
    assert pipeline._process_one_file(third, get_engine(), [analyzer], client_c, set())[1] == "analyzed"
    assert analyzer.calls == 2


def test_economy_window_selects_longest_generic_numbered_family(tmp_path):
    source = tmp_path / "fixture.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(11, 17):
            archive.writestr(f"long/{number:05}.jpg", b"image")
        for number in range(1, 5):
            archive.writestr(f"short/{number:05}.jpg", b"image")
    with zipfile.ZipFile(source) as archive:
        chosen = select_frame_window(archive, start=1, count=4)
        assert [Path(info.filename).name for info in chosen] == [
            f"{number:05}.jpg" for number in range(12, 16)]
        with pytest.raises(RuntimeError, match="no contiguous padded JPEG family"):
            select_frame_window(archive, start=3, count=4)


def test_bare_padded_sequence_sampling_requires_four_consecutive_frames(tmp_path):
    from donedatahoarder.proposals.sequence_identity import (
        is_confirmed_frame, numbered_frame_identity,
    )

    pair = [tmp_path / f"{n:05}.jpg" for n in (28, 29)]
    for path in pair:
        path.write_bytes(b"x")
    assert numbered_frame_identity(pair[0]) == ("", 28, 5)
    assert not is_confirmed_frame(pair[0])
    for n in (30, 31):
        (tmp_path / f"{n:05}.jpg").write_bytes(b"x")
    assert is_confirmed_frame(pair[0])


def test_preflight_counts_logical_bytes_without_hashing(tmp_path):
    from donedatahoarder.core.preflight import estimate_collection

    root = tmp_path / "collection"
    root.mkdir()
    (root / "frame_01.png").write_bytes(b"small")
    (root / "notes.txt").write_bytes(b"text")
    estimate = estimate_collection(root, mode="representative",
                                   sequence_sample_stride=10)
    assert estimate["files"] == 2
    assert estimate["logical_bytes"] == 9
    assert estimate["physically_hashed_bytes"] == 0
    assert estimate["estimated_ai_calls_range"][1] == 2


def test_preflight_marks_unreadable_subtree_as_incomplete(tmp_path, monkeypatch):
    from donedatahoarder.core import preflight

    root = tmp_path / "collection"
    root.mkdir()
    (root / "notes.txt").write_bytes(b"text")
    real_walk = preflight.os.walk

    def walk_with_error(*args, **kwargs):
        kwargs["onerror"](OSError(13, "Access denied", str(root / "private")))
        yield from real_walk(*args, **kwargs)

    monkeypatch.setattr(preflight.os, "walk", walk_with_error)
    result = preflight.estimate_collection(root)
    assert result["size_estimate_complete"] is False
    assert result["copy_plus_index_bytes_upper"] is None
    assert result["logical_bytes"] == 4

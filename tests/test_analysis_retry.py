"""Failed AI inference stays retryable and cannot become proposal evidence."""

from sqlalchemy.orm import Session
from typer.testing import CliRunner

from donedatahoarder import cli
from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers.base import AnalysisResult, BaseAnalyzer
from donedatahoarder.db.models import File, FileStatus, Proposal, UserSession
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.proposals.namer.core import generate_proposals


class StubAnalyzer(BaseAnalyzer):
    def __init__(self, failing=False):
        self.failing = failing
        self.calls = []

    def can_handle(self, mime_type, extension):
        return True

    def analyze(self, file_rec, context):
        self.calls.append(file_rec.id)
        if self.failing:
            return AnalysisResult(description="AI inference failed: timed out", confidence=0)
        return AnalysisResult(description="A blue house", suggested_name="blue_house", confidence=0.9)


def _workspace(tmp_path):
    init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(root), name="analysis-retry")
        db.add(user)
        db.flush()
        path = root / "photo.jpg"
        path.write_bytes(b"test")
        file = File(session_id=user.id, path=str(path), filename=path.name,
                    extension=".jpg", mime_type="image/jpeg", size_bytes=4,
                    status=FileStatus.ENRICHED)
        db.add(file)
        db.commit()
        return user.id, file.id


def test_inference_failure_is_error_with_no_proposals(tmp_path):
    session_id, file_id = _workspace(tmp_path)
    failed = StubAnalyzer(failing=True)
    fid, status, error = pipeline._process_one_file(
        file_id, get_engine(), [failed], object(), set(),
    )
    assert (fid, status) == (file_id, "error")
    assert "AI inference failed: timed out" in error
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        assert file.status == FileStatus.ERROR
        assert file.error_message.startswith("AI inference failed: timed out")
        assert file.analyzed_at is None

    generate_proposals(session_id=session_id)
    with Session(get_engine()) as db:
        assert db.query(Proposal).filter(Proposal.file_id == file_id).count() == 0


def test_opt_in_retry_selects_only_inference_errors_once(tmp_path, monkeypatch):
    session_id, file_id = _workspace(tmp_path)
    with Session(get_engine()) as db:
        retryable = db.get(File, file_id)
        retryable.status = FileStatus.ERROR
        retryable.error_message = "AI inference failed: timed out"
        path = tmp_path / "files" / "scanner_error.jpg"
        path.write_bytes(b"test")
        unrelated = File(session_id=session_id, path=str(path), filename=path.name,
                         extension=".jpg", mime_type="image/jpeg", size_bytes=4,
                         status=FileStatus.ERROR, error_message="Scanner failed")
        db.add(unrelated)
        db.commit()
        unrelated_id = unrelated.id

    fake = StubAnalyzer(failing=True)
    monkeypatch.setattr(pipeline, "_get_analyzer", lambda *_: fake)
    from donedatahoarder.ai import router
    monkeypatch.setattr(router, "get_client", lambda: object())
    events = list(pipeline.analyze_with_progress(session_id=session_id, retry_errors=True))
    assert events[-1]["done"] is True
    assert events[-1]["errors"] == 1
    assert fake.calls == [file_id]  # failed retry is not selected again this run

    fake.failing = False
    events = list(pipeline.analyze_with_progress(session_id=session_id, retry_errors=True))
    assert events[-1]["analyzed"] == 1
    assert fake.calls == [file_id, file_id]
    with Session(get_engine()) as db:
        retried = db.get(File, file_id)
        assert retried.status == FileStatus.ANALYZED
        assert retried.error_message is None
        assert retried.ai_description == "A blue house"
        assert db.get(File, unrelated_id).status == FileStatus.ERROR


def test_cli_forwards_scoped_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: None)
    monkeypatch.setattr(cli, "_init_db", lambda _path: None)
    monkeypatch.setattr(cli, "_init_ai", lambda *_args: None)
    received = []
    monkeypatch.setattr(pipeline, "analyze", lambda **kwargs: received.append(kwargs) or {
        "analyzed": 0, "skipped": 0, "errors": 0,
    })

    result = CliRunner().invoke(cli.app, [
        "analyze", "--db", str(tmp_path / "index.db"),
        "--session", "collection-id", "--retry-errors",
        "--model", "gemma4:26b", "--workers", "1",
    ])
    assert result.exit_code == 0, result.output
    assert received == [{
        "workers": 1, "limit": None, "min_size_kb": 1,
        "session_id": "collection-id", "retry_errors": True,
        "sequence_sample_stride": 0, "use_cache": True,
    }]

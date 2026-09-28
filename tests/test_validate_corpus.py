"""Safety checks for the disposable real-corpus harness."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scripts.validate_corpus import (
    completed_report, require_local_provider, run_ai_step, safe_members,
    validate_same_file_operations, write_json,
)
from donedatahoarder.db.models import ProposalType
from scripts import validate_corpus


def _zip(*names: str, symlink: str | None = None) -> zipfile.ZipFile:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in names:
            zf.writestr(name, b"content")
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            zf.writestr(info, b"target")
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "C:/drive.txt",
                                         "folder\\..\\escape.txt", "con.txt:stream"])
def test_rejects_unsafe_zip_paths(name):
    with _zip(name) as zf, pytest.raises(ValueError):
        safe_members(zf)


def test_rejects_symlink_and_case_collision():
    with _zip("normal.txt", symlink="shortcut") as zf, pytest.raises(ValueError):
        safe_members(zf)
    with _zip("Photo.jpg", "photo.JPG") as zf, pytest.raises(ValueError):
        safe_members(zf)


def test_accepts_regular_nested_files():
    with _zip("folder/a.txt", "folder/b.txt") as zf:
        assert [path.as_posix() for _, path in safe_members(zf)] == ["folder/a.txt", "folder/b.txt"]


def test_archive_selection_is_plain_zip_basename_in_corpus_root(tmp_path, monkeypatch):
    monkeypatch.setattr(validate_corpus, "CORPUS_DIR", tmp_path)
    source = tmp_path / "sample.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("file.txt", "safe")
    assert validate_corpus.archive_path(source.name) == source
    for unsafe in ("../sample.zip", "folder/sample.zip", "C:\\sample.zip",
                   "sample.zip/../sample.zip", "sample.txt"):
        with pytest.raises(ValueError):
            validate_corpus.archive_path(unsafe)
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(source)
    except OSError:
        # Windows hosts without symlink privilege still exercise the guard.
        link.write_bytes(source.read_bytes())
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink",
                            lambda path: path == link or real_is_symlink(path))
    with pytest.raises(ValueError, match="redirected"):
        validate_corpus.archive_path("link.zip")


def test_same_file_rename_then_matching_move_is_the_only_allowed_pair(tmp_path):
    original = str(tmp_path / "old.jpg")
    rename = SimpleNamespace(id=49, proposal_type=ProposalType.RENAME,
                             current_value=original,
                             proposed_value=str(tmp_path / "new.jpg"))
    move = SimpleNamespace(id=69, proposal_type=ProposalType.MOVE,
                           current_value=original,
                           proposed_value=str(tmp_path / "group" / "new.jpg"))
    validate_same_file_operations([rename, move])
    with pytest.raises(ValueError, match="matching move"):
        validate_same_file_operations([rename, SimpleNamespace(
            id=69, proposal_type=ProposalType.MOVE, current_value=original,
            proposed_value=str(tmp_path / "group" / "wrong.jpg"))])
    with pytest.raises(ValueError, match="ordered rename"):
        validate_same_file_operations([SimpleNamespace(
            id=100, proposal_type=ProposalType.MARK_DUPLICATE,
            current_value=original, proposed_value=original), move])


def test_ai_stage_refuses_missing_or_nonlocal_provider():
    from donedatahoarder.ai.provider import _ai_provider_var

    token = _ai_provider_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="provider not set"):
            require_local_provider("gemma4:e4b")

        class CloudStub:
            def get_client(self, failover=False):
                return object()

        _ai_provider_var.set(CloudStub())
        with pytest.raises(RuntimeError, match="local Ollama"):
            require_local_provider("gemma4:e4b")
    finally:
        _ai_provider_var.reset(token)


def test_relate_fallback_warning_fails_validation(tmp_path):
    log = tmp_path / "app.log"
    report_path = tmp_path / "pipeline.json"
    report = {"steps": {}}

    def swallowed_timeout():
        log.write_text("2026 | WARNING | relate | Relate LLM call failed: timed out\n",
                       encoding="utf-8")
        return {"groups": 0}

    with pytest.raises(RuntimeError, match="fallback/failure"):
        run_ai_step(report, report_path, "relate", swallowed_timeout, log)
    assert report["steps"]["relate"]["warning_count"] == 1
    assert "error" in report["steps"]["relate"]


def test_incomplete_pipeline_cannot_be_reviewed(tmp_path):
    reports = tmp_path / "reports"
    write_json(reports / "pipeline.json", {"session_id": "one", "steps": {"analyze": {}}})
    with pytest.raises(ValueError, match="incomplete"):
        completed_report(tmp_path)
    write_json(reports / "retry-downstream.json", {"session_id": "one", "status": "running"})
    with pytest.raises(ValueError, match="incomplete"):
        completed_report(tmp_path)


def test_completed_analysis_retry_is_resolved_without_overwriting_original(tmp_path):
    reports = tmp_path / "reports"
    write_json(reports / "pipeline.json", {"session_id": "one", "steps": {"scan": {}}})
    write_json(reports / "retry-analysis-1.json", {
        "session_id": "one", "status": "complete", "final": {"files": 3},
        "ai_coverage": {"eligible": 3, "attempted_unique": 3},
    })
    original, retry = completed_report(tmp_path)
    assert "final" not in original
    assert retry["final"]["files"] == 3

"""Real fixture preparation and honest, non-destructive qualification reports."""
import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from donedatahoarder.tui import diagnostics, qualification
from donedatahoarder.tui.images import ImageCapabilities, ImagePreviewCache


def test_diagnostics_capability_never_implies_qualification(monkeypatch):
    monkeypatch.setattr("donedatahoarder.tui.images.initialize_images",
                        lambda mode: ImageCapabilities("sixel", "detected", 9, 18))
    monkeypatch.setenv("TMUX", "/private/user/socket,1234,0")
    monkeypatch.setenv("API_SECRET", "must-not-appear")
    report = diagnostics.collect_diagnostics(terminal_name="foot", terminal_version="reported-version")
    serialized = json.dumps(report)
    assert "private/user/socket" not in serialized and "must-not-appear" not in serialized
    assert report["terminal"]["tmux_present"] is True
    assert report["terminal"]["capability"]["renderer"] == "sixel"
    assert report["reported_environment"]["terminal_name"] == "foot"
    assert report["qualification"] == "not_run"
    assert all(check["result"] == "not_run" for check in report["checks"])
    assert report["measurements"]["visible_cached_preview_p95_ms"] is None


def test_report_refuses_to_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    diagnostics.write_report({"qualification": "not_run"}, path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        diagnostics.write_report({"qualification": "pass"}, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("verbose", [False, True])
def test_diagnostics_cli_stdout_is_json_even_on_first_use(tmp_path, monkeypatch, verbose):
    from donedatahoarder import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DDH_LOG", "debug")
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: pytest.fail("JSON output cannot include a welcome panel"))
    result = CliRunner().invoke(cli.app, (["--verbose"] if verbose else []) + ["tui-diagnostics", "--images", "off"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["qualification"] == "not_run"
    assert not list(tmp_path.glob("*.db"))
    assert not list(tmp_path.glob("*.sqlite"))


def test_fixture_refuses_an_existing_directory_before_writing(tmp_path):
    sentinel = tmp_path / "user-data.txt"
    sentinel.write_text("keep me")
    with pytest.raises(FileExistsError):
        qualification.create_fixture(tmp_path, index=False)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "keep me"


def test_generated_cases_have_real_distinct_content_orientation_and_alpha(tmp_path):
    first = qualification.create_fixture(tmp_path / "one", index=False)
    second = qualification.create_fixture(tmp_path / "two", index=False)
    assert first["qualification"] == "not_run" and first["session_id"] is None
    assert first["faults_applied"] is False
    assert len(first["files"]) == 13
    assert [entry["generated_sha256"] for entry in first["files"]] == [entry["generated_sha256"] for entry in second["files"]]
    root = Path(first["root"])
    assert (root / "00-exact/original.png").read_bytes() == (root / "00-exact/copy.png").read_bytes()
    assert (root / "00-exact/original.png").read_bytes() != (root / "01-variants/edited.png").read_bytes()
    cache = ImagePreviewCache()
    oriented = cache.prepare(root / "02-orientation/exif-rotate-90.jpg")
    assert oriented.original_size == (800, 1200)
    oriented.image.close()
    with Image.open(root / "03-formats/transparent.png") as alpha:
        assert alpha.getpixel((0, 0))[3] == 0
    animated = cache.prepare(root / "03-formats/animation.gif")
    assert animated.is_animated
    animated.image.close()
    with Image.open(root / "04-large/12-megapixels.jpg") as large:
        assert large.size == (4000, 3000)
    cache.clear()


def test_failed_indexing_preserves_manifest_and_does_not_inject_faults(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("test indexing failure")
    monkeypatch.setattr(qualification, "_index_collection", fail)
    target = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="test indexing failure"):
        qualification.create_fixture(target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["preparation"] == "failed"
    assert manifest["qualification"] == "not_run"
    assert manifest["faults_applied"] is False
    assert (target / "collection/06-errors/missing-after-index.png").exists()


def test_real_index_creates_reviewable_session_without_ai_or_apply(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from donedatahoarder.ai import router
    from donedatahoarder.core import jobs
    from donedatahoarder.core.jobs import JobManager
    from donedatahoarder.db import session as database_module
    from donedatahoarder.db.models import File, Proposal, ProposalStatus
    from donedatahoarder.tui import service as service_module

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(database_module, "_engine", None)
    monkeypatch.setattr(database_module, "_SessionLocal", None)
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    monkeypatch.setattr(jobs, "job_manager", manager)
    monkeypatch.setattr(service_module, "job_manager", manager)
    monkeypatch.setattr(router, "init_ai", lambda *args, **kwargs: pytest.fail("No AI calls for this fixture"))
    result = qualification.create_fixture(tmp_path / "native")
    assert result["preparation"] == "indexed"
    assert result["index_result"]["steps"] == ["scan", "enrich", "dedup", "execute_dry"]
    assert result["index_result"]["counts"]["files"] == 13
    assert result["faults_applied"] is True
    root = Path(result["root"])
    assert not (root / "06-errors/missing-after-index.png").exists()
    changed = next(item for item in result["files"] if item["path"].endswith("changed-after-index.png"))
    assert changed["generated_sha256"] != changed["current_sha256"]
    assert not (root / ".ddh_trash").exists()
    engine = database_module.get_engine()
    try:
        with Session(engine) as db:
            assert db.query(File).filter(File.session_id == result["session_id"]).count() == 13
            proposals = db.query(Proposal).join(File, Proposal.file_id == File.id).filter(File.session_id == result["session_id"]).all()
            assert proposals
            assert all(item.status == ProposalStatus.PENDING for item in proposals)
    finally:
        engine.dispose()


def test_snapshot_failure_cancels_and_drains_started_fixture_work(tmp_path, monkeypatch):
    from donedatahoarder.core import jobs
    from donedatahoarder.core.jobs import JobManager
    from donedatahoarder.db import session as database_module
    from donedatahoarder.tui import service as service_module

    monkeypatch.setattr(database_module, "_engine", None)
    monkeypatch.setattr(database_module, "_SessionLocal", None)
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    monkeypatch.setattr(jobs, "job_manager", manager)
    monkeypatch.setattr(service_module, "job_manager", manager)
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))

    def unreadable_snapshot(*args, **kwargs):
        raise RuntimeError("simulated snapshot failure after worker start")
    monkeypatch.setattr(service_module.WorkspaceService, "snapshot", unreadable_snapshot)
    destination = tmp_path / "failed-run"
    with pytest.raises(RuntimeError, match="snapshot failure"):
        qualification.create_fixture(destination)
    assert not manager.has_live_workers()
    assert manager.get_active() is None
    assert (destination / "collection/06-errors/missing-after-index.png").exists()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["preparation"] == "failed"
    database_module.get_engine().dispose()

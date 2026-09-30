"""The optional interface must not make existing CLI installations unusable."""
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from donedatahoarder import cli
from donedatahoarder.tui import launch


@pytest.fixture(autouse=True)
def no_welcome(monkeypatch):
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: None)


def test_importing_cli_does_not_load_optional_tui():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import donedatahoarder.cli; "
         "assert 'textual' not in sys.modules; assert 'textual_image' not in sys.modules"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_tui_dispatches_explicit_options_without_starting_a_scan(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(launch, "launch", lambda root, **kwargs: calls.append((root, kwargs)))
    database = tmp_path / "workspace.db"
    result = CliRunner().invoke(cli.app, [
        "tui", str(tmp_path), "--db", str(database), "--images", "off",
        "--model", "local-model", "--ollama-host", "http://localhost:12345", "--workers", "2",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, {
        "session_id": None, "db_path": database, "model": "local-model",
        "ollama_host": "http://localhost:12345", "workers": 2, "images": "off",
    })]
    assert not database.exists()


@pytest.mark.parametrize("arguments", [
    ["--images", "guess"], ["--workers", "0"], [".", "--session", "existing"],
])
def test_tui_rejects_invalid_options_before_launch(monkeypatch, arguments):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid options must not launch the terminal interface")
    monkeypatch.setattr(launch, "launch", unexpected)
    result = CliRunner().invoke(cli.app, ["tui", *arguments])
    assert result.exit_code == 2


def test_missing_optional_dependencies_have_install_guidance(monkeypatch):
    monkeypatch.setattr(launch.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: None)
    result = CliRunner().invoke(cli.app, ["tui"])
    assert result.exit_code == 1
    assert "Install the terminal interface" in result.output
    assert ".[tui]" in result.output


def test_python_floor_is_specific_to_tui(monkeypatch):
    monkeypatch.setattr(launch.sys, "version_info", (3, 11, 0))
    with pytest.raises(RuntimeError, match="Python 3.12"):
        launch.check_requirements()


def test_default_database_honors_xdg_outside_collection(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert launch.default_database() == tmp_path / "donedatahoarder" / "index.db"


def test_default_database_ignores_relative_xdg_directory(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "relative-to-current-collection")
    assert launch.default_database().is_absolute()


def test_noninteractive_launch_does_not_create_a_database(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "check_requirements", lambda: None)
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: False)
    database = tmp_path / "not-created.db"
    with pytest.raises(RuntimeError, match="interactive terminal"):
        launch.launch(tmp_path, db_path=database)
    assert not database.exists()


def test_no_argument_launch_opens_picker_without_creating_cwd_session(monkeypatch, tmp_path):
    pytest.importorskip("textual")
    from donedatahoarder.tui import app, images
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.db.models import UserSession
    from sqlalchemy.orm import Session

    monkeypatch.setattr(launch, "check_requirements", lambda: None)
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(images, "initialize_images", lambda _: None)
    apps = []
    monkeypatch.setattr(app.DDHApp, "run", lambda self: apps.append(self))
    monkeypatch.chdir(tmp_path)
    launch.launch(None, db_path=tmp_path / "index.db")
    try:
        assert len(apps) == 1
        assert apps[0].service is None
        assert apps[0].catalog.list_sessions() == []
        with Session(get_engine()) as db:
            assert db.query(UserSession).count() == 0
    finally:
        get_engine().dispose()

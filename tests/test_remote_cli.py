"""Remote launch keeps Windows paths and databases on the workstation."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from donedatahoarder import cli
from donedatahoarder.remote.config import ensure_token, read_token, validate_listener
from donedatahoarder.tui import launch


@pytest.fixture(autouse=True)
def no_welcome(monkeypatch):
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: None)


def test_remote_cli_forwards_workstation_path_without_resolving_it(monkeypatch, tmp_path):
    calls = []
    monkeypatch.delenv("DDH_DB", raising=False)
    monkeypatch.setattr(launch, "launch", lambda root, **kwargs: calls.append((root, kwargs)))
    token = tmp_path / "token"
    result = CliRunner().invoke(cli.app, ["tui", r"E:\Hoard\Pictures", "--connect", "https://home:8765", "--token-file", str(token)])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == r"E:\Hoard\Pictures"
    assert calls[0][1]["connect"] == "https://home:8765"
    assert calls[0][1]["token_file"] == token


@pytest.mark.parametrize("arguments", [
    ["--connect", "https://home"], ["--token-file", "token"],
    ["--connect", "https://home", "--token-file", "token", "--db", "local.db"],
])
def test_incomplete_remote_configuration_does_not_launch(arguments, monkeypatch):
    monkeypatch.setattr(launch, "launch", lambda *args, **kwargs: pytest.fail("Invalid launch"))
    result = CliRunner().invoke(cli.app, ["tui", *arguments])
    assert result.exit_code == 2


def test_remote_launch_does_not_initialize_a_local_database(monkeypatch, tmp_path):
    pytest.importorskip("textual")
    from donedatahoarder.db import session
    from donedatahoarder.remote import client
    from donedatahoarder.tui import app, images
    monkeypatch.setattr(launch, "check_requirements", lambda: None)
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(session, "init_db", lambda *args: pytest.fail("Remote client initialized local DB"))
    monkeypatch.setattr(images, "initialize_images", lambda mode: None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []
    class Connection:
        def __init__(self, url, token, **kwargs):
            calls.append(("connection", url, kwargs))
        def connect(self):
            calls.append(("connect",))
        def close(self):
            calls.append(("close",))
    class Catalog:
        def __init__(self, connection):
            pass
        def open(self, **kwargs):
            calls.append(("open", kwargs))
            return "remote-service"
    class App:
        def __init__(self, service, **kwargs):
            assert service == "remote-service"
        def run(self):
            calls.append(("run",))
    monkeypatch.setattr(client, "RemoteConnection", Connection)
    monkeypatch.setattr(client, "RemoteSessionCatalog", Catalog)
    monkeypatch.setattr(app, "DDHApp", App)
    token = tmp_path / "token"
    ensure_token(token)
    launch.launch(r"Z:\FolderThatIsOnlyOnTheWorkstation", connect="https://home", token_file=token)
    assert ("open", {"root": r"Z:\FolderThatIsOnlyOnTheWorkstation", "session_id": None}) in calls
    assert calls[-2:] == [("run",), ("close",)]
    assert calls[0][2]["pending_file"].is_relative_to(tmp_path / "state")


def test_token_is_reused_without_printing_and_private_on_posix(tmp_path):
    import os
    file = tmp_path / "private" / "token"
    token = ensure_token(file)
    assert len(token) >= 32
    assert ensure_token(file) == token
    assert read_token(file) == token
    if os.name != "nt":
        assert file.stat().st_mode & 0o077 == 0


def test_lan_requires_tls_before_creating_token(tmp_path):
    token = tmp_path / "token"
    result = CliRunner().invoke(cli.app, ["remote-serve", "--root", str(tmp_path), "--token-file", str(token), "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "require HTTPS" in result.output
    assert not token.exists()
    validate_listener("127.0.0.1", None, None)
    validate_listener("::1", None, None)


def test_remote_server_dispatches_one_daemon_with_separate_scope(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import uvicorn
    from donedatahoarder.remote import server
    calls = []
    monkeypatch.setattr(server, "create_app", lambda database, **kwargs: calls.append((database, kwargs)) or "server-app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    root = tmp_path / "External SSD collection"
    root.mkdir()
    token = tmp_path / "private-token"
    result = CliRunner().invoke(cli.app, ["remote-serve", "--root", str(root), "--token-file", str(token),
                                        "--db", str(tmp_path / "index.db"), "--name", "HOME-PC"])
    assert result.exit_code == 0, result.output
    assert calls[0][1]["allowed_roots"] == [root]
    assert calls[1][0] == "server-app" and calls[1][1]["workers"] == 1
    assert read_token(token) not in result.output


@pytest.mark.parametrize("kind", ["token", "database", "journal"])
def test_server_control_state_cannot_be_indexed_in_collection(kind, monkeypatch, tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    token = root / "token" if kind == "token" else tmp_path / "token"
    database = root / "index.db" if kind == "database" else tmp_path / "index.db"
    if kind == "journal":
        monkeypatch.setenv("DDH_DATA_DIR", str(root / "journal"))
    result = CliRunner().invoke(cli.app, ["remote-serve", "--root", str(root), "--token-file", str(token), "--db", str(database)])
    assert result.exit_code == 1
    assert "outside authorized collection folders" in result.output
    assert not token.exists()
    assert not database.exists()
    assert not (root / "journal").exists()

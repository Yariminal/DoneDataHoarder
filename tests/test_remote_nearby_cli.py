"""Nearby CLI stays opt-in and advertises only a ready HTTPS listener."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from donedatahoarder import cli
from donedatahoarder.tui import launch


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: None)
    monkeypatch.delenv("DDH_DB", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))


def test_discover_forwards_remote_folder_and_session_without_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "launch", lambda root, **kwargs: calls.append((root, kwargs)))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["tui", r"E:\External SSD\Photos", "--discover"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == r"E:\External SSD\Photos"
    assert calls[0][1]["discover"] is True
    assert calls[0][1]["db_path"] is None
    result = runner.invoke(cli.app, ["tui", "--discover", "--session", "saved-session"])
    assert result.exit_code == 0, result.output
    assert calls[1][1]["session_id"] == "saved-session"


@pytest.mark.parametrize("options", [
    ["--connect", "https://home"], ["--token-file", "private"],
    ["--ca-file", "ca.pem"], ["--db", "local.db"],
])
def test_discovery_conflicting_modes_do_not_launch(options, monkeypatch):
    monkeypatch.setattr(launch, "launch", lambda *args, **kwargs: pytest.fail("Invalid launch"))
    result = CliRunner().invoke(cli.app, ["tui", "--discover", *options])
    assert result.exit_code == 2


def test_discovery_rejects_environment_database(monkeypatch, tmp_path):
    monkeypatch.setenv("DDH_DB", str(tmp_path / "local.db"))
    result = CliRunner().invoke(cli.app, ["tui", "--discover"])
    assert result.exit_code == 2
    assert "workstation database" in result.output
    assert not (tmp_path / "local.db").exists()


def test_discovery_launch_never_initializes_local_database(monkeypatch):
    pytest.importorskip("textual")
    from donedatahoarder.db import session
    from donedatahoarder.tui import app, images
    monkeypatch.setattr(launch, "check_requirements", lambda: None)
    monkeypatch.setattr(launch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(session, "init_db", lambda *args: pytest.fail("Laptop DB initialized"))
    monkeypatch.setattr(images, "initialize_images", lambda mode: None)
    calls = []
    class FakeApp:
        def __init__(self, service, **kwargs):
            calls.append((service, kwargs))
        def run(self):
            calls.append("run")
    monkeypatch.setattr(app, "DDHApp", FakeApp)
    launch.launch(r"E:\OnlyOnWorkstation", discover=True)
    assert calls[0][0] is None
    assert calls[0][1]["catalog"] is None
    assert calls[0][1]["discover"] is True
    assert calls[0][1]["remote_root"] == r"E:\OnlyOnWorkstation"
    assert calls[-1] == "run"


@pytest.mark.parametrize("options, message", [
    (["--pair"], "Use --pair with --discoverable"),
    (["--discoverable"], "LAN listener"),
    (["--discoverable", "--host", "0.0.0.0", "--cert-file", "custom.pem"], "manages its own TLS"),
])
def test_discoverable_validation_precedes_credential_creation(options, message, tmp_path):
    root = tmp_path / "external-collection"
    root.mkdir()
    token = tmp_path / "token"
    database = tmp_path / "index.db"
    result = CliRunner().invoke(cli.app, ["remote-serve", "--root", str(root), "--token-file", str(token),
                                        "--db", str(database), *options])
    assert result.exit_code == 1
    assert message in result.output
    assert not token.exists()
    assert not database.exists()


def test_discoverable_dispatches_managed_tls_without_printing_master_token(monkeypatch, tmp_path):
    pytest.importorskip("zeroconf")
    pytest.importorskip("cryptography")
    pytest.importorskip("fastapi")
    from donedatahoarder.remote import config, pairing, runtime, server
    root = tmp_path / "external"
    root.mkdir()
    certificate = tmp_path / "test.crt"
    certificate.write_text("test certificate", encoding="ascii")
    key = tmp_path / "test.key"
    key.write_text("test key", encoding="ascii")
    calls = []
    pairing_store = SimpleNamespace(create_invitation=lambda *args: "test-one-use-invitation")
    fake_app = SimpleNamespace(state=SimpleNamespace(remote_receipts=SimpleNamespace(server_id="server"), remote_pairing=pairing_store))
    monkeypatch.setattr(server, "create_app", lambda *args, **kwargs: calls.append(("create", kwargs)) or fake_app)
    monkeypatch.setattr(pairing, "ensure_tls", lambda *args: (certificate, key, "server.local"))
    monkeypatch.setattr(runtime, "run_discoverable", lambda app, **kwargs: calls.append(("run", kwargs)))
    token = tmp_path / "token"
    database = tmp_path / "index.db"
    result = CliRunner().invoke(cli.app, ["remote-serve", "--root", str(root), "--token-file", str(token),
                                        "--db", str(database), "--host", "0.0.0.0", "--discoverable", "--pair"])
    assert result.exit_code == 0, result.output
    assert calls[0][1]["pairing_path"] == Path(str(database) + ".remote-devices.sqlite3")
    assert calls[1][1]["hostname"] == "server.local"
    assert "test-one-use-invitation" in result.output
    assert config.read_token(token) not in result.output


def test_remote_pair_issues_invitation_without_starting_or_restarting_listener(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from donedatahoarder.remote import pairing
    database = tmp_path / "index.db"
    tls = Path(str(database) + ".remote-tls")
    tls.mkdir()
    cert = tls / "certificate.pem"
    cert.write_text("test certificate", encoding="ascii")
    calls = []
    store = SimpleNamespace(server_id="saved-server", create_invitation=lambda *args: calls.append(args) or "fresh-one-use-invitation")
    monkeypatch.setattr(cli, "_workstation_pairing_store", lambda path: store)
    monkeypatch.setattr(pairing, "ensure_tls", lambda *args: (cert, tls / "key.pem", "server.local"))
    result = CliRunner().invoke(cli.app, ["remote-pair", "--db", str(database)])
    assert result.exit_code == 0, result.output
    assert "fresh-one-use-invitation" in result.output
    assert calls == [("test certificate", "server.local")]
    assert not database.exists()


def test_device_management_reports_revocation_and_unknown_id(monkeypatch, tmp_path):
    devices = [{"device_id": "active-id", "name": "Laptop [red]", "revoked_at": None},
               {"device_id": "revoked-id", "name": "Old laptop", "revoked_at": "2026-09-29"}]
    calls = []
    store = SimpleNamespace(list_devices=lambda: devices,
                            revoke=lambda value: calls.append(value) or value == "active-id")
    monkeypatch.setattr(cli, "_workstation_pairing_store", lambda path: store)
    result = CliRunner().invoke(cli.app, ["remote-devices", "--db", str(tmp_path / "index.db")])
    assert result.exit_code == 0, result.output
    assert "Laptop [red]  active" in result.output
    assert "Old laptop  revoked" in result.output
    result = CliRunner().invoke(cli.app, ["remote-devices", "--db", str(tmp_path / "index.db"), "--revoke", "unknown"])
    assert result.exit_code == 1
    assert "No paired device has that ID" in result.output
    assert calls == ["unknown"]


@pytest.mark.parametrize("started, fail_start, fail_close", [(True, False, False), (False, False, False),
                                                            (True, True, False), (True, False, True)])
def test_runtime_orders_advertisement_after_listener_and_always_cleans_up(monkeypatch, started, fail_start, fail_close):
    uvicorn = pytest.importorskip("uvicorn")
    from donedatahoarder.remote import discovery, runtime
    events = []
    class Publisher:
        def __init__(self, value):
            events.append("candidate")
        def start(self):
            events.append("advertise")
            if fail_start:
                raise RuntimeError("multicast unavailable")
        def close(self):
            events.append("withdraw")
            if fail_close:
                raise RuntimeError("interface disappeared")
    class Server:
        def __init__(self, config):
            self.started = False
        async def startup(self, sockets=None):
            events.append("listener")
            self.started = started
        async def shutdown(self, sockets=None):
            events.append("listener-closed")
        def run(self):
            async def lifecycle():
                await self.startup()
                await self.shutdown()
            asyncio.run(lifecycle())
    monkeypatch.setattr(discovery, "Advertiser", Publisher)
    monkeypatch.setattr(discovery, "advertised_addresses", lambda host: ("192.168.1.10",))
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: kwargs)
    app = SimpleNamespace(state=SimpleNamespace(remote_receipts=SimpleNamespace(server_id="cc5c83a7-5547-4bdf-a786-4bc7d9f42eb9")))
    try:
        runtime.run_discoverable(app, host="0.0.0.0", port=8765, cert_file="cert", key_file="key",
                                 hostname="ddh.local", name="HOME-PC")
    except RuntimeError:
        assert fail_close
    assert "listener-closed" in events
    assert ("advertise" in events) is started
    if started:
        assert events.index("listener") < events.index("advertise")
    assert events.count("withdraw") == 2

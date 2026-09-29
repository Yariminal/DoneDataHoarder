"""Remote adapters preserve session ownership and never replay lost commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import UUID

import httpx
import pytest

from donedatahoarder.remote.client import RemoteConnection, RemoteError, RemoteSessionCatalog, RemoteWorkspaceService


HELLO = {"protocol": 1, "server_id": "workstation-identity", "name": "HOME-PC", "version": "0.6.0",
         "path_flavor": "windows", "default_root": r"D:\Photos", "model": "gemma3:12b", "workers": 1}


def connection(handler=None, **kwargs):
    def route(request):
        if request.url.path == "/remote/v1/hello":
            return httpx.Response(200, json=HELLO)
        if handler:
            return handler(request)
        return httpx.Response(200, json={})
    result = RemoteConnection("https://workstation.test:8042", "private-token", transport=httpx.MockTransport(route), **kwargs)
    result.connect()
    return result


@pytest.mark.parametrize("url", [
    "http://192.168.1.4:8042", "http://workstation", "http://127.0.0.1.attacker.test", "http://2130706433",
    "https://user:password@host", "https://host/path", "https://host/?secret=x", "https://host/#secret",
    "https://host?", "https://host#", "https://host\\evil", "https://host:0", "https://host:99999",
    " https://host", "https://host\n", "file:///tmp/test", "https://", "http://[not-an-address]",
])
def test_reject_unsafe_endpoint_without_revealing_url(url):
    with pytest.raises(RemoteError) as caught:
        RemoteConnection(url, "private-token")
    assert "password" not in str(caught.value)
    assert "private-token" not in str(caught.value)


@pytest.mark.parametrize("url", ["https://workstation.test/", "https://192.168.1.4:8042", "http://localhost:8042", "http://127.0.0.1:8042", "http://[::1]:8042"])
def test_accept_https_or_literal_loopback(url):
    client = RemoteConnection(url, "private-token", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=HELLO)))
    try:
        assert client.connect() == HELLO
        assert client.state == "connected"
        assert client.latency_ms is not None
        assert client.generation == 1
        assert client.url == url.rstrip("/")
    finally:
        client.close()


def test_client_configuration_disables_proxy_redirect_and_keeps_tls_validation(monkeypatch):
    options = {}
    real_client = httpx.Client
    def capture(**kwargs):
        options.update(kwargs)
        return real_client(**kwargs)
    monkeypatch.setattr(httpx, "Client", capture)
    client = connection()
    client.close()
    assert options["verify"] is True
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert options["timeout"].connect == 5
    assert options["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("token", ["", "bad\ntoken", "space token", "é", "a" * 4097])
def test_invalid_header_token_is_rejected(token):
    with pytest.raises(RemoteError, match="access token"):
        RemoteConnection("https://host", token)


def test_catalog_and_workspace_serialize_semantic_commands_without_local_paths(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if request.method == "POST":
            body = json.loads(request.content)
            UUID(body["request_id"])
            result = {"session_id": "remote-session"} if request.url.path.endswith("/sessions") else {"ok": True}
            return httpx.Response(200, json={"request_id": body["request_id"], "state": "completed", "result": result})
        if request.url.path.endswith("/readiness"):
            return httpx.Response(200, json={"message": "Ready on workstation"})
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json=[{"id": "remote-session", "root_path": r"D:\Photos"}])
        return httpx.Response(200, json={"path_flavor": "windows", "files": []})
    client = connection(handler)
    try:
        catalog = RemoteSessionCatalog(client)
        assert catalog.default_root == r"D:\Photos"
        assert catalog.path_flavor == "windows"
        assert "HOME-PC" in catalog.path_label
        assert catalog.ollama_host == "Ollama on HOME-PC"
        assert catalog.workers == 1
        assert catalog.model == "gemma3:12b"
        assert catalog.list_sessions()[0]["id"] == "remote-session"
        assert catalog.readiness("new-model") == "Ready on workstation"
        assert catalog.session_readiness("remote-session") == "Ready on workstation"
        service = catalog.open(root=r"Z:\NotMountedOnLaptop", model="other-model")
        assert service.session_id == "remote-session"
        assert service.path_flavor == "windows"
        assert service.is_remote
        assert service.snapshot(limit=250, offset=500)["path_flavor"] == "windows"
        service.get_file(4)
        service.start_pipeline(metadata_only=True)
        service.resume_pipeline(retry_errors=True)
        service.pause_pipeline()
        service.cancel_pipeline()
        service.approve(3, "review-evidence-token")
        service.reject(4)
        service.edit(5, "Readable name.png")
        service.approve_clear(0.95)
        service.preview()
        service.apply("preview-token", confirmed=True)
        service.history(50)
        service.undo_preview()
        service.undo("undo-token", confirmed=True)
        service.update_settings("new-model", 2)
        service.preflight(metadata_only=True)
        bodies = [json.loads(request.content) for request in requests if request.method == "POST"]
        assert bodies[0]["root"] == r"Z:\NotMountedOnLaptop"
        assert bodies[0]["model"] == "other-model"
        commands = {body["command"]: body["params"] for body in bodies[1:]}
        assert commands["approve"] == {"proposal_id": 3, "review_token": "review-evidence-token"}
        assert commands["apply"] == {"token": "preview-token", "confirmed": True}
        assert commands["undo"] == {"token": "undo-token", "confirmed": True}
        assert commands["edit"] == {"proposal_id": 5, "value": "Readable name.png"}
        assert commands["update_settings"] == {"model": "new-model", "workers": 2}
        assert len({body["request_id"] for body in bodies}) == len(bodies)
        snapshot_request = next(request for request in requests if request.url.path.endswith("/snapshot"))
        assert dict(snapshot_request.url.params) == {"limit": "250", "offset": "500"}
        assert all(request.url.path.startswith("/remote/v1/sessions/remote-session/") for request in requests if request.url.path.endswith("/commands"))
    finally:
        client.close()


def test_lost_mutation_response_reconciles_receipt_without_replaying():
    posts = []
    receipt_reads = []
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            raise httpx.ReadTimeout("private-token must not leak", request=request)
        if "/commands/" in request.url.path:
            receipt_reads.append(request)
            return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": "completed", "result": {"applied": 1}})
        return httpx.Response(200, json={"session": {"id": "session"}})
    client = connection(handler)
    try:
        service = RemoteWorkspaceService(client, "session")
        with pytest.raises(RemoteError) as caught:
            service.apply("preview-token", confirmed=True)
        assert "private-token" not in str(caught.value)
        assert client.state == "reconnecting"
        assert client.pending_request_id == posts[0]["request_id"]
        with pytest.raises(RemoteError):
            service.apply("preview-token", confirmed=True)
        assert len(posts) == 1
        assert service.snapshot()["session"]["id"] == "session"
        assert client.state == "connected"
        assert client.pending_request_id is None
        assert client.generation == 2
        assert len(posts) == len(receipt_reads) == 1
        assert client.last_receipt["result"] == {"applied": 1}
    finally:
        client.close()


@pytest.mark.parametrize("receipt_state", ["running", "uncertain", "missing"])
def test_pending_outcomes_allow_reads_but_block_new_mutations(receipt_state):
    posts = []
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            return httpx.Response(200, json={"request_id": body["request_id"], "state": "running"})
        if "/commands/" in request.url.path:
            if receipt_state == "missing":
                return httpx.Response(404, json={"detail": "No receipt"})
            return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": receipt_state})
        return httpx.Response(200, json={"files": []})
    client = connection(handler)
    try:
        service = RemoteWorkspaceService(client, "session")
        with pytest.raises(RemoteError, match="running"):
            service.start_pipeline()
        assert service.snapshot() == {"files": []}
        assert client.pending_request_id
        assert not client.can_mutate
        with pytest.raises(RemoteError):
            service.cancel_pipeline()
        assert len(posts) == 1
    finally:
        client.close()


def test_existing_session_can_open_and_show_history_while_outcome_is_uncertain():
    posts = []
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            return httpx.Response(200, json={"request_id": body["request_id"], "state": "uncertain"})
        if "/commands/" in request.url.path:
            return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": "uncertain"})
        return httpx.Response(200, json={"session": {"id": "session"}, "history": [{"operation": "rename", "state": "outstanding"}]})
    client = connection(handler)
    try:
        with pytest.raises(RemoteError, match="cannot confirm"):
            RemoteWorkspaceService(client, "session").apply("preview", confirmed=True)
        workspace = RemoteSessionCatalog(client).open(session_id="session")
        assert workspace.snapshot()["history"][0]["operation"] == "rename"
        assert client.pending_request_id
        assert not client.can_mutate
        assert len(posts) == 1
        with pytest.raises(RemoteError):
            workspace.undo("recovery", confirmed=True)
        assert len(posts) == 1
    finally:
        client.close()


def test_failed_receipt_clears_pending_and_redacts_secret():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"request_id": body["request_id"], "state": "failed",
            "error": {"detail": "Rejected private-token", "status_code": 409}})
    client = connection(handler)
    try:
        with pytest.raises(RemoteError) as caught:
            RemoteWorkspaceService(client, "session").preview()
        assert str(caught.value) == "Rejected [redacted]"
        assert caught.value.status_code == 409
        assert client.pending_request_id is None
        assert client.can_mutate
    finally:
        client.close()


def test_folder_permission_denied_keeps_connection_and_clears_rejected_command():
    client = connection(lambda request: httpx.Response(403, json={"detail": "Folder is outside the shared roots"}))
    try:
        with pytest.raises(RemoteError, match="outside the shared roots"):
            RemoteSessionCatalog(client).open(root=r"D:\NotShared")
        assert client.state == "connected"
        assert client.pending_request_id is None
        assert client.can_mutate
    finally:
        client.close()


def test_snapshot_during_inflight_command_does_not_look_for_premature_receipt():
    entered, finish = threading.Event(), threading.Event()
    receipt_reads = []
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            entered.set()
            assert finish.wait(3)
            return httpx.Response(200, json={"request_id": body["request_id"], "state": "completed", "result": {}})
        if "/commands/" in request.url.path:
            receipt_reads.append(request)
            return httpx.Response(404, json={"detail": "Not accepted yet"})
        return httpx.Response(200, json={"files": []})
    client = connection(handler)
    service = RemoteWorkspaceService(client, "session")
    thread = threading.Thread(target=service.start_pipeline)
    try:
        thread.start()
        assert entered.wait(3)
        assert service.snapshot() == {"files": []}
        assert receipt_reads == []
        assert client.error is None
    finally:
        finish.set()
        thread.join(3)
        client.close()


@pytest.mark.parametrize("status", [401, 403, 302])
def test_auth_and_redirect_errors_require_explicit_reconnect(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.test"}, json={"detail": "private-token"})
    client = RemoteConnection("https://host", "private-token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RemoteError) as caught:
            client.connect()
        assert "private-token" not in str(caught.value)
        assert client.state == ("incompatible" if status == 302 else "auth_error")
        with pytest.raises(RemoteError):
            client.request_json("GET", "/sessions")
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize("change", [{"protocol": 2}, {"protocol": True}, {"path_flavor": "unknown"}, {"workers": 0}, {"server_id": ""}])
def test_protocol_handshake_is_validated(change):
    client = RemoteConnection("https://host", "private-token", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={**HELLO, **change})))
    try:
        with pytest.raises(RemoteError, match="incompatible"):
            client.connect()
        assert client.state == "incompatible"
    finally:
        client.close()


def test_refuse_workstation_identity_change_after_reconnect():
    identity = dict(HELLO)
    client = RemoteConnection("https://host", "private-token", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=identity)))
    try:
        client.connect()
        identity["server_id"] = "different-workstation"
        with pytest.raises(RemoteError, match="different workstation"):
            client.connect()
        assert client.server_id == HELLO["server_id"]
        assert client.generation == 1
        assert client.state == "incompatible"
    finally:
        client.close()


def test_paired_identity_checked_on_first_connection_before_commands():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=HELLO)
    client = RemoteConnection("https://host", "private-token", expected_server_id="paired-workstation",
                              transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RemoteError, match="different workstation"):
            client.connect()
        assert not client.can_mutate
        assert client.state == "incompatible"
        assert [request.method for request in requests] == ["GET"]
    finally:
        client.close()


def test_tls_hostname_separates_discovery_address_from_authenticated_name():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=HELLO)
    client = RemoteConnection("https://192.168.1.5:8765", "private-token", tls_hostname="ddh-trusted.local",
                              transport=httpx.MockTransport(handler))
    try:
        client.connect()
        assert requests[0].url.host == "192.168.1.5"
        assert requests[0].extensions["sni_hostname"] == "ddh-trusted.local"
    finally:
        client.close()


@pytest.mark.parametrize("hostname", ["bad\nname", "user:password@host", "foo/path", "", "[::1]"])
def test_invalid_tls_hostname_is_rejected(hostname):
    with pytest.raises(RemoteError, match="TLS hostname"):
        RemoteConnection("https://host", "private-token", tls_hostname=hostname)


def test_discovery_resolver_cannot_operate_without_paired_tls_identity():
    with pytest.raises(RemoteError, match="paired workstation"):
        RemoteConnection("https://host", "private-token", resolver=lambda: "https://other-host")


def test_expected_identity_must_match_saved_guard_before_network(tmp_path):
    guard = tmp_path / "state.json"
    guard.write_text(json.dumps({"server_id": "another-server", "request_id": None}))
    requests = []
    with pytest.raises(RemoteError, match="different workstation"):
        RemoteConnection("https://host", "private-token", expected_server_id=HELLO["server_id"],
                         pending_file=guard, transport=httpx.MockTransport(lambda request: requests.append(request)))
    assert requests == []
    assert json.loads(guard.read_text())["server_id"] == "another-server"


@pytest.mark.parametrize("declared", [False, True])
def test_response_size_is_bounded_before_json_decode(monkeypatch, declared):
    from donedatahoarder.remote import client as module
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 128)
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b" " * 80
            yield b" " * 80
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Length": "160"} if declared else {}, stream=Chunks()))
    client = RemoteConnection("https://host", "private-token", transport=transport)
    try:
        with pytest.raises(RemoteError, match="limit"):
            client.connect()
        assert client.state == "incompatible"
    finally:
        client.close()


def test_unexpected_compression_is_rejected_before_stream_expansion():
    class Unread(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("compressed body must not be decoded")
            yield b""
    client = RemoteConnection("https://host", "private-token", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Unread())))
    try:
        with pytest.raises(RemoteError, match="compressed"):
            client.connect()
    finally:
        client.close()


def test_pending_guard_survives_restart_and_reconciles_without_post(tmp_path):
    guard = tmp_path / "state" / "endpoint.json"
    posts = []
    def first(request):
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            saved = json.loads(guard.read_text())
            assert saved == {"server_id": HELLO["server_id"], "request_id": body["request_id"]}
            assert "private-token" not in guard.read_text()
            assert "preview-token" not in guard.read_text()
            raise httpx.ReadTimeout("lost reply", request=request)
        raise AssertionError(request)
    client = connection(first, pending_file=guard)
    with pytest.raises(RemoteError):
        RemoteWorkspaceService(client, "session").apply("preview-token", confirmed=True)
    client.close()
    resumed_requests = []
    def resumed(request):
        resumed_requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": "completed", "result": {"applied": 1}})
    resumed_client = connection(resumed, pending_file=guard)
    try:
        assert resumed_client.pending_request_id is None
        assert resumed_client.can_mutate
        assert json.loads(guard.read_text()) == {"server_id": HELLO["server_id"], "request_id": None}
        assert len(resumed_requests) == 1
        if os.name != "nt":
            assert guard.stat().st_mode & 0o777 == 0o600
    finally:
        resumed_client.close()


def test_persisted_identity_refuses_different_workstation(tmp_path):
    guard = tmp_path / "endpoint.json"
    guard.write_text(json.dumps({"server_id": "other-workstation", "request_id": None}))
    with pytest.raises(RemoteError, match="different workstation"):
        connection(pending_file=guard)
    assert json.loads(guard.read_text())["server_id"] == "other-workstation"


def test_state_write_failure_prevents_post(tmp_path, monkeypatch):
    posts = []
    client = connection(lambda request: posts.append(request), pending_file=tmp_path / "state.json")
    def fail_write(request_id):
        raise RemoteError("Cannot save workstation command state")
    monkeypatch.setattr(client, "_persist_guard", fail_write)
    try:
        with pytest.raises(RemoteError, match="Cannot save"):
            RemoteWorkspaceService(client, "session").start_pipeline()
        assert posts == []
    finally:
        client.close()


def test_corrupt_state_is_preserved_and_rejected(tmp_path):
    guard = tmp_path / "state.json"
    guard.write_text("broken")
    with pytest.raises(RemoteError, match="saved workstation command state"):
        RemoteConnection("https://host", "private-token", pending_file=guard)
    assert guard.read_text() == "broken"


def test_read_transport_cannot_bypass_mutation_or_target_another_host():
    client = connection()
    try:
        with pytest.raises(RemoteError, match="command submission"):
            client.request_json("POST", "/sessions", json={})
        for path in ("//attacker.test", "/../sessions", "/sessions?token=x", "https://other.test"):
            with pytest.raises(RemoteError, match="API path"):
                client.request_json("GET", path)
    finally:
        client.close()


def test_adapter_import_does_not_load_database_or_textual():
    root = Path(__file__).resolve().parents[1]
    code = "import sys; import donedatahoarder.remote.client; assert 'donedatahoarder.db.session' not in sys.modules; assert 'textual' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_adapter_real_server_roundtrip_runs_workstation_pipeline(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from donedatahoarder.core import jobs
    from donedatahoarder.core.jobs import JobManager
    from donedatahoarder.remote.server import create_app
    from donedatahoarder.tui import onboarding, service as local_service

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    for module in (jobs, onboarding, local_service):
        monkeypatch.setattr(module, "job_manager", manager)
    root = tmp_path / "workstation-collection"
    root.mkdir()
    (root / "notes.txt").write_text("A note for the remote pipeline", encoding="utf-8")
    token = "test-token-" + "a" * 48
    app = create_app(tmp_path / "workstation.db", token=token, allowed_roots=[root], name="HOME-PC")
    with TestClient(app) as server:
        def forward(request):
            response = server.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        client = RemoteConnection("https://workstation.test", token, transport=httpx.MockTransport(forward),
                                  pending_file=tmp_path / "laptop" / "connection.json")
        try:
            catalog = RemoteSessionCatalog(client)
            assert catalog.default_root == str(root)
            workspace = catalog.open(root=str(root))
            workspace.update_settings("test-model", 1)
            assert workspace.snapshot()["session"]["model"] == "test-model"
            workspace.start_pipeline(metadata_only=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                snapshot = workspace.snapshot()
                if snapshot["plan"]["state"] in {"completed", "failed"} and not snapshot["has_live_workers"]:
                    break
                time.sleep(0.02)
            assert snapshot["plan"]["state"] == "completed", snapshot.get("job")
            assert snapshot["counts"]["files"] == 1
            indexed = workspace.get_file(snapshot["files"][0]["id"])
            assert indexed["filename"] == "notes.txt"
            assert workspace.preview()["total"] == 0
            assert workspace.history() == []
            assert workspace.undo_preview()["total"] == 0
            assert client.pending_request_id is None
            assert (root / "notes.txt").read_text(encoding="utf-8") == "A note for the remote pipeline"
        finally:
            client.close()
            for thread in list(manager._worker_threads.values()):
                thread.join(timeout=10)

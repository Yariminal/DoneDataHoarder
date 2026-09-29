"""Discovery never grants trust; paired identities preserve pending outcomes."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
import threading
import time
from uuid import uuid4

import httpx
import pytest

from donedatahoarder.remote.client import RemoteError, RemoteWorkspaceService
from donedatahoarder.remote.profiles import ProfileStore, SavedProfile, pair_device

SERVER_ID = "b6b4ff41-cb3e-49b9-8f2d-a5e9cf95d578"
HOSTNAME = "ddh-" + SERVER_ID + ".local"
TOKEN = "device-" + "a" * 48
SECRET = "invitation-" + "b" * 48
HELLO = {"protocol": 1, "server_id": SERVER_ID, "name": "HOME-PC", "version": "0.6.0",
         "path_flavor": "windows", "default_root": r"E:\Photos", "model": "gemma3:12b", "workers": 1}


@pytest.fixture
def certificate(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def generate(hostname=HOSTNAME, suffix=""):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                            key_encipherment=True, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=True, crl_sign=True,
                                            encipher_only=False, decipher_only=False), critical=True)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / f"server{suffix}.crt", tmp_path / f"server{suffix}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        return cert_path.read_text(), cert_path, key_path
    return generate


def profile(certificate, **overrides):
    pem, _, _ = certificate()
    return SavedProfile(server_id=SERVER_ID, name="HOME-PC", device_id=str(uuid4()), token=TOKEN,
                        certificate_pem=pem, hostname=HOSTNAME, last_url="https://192.168.1.5:8765", **overrides)


def invitation(pem):
    value = {"version": 1, "server_id": SERVER_ID, "hostname": HOSTNAME,
             "certificate_pem": pem, "secret": SECRET, "expires_at": int(time.time()) + 120}
    return "ddh-pair-v1:" + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def test_profile_roundtrip_private_permissions_no_repr_secret_and_forget_retains_guard(tmp_path, certificate):
    store = ProfileStore(tmp_path / "state")
    saved = profile(certificate, auto_reconnect=True)
    assert store.list() == []
    store.save(saved)
    assert store.load(SERVER_ID) == saved
    assert store.list() == [saved]
    assert TOKEN not in repr(saved)
    assert "BEGIN CERTIFICATE" not in repr(saved)
    guard = store.guard_path(SERVER_ID)
    request_id = str(uuid4())
    guard.write_text(json.dumps({"server_id": SERVER_ID, "request_id": request_id}))
    if os.name != "nt":
        assert store._profile_path(SERVER_ID).stat().st_mode & 0o777 == 0o600
        assert store.directory.stat().st_mode & 0o777 == 0o700
    store.forget(SERVER_ID)
    assert store.load(SERVER_ID) is None
    assert store.list() == []
    assert json.loads(guard.read_text())["request_id"] == request_id


def test_manual_connection_adopts_identity_guard_after_pairing(tmp_path, certificate):
    from donedatahoarder.remote.client import RemoteConnection
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    request_id = str(uuid4())
    guard = store.guard_path(SERVER_ID)
    guard.write_text(json.dumps({"server_id": SERVER_ID, "request_id": request_id}))
    old_url_guard = tmp_path / (hashlib.sha256(saved.last_url.encode()).hexdigest() + ".json")
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        return httpx.Response(200, json={"request_id": request_id, "state": "uncertain"})
    connection = RemoteConnection(saved.last_url, TOKEN, ca_pem=saved.certificate_pem,
                                  pending_file=old_url_guard, guard_directory=tmp_path,
                                  transport=httpx.MockTransport(handler))
    try:
        connection.connect()
        assert connection.pending_request_id == request_id
        assert not connection.can_mutate
        assert len(requests) == 2
        assert not old_url_guard.exists()
        with pytest.raises(RemoteError):
            RemoteWorkspaceService(connection, "session").apply("preview", confirmed=True)
        assert len(requests) == 2
    finally:
        connection.close()


def test_auto_reconnect_opt_out_still_allows_explicit_connection(tmp_path, certificate):
    saved = profile(certificate, auto_reconnect=False)
    requests = []
    fail = [False]
    def handler(request):
        requests.append(request)
        if fail[0]:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=HELLO if request.url.path.endswith("/hello") else [])
    connection = ProfileStore(tmp_path).connection(saved, transport=httpx.MockTransport(handler))
    try:
        connection.connect()
        fail[0] = True
        with pytest.raises(RemoteError):
            connection.request_json("GET", "/sessions")
        before = len(requests)
        fail[0] = False
        with pytest.raises(RemoteError, match="disabled"):
            connection.request_json("GET", "/sessions")
        assert len(requests) == before
        connection.connect()
        assert connection.state == "connected"
        assert connection.request_json("GET", "/sessions") == []
    finally:
        connection.close()


def test_multiple_discovered_addresses_try_only_read_handshakes(tmp_path, certificate):
    saved = profile(certificate)
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.host == "192.168.1.99":
            raise httpx.ConnectError("unreachable VPN adapter", request=request)
        return httpx.Response(200, json=HELLO)
    connection = ProfileStore(tmp_path).connection(
        saved, resolver=lambda: ["https://192.168.1.99:8765", "https://192.168.1.100:8765"],
        transport=httpx.MockTransport(handler))
    try:
        connection.connect()
        assert [request.url.host for request in requests] == ["192.168.1.99", "192.168.1.100"]
        assert connection.url == "https://192.168.1.100:8765"
    finally:
        connection.close()


def test_pairing_endpoint_selection_sends_no_secret_across_multiple_adapters(certificate):
    from donedatahoarder.remote.profiles import select_pairing_endpoint
    pem, _, _ = certificate()
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert request.content == b""
        assert SECRET not in str(request.url)
        assert request.extensions["sni_hostname"] == HOSTNAME
        if request.url.host == "192.168.1.99":
            raise httpx.ConnectError("unreachable VPN adapter", request=request)
        return httpx.Response(401, json={"detail": "Unauthorized"})
    endpoint = select_pairing_endpoint(
        ["https://192.168.1.99:8765", "https://192.168.1.100:8765"], invitation(pem),
        transport=httpx.MockTransport(handler))
    assert endpoint == "https://192.168.1.100:8765"
    assert len(requests) == 2


def test_real_daemon_https_pairing_session_and_revocation(tmp_path, monkeypatch):
    """Exercise actual TLS, ASGI auth middleware, pairing persistence and client."""
    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    import socket
    import uvicorn
    from donedatahoarder.remote.client import RemoteSessionCatalog
    from donedatahoarder.remote.pairing import ensure_tls
    from donedatahoarder.remote.server import create_app

    root = tmp_path / "ssd"
    root.mkdir()
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    app = create_app(tmp_path / "workstation.db", token="admin-" + "z" * 48,
                     allowed_roots=[root], name="HOME-PC", pairing_path=tmp_path / "devices.sqlite3")
    pairing_store = app.state.remote_pairing
    cert, key, hostname = ensure_tls(tmp_path / "tls", pairing_store.server_id)
    invite = pairing_store.create_invitation(cert.read_text(), hostname)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          ssl_certfile=str(cert), ssl_keyfile=str(key),
                                          log_level="error", access_log=False, ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    client = None
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        store = ProfileStore(tmp_path / "laptop")
        saved = pair_device(f"https://127.0.0.1:{port}", invite, "OMARCHY-LAPTOP", store=store)
        assert saved.server_id == pairing_store.server_id
        assert pairing_store.list_devices()[0]["name"] == "OMARCHY-LAPTOP"
        client = store.connection(saved)
        client.connect()
        catalog = RemoteSessionCatalog(client)
        workspace = catalog.open(root=str(root))
        assert workspace.snapshot()["session"]["root_path"] == str(root)
        with pytest.raises(RemoteError, match="not accepted"):
            pair_device(f"https://127.0.0.1:{port}", invite, "SECOND-LAPTOP",
                        store=ProfileStore(tmp_path / "another-laptop"))
        pairing_store.revoke(saved.device_id)
        with pytest.raises(RemoteError, match="denied"):
            client.connect()
        assert client.state == "auth_error"
        assert not client.can_mutate
    finally:
        if client is not None:
            client.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def test_corrupt_profile_is_preserved_and_never_silently_repaired(tmp_path):
    store = ProfileStore(tmp_path)
    path = store._profile_path(SERVER_ID)
    path.write_text("broken private-state")
    for operation in (lambda: store.load(SERVER_ID), store.list):
        with pytest.raises(RemoteError, match="saved workstation profile"):
            operation()
    assert path.read_text() == "broken private-state"


def test_profile_identity_must_match_filename(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    store.save(saved)
    path = store._profile_path(SERVER_ID)
    data = json.loads(path.read_text())
    data["server_id"] = str(uuid4())
    path.write_text(json.dumps(data))
    with pytest.raises(RemoteError):
        store.load(SERVER_ID)
    with pytest.raises(RemoteError):
        store.list()


def test_stable_guard_migrates_lost_response_across_ip_change_without_post(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    store.save(saved)
    request_id = str(uuid4())
    old = tmp_path / (hashlib.sha256(saved.last_url.encode()).hexdigest() + ".json")
    old.write_text(json.dumps({"server_id": SERVER_ID, "request_id": request_id}))
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "192.168.1.99"
        assert request.extensions["sni_hostname"] == HOSTNAME
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        assert request.url.path.endswith("/commands/" + request_id)
        return httpx.Response(200, json={"request_id": request_id, "state": "completed", "result": {}})
    connection = store.connection(saved, resolver=lambda: "https://192.168.1.99:8765", transport=httpx.MockTransport(handler))
    try:
        assert json.loads(old.read_text())["request_id"] == request_id
        connection.connect()
        assert connection.pending_request_id is None
        assert connection.can_mutate
        assert store.load(SERVER_ID).last_url == "https://192.168.1.99:8765"
        assert json.loads(store.guard_path(SERVER_ID).read_text()) == {"server_id": SERVER_ID, "request_id": None}
        assert not old.exists()
        assert len(requests) == 2
    finally:
        connection.close()


def test_reconnect_resolves_changed_address_and_only_reads_lost_command_receipt(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    store.save(saved)
    address = [saved.last_url]
    posts, reads = [], []
    def handler(request):
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        if request.method == "POST":
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout("lost reply with " + TOKEN, request=request)
        reads.append(request)
        return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": "completed", "result": {}})
    connection = store.connection(saved, resolver=lambda: address[0], transport=httpx.MockTransport(handler))
    try:
        connection.connect()
        with pytest.raises(RemoteError):
            RemoteWorkspaceService(connection, "session").apply("preview", confirmed=True)
        address[0] = "https://192.168.1.100:8765"
        connection.connect()
        assert len(posts) == len(reads) == 1
        assert reads[0].url.host == "192.168.1.100"
        assert connection.pending_request_id is None
    finally:
        connection.close()


def test_conflicting_guard_migration_preserves_every_pending_id(tmp_path):
    store = ProfileStore(tmp_path)
    original = {}
    for name in ("a" * 64, "b" * 64):
        path = tmp_path / (name + ".json")
        original[path] = json.dumps({"server_id": SERVER_ID, "request_id": str(uuid4())})
        path.write_text(original[path])
    with pytest.raises(RemoteError, match="conflict"):
        store.migrate_guard(SERVER_ID)
    assert not store.guard_path(SERVER_ID).exists()
    assert all(path.read_text() == value for path, value in original.items())


def test_other_server_guard_is_not_migrated_and_corrupt_guard_is_preserved(tmp_path):
    store = ProfileStore(tmp_path)
    path = tmp_path / ("a" * 64 + ".json")
    original = json.dumps({"server_id": "other-server", "request_id": str(uuid4())})
    path.write_text(original)
    store.migrate_guard(SERVER_ID)
    assert path.read_text() == original
    path.write_text("broken")
    with pytest.raises(RemoteError, match="unreadable"):
        store.migrate_guard(SERVER_ID)
    assert path.read_text() == "broken"


def test_suspicious_profile_symlink_is_rejected(tmp_path, certificate):
    store = ProfileStore(tmp_path / "state")
    saved = profile(certificate)
    store.directory.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text("unchanged")
    try:
        store._profile_path(SERVER_ID).symlink_to(target)
    except OSError:
        pytest.skip("Host cannot create symlinks without elevated privileges")
    with pytest.raises(RemoteError, match="symlinks"):
        store.save(saved)
    with pytest.raises(RemoteError, match="symlinks"):
        store.forget(SERVER_ID)
    assert target.read_text() == "unchanged"


def test_profile_write_preserves_preflight_policy_error_without_writes(tmp_path, certificate, monkeypatch):
    # Windows may not permit creating the real symlink used above. Exercise
    # policy-error propagation on every platform so the diagnostic cannot be
    # accidentally wrapped as a generic serialization failure again.
    from donedatahoarder.remote import profiles
    store = ProfileStore(tmp_path / "uncreated-state")
    saved = profile(certificate)
    rejected = RemoteError("Saved workstation state must not use symlinks or junctions.")

    def reject_path(path):
        raise rejected

    monkeypatch.setattr(profiles, "_no_links", reject_path)
    with pytest.raises(RemoteError, match="symlinks") as caught:
        store.save(saved)
    assert caught.value is rejected
    assert not store.directory.exists()


def test_unavailable_discovery_falls_back_to_last_address(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    def unavailable():
        raise OSError("multicast blocked")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=HELLO)
    connection = store.connection(saved, resolver=unavailable, transport=httpx.MockTransport(handler))
    try:
        connection.connect()
        assert str(requests[0].url).startswith(saved.last_url)
    finally:
        connection.close()


@pytest.fixture
def tls_server(certificate):
    servers = []
    def start(*, foreign_certificate=False):
        pem, cert_path, key_path = certificate(suffix="foreign" if foreign_certificate else "")
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.command, self.path, body, self.headers.get("Authorization")))
                self.reply({"token": TOKEN, "device_id": str(uuid4()), "server_id": SERVER_ID})
            def do_GET(self):
                requests.append((self.command, self.path, None, self.headers.get("Authorization")))
                self.reply(HELLO)
            def reply(self, body):
                binary = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(binary)))
                self.end_headers()
                self.wfile.write(binary)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"https://127.0.0.1:{server.server_address[1]}", pem, requests
    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_tls_pairing_pins_certificate_and_uses_hostname_over_ip(tmp_path, tls_server):
    url, pem, requests = tls_server()
    store = ProfileStore(tmp_path / "laptop")
    saved = pair_device(url, invitation(pem), "OMARCHY-LAPTOP", store=store, auto_reconnect=True)
    assert saved.server_id == SERVER_ID
    assert saved.name == "HOME-PC"
    assert saved.auto_reconnect
    assert store.load(SERVER_ID) == saved
    assert requests[0] == ("POST", "/remote/v1/pair", {"secret": SECRET, "device_name": "OMARCHY-LAPTOP"}, None)
    assert requests[1] == ("GET", "/remote/v1/hello", None, "Bearer " + TOKEN)


def test_forged_discovery_endpoint_gets_no_pairing_secret(tmp_path, certificate, tls_server):
    trusted_pem, _, _ = certificate()
    url, _, requests = tls_server(foreign_certificate=True)
    store = ProfileStore(tmp_path / "laptop")
    with pytest.raises(RemoteError, match="securely pair") as caught:
        pair_device(url, invitation(trusted_pem), "LAPTOP", store=store)
    assert SECRET not in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert requests == []
    assert store.list() == []


def test_saved_wrong_hostname_fails_tls_before_authorization_header(tmp_path, tls_server):
    url, pem, requests = tls_server()
    saved = SavedProfile(server_id=SERVER_ID, name="HOME-PC", device_id=str(uuid4()), token=TOKEN,
                         certificate_pem=pem, hostname="another-workstation.local", last_url=url)
    connection = ProfileStore(tmp_path / "laptop").connection(saved)
    try:
        with pytest.raises(RemoteError, match="Cannot reach"):
            connection.connect()
        assert requests == []
    finally:
        connection.close()


@pytest.mark.parametrize("status", [401, 429, 302, 500])
def test_pairing_errors_never_expose_secret_response_or_replay(tmp_path, certificate, status):
    pem, _, _ = certificate()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.test"},
                              json={"detail": SECRET + TOKEN})
    store = ProfileStore(tmp_path / "laptop")
    with pytest.raises(RemoteError) as caught:
        pair_device("https://192.168.1.5", invitation(pem), "LAPTOP", store=store,
                    transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert SECRET not in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert store.list() == []


def test_pairing_response_cannot_replace_invitation_identity(tmp_path, certificate):
    pem, _, _ = certificate()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"server_id": "forged", "token": TOKEN, "device_id": str(uuid4())})
    store = ProfileStore(tmp_path / "laptop")
    with pytest.raises(RemoteError, match="different workstation"):
        pair_device("https://192.168.1.5", invitation(pem), "LAPTOP", store=store,
                    transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert store.list() == []


def test_identity_guard_lease_blocks_two_tuis_and_reloads_pending_after_release(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    posts = []
    def first_handler(request):
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        posts.append(json.loads(request.content))
        raise httpx.ReadTimeout("reply lost", request=request)
    first = store.connection(saved, transport=httpx.MockTransport(first_handler))
    second_requests = []
    def second_handler(request):
        second_requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        return httpx.Response(200, json={"request_id": posts[0]["request_id"], "state": "uncertain"})
    # Construct before the first client creates pending work. The second must
    # reload the guard only after it has obtained exclusive ownership.
    second = store.connection(saved, url="https://192.168.1.99:8765",
                              transport=httpx.MockTransport(second_handler))
    try:
        first.connect()
        with pytest.raises(RemoteError):
            RemoteWorkspaceService(first, "session").apply("preview", confirmed=True)
        guard_before = store.guard_path(SERVER_ID).read_text()
        with pytest.raises(RemoteError, match="already open in another connection"):
            second.connect()
        assert store.guard_path(SERVER_ID).read_text() == guard_before
        assert not second.can_mutate
        assert len(second_requests) == 1
        first.close()
        second.connect()
        assert second.pending_request_id == posts[0]["request_id"]
        assert not second.can_mutate
        assert len(posts) == 1
        assert len(second_requests) == 3
    finally:
        first.close()
        second.close()
    assert store.guard_path(SERVER_ID).with_suffix(".json.lock").exists()


def test_guard_lease_is_released_by_process_crash(tmp_path):
    import subprocess
    import sys
    from donedatahoarder.remote.client import RemoteConnection
    guard = tmp_path / "guard.json"
    code = (
        "import os,sys; from pathlib import Path; import httpx; "
        "from donedatahoarder.remote.client import RemoteConnection; "
        "client=RemoteConnection('https://host','private-token',transport=httpx.MockTransport(lambda request: None)); "
        "client._acquire_guard_lease(Path(sys.argv[1])); os._exit(0)"
    )
    completed = subprocess.run([sys.executable, "-c", code, str(guard)],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    client = RemoteConnection("https://host", "private-token",
                              transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        client._acquire_guard_lease(guard)
    finally:
        client.close()
    assert guard.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("phase", ["post", "receipt"])
def test_closed_worker_cannot_erase_next_connections_pending_guard(tmp_path, certificate, phase):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    entered, release = threading.Event(), threading.Event()
    first_ids, worker_errors = [], []

    def old_handler(request):
        if request.url.path.endswith("/hello"):
            return httpx.Response(200, json=HELLO)
        if request.method == "POST":
            first_ids.append(json.loads(request.content)["request_id"])
            if phase == "receipt":
                return httpx.Response(200, json={"request_id": first_ids[0], "state": "running"})
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"request_id": first_ids[0], "state": "completed", "result": {}})

    first = store.connection(saved, transport=httpx.MockTransport(old_handler))
    first.connect()
    if phase == "receipt":
        with pytest.raises(RemoteError, match="running"):
            RemoteWorkspaceService(first, "session").apply("old-preview", confirmed=True)

    def old_work():
        try:
            if phase == "post":
                RemoteWorkspaceService(first, "session").apply("old-preview", confirmed=True)
            else:
                first.request_json("GET", "/sessions")
        except Exception as exc:
            worker_errors.append(exc)

    thread = threading.Thread(target=old_work, daemon=True)
    second = None
    second_ids = []
    try:
        thread.start()
        assert entered.wait(3)
        first.close()
        def new_handler(request):
            if request.url.path.endswith("/hello"):
                return httpx.Response(200, json=HELLO)
            if request.method == "GET":
                return httpx.Response(200, json={"request_id": first_ids[0], "state": "completed", "result": {}})
            second_ids.append(json.loads(request.content)["request_id"])
            return httpx.Response(200, json={"request_id": second_ids[0], "state": "running"})
        second = store.connection(saved, transport=httpx.MockTransport(new_handler))
        second.connect()
        with pytest.raises(RemoteError, match="running"):
            RemoteWorkspaceService(second, "session").apply("new-preview", confirmed=True)
        guard_before = store.guard_path(SERVER_ID).read_text()
        assert json.loads(guard_before)["request_id"] == second_ids[0]
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert len(worker_errors) == 1
        assert isinstance(worker_errors[0], RemoteError)
        assert "closed" in str(worker_errors[0])
        assert first.pending_request_id == first_ids[0]
        assert store.guard_path(SERVER_ID).read_text() == guard_before
    finally:
        release.set()
        thread.join(timeout=3)
        first.close()
        if second is not None:
            second.close()


def test_close_during_hello_prevents_late_guard_acquisition_and_profile_update(tmp_path, certificate):
    store = ProfileStore(tmp_path)
    saved = profile(certificate)
    store.save(saved)
    entered, release = threading.Event(), threading.Event()
    worker_errors = []
    def old_handler(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=HELLO)
    first = store.connection(saved, transport=httpx.MockTransport(old_handler))
    def old_work():
        try:
            first.connect()
        except Exception as exc:
            worker_errors.append(exc)
    thread = threading.Thread(target=old_work, daemon=True)
    second = None
    try:
        thread.start()
        assert entered.wait(3)
        first.close()
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert len(worker_errors) == 1
        assert isinstance(worker_errors[0], RemoteError)
        assert "closed" in str(worker_errors[0])
        assert first._guard_lease is None
        assert not store.guard_path(SERVER_ID).exists()
        assert store.load(SERVER_ID) == saved
        second = store.connection(saved, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=HELLO)))
        second.connect()
        assert second.can_mutate
    finally:
        release.set()
        thread.join(timeout=3)
        first.close()
        if second is not None:
            second.close()

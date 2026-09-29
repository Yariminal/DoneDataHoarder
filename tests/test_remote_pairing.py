"""Owner-issued bootstrap, TLS identity persistence, and device revocation."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import ssl
import threading
from uuid import uuid4

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from donedatahoarder.remote import pairing
from donedatahoarder.remote.pairing import (
    INVITATION_PREFIX, PairingError, PairingRateLimited, PairingStore,
    ensure_tls, parse_invitation, redact_invitation,
)
from donedatahoarder.remote.server import create_app, PREFIX


@pytest.fixture
def credentials(tmp_path):
    server_id = str(uuid4())
    store = PairingStore(tmp_path / "devices.db", server_id)
    certificate, key, hostname = ensure_tls(tmp_path / "tls", server_id)
    return store, certificate, key, hostname


def invitation(credentials, ttl=600):
    store, certificate, _, hostname = credentials
    return store.create_invitation(certificate.read_text(encoding="ascii"), hostname, ttl)


def encode(data):
    return INVITATION_PREFIX + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def test_invitation_issues_one_revocable_device_and_persists_only_hashes(credentials):
    store, certificate, _, hostname = credentials
    value = invitation(credentials)
    parsed = parse_invitation(value)
    assert parsed["hostname"] == hostname
    assert parsed["certificate_pem"] == certificate.read_text(encoding="ascii")
    assert len(base64.urlsafe_b64decode(parsed["secret"] + "=")) == 32
    issued = store.redeem(parsed["secret"], "  Omarchy laptop  ")
    assert issued["server_id"] == store.server_id
    assert store.authenticate(issued["token"])
    assert not store.authenticate("wrong")
    with pytest.raises(PairingError, match="not accepted"):
        store.redeem(parsed["secret"], "second laptop")
    restarted = PairingStore(store.path, store.server_id)
    assert restarted.authenticate(issued["token"])
    assert restarted.list_devices()[0]["name"] == "Omarchy laptop"
    for secret in (parsed["secret"], issued["token"]):
        assert secret.encode() not in store.path.read_bytes()
        assert secret not in json.dumps(restarted.list_devices())
    assert restarted.revoke(issued["device_id"])
    assert not store.authenticate(issued["token"])
    assert not restarted.revoke(str(uuid4()))
    assert restarted.list_devices()[0]["revoked_at"] is not None


def test_new_invitation_replaces_previous_and_expiry_is_enforced(credentials, monkeypatch):
    store = credentials[0]
    first = parse_invitation(invitation(credentials))
    second = parse_invitation(invitation(credentials, ttl=10))
    with pytest.raises(PairingError):
        store.redeem(first["secret"], "old")
    monkeypatch.setattr(pairing.time, "time", lambda: second["expires_at"] + 1)
    with pytest.raises(PairingError):
        store.redeem(second["secret"], "expired")
    with pytest.raises(PairingError, match="expired"):
        parse_invitation(encode(second))


def test_concurrent_redemption_consumes_exactly_once(credentials):
    store = credentials[0]
    secret = parse_invitation(invitation(credentials))["secret"]

    def redeem(index):
        try:
            return store.redeem(secret, f"laptop {index}")
        except PairingError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(redeem, range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_devices()) == 1


def test_invalid_attempt_limit_survives_restart_and_then_expires(credentials, monkeypatch):
    store = credentials[0]
    now = pairing.time.time()
    monkeypatch.setattr(pairing.time, "time", lambda: now)
    secret = parse_invitation(invitation(credentials))["secret"]
    for _ in range(20):
        with pytest.raises(PairingError):
            store.redeem("wrong", "laptop")
    restarted = PairingStore(store.path, store.server_id)
    with pytest.raises(PairingRateLimited):
        restarted.redeem(secret, "laptop")
    monkeypatch.setattr(pairing.time, "time", lambda: now + 61)
    assert restarted.redeem(secret, "laptop")["token"]


@pytest.mark.parametrize("change", [
    {"server_id": "not-uuid"}, {"hostname": "evil.example"},
    {"secret": "short"}, {"secret": []}, {"version": 2}, {"version": True},
    {"expires_at": float("inf")}, {"expires_at": float("nan")},
    {"expires_at": 0}, {"expires_at": "soon"}, {"certificate_pem": "not a certificate"},
    {"unknown": "field"},
])
def test_invitation_validation_does_not_reflect_secret(credentials, change):
    parsed = parse_invitation(invitation(credentials))
    secret = parsed["secret"]
    parsed.update(change)
    with pytest.raises(PairingError) as caught:
        parse_invitation(encode(parsed))
    assert secret not in str(caught.value)


@pytest.mark.parametrize("value", ["", "short", "ddh-pair-v1:***", "x" * 17000, None, 3])
def test_malformed_invitation_rejected(value):
    with pytest.raises(PairingError):
        parse_invitation(value)


def test_redaction(credentials):
    value = invitation(credentials)
    assert redact_invitation("Unable to use " + value + ".") == "Unable to use [pairing invitation redacted]."


@pytest.mark.parametrize("name", ["", " ", "x" * 81, "bad\nname", "bad\x1bname", "bad\x7fname", None])
def test_invalid_device_name_preserves_invitation(credentials, name):
    store = credentials[0]
    secret = parse_invitation(invitation(credentials))["secret"]
    with pytest.raises(PairingError):
        store.redeem(secret, name)
    assert store.redeem(secret, "valid laptop")


def test_identity_cannot_be_rebound(credentials):
    store, certificate, key, _ = credentials
    before = certificate.read_bytes(), key.read_bytes()
    with pytest.raises(PairingError, match="different workstation"):
        PairingStore(store.path, str(uuid4()))
    with pytest.raises(PairingError, match="TLS identity"):
        ensure_tls(certificate.parent, str(uuid4()))
    assert (certificate.read_bytes(), key.read_bytes()) == before


def test_tls_identity_persists_and_partial_identity_is_not_replaced(credentials):
    store, certificate, key, hostname = credentials
    original = certificate.read_bytes(), key.read_bytes()
    assert ensure_tls(certificate.parent, store.server_id) == (certificate, key, hostname)
    assert original == (certificate.read_bytes(), key.read_bytes())
    certificate.unlink()
    with pytest.raises(PairingError, match="incomplete"):
        ensure_tls(certificate.parent, store.server_id)
    assert not certificate.exists()
    assert key.read_bytes() == original[1]


def test_mismatched_saved_key_is_rejected(credentials, tmp_path):
    store, certificate, key, _ = credentials
    _, unrelated_key, _ = ensure_tls(tmp_path / "other-tls", str(uuid4()))
    key.write_bytes(unrelated_key.read_bytes())
    with pytest.raises(PairingError, match="TLS identity"):
        ensure_tls(certificate.parent, store.server_id)


def test_real_tls_accepts_invitation_certificate_and_rejects_wrong_hostname(credentials):
    _, certificate, key, hostname = credentials
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    client_context = ssl.create_default_context(cadata=certificate.read_text(encoding="ascii"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    failures = []

    def serve():
        try:
            for _ in range(2):
                connection, _ = listener.accept()
                try:
                    with server_context.wrap_socket(connection, server_side=True) as secure:
                        assert secure.recv(3) == b"DDH"
                        secure.sendall(b"OK")
                except ssl.SSLError:
                    connection.close()
        except Exception as exc:
            failures.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    address = listener.getsockname()
    with socket.create_connection(address, timeout=5) as connection:
        with client_context.wrap_socket(connection, server_hostname=hostname) as secure:
            secure.sendall(b"DDH")
            assert secure.recv(2) == b"OK"
    with socket.create_connection(address, timeout=5) as connection:
        with pytest.raises(ssl.SSLCertVerificationError):
            client_context.wrap_socket(connection, server_hostname="impostor.local")
    thread.join(timeout=7)
    assert not thread.is_alive()
    assert not failures


@pytest.fixture
def paired_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    root = tmp_path / "collection"
    root.mkdir()
    owner_token = "owner-" + "a" * 48
    app = create_app(tmp_path / "index.db", token=owner_token, allowed_roots=[root],
                     pairing_path=tmp_path / "paired-devices.db")
    store = app.state.remote_pairing
    certificate, key, hostname = ensure_tls(tmp_path / "tls", store.server_id)
    data = parse_invitation(store.create_invitation(certificate.read_text(encoding="ascii"), hostname))
    return app, store, data, owner_token


def test_pair_api_then_per_device_auth_and_revocation(paired_app):
    app, store, invitation_data, owner_token = paired_app
    with TestClient(app, base_url="https://workstation") as client:
        assert client.get(PREFIX + "/hello").status_code == 401
        response = client.post(PREFIX + "/pair", json={
            "secret": invitation_data["secret"], "device_name": "Omarchy"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        issued = response.json()
        client.headers["Authorization"] = "Bearer " + issued["token"]
        assert client.get(PREFIX + "/hello").json()["server_id"] == store.server_id
        assert store.revoke(issued["device_id"])
        assert client.get(PREFIX + "/hello").status_code == 401
        client.headers["Authorization"] = "Bearer " + owner_token
        assert client.get(PREFIX + "/hello").status_code == 200


def test_pair_requires_https_and_does_not_consume_invitation_over_http(paired_app):
    app, store, data, _ = paired_app
    with TestClient(app) as client:
        response = client.post(PREFIX + "/pair", json={"secret": data["secret"], "device_name": "laptop"})
        assert response.status_code == 400
        assert data["secret"] not in response.text
    assert store.redeem(data["secret"], "laptop")


def test_paired_device_credentials_require_https_but_owner_supports_ssh_http(paired_app):
    app, store, data, owner_token = paired_app
    device = store.redeem(data["secret"], "laptop")
    with TestClient(app) as client:
        response = client.get(PREFIX + "/hello", headers={"Authorization": "Bearer " + device["token"]})
        assert response.status_code == 401
        response = client.get(PREFIX + "/hello", headers={"Authorization": "Bearer " + owner_token})
        assert response.status_code == 200


def test_pair_api_bounds_input_and_does_not_echo_secrets(paired_app):
    app, store, data, _ = paired_app
    with TestClient(app, base_url="https://workstation") as client:
        assert client.get(PREFIX + "/pair").status_code == 401
        assert client.post(PREFIX + "/pair/").status_code == 401
        for payload in ({"secret": data["secret"]},
                        {"secret": data["secret"], "device_name": 5},
                        {"secret": data["secret"], "device_name": "laptop", "unexpected": True}):
            response = client.post(PREFIX + "/pair", json=payload)
            assert response.status_code == 422
            assert data["secret"] not in response.text
        response = client.post(PREFIX + "/pair", content="x" * 9000, headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert client.post(PREFIX + "/pair", content="bad").status_code == 415
        assert client.post(PREFIX + "/pair", content="bad", headers={"Content-Type": "application/json"}).status_code == 422
        # Deeply nested valid JSON still fits the byte bound but must not leak a
        # RecursionError through the ASGI server or its diagnostic logs.
        nested = "[" * 2000 + "0" + "]" * 2000
        response = client.post(PREFIX + "/pair", content=nested, headers={"Content-Type": "application/json"})
        assert response.status_code == 422
    assert store.redeem(data["secret"], "laptop")


def test_pair_api_limits_invalid_attempts_without_secret_reflection(paired_app):
    app, _, _, _ = paired_app
    with TestClient(app, base_url="https://workstation") as client:
        for _ in range(20):
            response = client.post(PREFIX + "/pair", json={"secret": "bad", "device_name": "laptop"})
            assert response.status_code == 401
        response = client.post(PREFIX + "/pair", json={"secret": "bad", "device_name": "laptop"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


def test_pairing_database_cannot_live_in_authorized_collection(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    database = tmp_path / "index.db"
    with pytest.raises(ValueError, match="paired device credentials"):
        create_app(database, token="x" * 48, allowed_roots=[root], pairing_path=root / "devices.db")
    assert not database.exists()
    assert not (root / "devices.db").exists()

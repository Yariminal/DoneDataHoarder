"""Owner-issued TLS invitations and individually revocable workstation credentials.

Discovery is deliberately absent here: certificate trust and the one-use secret
come from the workstation owner's invitation, never an mDNS announcement.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import UUID, uuid4

INVITATION_PREFIX = "ddh-pair-v1:"
MAX_INVITATION_BYTES = 16384
MAX_PAIR_BODY_BYTES = 8192
_SECRET = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


class PairingError(ValueError):
    """An invitation or device credential was not accepted."""


class PairingRateLimited(PairingError):
    """Too many unsuccessful invitation redemption attempts."""


def _identity(value: str) -> str:
    try:
        identity = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PairingError("Invalid workstation identity") from exc
    if identity != value:
        raise PairingError("Invalid workstation identity")
    return identity


def _hostname(server_id: str) -> str:
    return "ddh-" + _identity(server_id) + ".local"


def _private_path(path: Path) -> Path:
    """Reject symlink/junction ancestry before writing private state."""
    from donedatahoarder.core.scanner import _is_link_or_reparse
    path = Path(path).expanduser().absolute()
    for component in (path, *path.parents):
        try:
            component.lstat()
        except FileNotFoundError:
            continue
        if _is_link_or_reparse(component):
            raise PairingError("Pairing state must not use symlinks or junctions")
    return path


def _create_private(path: Path, content: bytes) -> None:
    path = _private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _certificate(pem: str, hostname: str):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    try:
        if (not isinstance(pem, str) or len(pem) > 8192
                or pem.count("-----BEGIN CERTIFICATE-----") != 1
                or pem.count("-----END CERTIFICATE-----") != 1):
            raise ValueError("Invalid certificate")
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        cert.verify_directly_issued_by(cert)
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if names.get_values_for_type(x509.DNSName) != [hostname]:
            raise ValueError("Wrong hostname")
        now = datetime.now(timezone.utc)
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError("Certificate is not current")
        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
            raise ValueError("Unsupported certificate key")
        return cert
    except Exception as exc:
        raise PairingError("Invalid or expired workstation certificate") from exc


def ensure_tls(directory: Path, server_id: str) -> tuple[Path, Path, str]:
    """Create a persistent self-signed identity, or verify it without rotating it.

    A partial or mismatched identity fails closed so an existing pairing is never
    silently replaced. The caller keeps this directory outside collection roots.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    hostname = _hostname(server_id)
    directory = _private_path(directory)
    cert_path = directory / "certificate.pem"
    key_path = directory / "private-key.pem"
    _private_path(cert_path)
    _private_path(key_path)
    if cert_path.exists() != key_path.exists():
        raise PairingError("TLS identity is incomplete; restore both saved certificate and key")
    if not cert_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
                               content_commitment=False, data_encipherment=False,
                               key_agreement=False, key_cert_sign=False, crl_sign=False,
                               encipher_only=None, decipher_only=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
        _create_private(key_path, key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        _create_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    try:
        if cert_path.stat().st_size > 8192 or key_path.stat().st_size > 16384:
            raise ValueError("Oversized identity file")
        cert = _certificate(cert_path.read_text(encoding="ascii"), hostname)
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if (cert.public_key().public_bytes(serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo) !=
                key.public_key().public_bytes(serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo)):
            raise ValueError("Certificate and key do not match")
    except Exception as exc:
        raise PairingError("Saved TLS identity is invalid; restore its matching certificate and key") from exc
    return cert_path, key_path, hostname


def parse_invitation(value: str) -> dict:
    """Validate an owner-transferred invitation without trusting network metadata."""
    try:
        if not isinstance(value, str) or len(value) > MAX_INVITATION_BYTES:
            raise ValueError("Invalid invitation size")
        value = value.strip()
        if not value.startswith(INVITATION_PREFIX):
            raise ValueError("Invalid invitation version")
        encoded = value[len(INVITATION_PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ValueError("Invalid invitation encoding")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {
                "version", "server_id", "hostname", "certificate_pem", "secret", "expires_at"}
                or type(data["version"]) is not int or data["version"] != 1):
            raise ValueError("Invalid invitation fields")
        if data["hostname"] != _hostname(data["server_id"]):
            raise ValueError("Invalid invitation hostname")
        if not isinstance(data["secret"], str) or not _SECRET.fullmatch(data["secret"]):
            raise ValueError("Invalid invitation secret")
        if type(data["expires_at"]) not in (int, float) or not time.time() < data["expires_at"] <= time.time() + 3600:
            raise ValueError("Expired invitation")
        _certificate(data["certificate_pem"], data["hostname"])
        return data
    except Exception as exc:
        raise PairingError("Invalid or expired pairing invitation; request a new one on the workstation") from exc


def redact_invitation(value: str) -> str:
    """Remove complete invitation strings before showing diagnostic text."""
    return re.sub(r"ddh-pair-v1:[A-Za-z0-9_-]+", "[pairing invitation redacted]", str(value))


def _digest(kind: str, value: str) -> str:
    return hashlib.sha256((kind + "\0" + value).encode("ascii")).hexdigest()


class PairingStore:
    """Persistent invitation consumption and hashed, revocable device tokens."""

    def __init__(self, path: Path, server_id: str):
        self.server_id = _identity(server_id)
        self.path = _private_path(path)
        if not self.path.exists():
            try:
                _create_private(self.path, b"")
            except FileExistsError:
                pass
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pairing_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO pairing_metadata VALUES ('server_id',?)", (self.server_id,))
            if db.execute("SELECT value FROM pairing_metadata WHERE key='server_id'").fetchone()[0] != self.server_id:
                raise PairingError("Pairing database belongs to a different workstation identity")
            db.execute("""CREATE TABLE IF NOT EXISTS invitations (
                digest TEXT PRIMARY KEY, expires_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_digest TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, revoked_at REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pairing_attempts (
                id INTEGER PRIMARY KEY CHECK(id=1), window_start REAL NOT NULL, failures INTEGER NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO pairing_attempts VALUES (1,0,0)")

    @contextmanager
    def _connect(self):
        _private_path(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def create_invitation(self, certificate_pem: str, hostname: str, ttl: int = 600) -> str:
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise PairingError("Invitation lifetime must be between 1 and 3600 seconds")
        if hostname != _hostname(self.server_id):
            raise PairingError("Invitation hostname does not match the workstation")
        _certificate(certificate_pem, hostname)
        secret = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl
        with self._connect() as db:
            # Issuing a new invitation invalidates any previous unconsumed one.
            db.execute("DELETE FROM invitations")
            db.execute("INSERT INTO invitations VALUES (?,?)", (_digest("invitation", secret), expires_at))
        data = {"version": 1, "server_id": self.server_id, "hostname": hostname,
                "certificate_pem": certificate_pem, "secret": secret, "expires_at": expires_at}
        return INVITATION_PREFIX + base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")

    def redeem(self, invitation_secret: str, device_name: str) -> dict:
        if (not isinstance(device_name, str) or not device_name.strip() or len(device_name) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in device_name)):
            raise PairingError("Choose a device name between 1 and 80 characters")
        valid_format = isinstance(invitation_secret, str) and bool(_SECRET.fullmatch(invitation_secret))
        digest = _digest("invitation", invitation_secret) if valid_format else ""
        now = time.time()
        error = None
        result = None
        with self._connect() as db:
            window = db.execute("SELECT window_start,failures FROM pairing_attempts WHERE id=1").fetchone()
            if now < window[0] or now - window[0] >= 60:
                db.execute("UPDATE pairing_attempts SET window_start=?,failures=0 WHERE id=1", (now,))
                failures = 0
            else:
                failures = window[1]
            if failures >= 20:
                error = PairingRateLimited("Too many pairing attempts; try again in one minute")
            else:
                invitation = db.execute("SELECT expires_at FROM invitations WHERE digest=?", (digest,)).fetchone()
                if invitation is None or invitation[0] <= now:
                    db.execute("UPDATE pairing_attempts SET failures=failures+1 WHERE id=1")
                    error = PairingError("Invitation was not accepted; request a new one on the workstation")
                elif db.execute("SELECT count(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0] >= 100:
                    error = PairingError("Too many paired devices; revoke an unused device on the workstation")
                else:
                    token = secrets.token_urlsafe(48)
                    device_id = str(uuid4())
                    db.execute("DELETE FROM invitations WHERE digest=?", (digest,))
                    db.execute("INSERT INTO devices VALUES (?,?,?,?,NULL)",
                               (device_id, device_name.strip(), _digest("device", token), now))
                    result = {"token": token, "device_id": device_id, "server_id": self.server_id}
        # Raise after committing rate-limit state; invalid attempts persist across restart.
        if error is not None:
            raise error
        return result

    def authenticate(self, token: str) -> bool:
        if not isinstance(token, str) or not _SECRET.fullmatch(token):
            return False
        with self._connect() as db:
            return db.execute("SELECT 1 FROM devices WHERE token_digest=? AND revoked_at IS NULL",
                              (_digest("device", token),)).fetchone() is not None

    def list_devices(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT device_id,name,created_at,revoked_at FROM devices ORDER BY created_at")]

    def revoke(self, device_id: str) -> bool:
        device_id = _identity(device_id)
        with self._connect() as db:
            cursor = db.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at,?) WHERE device_id=?",
                                (time.time(), device_id))
            return cursor.rowcount == 1

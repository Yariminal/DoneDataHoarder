"""Private paired-device profiles and identity-bound command recovery state.

Discovery supplies addresses, never trust anchors. Only an out-of-band
invitation or an already saved profile can supply a workstation certificate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable
from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from donedatahoarder.remote.client import API_PREFIX, RemoteConnection, RemoteError, _base_url, _tls_context, _tls_name

MAX_PROFILE_BYTES = 32 * 1024


def state_directory() -> Path:
    configured = Path(os.environ.get("XDG_STATE_HOME", "")).expanduser()
    state = configured if configured.is_absolute() else Path.home() / ".local" / "state"
    return state / "donedatahoarder" / "remote"


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 33 for c in value):
        raise RemoteError("Invalid saved workstation identity.")
    return value


def _key(server_id: str) -> str:
    return hashlib.sha256(_identity(server_id).encode()).hexdigest()


def _no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise RemoteError("Saved workstation state must not use symlinks or junctions.")


def _read(path: Path, limit: int = MAX_PROFILE_BYTES) -> dict:
    _no_links(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise RemoteError("Cannot read saved workstation state. Preserve the file for recovery.")
    with path.open("r", encoding="utf-8") as handle:
        text = handle.read(limit + 1)
    if len(text) > limit:
        raise ValueError
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _write(path: Path, value: dict) -> None:
    temporary = None
    try:
        _no_links(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".ddh-profile-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except RemoteError:
        # Policy failures already carry bounded, secret-free diagnostics.
        # RemoteError inherits ValueError, so preserve them before wrapping
        # lower-level serialization and filesystem errors.
        raise
    except (OSError, ValueError, TypeError):
        raise RemoteError("Cannot save workstation state. Check its private state directory.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class SavedProfile:
    server_id: str
    name: str
    device_id: str
    token: str = field(repr=False)
    certificate_pem: str = field(repr=False)
    hostname: str
    last_url: str
    auto_reconnect: bool = False

    def __post_init__(self):
        _identity(self.server_id)
        _identity(self.device_id)
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 100 or
                any(ord(c) < 32 or ord(c) == 127 for c in self.name) or type(self.auto_reconnect) is not bool):
            raise RemoteError("Invalid saved workstation profile.")
        if (not isinstance(self.token, str) or not 32 <= len(self.token) <= 4096 or
                any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise RemoteError("Invalid saved workstation credential.")
        if not isinstance(self.certificate_pem, str) or len(self.certificate_pem) > 16384:
            raise RemoteError("Invalid saved workstation certificate.")
        _tls_context(None, self.certificate_pem)
        if not self.hostname:
            raise RemoteError("Invalid saved workstation TLS hostname.")
        _tls_name(self.hostname)
        if urlsplit(_base_url(self.last_url)).scheme != "https":
            raise RemoteError("Paired workstation connections require HTTPS.")


class ProfileStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory).expanduser() if directory is not None else state_directory()

    def _profile_path(self, server_id: str) -> Path:
        return self.directory / ("device-" + _key(server_id) + ".json")

    def guard_path(self, server_id: str) -> Path:
        return self.directory / ("server-" + _key(server_id) + ".json")

    def load(self, server_id: str) -> SavedProfile | None:
        path = self._profile_path(server_id)
        _no_links(path)
        if not path.exists():
            return None
        try:
            data = _read(path)
            version = data.pop("version", None)
            if type(version) is not int or version != 1:
                raise ValueError
            result = SavedProfile(**data)
            if result.server_id != server_id:
                raise ValueError
            return result
        except (OSError, ValueError, TypeError, RecursionError):
            raise RemoteError("Cannot read the saved workstation profile. Preserve it before pairing again.") from None

    def list(self) -> list[SavedProfile]:
        _no_links(self.directory)
        result = []
        for path in sorted(self.directory.glob("device-*.json")):
            try:
                data = _read(path)
                server_id = data.get("server_id")
                if self._profile_path(server_id) != path:
                    raise ValueError
                profile = self.load(server_id)
                if profile is not None:
                    result.append(profile)
            except (OSError, ValueError, TypeError, RecursionError):
                raise RemoteError("Cannot read a saved workstation profile. Preserve it before pairing again.") from None
        return sorted(result, key=lambda profile: (profile.name.casefold(), profile.server_id))

    def save(self, profile: SavedProfile) -> None:
        if not isinstance(profile, SavedProfile):
            raise RemoteError("Invalid saved workstation profile.")
        _write(self._profile_path(profile.server_id), {"version": 1, **asdict(profile)})

    def forget(self, server_id: str) -> None:
        """Remove credentials, retaining all command guards for future recovery.

        Forgetting does not revoke credentials on the workstation. Revocation
        is a separate workstation-owner action.
        """
        path = self._profile_path(server_id)
        _no_links(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise RemoteError("Cannot remove the saved workstation profile.") from None

    def migrate_guard(self, server_id: str) -> Path:
        """Merge old URL-keyed guards before using any newly discovered address.

        Distinct unresolved IDs cannot be represented by the single-command
        guard, so fail closed and leave every file intact in that case.
        """
        canonical = self.guard_path(server_id)
        _no_links(self.directory)
        paths = [canonical] if canonical.exists() else []
        paths.extend(path for path in self.directory.glob("*.json")
                     if re.fullmatch(r"[0-9a-f]{64}\.json", path.name))
        selected, pending = [], set()
        try:
            for path in paths:
                data = _read(path, 8192)
                if set(data) != {"server_id", "request_id"}:
                    raise ValueError
                _identity(data["server_id"])
                request_id = data["request_id"]
                if request_id is not None:
                    UUID(request_id)
                if path == canonical and data["server_id"] != server_id:
                    raise ValueError
                if data["server_id"] == server_id:
                    selected.append(path)
                    if request_id is not None:
                        pending.add(request_id)
            if len(pending) > 1:
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            raise RemoteError("Saved command states conflict or are unreadable. Preserve them and resolve their outcomes before reconnecting.") from None
        if selected:
            _write(canonical, {"server_id": server_id, "request_id": next(iter(pending), None)})
            try:
                for path in selected:
                    if path != canonical:
                        path.unlink()
            except OSError:
                raise RemoteError("Cannot finish migrating saved command state. Preserve the state directory before reconnecting.") from None
        return canonical

    def connection(self, profile: SavedProfile, *, url: str | None = None,
                   resolver: Callable[[], str | Sequence[str] | None] | None = None,
                   transport: httpx.BaseTransport | None = None) -> RemoteConnection:
        pending_file = self.guard_path(profile.server_id)

        def remember(connection: RemoteConnection) -> None:
            # Retain explicit preference changes made while a session runs.
            current = self.load(profile.server_id)
            if current is not None:
                self.save(replace(current, last_url=connection.url, name=connection.name))

        return RemoteConnection(url or profile.last_url, profile.token,
                                ca_pem=profile.certificate_pem, tls_hostname=profile.hostname,
                                expected_server_id=profile.server_id, pending_file=pending_file,
                                resolver=resolver, on_connect=remember, transport=transport,
                                auto_reconnect=profile.auto_reconnect, guard_directory=self.directory)


def choose_verified_endpoint(urls: Sequence[str], certificate_pem: str, hostname: str, *,
                             transport: httpx.BaseTransport | None = None) -> str:
    """Try addresses without sending an invitation secret or device credential."""
    if isinstance(urls, str) or not isinstance(urls, Sequence) or not 1 <= len(urls) <= 16:
        raise RemoteError("No usable nearby workstation addresses were found.")
    verify = _tls_context(None, certificate_pem)
    hostname = _tls_name(hostname)
    with httpx.Client(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                      timeout=httpx.Timeout(2.0, connect=1.5)) as client:
        for address in urls:
            endpoint = _base_url(address)
            if urlsplit(endpoint).scheme != "https":
                raise RemoteError("Pairing requires HTTPS.")
            try:
                with client.stream("GET", endpoint + API_PREFIX + "/hello",
                                   extensions={"sni_hostname": hostname},
                                   headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code == 401:
                        return endpoint
            except httpx.HTTPError:
                continue
    raise RemoteError("Cannot securely reach this workstation on its advertised addresses. Check the invitation or enter its HTTPS address manually.")


def select_pairing_endpoint(urls: Sequence[str], invitation_text: str, *,
                            transport: httpx.BaseTransport | None = None) -> str:
    from donedatahoarder.remote.pairing import parse_invitation
    try:
        invitation = parse_invitation(invitation_text)
    except (ValueError, TypeError, RecursionError):
        raise RemoteError("The pairing invitation is invalid or expired. Create a new one on the workstation.") from None
    return choose_verified_endpoint(urls, invitation["certificate_pem"], invitation["hostname"], transport=transport)


def pair_device(url: str, invitation_text: str, device_name: str, *,
                store: ProfileStore | None = None, name: str = "Workstation",
                auto_reconnect: bool = False,
                transport: httpx.BaseTransport | None = None) -> SavedProfile:
    """Redeem an owner-issued invitation over pinned, hostname-verified TLS.

    The invitation must arrive independently of discovery (for example pasted
    from the workstation). Pairing secrets and response bodies never appear in
    exception messages, and failed requests are not retried automatically.
    """
    from donedatahoarder.remote.pairing import parse_invitation

    try:
        invitation = parse_invitation(invitation_text)
    except (ValueError, TypeError, RecursionError):
        raise RemoteError("The pairing invitation is invalid or expired. Create a new one on the workstation.") from None
    endpoint = _base_url(url)
    if urlsplit(endpoint).scheme != "https":
        raise RemoteError("Pairing requires HTTPS.")
    if (not isinstance(device_name, str) or not device_name.strip() or not 1 <= len(device_name) <= 80 or
            any(ord(c) < 32 or ord(c) == 127 for c in device_name)):
        raise RemoteError("Enter a device name of 1–80 characters.")
    device_name = device_name.strip()
    context = _tls_context(None, invitation["certificate_pem"])
    hostname = _tls_name(invitation["hostname"])
    try:
        with httpx.Client(verify=context, transport=transport, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            with client.stream("POST", endpoint + API_PREFIX + "/pair",
                               json={"secret": invitation["secret"], "device_name": device_name},
                               headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                               extensions={"sni_hostname": hostname}) as response:
                if response.status_code != 200:
                    raise RemoteError("Pairing was not accepted. Check the invitation on the workstation and try again.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise RemoteError("Invalid workstation pairing response.")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PROFILE_BYTES:
                        raise RemoteError("Invalid workstation pairing response.")
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
        if not isinstance(result, dict) or result.get("server_id") != invitation["server_id"]:
            raise RemoteError("The invitation belongs to a different workstation.")
        profile = SavedProfile(server_id=invitation["server_id"], name=name,
                               device_id=result["device_id"], token=result["token"],
                               certificate_pem=invitation["certificate_pem"], hostname=hostname,
                               last_url=endpoint, auto_reconnect=auto_reconnect)
    except RemoteError:
        raise
    except httpx.HTTPError:
        raise RemoteError("Cannot securely pair with this workstation. Check its address, certificate, and invitation; no request was retried.") from None
    except (KeyError, ValueError, TypeError, RecursionError):
        raise RemoteError("Invalid workstation pairing response.") from None
    store = store or ProfileStore()
    connection = store.connection(profile, transport=transport)
    try:
        connection.connect()
        profile = replace(profile, name=connection.name)
        store.save(profile)
    finally:
        connection.close()
    return profile

"""Authenticated remote workspace adapters, without local database access.

The TUI renders locally. Only structured workspace commands and bounded results
cross this transport. A command whose reply was lost is never replayed.
"""
from __future__ import annotations

import ipaddress
import json as json_module
import os
from pathlib import Path
import re
import ssl
import threading
import time
import tempfile
from collections.abc import Sequence
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx

API_PREFIX = "/remote/v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class RemoteError(ValueError):
    """A safe, user-facing transport or workstation error."""

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.status_code = status_code


def _base_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or any(ord(c) < 33 for c in value):
        raise RemoteError("Enter a workstation HTTPS address without whitespace.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"https", "http"} or not host or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in {"", "/"} or
                "?" in value or "#" in value or "\\" in value):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            literal, marker, scope = host.partition("%25")
            if "%" in host and (not marker or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", scope)):
                raise ValueError
            address = ipaddress.ip_address(literal)
            if marker and (address.version != 6 or not address.is_link_local):
                raise ValueError
        except ValueError:
            address = None
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                raise ValueError
        if parsed.scheme == "http" and host.lower() != "localhost" and not (address and address.is_loopback):
            raise RemoteError("LAN connections require HTTPS. HTTP is allowed only on localhost or a literal loopback address for an SSH tunnel.")
    except (ValueError, TypeError) as exc:
        if isinstance(exc, RemoteError):
            raise
        raise RemoteError("Use an http(s) workstation address without credentials, a path, query, or fragment.") from None
    return value.rstrip("/")


def _transport_url(value: str) -> str:
    """Decode IPv6's URI zone delimiter for HTTPX's literal socket hostname.

    Discovery and saved profiles use RFC 6874's %25 delimiter. HTTPX passes
    its hostname to the socket backend unchanged, which needs the raw % scope.
    Only validated scoped IPv6 literals can contain a percent in a base URL.
    """
    parsed = urlsplit(value)
    if parsed.hostname and "%25" in parsed.hostname:
        return value.replace("%25", "%", 1)
    return value


def _part(value: Any) -> str:
    return quote(str(value), safe="")


def _tls_name(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 253 or
                              not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)):
        raise RemoteError("A valid workstation TLS hostname is required.")
    return value


def _tls_context(ca_file: str | Path | None, ca_pem: str | None) -> bool | ssl.SSLContext:
    if ca_file is not None and ca_pem is not None:
        raise RemoteError("Choose one workstation CA certificate source.")
    if ca_file is None and ca_pem is None:
        return True
    try:
        # A saved workstation certificate is the sole trust anchor. Loading
        # platform roots too would weaken the invitation's explicit trust.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(ca_file) if ca_file is not None else None,
                                      cadata=ca_pem)
        return context
    except (OSError, ssl.SSLError, ValueError, TypeError):
        raise RemoteError("Cannot load the workstation CA certificate.") from None


class RemoteConnection:
    """One verified endpoint and its connection/command-outcome state.

    ``request_json`` is read-only; ``submit`` is the only mutation entry point.
    Both are synchronous so Textual can call them in its existing workers.
    """

    def __init__(self, url: str, token: str, *, ca_file: str | Path | None = None,
                 transport: httpx.BaseTransport | None = None,
                 pending_file: str | Path | None = None,
                 ca_pem: str | None = None, expected_server_id: str | None = None,
                 tls_hostname: str | None = None,
                 resolver: Callable[[], str | Sequence[str] | None] | None = None,
                 on_connect: Callable[[RemoteConnection], None] | None = None,
                 guard_directory: str | Path | None = None,
                 auto_reconnect: bool = True):
        self.url = _base_url(url)
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise RemoteError("A valid workstation access token is required.")
        self._token = token
        self.tls_hostname = _tls_name(tls_hostname)
        if self.tls_hostname and urlsplit(self.url).scheme != "https":
            raise RemoteError("Paired workstation connections require HTTPS.")
        if expected_server_id is not None and (not isinstance(expected_server_id, str) or
                                              not expected_server_id or len(expected_server_id) > 256):
            raise RemoteError("A valid saved workstation identity is required.")
        if resolver is not None and not (expected_server_id and self.tls_hostname and (ca_file or ca_pem)):
            raise RemoteError("Address discovery requires a paired workstation identity and TLS certificate.")
        self.expected_server_id = expected_server_id
        self._resolver, self._on_connect = resolver, on_connect
        self.auto_reconnect = auto_reconnect
        self._guard_directory = Path(guard_directory) if guard_directory is not None else None
        self._guard_adopted = False
        self._guard_lease = None
        verify = _tls_context(ca_file, ca_pem)
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0), verify=verify,
            transport=transport, trust_env=False, follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Encoding": "identity"},
        )
        self._lock = threading.RLock()
        self._submission_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        self._closed = False
        self.hello: dict = {}
        self.state = "offline"
        self.name = urlsplit(self.url).hostname or "Workstation"
        self.latency_ms: float | None = None
        self.error: str | None = None
        self.pending_request_id: str | None = None
        self.last_receipt: dict | None = None
        self.generation = 0
        self.server_id: str | None = None
        self._pending_file = Path(pending_file) if pending_file is not None else None
        try:
            self._load_guard()
            if expected_server_id is not None and self.server_id not in {None, expected_server_id}:
                raise RemoteError("Saved command state belongs to a different workstation. Preserve it before reconnecting.")
        except Exception:
            self._client.close()
            raise

    def _load_guard(self) -> None:
        if self._pending_file is None:
            return
        try:
            with self._pending_file.open("r", encoding="utf-8") as handle:
                content = handle.read(8193)
                if len(content) > 8192:
                    raise ValueError
                value = json_module.loads(content)
            if (not isinstance(value, dict) or set(value) != {"server_id", "request_id"} or
                    not isinstance(value["server_id"], str) or not value["server_id"]):
                raise ValueError
            request_id = value["request_id"]
            if request_id is not None:
                if not isinstance(request_id, str):
                    raise ValueError
                UUID(request_id)
            self.server_id, self.pending_request_id = value["server_id"], request_id
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, RecursionError):
            raise RemoteError("Cannot read the saved workstation command state. Preserve it and resolve the pending outcome before reconnecting.") from None

    def _persist_guard(self, request_id: str | None) -> None:
        # A reply may arrive after close released this connection's lease.
        # Serialize every write with close so a retired worker cannot erase
        # the next connection's pending command.
        with self._lock:
            if self._closed:
                raise RemoteError("The workstation connection is closed. Its pending command state was preserved.")
            self._write_guard(request_id)

    def _write_guard(self, request_id: str | None) -> None:
        if self._pending_file is None:
            return
        temporary = None
        try:
            self._pending_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._pending_file.parent,
                                             prefix=".ddh-remote-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json_module.dump({"server_id": self.server_id, "request_id": request_id}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._pending_file)
            if os.name != "nt":
                descriptor = os.open(self._pending_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError, TypeError):
            raise RemoteError("Cannot save workstation command state. No new command can be sent until this is writable.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _acquire_guard_lease(self, path: Path) -> None:
        """One local connection owns a workstation's durable command guard.

        The OS releases this lock after a crash. The file stays in place so two
        processes can never hold locks on different inodes for the same guard.
        """
        with self._lock:
            if self._closed:
                raise RemoteError("The workstation connection is closed.")
            self._lock_guard(path)

    def _lock_guard(self, path: Path) -> None:
        if self._guard_lease is not None:
            return
        from donedatahoarder.remote.profiles import _no_links
        lock_path = Path(str(path) + ".lock")
        handle = None
        try:
            _no_links(lock_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            handle = os.fdopen(descriptor, "r+b")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._guard_lease = handle
        except (OSError, ValueError):
            if handle is not None:
                handle.close()
            raise self._fail("This workstation is already open in another connection, or its command state is unavailable. Close the other connection before reconnecting.", state="offline") from None

    @property
    def can_mutate(self) -> bool:
        return self.state == "connected" and self.pending_request_id is None and not self._closed

    def _safe(self, detail: Any) -> str:
        value = str(detail).replace(self._token, "[redacted]")
        return "".join(char for char in value if char in "\n\t" or ord(char) >= 32)[:1000]

    def _fail(self, message: str, *, state: str | None = None, status: int | None = None) -> RemoteError:
        with self._lock:
            self.error = self._safe(message)
            if state:
                self.state = "offline" if self._closed else state
        return RemoteError(self.error, status)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None) -> Any:
        if (not path.startswith("/") or path.startswith("//") or
                any(char in path for char in "?#\\") or
                any(part in {".", ".."} for part in path.split("/"))):
            raise RemoteError("Invalid remote API path.")
        if self._closed:
            raise RemoteError("The workstation connection is closed.")
        start = time.monotonic()
        try:
            extensions = {"sni_hostname": self.tls_hostname} if self.tls_hostname else None
            with self._client.stream(method, _transport_url(self.url) + API_PREFIX + path, params=params, json=json,
                                     extensions=extensions) as response:
                status = response.status_code
                if status == 401 or (status == 403 and path == "/hello"):
                    # Do not display authentication response bodies or headers.
                    raise self._fail("Workstation access was denied. Check the access token and permissions.",
                                     state="auth_error", status=status)
                if 300 <= status < 400:
                    raise self._fail("Workstation redirects are not allowed. Check the server address.",
                                     state="incompatible", status=status)
                # Prevent compressed responses from expanding before the size
                # bound can inspect their decoded chunks. The client explicitly
                # requests identity encoding, including for preview pixels.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise self._fail("Workstation returned an unsupported compressed response.", state="incompatible")
                length = response.headers.get("content-length")
                if length and length.isdigit() and (len(length) > 20 or int(length) > MAX_RESPONSE_BYTES):
                    raise self._fail("Workstation response exceeds the 16 MiB limit.", state="incompatible")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise self._fail("Workstation response exceeds the 16 MiB limit.", state="incompatible")
                    chunks.append(chunk)
                body = b"".join(chunks)
                try:
                    data = json_module.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise self._fail("Workstation returned an invalid JSON response.", state="incompatible") from None
                if status >= 400:
                    detail = data.get("detail") if isinstance(data, dict) else None
                    message = self._safe(detail) if isinstance(detail, str) else f"Workstation request failed (HTTP {status})."
                    raise self._fail(message, state="reconnecting" if status >= 500 else None, status=status)
                with self._lock:
                    self.latency_ms = round((time.monotonic() - start) * 1000, 1)
                return data
        except httpx.HTTPError:
            raise self._fail("Cannot reach the workstation. Reconnecting; remote processing may still be running.",
                             state="reconnecting" if self.hello else "offline") from None

    def connect(self) -> dict:
        """Authenticate and validate the protocol, including after an error."""
        with self._connect_lock:
            candidates = [self.url]
            if self._resolver is not None:
                try:
                    resolved = self._resolver()
                    if resolved is not None:
                        if isinstance(resolved, str):
                            resolved = [resolved]
                        if not isinstance(resolved, Sequence) or len(resolved) > 16:
                            raise RemoteError("Invalid workstation address discovery result.")
                        candidates = []
                        for address in [*resolved, self.url]:
                            candidate = _base_url(address)
                            if urlsplit(candidate).scheme != "https":
                                raise RemoteError("Paired workstation connections require HTTPS.")
                            if candidate not in candidates:
                                candidates.append(candidate)
                except RemoteError:
                    raise
                except Exception:
                    # Multicast may be unavailable; the last verified address
                    # remains a usable fallback. Never replay a mutation here.
                    pass
            for index, candidate in enumerate(candidates):
                self.url = candidate
                try:
                    hello = self._request("GET", "/hello")
                    break
                except RemoteError as exc:
                    if (index == len(candidates) - 1 or exc.status_code is not None or
                            self.state not in {"offline", "reconnecting"}):
                        raise
            if (not isinstance(hello, dict) or type(hello.get("protocol")) is not int or hello["protocol"] != 1 or
                    hello.get("path_flavor") not in {"windows", "posix"} or
                    not all(isinstance(hello.get(key), str) and hello[key] for key in ("server_id", "name", "version", "default_root", "model")) or
                    type(hello.get("workers")) is not int or not 1 <= hello["workers"] <= 32):
                raise self._fail("The workstation uses an incompatible remote protocol. Update both DDH installations.", state="incompatible")
            with self._lock:
                if self._closed:
                    raise RemoteError("The workstation connection is closed.")
                if ((self.server_id is not None and self.server_id != hello["server_id"]) or
                        (self.expected_server_id is not None and self.expected_server_id != hello["server_id"])):
                    raise self._fail("This address now belongs to a different workstation. Refusing to attach to a different server.", state="incompatible")
                self.server_id = hello["server_id"]
                if not self._guard_adopted:
                    if self._guard_directory is not None:
                        from donedatahoarder.remote.profiles import ProfileStore
                        store = ProfileStore(self._guard_directory)
                        self._acquire_guard_lease(store.guard_path(self.server_id))
                        self._pending_file = store.migrate_guard(self.server_id)
                    elif self._pending_file is not None:
                        self._acquire_guard_lease(self._pending_file)
                    self._load_guard()  # Re-read after acquiring the lifetime lease.
                    if self.server_id != hello["server_id"]:
                        raise self._fail("Saved command state belongs to a different workstation.", state="incompatible")
                    self._guard_adopted = True
                self._persist_guard(self.pending_request_id)
                self.hello = dict(hello)
                self.name = self._safe(hello["name"])
                self.state, self.error = "connected", None
                self.generation += 1
                if self._on_connect is not None:
                    try:
                        self._on_connect(self)
                    except Exception:
                        raise self._fail("Cannot update the saved workstation profile. Check its state directory.", state="offline") from None
        self._reconcile_pending()
        return dict(hello)

    def _ensure_connected(self) -> None:
        if self._closed:
            raise RemoteError("The workstation connection is closed.")
        if self.state in {"auth_error", "incompatible"}:
            raise RemoteError(self.error or "Reconnect to the workstation after checking its settings.")
        if self.state != "connected":
            if self.hello and not self.auto_reconnect:
                raise RemoteError("Automatic reconnect is disabled. Reconnect from the connection panel.")
            self.connect()

    def _receipt(self, receipt: Any, request_id: str) -> str:
        if (not isinstance(receipt, dict) or receipt.get("request_id") != request_id or
                receipt.get("state") not in {"completed", "failed", "running", "uncertain"} or
                (receipt.get("state") == "failed" and
                 (not isinstance(receipt.get("error"), dict) or
                  not isinstance(receipt["error"].get("detail"), str) or
                  type(receipt["error"].get("status_code")) is not int))):
            raise self._fail("Cannot verify the workstation command receipt. Further changes are blocked.", state="incompatible")
        state = receipt["state"]
        with self._lock:
            if self._closed:
                raise RemoteError("The workstation connection is closed. Its pending command state was preserved.")
            self.last_receipt = dict(receipt)
            if state in {"completed", "failed"}:
                if self.pending_request_id == request_id:
                    self._persist_guard(None)
                    self.pending_request_id = None
                self.error = None
            else:
                self.pending_request_id = request_id
                self.error = ("The workstation command is still running. Further changes are blocked until it finishes."
                              if state == "running" else
                              "The workstation cannot confirm the command outcome. Further changes are blocked; inspect its session history before recovery.")
        return state

    def _reconcile_pending(self) -> None:
        # Snapshot/preview workers may read while the command worker is still
        # sending. A receipt can legitimately be absent until POST is accepted.
        if self._submission_lock.locked() or not self.pending_request_id or not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            request_id = self.pending_request_id
            if not request_id:
                return
            try:
                receipt = self._request("GET", f"/commands/{_part(request_id)}")
            except RemoteError as exc:
                if exc.status_code == 404:
                    self.error = "The workstation has no receipt for the pending command. Its outcome is unknown; further changes are blocked."
                    return
                raise
            state = self._receipt(receipt, request_id)
            if state == "failed":
                error = receipt.get("error") or {}
                self.error = self._safe(error.get("detail", "The workstation command failed."))
        finally:
            self._reconcile_lock.release()

    def request_json(self, method: str, path: str, *, params: dict | None = None,
                     json: dict | None = None) -> Any:
        """Read bounded JSON and reconcile any command whose reply was lost."""
        if method.upper() != "GET" or json is not None:
            raise RemoteError("Use command submission for workstation changes.")
        self._ensure_connected()
        self._reconcile_pending()
        return self._request("GET", path, params=params)

    def submit(self, path: str, payload: dict) -> Any:
        """Submit exactly once; retain a receipt ID if its outcome is unknown."""
        if not self._submission_lock.acquire(blocking=False):
            raise RemoteError("Wait for the current workstation command to finish.")
        try:
            if not self.can_mutate:
                raise RemoteError(self.error or "Connect to the workstation before making changes.")
            request_id = str(uuid4())
            with self._lock:
                self._persist_guard(request_id)
                self.pending_request_id = request_id
            try:
                receipt = self._request("POST", path, json={**payload, "request_id": request_id})
            except RemoteError as exc:
                # A definitive client/auth/validation rejection did not dispatch
                # the command. A timeout, server failure, or malformed response
                # can follow execution, so retain its ID for reconciliation.
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    with self._lock:
                        self._persist_guard(None)
                        self.pending_request_id = None
                raise
            state = self._receipt(receipt, request_id)
            if state == "failed":
                error = receipt.get("error") or {}
                raise self._fail(self._safe(error.get("detail", "The workstation command failed.")),
                                 status=error.get("status_code"))
            if state != "completed":
                raise RemoteError(self.error or "The workstation command outcome is pending.")
            return receipt.get("result")
        finally:
            self._submission_lock.release()

    def close(self) -> None:
        """Disconnect without cancelling workstation work."""
        with self._lock:
            self._closed = True
            self.state = "offline"
        try:
            self._client.close()
        finally:
            with self._lock:
                if self._guard_lease is not None:
                    # Closing the descriptor releases flock / the Windows
                    # byte-range lock even when close runs in another worker.
                    self._guard_lease.close()
                    self._guard_lease = None


class RemoteSessionCatalog:
    is_remote = True

    def __init__(self, connection: RemoteConnection):
        self.connection = connection
        if not connection.hello:
            connection.connect()

    @property
    def model(self) -> str:
        return self.connection.hello["model"]

    @property
    def workers(self) -> int:
        return self.connection.hello["workers"]

    @property
    def path_flavor(self) -> str:
        return self.connection.hello["path_flavor"]

    @property
    def default_root(self) -> str:
        return self.connection.hello["default_root"]

    @property
    def path_label(self) -> str:
        return f"Collection folder on {self.connection.name}"

    @property
    def ollama_host(self) -> str:
        return f"Ollama on {self.connection.name}"

    def list_sessions(self) -> list[dict]:
        return self.connection.request_json("GET", "/sessions")

    def open(self, *, root: str | None = None, session_id: str | None = None,
             model: str | None = None) -> RemoteWorkspaceService:
        if root is not None and session_id is not None:
            raise RemoteError("Choose a folder or a saved session, not both.")
        if session_id is not None:
            # Attaching to saved work is read-only. In particular, a pending
            # receipt must not prevent inspecting its session after relaunch.
            workspace = RemoteWorkspaceService(self.connection, session_id)
            snapshot = workspace.snapshot(limit=1)
            if not isinstance(snapshot, dict) or (snapshot.get("session") or {}).get("id") != session_id:
                raise RemoteError("The workstation returned a different session.")
            return workspace
        payload = {key: value for key, value in {"root": root, "session_id": session_id, "model": model}.items()
                   if value is not None}
        result = self.connection.submit("/sessions", payload)
        if not isinstance(result, dict) or not isinstance(result.get("session_id"), str) or not result["session_id"]:
            raise RemoteError("The workstation returned an invalid session identifier.")
        return RemoteWorkspaceService(self.connection, result["session_id"])

    def readiness(self, model: str) -> str:
        return self.connection.request_json("GET", "/readiness", params={"model": model})["message"]

    def session_readiness(self, session_id: str) -> str:
        return self.connection.request_json("GET", f"/sessions/{_part(session_id)}/readiness")["message"]


class RemoteWorkspaceService:
    """The same synchronous workspace interface, backed by a remote process."""

    is_remote = True

    def __init__(self, connection: RemoteConnection, session_id: str):
        self.connection, self.session_id = connection, session_id
        self._path = f"/sessions/{_part(session_id)}"

    @property
    def path_flavor(self) -> str:
        return self.connection.hello["path_flavor"]

    def _command(self, command: str, **params: Any) -> Any:
        return self.connection.submit(self._path + "/commands", {"command": command, "params": params})

    def snapshot(self, *, limit: int = 500, offset: int = 0) -> dict:
        return self.connection.request_json("GET", self._path + "/snapshot", params={"limit": limit, "offset": offset})

    def get_file(self, file_id: int) -> dict:
        return self.connection.request_json("GET", self._path + f"/files/{_part(file_id)}")

    def list_sessions(self, limit: int = 100) -> list[dict]:
        return self.connection.request_json("GET", "/sessions")[:max(1, min(limit, 1000))]

    def preflight(self, *, metadata_only: bool = False) -> dict:
        return self._command("preflight", metadata_only=metadata_only)

    def start_pipeline(self, *, metadata_only: bool = False) -> dict:
        return self._command("start_pipeline", metadata_only=metadata_only)

    def resume_pipeline(self, *, retry_errors: bool = False) -> dict:
        return self._command("resume_pipeline", retry_errors=retry_errors)

    def pause_pipeline(self) -> None:
        return self._command("pause_pipeline")

    def cancel_pipeline(self) -> None:
        return self._command("cancel_pipeline")

    def approve(self, proposal_id: int, review_token: str | None = None) -> dict:
        return self._command("approve", proposal_id=proposal_id, review_token=review_token)

    def reject(self, proposal_id: int) -> dict:
        return self._command("reject", proposal_id=proposal_id)

    def set_keeper(self, group_id: int, file_id: int,
                   expected_keeper_id: int | None = None) -> dict:
        return self._command("set_keeper", group_id=group_id, file_id=file_id,
                             expected_keeper_id=expected_keeper_id)

    def edit(self, proposal_id: int, value: str) -> dict:
        return self._command("edit", proposal_id=proposal_id, value=value)

    def approve_clear(self, min_confidence: float = 0.9) -> dict:
        return self._command("approve_clear", min_confidence=min_confidence)

    def preview(self) -> dict:
        return self._command("preview")

    def apply(self, token: str, *, confirmed: bool = False) -> dict:
        return self._command("apply", token=token, confirmed=confirmed)

    def history(self, limit: int = 100) -> list[dict]:
        return self._command("history", limit=limit)

    def undo_preview(self) -> dict:
        return self._command("undo_preview")

    def undo(self, token: str, *, confirmed: bool = False) -> dict:
        return self._command("undo", token=token, confirmed=confirmed)

    def update_settings(self, model: str, workers: int) -> dict:
        return self._command("update_settings", model=model, workers=workers)

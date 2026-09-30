"""Opt-in mDNS discovery. Every result is an untrusted connection candidate.

Importing this module, constructing a browser, and reading its snapshot do not
open sockets. Only ``start`` enables network activity. No credentials or file
information are carried in announcements.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import importlib
import ipaddress
import re
import threading
import time
from typing import Any, Callable
from uuid import UUID


SERVICE_TYPE = "_ddh._tcp.local."
MAX_CANDIDATES = 64
MAX_ADDRESSES = 16
RECORD_TTL = 120
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_SCOPE = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")


def _zeroconf() -> Any:
    try:
        return importlib.import_module("zeroconf")
    except ImportError as exc:
        raise RuntimeError(
            "Nearby discovery requires the nearby extra: pip install 'donedatahoarder[nearby]'."
        ) from exc


def _address(value: str) -> str:
    if not isinstance(value, str) or len(value) > 96:
        raise ValueError("Invalid discovery address.")
    base, marker, scope = value.partition("%")
    parsed = ipaddress.ip_address(base)
    if parsed.is_unspecified or parsed.is_loopback or parsed.is_multicast or parsed.is_reserved:
        raise ValueError("Discovery requires a usable network address.")
    if marker and (parsed.version != 6 or not _SCOPE.fullmatch(scope)):
        raise ValueError("Invalid IPv6 interface scope.")
    if parsed.version == 6 and parsed.is_link_local and not marker:
        raise ValueError("Link-local IPv6 requires an interface scope.")
    return str(parsed) + (f"%{scope}" if marker else "")


def _address_key(value: str) -> tuple[int, bool, str]:
    parsed = ipaddress.ip_address(value.partition("%")[0])
    return parsed.version, parsed.is_link_local, value


@dataclass(frozen=True)
class Candidate:
    """Unverified discovery metadata; ``server_id`` is a claim, never trust."""

    server_id: str
    name: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    protocol: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or len(self.server_id) != 36:
            raise ValueError("Discovery server identity must be a UUID.")
        identity = str(UUID(self.server_id))
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 80:
            raise ValueError("Discovery name must contain 1–80 printable characters.")
        if self.name != self.name.strip() or not self.name.isprintable() or len(self.name.encode("utf-8")) > 160:
            raise ValueError("Invalid discovery display name.")
        if not isinstance(self.hostname, str) or len(self.hostname) > 254:
            raise ValueError("Invalid discovery hostname.")
        hostname = self.hostname.removesuffix(".").lower()
        if not hostname or not all(_HOST_LABEL.fullmatch(label) for label in hostname.split(".")):
            raise ValueError("Invalid discovery hostname.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid discovery port.")
        if type(self.protocol) is not int or self.protocol != 1:
            raise ValueError("Unsupported discovery protocol.")
        if not isinstance(self.addresses, tuple) or not 1 <= len(self.addresses) <= MAX_ADDRESSES:
            raise ValueError("Discovery requires a bounded address list.")
        addresses = tuple(sorted({_address(value) for value in self.addresses}, key=_address_key))
        object.__setattr__(self, "server_id", identity)
        object.__setattr__(self, "hostname", hostname)
        object.__setattr__(self, "addresses", addresses)

    @property
    def endpoints(self) -> tuple[str, ...]:
        """IP endpoints, IPv4 first; TLS verification remains the caller's job."""
        return tuple(
            f"https://[{address.replace('%', '%25')}]:{self.port}" if ":" in address
            else f"https://{address}:{self.port}"
            for address in self.addresses
        )

    @property
    def endpoint(self) -> str:
        return self.endpoints[0]


def parse_service(info: Any) -> Candidate | None:
    """Validate an untrusted Zeroconf ServiceInfo without exposing TXT extras."""
    try:
        if info is None or info.type != SERVICE_TYPE:
            return None
        props = info.properties
        if not isinstance(props, dict) or len(props) > 8:
            return None
        if any(not isinstance(key, bytes) or not isinstance(value, bytes) for key, value in props.items()):
            return None
        if sum(len(key) + len(value) for key, value in props.items()) > 1024:
            return None
        if props.get(b"tls") != b"1" or props.get(b"protocol") != b"1":
            return None
        addresses = info.parsed_scoped_addresses()
        if not 1 <= len(addresses) <= MAX_ADDRESSES:
            return None
        return Candidate(
            server_id=props[b"server_id"].decode("ascii"),
            name=props[b"name"].decode("utf-8"),
            hostname=info.server,
            port=info.port,
            addresses=tuple(addresses),
        )
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        return None


def advertised_addresses(host: str) -> tuple[str, ...]:
    """Addresses for a literal listener; wildcard listeners enumerate adapters.

    IPv6 scope identifiers are local to the advertising machine and are removed
    by ServiceInfo when encoding A/AAAA records. Receiving Zeroconf supplies its
    own interface scope. A wildcard IPv4 listener never advertises IPv6.
    """
    try:
        bind = ipaddress.ip_address(host.partition("%")[0])
    except ValueError as exc:
        raise ValueError("Discoverable listeners require a literal network IP or wildcard address.") from exc
    if not bind.is_unspecified:
        return (_address(host),)
    _zeroconf()
    ifaddr = importlib.import_module("ifaddr")
    result: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for interface in adapter.ips:
            value = interface.ip
            if isinstance(value, tuple):
                value = value[0] + (f"%{value[2]}" if value[2] else "")
            try:
                address = _address(value)
                if ipaddress.ip_address(address.partition("%")[0]).version == bind.version:
                    result.add(address)
            except (TypeError, ValueError):
                continue
    if not result:
        raise ValueError("No usable network interface is available for discovery.")
    return tuple(sorted(result, key=_address_key)[:MAX_ADDRESSES])


class Advertiser:
    """Advertise only after the HTTPS listener is ready; close sends goodbye."""

    def __init__(self, candidate: Candidate, *, backend: Any = None) -> None:
        self.candidate = candidate
        self._backend = backend
        self._zc: Any = None
        self._info: Any = None
        self._lifecycle = threading.RLock()

    def start(self) -> Advertiser:
        with self._lifecycle:
            return self._start()

    def _start(self) -> Advertiser:
        if self._zc is not None:
            return self
        api = self._backend or _zeroconf()
        info = api.ServiceInfo(
            SERVICE_TYPE, f"{self.candidate.server_id}.{SERVICE_TYPE}",
            server=self.candidate.hostname + ".", port=self.candidate.port,
            addresses=[ipaddress.ip_address(address.partition("%")[0]).packed for address in self.candidate.addresses],
            properties={
                "server_id": self.candidate.server_id, "name": self.candidate.name,
                "protocol": "1", "tls": "1",
            },
            host_ttl=RECORD_TTL, other_ttl=RECORD_TTL,
        )
        zc = api.Zeroconf(ip_version=api.IPVersion.All)
        try:
            zc.register_service(info)
        except BaseException:
            zc.close()
            raise
        self._zc, self._info = zc, info
        return self

    def close(self) -> None:
        with self._lifecycle:
            self._close()

    def _close(self) -> None:
        zc, info = self._zc, self._info
        self._zc, self._info = None, None
        if zc is not None:
            try:
                zc.unregister_service(info)
            finally:
                zc.close()

    def __enter__(self) -> Advertiser:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


@dataclass
class _Entry:
    ticket: int
    expires: float
    refresh: float
    candidate: Candidate | None = None


class DiscoveryBrowser:
    """Bounded, background resolution with cheap thread-safe UI snapshots.

    Callbacks only enqueue work. A single resolver handles at most one one-second
    request at a time; updates/removals invalidate in-flight requests. Periodic
    resolution keeps live records fresh even when their TXT values never change.
    """

    def __init__(self, *, backend: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._backend = backend
        self._clock = clock
        self._condition = threading.Condition()
        self._entries: dict[str, _Entry] = {}
        self._ticket = 0
        self._stopped = True
        self._zc: Any = None
        self._browser: Any = None
        self._worker: threading.Thread | None = None
        self._lifecycle = threading.RLock()

    def start(self) -> DiscoveryBrowser:
        with self._lifecycle:
            return self._start()

    def _start(self) -> DiscoveryBrowser:
        if self._zc is not None:
            return self
        api = self._backend or _zeroconf()
        zc = api.Zeroconf(ip_version=api.IPVersion.All)
        self._zc, self._stopped = zc, False
        try:
            self._worker = threading.Thread(target=self._resolve, name="ddh-discovery", daemon=True)
            self._worker.start()
            self._browser = api.ServiceBrowser(zc, SERVICE_TYPE, listener=self)
        except BaseException:
            self.close()
            raise
        return self

    def _expire(self, now: float) -> None:
        for name in [name for name, entry in self._entries.items() if entry.expires <= now]:
            del self._entries[name]

    def _enqueue(self, type_: str, name: str) -> None:
        if type_ != SERVICE_TYPE or not isinstance(name, str) or len(name) > 255 or not name.endswith(SERVICE_TYPE):
            return
        with self._condition:
            if self._stopped:
                return
            now = self._clock()
            self._expire(now)
            previous = self._entries.get(name)
            if previous is None and len(self._entries) >= MAX_CANDIDATES:
                return
            self._ticket += 1
            self._entries[name] = _Entry(
                self._ticket, previous.expires if previous else now + RECORD_TTL,
                now, previous.candidate if previous else None,
            )
            self._condition.notify()

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        self._enqueue(type_, name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        self._enqueue(type_, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        if type_ == SERVICE_TYPE:
            with self._condition:
                self._entries.pop(name, None)
                self._condition.notify()

    def _resolve(self) -> None:
        while True:
            with self._condition:
                if self._stopped:
                    return
                now = self._clock()
                self._expire(now)
                due = [(entry.refresh, name, entry.ticket) for name, entry in self._entries.items() if entry.refresh <= now]
                if not due:
                    self._condition.wait(timeout=1.0)
                    continue
                _, name, ticket = min(due)
                self._entries[name].refresh = now + 30
                zc = self._zc
            info = None
            try:
                info = zc.get_service_info(SERVICE_TYPE, name, timeout=1000)
                candidate = parse_service(info)
            except Exception:
                candidate = None
            with self._condition:
                if self._stopped:
                    return
                entry = self._entries.get(name)
                if entry is not None and entry.ticket == ticket:
                    if candidate is not None:
                        entry.candidate = candidate
                        entry.expires = self._clock() + RECORD_TTL
                    elif info is not None:
                        # A changed invalid announcement must not retain an old
                        # endpoint while the UI presents it as currently valid.
                        self._entries.pop(name, None)

    def snapshot(self) -> list[Candidate]:
        with self._condition:
            self._expire(self._clock())
            candidates = [entry.candidate for entry in self._entries.values() if entry.candidate is not None]
        merged: dict[str, Candidate] = {}
        for candidate in candidates:
            previous = merged.get(candidate.server_id)
            if previous is None:
                merged[candidate.server_id] = candidate
            elif (previous.hostname, previous.port, previous.name) == (candidate.hostname, candidate.port, candidate.name):
                addresses = tuple(sorted(set(previous.addresses + candidate.addresses), key=_address_key)[:MAX_ADDRESSES])
                merged[candidate.server_id] = replace(previous, addresses=addresses)
            # Conflicting claims to the same ID are not merged into trusted data.
            # Selection and TLS pairing must authenticate whichever endpoint wins.
        return sorted(merged.values(), key=lambda item: (item.name.casefold(), item.server_id))[:MAX_CANDIDATES]

    def close(self) -> None:
        with self._lifecycle:
            self._close()

    def _close(self) -> None:
        with self._condition:
            self._stopped = True
            self._entries.clear()
            self._condition.notify_all()
        browser, zc, worker = self._browser, self._zc, self._worker
        self._browser, self._zc, self._worker = None, None, None
        try:
            if browser is not None:
                browser.cancel()
        finally:
            try:
                if zc is not None:
                    zc.close()
            finally:
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=2)

    def __enter__(self) -> DiscoveryBrowser:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()

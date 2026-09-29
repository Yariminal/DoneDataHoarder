"""Discovery never trusts advertisements or performs work on the UI thread."""
from dataclasses import FrozenInstanceError
import ipaddress
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from donedatahoarder.remote import discovery
from donedatahoarder.remote.discovery import (
    Advertiser, Candidate, DiscoveryBrowser, MAX_CANDIDATES,
    SERVICE_TYPE, advertised_addresses, parse_service,
)


def candidate(**kwargs):
    fields = dict(server_id="665d8a36-1ef5-4831-9bfc-0c8ee8ab08e0", name="HOME-PC",
                  hostname="home-pc.local.", port=8765, addresses=("192.168.1.4",))
    fields.update(kwargs)
    return Candidate(**fields)


def service(value=None, **kwargs):
    value = value or candidate()
    fields = dict(type=SERVICE_TYPE, server=value.hostname, port=value.port,
                  properties={b"server_id": value.server_id.encode(), b"name": value.name.encode(),
                              b"protocol": b"1", b"tls": b"1"},
                  parsed_scoped_addresses=lambda: list(value.addresses))
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class FakeZeroconf:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.services = {}
        self.registered = []
        self.unregistered = []
        self.closed = 0
        self.lookup = None
        self.calls = []

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed += 1

    def get_service_info(self, type_, name, timeout):
        self.calls.append((type_, name, timeout, threading.get_ident()))
        if self.lookup:
            return self.lookup(name)
        return self.services.get(name)


class FakeBackend:
    IPVersion = SimpleNamespace(All="all")

    def __init__(self):
        self.instances = []
        self.browsers = []

    def Zeroconf(self, **kwargs):
        zc = FakeZeroconf(**kwargs)
        self.instances.append(zc)
        return zc

    def ServiceInfo(self, type_, name, **kwargs):
        return SimpleNamespace(type=type_, name=name, **kwargs)

    def ServiceBrowser(self, zc, type_, listener):
        browser = SimpleNamespace(zc=zc, type=type_, listener=listener, cancelled=False)
        browser.cancel = lambda: setattr(browser, "cancelled", True)
        self.browsers.append(browser)
        return browser


def eventually(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "Background discovery did not settle"
        time.sleep(0.005)


def test_candidate_is_immutable_and_endpoint_brackets_ipv6():
    result = candidate(addresses=("fd01::8", "192.168.1.4", "192.168.1.4"))
    assert result.hostname == "home-pc.local"
    assert result.addresses == ("192.168.1.4", "fd01::8")
    assert result.endpoint == "https://192.168.1.4:8765"
    assert result.endpoints == ("https://192.168.1.4:8765", "https://[fd01::8]:8765")
    assert candidate(addresses=("fe80::1%7",)).endpoint == "https://[fe80::1%257]:8765"
    with pytest.raises(FrozenInstanceError):
        result.name = "changed"


@pytest.mark.parametrize("changes", [
    {"server_id": "not-a-uuid"}, {"name": ""}, {"name": "\x1b[31mdevice"},
    {"name": "x" * 81}, {"name": " ending "}, {"name": "bidi\u202edevice"},
    {"hostname": "host/steal"}, {"hostname": "user@host"}, {"hostname": "bad..host"}, {"hostname": "bad.."},
    {"hostname": "a" * 64 + ".local"}, {"port": 0}, {"port": 65536}, {"port": True},
    {"protocol": 2}, {"addresses": ()}, {"addresses": ("127.0.0.1",)},
    {"addresses": ("::1",)}, {"addresses": ("0.0.0.0",)},
    {"addresses": ("224.0.0.251",)}, {"addresses": ("fe80::1",)},
    {"addresses": ("fe80::1%2/steal",)}, {"addresses": ("192.168.1.3%4",)},
    {"addresses": ("192.168.1.3",) * 17},
])
def test_reject_malformed_or_non_network_candidates(changes):
    with pytest.raises(ValueError):
        candidate(**changes)


def test_parse_service_strict_protocol_tls_and_bounded_txt():
    expected = candidate()
    assert parse_service(service(expected)) == expected
    for changes in ({b"tls": b"0"}, {b"protocol": b"2"}, {b"name": b"\xff"},
                    {b"server_id": b"fake"}, {b"extra": b"x" * 1024}, {b"name": None}):
        info = service(expected)
        info.properties.update(changes)
        assert parse_service(info) is None
    assert parse_service(service(type="_http._tcp.local.")) is None
    assert parse_service(service(properties={})) is None
    assert parse_service(None) is None
    assert parse_service(service(parsed_scoped_addresses=lambda: ["192.168.1.4"] * 17)) is None


def test_lifecycle_is_lazy_idempotent_and_advertises_no_sensitive_data():
    backend = FakeBackend()
    publisher = Advertiser(candidate(), backend=backend)
    browser = DiscoveryBrowser(backend=backend)
    assert not backend.instances
    assert browser.snapshot() == []
    with publisher as active:
        assert active.start() is publisher
        zc = backend.instances[0]
        info = zc.registered[0]
        assert info.name == candidate().server_id + "." + SERVICE_TYPE
        assert info.properties == {"server_id": candidate().server_id, "name": "HOME-PC", "protocol": "1", "tls": "1"}
        assert info.addresses == [ipaddress.ip_address("192.168.1.4").packed]
        assert info.server == "home-pc.local."
    publisher.close()
    assert zc.unregistered == [info]
    assert zc.closed == 1
    with browser:
        browser.start()
        assert len(backend.browsers) == 1
    browser.close()
    assert backend.browsers[0].cancelled
    assert backend.instances[1].closed == 1


def test_add_update_remove_resolve_off_caller_thread():
    backend = FakeBackend()
    with DiscoveryBrowser(backend=backend) as browser:
        zc = backend.instances[0]
        name = candidate().server_id + "." + SERVICE_TYPE
        zc.services[name] = service()
        browser.add_service(zc, SERVICE_TYPE, name)
        eventually(lambda: browser.snapshot() == [candidate()])
        assert zc.calls[0][2] == 1000
        assert zc.calls[0][3] != threading.get_ident()
        changed = candidate(addresses=("192.168.1.99",))
        zc.services[name] = service(changed)
        browser.update_service(zc, SERVICE_TYPE, name)
        eventually(lambda: browser.snapshot() == [changed])
        browser.remove_service(zc, SERVICE_TYPE, name)
        assert browser.snapshot() == []


def test_removal_invalidates_in_flight_resolution():
    backend = FakeBackend()
    begun, release = threading.Event(), threading.Event()
    with DiscoveryBrowser(backend=backend) as browser:
        zc = backend.instances[0]
        def lookup(name):
            begun.set()
            assert release.wait(2)
            return service()
        zc.lookup = lookup
        name = "pending." + SERVICE_TYPE
        browser.add_service(zc, SERVICE_TYPE, name)
        assert begun.wait(2)
        browser.remove_service(zc, SERVICE_TYPE, name)
        release.set()
        eventually(lambda: not browser._entries)
    assert browser.snapshot() == []


def test_deduplicate_interfaces_keep_same_names_separate_and_expire():
    backend = FakeBackend()
    now = [0.0]
    with DiscoveryBrowser(backend=backend, clock=lambda: now[0]) as browser:
        zc = backend.instances[0]
        values = [candidate(), candidate(addresses=("fd01::2",)), candidate(server_id=str(uuid4()))]
        for index, value in enumerate(values):
            name = f"interface{index}." + SERVICE_TYPE
            zc.services[name] = service(value)
            browser.add_service(zc, SERVICE_TYPE, name)
        eventually(lambda: len(browser.snapshot()) == 2 and any(len(c.addresses) == 2 for c in browser.snapshot()))
        assert {c.name for c in browser.snapshot()} == {"HOME-PC"}
        now[0] = 121
        assert browser.snapshot() == []


def test_unchanged_records_refresh_and_invalid_update_is_removed():
    backend = FakeBackend()
    now = [0.0]
    with DiscoveryBrowser(backend=backend, clock=lambda: now[0]) as browser:
        zc = backend.instances[0]
        name = "test." + SERVICE_TYPE
        zc.services[name] = service()
        browser.add_service(zc, SERVICE_TYPE, name)
        eventually(lambda: bool(browser.snapshot()))
        now[0] = 31
        with browser._condition:
            browser._condition.notify()
        eventually(lambda: len(zc.calls) == 2)
        now[0] = 130
        assert browser.snapshot() == [candidate()]
        zc.services[name] = service(properties={b"tls": b"0"})
        browser.update_service(zc, SERVICE_TYPE, name)
        eventually(lambda: browser.snapshot() == [])


def test_events_are_bounded_even_before_resolution():
    browser = DiscoveryBrowser(backend=FakeBackend())
    browser._stopped = False
    for index in range(1000):
        browser.add_service(None, SERVICE_TYPE, f"{index}." + SERVICE_TYPE)
    assert len(browser._entries) == MAX_CANDIDATES
    assert browser.snapshot() == []
    browser.close()
    browser.add_service(None, SERVICE_TYPE, "late." + SERVICE_TYPE)
    assert not browser._entries


def test_resolution_failure_does_not_kill_browser():
    backend = FakeBackend()
    with DiscoveryBrowser(backend=backend) as browser:
        zc = backend.instances[0]
        zc.lookup = lambda name: (_ for _ in ()).throw(OSError("interface vanished"))
        browser.add_service(zc, SERVICE_TYPE, "failed." + SERVICE_TYPE)
        eventually(lambda: len(zc.calls) == 1)
        zc.lookup = None
        name = "good." + SERVICE_TYPE
        zc.services[name] = service()
        browser.add_service(zc, SERVICE_TYPE, name)
        eventually(lambda: browser.snapshot() == [candidate()])


def test_start_failure_closes_zeroconf(monkeypatch):
    backend = FakeBackend()
    def failure(*args, **kwargs):
        raise OSError("network unavailable")
    monkeypatch.setattr(backend, "ServiceBrowser", failure)
    with pytest.raises(OSError):
        DiscoveryBrowser(backend=backend).start()
    assert backend.instances[0].closed == 1
    backend = FakeBackend()
    original = backend.Zeroconf
    def bad_registration(**kwargs):
        zc = original(**kwargs)
        zc.register_service = failure
        return zc
    monkeypatch.setattr(backend, "Zeroconf", bad_registration)
    with pytest.raises(OSError):
        Advertiser(candidate(), backend=backend).start()
    assert backend.instances[0].closed == 1


@pytest.mark.parametrize("kind", ["browser", "advertiser"])
def test_close_during_startup_waits_and_closes_new_socket(monkeypatch, kind):
    backend = FakeBackend()
    begun, release, closing = threading.Event(), threading.Event(), threading.Event()
    original = backend.Zeroconf
    def delayed_start(**kwargs):
        begun.set()
        assert release.wait(2)
        return original(**kwargs)
    monkeypatch.setattr(backend, "Zeroconf", delayed_start)
    owner = DiscoveryBrowser(backend=backend) if kind == "browser" else Advertiser(candidate(), backend=backend)
    starter = threading.Thread(target=owner.start)
    def close():
        closing.set()
        owner.close()
    closer = threading.Thread(target=close)
    try:
        starter.start()
        assert begun.wait(2)
        closer.start()
        assert closing.wait(2)
        release.set()
        starter.join(3)
        closer.join(3)
        assert not starter.is_alive() and not closer.is_alive()
        assert backend.instances[0].closed == 1
        if kind == "browser":
            assert backend.browsers[0].cancelled
            assert owner.snapshot() == []
        else:
            assert len(backend.instances[0].unregistered) == 1
    finally:
        release.set()
        starter.join(3)
        if closer.ident is not None:
            closer.join(3)


def test_missing_extra_explains_install_without_opening_network(monkeypatch):
    def unavailable(name):
        raise ImportError(name)
    monkeypatch.setattr(discovery.importlib, "import_module", unavailable)
    browser = DiscoveryBrowser()
    assert browser.snapshot() == []
    with pytest.raises(RuntimeError, match=r"donedatahoarder\[nearby\]"):
        browser.start()


def test_wildcard_enumeration_filters_loopback_and_wrong_family(monkeypatch):
    adapters = [SimpleNamespace(ips=[SimpleNamespace(ip=value) for value in (
        "127.0.0.1", "192.168.1.4", "192.168.1.4", "0.0.0.0", "224.0.0.251",
        ("::1", 0, 0), ("fd01::1", 0, 0), ("fe80::1", 0, 7),
    )])]
    monkeypatch.setattr(discovery, "_zeroconf", lambda: None)
    monkeypatch.setattr(discovery.importlib, "import_module", lambda name: SimpleNamespace(get_adapters=lambda: adapters))
    assert advertised_addresses("0.0.0.0") == ("192.168.1.4",)
    assert advertised_addresses("::") == ("fd01::1", "fe80::1%7")
    assert advertised_addresses("192.168.1.7") == ("192.168.1.7",)
    with pytest.raises(ValueError, match="literal"):
        advertised_addresses("home-pc.local")
    with pytest.raises(ValueError, match="usable"):
        advertised_addresses("127.0.0.1")

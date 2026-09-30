"""Nearby orchestration never turns discovery metadata into connection trust."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("httpx")

from donedatahoarder.remote import nearby, profiles
from donedatahoarder.remote.client import RemoteError
from donedatahoarder.remote.discovery import Candidate
from donedatahoarder.remote.pairing import PairingStore, ensure_tls
from donedatahoarder.remote.profiles import SavedProfile


SERVER = "e7739d6f-dcc6-40ad-aa3c-5c60e0c3f860"


def candidate(**changes):
    values = dict(server_id=SERVER, name="Unverified advertised name",
                  hostname=f"ddh-{SERVER}.local", port=8765,
                  addresses=("192.168.1.15", "10.0.0.2"))
    values.update(changes)
    return Candidate(**values)


class Browser:
    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.starts = 0
        self.closed = False

    def start(self):
        self.starts += 1
        if self.error:
            raise self.error

    def snapshot(self):
        return list(self.items)

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, url, *, error=None, events=None):
        self.url = url
        self.name = "Verified HOME-PC"
        self.error = error
        self.events = events if events is not None else []
        self.closed = False
        self.connected = False

    def connect(self):
        self.events.append("connect")
        if self.error:
            raise self.error
        self.connected = True

    def close(self):
        self.closed = True


class Store:
    def __init__(self, saved=(), *, error=None, save_error=None, events=None):
        self.saved = {profile.server_id: profile for profile in saved}
        self.calls = []
        self.connections = []
        self.error, self.save_error = error, save_error
        self.events = events if events is not None else []

    def list(self):
        return list(self.saved.values())

    def load(self, server_id):
        return self.saved.get(server_id)

    def connection(self, profile, *, url, resolver):
        self.calls.append(dict(profile=profile, url=url, resolver=resolver))
        connection = Connection(url, error=self.error, events=self.events)
        self.connections.append(connection)
        return connection

    def save(self, profile):
        self.events.append("save")
        if self.save_error:
            raise self.save_error
        self.saved[profile.server_id] = profile


@pytest.fixture(scope="module")
def identity(tmp_path_factory):
    directory = tmp_path_factory.mktemp("nearby-manager-identity")
    cert, _, hostname = ensure_tls(directory / "tls", SERVER)
    store = PairingStore(directory / "devices.db", SERVER)
    pem = cert.read_text(encoding="ascii")
    value = store.create_invitation(pem, hostname)
    profile = SavedProfile(server_id=SERVER, device_id=str(uuid4()), name="Previously verified HOME-PC",
                           token="a" * 64, certificate_pem=pem, hostname=hostname,
                           last_url="https://192.168.1.5:8765")
    return value, profile


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected network attempt")


def test_invitation_identity_mismatch_is_rejected_before_preflight_or_redemption(identity, monkeypatch):
    invitation, _ = identity
    browser = Browser([candidate()])
    manager = nearby.NearbyManager(store=Store(), browser=browser)
    monkeypatch.setattr(nearby, "pair_device", forbidden)
    monkeypatch.setattr(profiles, "select_pairing_endpoint", forbidden)
    with pytest.raises(RemoteError, match="different workstation"):
        manager.connect(server_id=str(uuid4()), invitation=invitation)
    assert manager.store.calls == []


def test_all_candidate_addresses_are_preflighted_before_one_redemption(identity, monkeypatch):
    invitation, profile = identity
    discovered = candidate()
    events, probes, redemptions = [], [], []
    store = Store(events=events)
    manager = nearby.NearbyManager(store=store, browser=Browser([discovered]))

    def preflight(endpoints, supplied_invitation):
        events.append("preflight")
        probes.append((endpoints, supplied_invitation))
        # The advertised first adapter is unavailable; use the second address.
        return endpoints[1]

    def redeem(url, supplied_invitation, device_name, **options):
        events.append("redeem")
        redemptions.append((url, supplied_invitation, device_name, options))
        return replace(profile, last_url=url, auto_reconnect=options["auto_reconnect"])

    monkeypatch.setattr(profiles, "select_pairing_endpoint", preflight)
    monkeypatch.setattr(nearby, "pair_device", redeem)
    connection = manager.connect(server_id=SERVER, invitation=invitation,
                                 device_name="Omarchy laptop", auto_reconnect=True)
    assert probes == [(discovered.endpoints, invitation)]
    assert len(redemptions) == 1
    assert redemptions[0][0:3] == (discovered.endpoints[1], invitation, "Omarchy laptop")
    assert redemptions[0][3]["store"] is store
    assert connection.url == discovered.endpoints[1]
    assert events == ["preflight", "redeem", "connect", "save"]
    assert store.saved[SERVER].name == "Verified HOME-PC"
    assert store.saved[SERVER].name != discovered.name
    assert store.saved[SERVER].auto_reconnect is True


def test_failed_preflight_never_consumes_invitation(identity, monkeypatch):
    invitation, _ = identity
    manager = nearby.NearbyManager(store=Store(), browser=Browser([candidate()]))

    def unavailable(*args, **kwargs):
        raise RemoteError("No address verified")

    monkeypatch.setattr(profiles, "select_pairing_endpoint", unavailable)
    monkeypatch.setattr(nearby, "pair_device", forbidden)
    with pytest.raises(RemoteError, match="No address verified"):
        manager.connect(server_id=SERVER, invitation=invitation)
    assert manager.store.connections == []


def test_failed_redemption_is_not_retried_at_another_advertised_address(identity, monkeypatch):
    invitation, _ = identity
    discovered = candidate()
    manager = nearby.NearbyManager(store=Store(), browser=Browser([discovered]))
    redemptions = []
    monkeypatch.setattr(profiles, "select_pairing_endpoint", lambda urls, value: urls[0])

    def ambiguous_failure(url, *args, **kwargs):
        redemptions.append(url)
        raise RemoteError("Reply was lost; create a fresh invitation")

    monkeypatch.setattr(nearby, "pair_device", ambiguous_failure)
    with pytest.raises(RemoteError, match="Reply was lost"):
        manager.connect(server_id=SERVER, invitation=invitation)
    assert redemptions == [discovered.endpoints[0]]
    assert manager.store.saved == {}


def test_choices_use_saved_identity_name_and_preserve_offline_profiles(identity):
    _, profile = identity
    offline = replace(profile, server_id=str(uuid4()), name="Other verified workstation", auto_reconnect=True)
    new = candidate(server_id=str(uuid4()), name="Another nearby workstation")
    manager = nearby.NearbyManager(store=Store([profile, offline]), browser=Browser([candidate(), new]))
    items = {item["server_id"]: item for item in manager.choices()}
    assert items[SERVER]["name"] == profile.name
    assert items[SERVER]["saved"] and items[SERVER]["nearby"]
    assert items[offline.server_id] == {
        "server_id": offline.server_id, "name": offline.name, "saved": True, "nearby": False,
        "auto_reconnect": True, "url": offline.last_url,
    }
    assert items[new.server_id]["name"] == new.name
    assert not items[new.server_id]["saved"]


@pytest.mark.parametrize("manual_url", ["", "https://192.168.1.40:8765"])
def test_saved_offline_profiles_connect_using_manual_or_last_verified_address(identity, monkeypatch, manual_url):
    _, profile = identity
    store = Store([profile])
    manager = nearby.NearbyManager(store=store, browser=Browser())
    monkeypatch.setattr(profiles, "choose_verified_endpoint", forbidden)
    monkeypatch.setattr(nearby, "pair_device", forbidden)
    assert manager.choices()[0]["nearby"] is False
    connection = manager.connect(server_id=SERVER, url=manual_url)
    assert connection.url == (manual_url or profile.last_url)
    assert connection.connected
    assert store.calls[0]["resolver"] is None


def test_saved_reconnect_uses_pinned_trust_and_resolves_all_current_addresses(identity, monkeypatch):
    _, profile = identity
    discovered = candidate()
    browser = Browser([discovered])
    store = Store([profile])
    probes = []

    def choose(endpoints, certificate_pem, hostname):
        probes.append((endpoints, certificate_pem, hostname))
        return endpoints[-1]

    monkeypatch.setattr(profiles, "choose_verified_endpoint", choose)
    manager = nearby.NearbyManager(store=store, browser=browser)
    connection = manager.connect(server_id=SERVER, auto_reconnect=True)
    assert probes == [(discovered.endpoints, profile.certificate_pem, profile.hostname)]
    assert connection.url == discovered.endpoints[-1]
    resolver = store.calls[0]["resolver"]
    assert resolver() == discovered.endpoints
    updated = candidate(addresses=("192.168.1.35", "10.0.0.4"))
    browser.items = [candidate(server_id=str(uuid4())), updated]
    assert resolver() == updated.endpoints
    browser.items = []
    assert resolver() is None


def test_auto_reconnect_off_omits_resolver_and_updates_saved_preference(identity, monkeypatch):
    _, original = identity
    profile = replace(original, auto_reconnect=True)
    store = Store([profile])
    manager = nearby.NearbyManager(store=store, browser=Browser([candidate()]))
    monkeypatch.setattr(profiles, "choose_verified_endpoint", lambda endpoints, *trust: endpoints[0])
    manager.connect(server_id=SERVER, auto_reconnect=False)
    assert store.calls[0]["resolver"] is None
    assert store.saved[SERVER].auto_reconnect is False


@pytest.mark.parametrize("during_save", [False, True])
def test_failed_connection_or_profile_save_closes_transport_and_preserves_profile(identity, during_save):
    _, profile = identity
    error = RemoteError("Endpoint did not verify")
    store = Store([profile], error=None if during_save else error,
                  save_error=error if during_save else None)
    manager = nearby.NearbyManager(store=store, browser=Browser())
    with pytest.raises(RemoteError, match="did not verify"):
        manager.connect(server_id=SERVER, url="https://192.168.1.90:8765", auto_reconnect=True)
    assert store.connections[0].closed
    assert store.saved[SERVER] == profile


def test_browser_startup_failure_leaves_manual_pairing_usable(identity, monkeypatch):
    invitation, profile = identity
    browser = Browser(error=OSError("multicast unavailable"))
    store = Store()
    manager = nearby.NearbyManager(store=store, browser=browser)
    manager.start()
    manager.start()
    assert browser.starts == 1
    assert "discovery unavailable" in manager.error
    assert manager.choices() == []
    monkeypatch.setattr(profiles, "select_pairing_endpoint", forbidden)
    redemptions = []

    def redeem(url, *args, **kwargs):
        redemptions.append(url)
        return replace(profile, last_url=url)

    monkeypatch.setattr(nearby, "pair_device", redeem)
    connection = manager.connect(invitation=invitation, url="https://192.168.1.90:8765")
    assert connection.connected
    assert redemptions == ["https://192.168.1.90:8765"]
    manager.close()
    assert browser.closed


def test_unpaired_selection_without_invitation_never_attempts_connection(monkeypatch):
    manager = nearby.NearbyManager(store=Store(), browser=Browser([candidate()]))
    monkeypatch.setattr(profiles, "choose_verified_endpoint", forbidden)
    monkeypatch.setattr(nearby, "pair_device", forbidden)
    with pytest.raises(RemoteError, match="Paste the invitation"):
        manager.connect(server_id=SERVER)
    assert not manager.store.connections
def test_manager_shutdown_cleans_browser_started_in_background():
    import threading
    from types import SimpleNamespace
    from donedatahoarder.remote.nearby import NearbyManager
    begun, release, closed = threading.Event(), threading.Event(), threading.Event()
    def start():
        begun.set()
        assert release.wait(3)
    browser = SimpleNamespace(start=start, close=closed.set)
    manager = NearbyManager(store=object(), browser=browser)
    starter = threading.Thread(target=manager.start)
    stopper = threading.Thread(target=manager.close)
    try:
        starter.start()
        assert begun.wait(2)
        stopper.start()
        assert not closed.wait(0.05)
    finally:
        release.set()
        starter.join(3)
        if stopper.ident is not None:
            stopper.join(3)
    assert closed.is_set()
    assert not starter.is_alive() and not stopper.is_alive()


def test_manager_does_not_start_after_shutdown():
    from types import SimpleNamespace
    from donedatahoarder.remote.nearby import NearbyManager
    calls = []
    manager = NearbyManager(store=object(), browser=SimpleNamespace(
        start=lambda: calls.append("start"), close=lambda: calls.append("close")))
    manager.close()
    manager.start()
    assert calls == ["close"]

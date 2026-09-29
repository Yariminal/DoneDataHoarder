"""Nearby device selection remains responsive and keeps credentials private."""
import asyncio
from dataclasses import dataclass
import threading
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Select, Static

from donedatahoarder.remote import nearby
from donedatahoarder.remote.discovery import Candidate
from donedatahoarder.tui.app import DDHApp
from donedatahoarder.tui.connection_screens import ConnectionScreen
from donedatahoarder.tui.discovery_screens import NearbyScreen
from donedatahoarder.tui.images import ImageCapabilities


SERVER = "cc5c83a7-5547-4bdf-a786-4bc7d9f42eb9"


def choice(**changes):
    value = dict(server_id=SERVER, name="HOME-PC [red]", saved=True, nearby=True,
                 auto_reconnect=False, url="https://192.168.1.10:8765")
    value.update(changes)
    return value


class Connection:
    def __init__(self, error=None):
        self.closed = False
        self.error = error
        self.url = "https://192.168.1.10:8765"
        self.name = "Verified HOME-PC"
        self.connected = False

    def connect(self):
        if self.error:
            raise self.error
        self.connected = True

    def close(self):
        self.closed = True


class Manager:
    error = ""

    def __init__(self, choices=None):
        self.items = choices or []
        self.calls = []
        self.connection = Connection()
        self.store = SimpleNamespace(forget=lambda value: self.calls.append(("forget", value)))
        self.closed = False

    def start(self):
        self.calls.append(("start", threading.get_ident()))

    def choices(self):
        return self.items

    def connect(self, **kwargs):
        self.calls.append(("connect", kwargs))
        return self.connection

    def close(self):
        self.closed = True


class PickerApp(App):
    def __init__(self, manager, **screen_options):
        super().__init__()
        self.picker = NearbyScreen(manager, **screen_options)
        self.result = "waiting"

    def on_mount(self):
        self.push_screen(self.picker, lambda value: setattr(self, "result", value))


def test_picker_discovery_runs_off_ui_thread_and_manual_fallback_stays_usable():
    async def scenario():
        manager = Manager()
        begun, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def slow_start():
            assert threading.get_ident() != main_thread
            begun.set()
            assert release.wait(5)
            manager.error = "Discovery unavailable; enter an HTTPS address."
        manager.start = slow_start
        app = PickerApp(manager)
        try:
            async with app.run_test(size=(120, 42)) as pilot:
                for _ in range(20):
                    if begun.is_set():
                        break
                    await pilot.pause(0.01)
                assert begun.is_set()
                manual = app.picker.query_one("#nearby-url", Input)
                manual.focus()
                await pilot.press("h", "t", "t", "p", "s")
                assert manual.value == "https"
                release.set()
                await pilot.pause(0.1)
                assert "No nearby" in app.picker.query_one("#nearby-devices", Select).prompt
                assert "Discovery unavailable" in str(app.picker.query_one("#nearby-message", Static).render())
        finally:
            release.set()
    asyncio.run(scenario())


def test_pairing_error_clears_invitation_and_never_reflects_it():
    async def scenario():
        manager = Manager()
        secret = "ddh-invitation-private-test-value"
        def fail(**values):
            assert values["invitation"] == secret
            assert values["server_id"] is None
            raise ValueError("Unexpected transport input: " + values["invitation"])
        manager.connect = fail
        app = PickerApp(manager)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.1)
            app.picker.query_one("#nearby-invitation", Input).value = secret
            app.picker.query_one("#nearby-url", Input).value = "https://192.168.1.10:8765"
            app.picker.connect_selected()
            await pilot.pause(0.15)
            assert app.picker.query_one("#nearby-invitation", Input).value == ""
            assert secret not in str(app.picker.query_one("#nearby-message", Static).render())
            assert not app.picker.query_one("#nearby-connect", Button).disabled
    asyncio.run(scenario())


def test_saved_auto_reconnect_is_opt_in_and_preserves_preference():
    async def scenario():
        manager = Manager([choice(auto_reconnect=True)])
        app = PickerApp(manager, initial=True)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.2)
            assert app.result is manager.connection
            assert [call[1] for call in manager.calls if call[0] == "connect"] == [
                dict(server_id=SERVER, url="", invitation="",
                     device_name=socket.gethostname(),
                     auto_reconnect=True)
            ]
        manager = Manager([choice(auto_reconnect=False)])
        app = PickerApp(manager, initial=True)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.1)
            assert app.result == "waiting"
            app.picker.query_one("#nearby-devices", Select).value = SERVER
            await pilot.pause()
            assert not app.picker.query_one("#nearby-auto", Checkbox).value
            assert "[red]" in str(app.picker.query_one("#nearby-details", Static).render())
    asyncio.run(scenario())


def test_closing_picker_closes_late_successful_connection():
    async def scenario():
        manager = Manager([choice()])
        begun, release = threading.Event(), threading.Event()
        def slow_connect(**values):
            begun.set()
            assert release.wait(5)
            return manager.connection
        manager.connect = slow_connect
        app = PickerApp(manager)
        try:
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause(0.1)
                app.picker.query_one("#nearby-devices", Select).value = SERVER
                app.picker.connect_selected()
                assert await asyncio.wait_for(asyncio.to_thread(begun.wait, 2), 3)
                app.picker.action_close()
                await pilot.pause()
                release.set()
                await pilot.pause(0.15)
                assert manager.connection.closed
                assert app.result is None
        finally:
            release.set()
    asyncio.run(scenario())


def test_nearby_preserves_main_workspace_and_f2_navigation():
    async def scenario():
        manager = Manager([choice()])
        data = {"session": {"id": "session", "root_path": r"E:\Photos"},
                "files": [], "proposals": [], "collections": [], "history": [],
                "duplicates": [], "counts": {}, "page": {}}
        connection = SimpleNamespace(state="connected", name="HOME-PC", pending_request_id=None,
                                     server_id=SERVER, latency_ms=2, url="https://home", error=None)
        service = SimpleNamespace(is_remote=True, path_flavor="windows", session_id="session",
                                  connection=connection, snapshot=lambda **kwargs: data)
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Test"), nearby_manager=manager)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.1)
            assert "HOME-PC" in str(app.query_one("#connection", Button).label)
            await pilot.press("f2")
            assert isinstance(app.screen, ConnectionScreen)
            await pilot.click("#connection-nearby")
            await pilot.pause(0.1)
            assert isinstance(app.screen, NearbyScreen)
            await app.screen.refresh_devices().wait()
            app.screen.query_one("#nearby-devices", Select).value = SERVER
            await pilot.pause()
            assert app.screen.query_one("#nearby-forget", Button).disabled
            assert app.screen.query_one("#nearby-connect", Button).disabled
            await pilot.press("escape")
            await pilot.pause()
            assert app.service is service
            assert "HOME-PC" in str(app.query_one("#connection", Button).label)
        assert manager.closed
    asyncio.run(scenario())


@dataclass
class Profile:
    server_id: str = SERVER
    name: str = "Trusted HOME-PC"
    last_url: str = "https://192.168.1.2:8765"
    auto_reconnect: bool = False
    certificate_pem: str = "fake certificate"
    hostname: str = "home.local"


class Store:
    def __init__(self, profiles=()):
        self.profiles = {value.server_id: value for value in profiles}
        self.saved = []
        self.connection_args = None
        self.result = Connection()

    def list(self):
        return list(self.profiles.values())

    def load(self, server_id):
        return self.profiles.get(server_id)

    def save(self, profile):
        self.saved.append(profile)

    def connection(self, profile, **kwargs):
        self.connection_args = (profile, kwargs)
        return self.result


def test_manager_uses_saved_trust_and_saves_preferences_after_success(monkeypatch):
    from donedatahoarder.remote import profiles
    monkeypatch.setattr(profiles, "choose_verified_endpoint", lambda endpoints, *args: endpoints[0])
    value = Candidate(SERVER, "Untrusted broadcast name", "home.local", 8765, ("192.168.1.10",))
    items = [value]
    browser = SimpleNamespace(snapshot=lambda: items)
    store = Store([Profile()])
    manager = nearby.NearbyManager(store=store, browser=browser)
    assert manager.choices()[0]["name"] == "Trusted HOME-PC"
    assert manager.connect(server_id=SERVER, auto_reconnect=True) is store.result
    assert store.saved[0].auto_reconnect is True
    assert store.saved[0].last_url == store.result.url
    profile, args = store.connection_args
    assert profile.name == "Trusted HOME-PC"
    assert args["url"] == value.endpoint
    items[:] = [Candidate(SERVER, "New name", "home.local", 8765, ("192.168.1.15",))]
    assert args["resolver"]() == ("https://192.168.1.15:8765",)


def test_manager_failure_closes_connection_and_does_not_save_preference():
    store = Store([Profile()])
    store.result = Connection(ValueError("Identity mismatch"))
    manager = nearby.NearbyManager(store=store, browser=SimpleNamespace(snapshot=lambda: []))
    with pytest.raises(ValueError, match="Identity mismatch"):
        manager.connect(server_id=SERVER, auto_reconnect=True)
    assert store.result.closed
    assert store.saved == []


def test_manager_never_pairs_to_different_selected_identity(monkeypatch):
    from donedatahoarder.remote import pairing
    monkeypatch.setattr(pairing, "parse_invitation", lambda text: {"server_id": SERVER})
    monkeypatch.setattr(nearby, "pair_device", lambda *args, **kwargs: pytest.fail("Wrong workstation paired"))
    manager = nearby.NearbyManager(store=Store(), browser=SimpleNamespace(snapshot=lambda: []))
    with pytest.raises(ValueError, match="different workstation"):
        manager.connect(server_id="another-server", invitation="private", url="https://192.168.1.4")

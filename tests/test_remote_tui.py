"""The remote adapter changes execution ownership, not the terminal workflow."""
import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")
from textual.widgets import Button, Input, Static, Tree
from donedatahoarder.tui.app import DDHApp, EditScreen, ImageScreen
from donedatahoarder.tui.connection_screens import ConnectionScreen
from donedatahoarder.tui.images import ImageCapabilities


class RemoteWorkspace:
    is_remote = True
    path_flavor = "windows"
    session_id = "home-session"

    def __init__(self):
        self.calls = []
        self.fail = False
        self.connection = SimpleNamespace(state="connected", name="HOME-PC", pending_request_id=None,
                                          latency_ms=8, url="https://home-pc:8765", error=None, generation=1)
        self.data = {
            "session": {"id": self.session_id, "root_path": r"D:\Photos", "model": "gemma3:12b", "workers": 1},
            "files": [{"id": 1, "path": r"D:\Photos\Trip\photo.png", "filename": "photo.png", "mime_type": "image/png"},
                      {"id": 2, "path": r"D:\Photos\Trip\copy.png", "filename": "copy.png", "mime_type": "image/png"}],
            "proposals": [{"id": 7, "file_id": 1, "proposed_path": r"D:\Photos\Trip\sunset.png", "proposal_type": "rename", "status": "pending"}],
            "collections": [], "history": [], "duplicates": [], "counts": {"files": 2}, "page": {},
        }

    def snapshot(self, **kwargs):
        if self.fail:
            self.connection.state = "reconnecting"
            raise ValueError("Connection lost")
        return copy.deepcopy(self.data)

    def start_pipeline(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"started": 1}

    def update_settings(self, **kwargs):
        self.calls.append(("settings", kwargs))
        self.data["session"].update(kwargs)
        return self.data["session"]

    def cancel_pipeline(self):
        pytest.fail("Remote disconnect must never cancel the workstation pipeline")


def test_remote_paths_settings_and_image_sources_stay_on_workstation():
    async def scenario():
        service = RemoteWorkspace()
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Preview off in test"))
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.2)
            tree = app.query_one("#files", Tree)
            assert str(tree.root.label).startswith("Photos/")
            assert "Trip/" == str(tree.root.children[0].label)
            assert "HOME-PC" in str(app.query_one("#connection", Button).label)
            source = app.preview_source(service.data["files"][0])
            assert source.file_id == 1 and source.session_id == "home-session"
            assert not hasattr(source, "path")
            await pilot.press("f2")
            await pilot.pause()
            assert isinstance(app.screen, ConnectionScreen)
            app.screen.query_one("#connection-model", Input).value = "gemma3:27b"
            app.screen.query_one("#connection-workers", Input).value = "2"
            await pilot.click("#connection-save")
            await pilot.pause(0.2)
            assert service.calls == [("settings", {"model": "gemma3:27b", "workers": 2})]
            app.action_workspace("review")
            await pilot.pause()
            app.action_edit()
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            assert app.screen.query_one("#destination", Input).value == "sunset.png"
            await pilot.press("escape")
            app.action_image()
            await pilot.pause()
            assert isinstance(app.screen, ImageScreen)
            assert app.screen.query_one("#image-external", Button).disabled
    asyncio.run(scenario())


def test_disconnect_preserves_snapshot_blocks_keyboard_and_does_not_repeat_errors():
    async def scenario():
        service = RemoteWorkspace()
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            previous = copy.deepcopy(app.snapshot)
            service.fail = True
            app.refresh_snapshot()
            await pilot.pause(0.2)
            assert app.snapshot == previous
            assert app.query_one("#run", Button).disabled
            assert "last received state" in str(app.query_one("#status", Static).render())
            await pilot.press("m", "space")
            await pilot.pause()
            assert service.calls == []
            assert app._connection_failures == 1
            app.refresh_snapshot()
            await pilot.pause()
            assert app._connection_failures == 1  # bounded retry delay
    asyncio.run(scenario())


def test_remote_quit_leaves_active_worker_running():
    async def scenario():
        service = RemoteWorkspace()
        service.data["active_job"] = {"state": "running", "job_type": "analyze"}
        service.data["owned_live_workers"] = True
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.2)
            exits = []
            app.exit = lambda *args, **kwargs: exits.append(True)
            await pilot.press("q")
            assert exits == [True]
            assert service.calls == []
    asyncio.run(scenario())


def test_obsolete_snapshot_failure_does_not_disconnect_new_workspace():
    async def scenario():
        old = RemoteWorkspace()
        replacement = RemoteWorkspace()
        replacement.session_id = "replacement-session"
        replacement.data["session"]["id"] = replacement.session_id
        started, release = threading.Event(), threading.Event()
        app = DDHApp(old, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)

            def stale_snapshot(**kwargs):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Test snapshot was not released")
                raise ValueError("Previous workstation disconnected")

            old.snapshot = stale_snapshot
            app.refresh_snapshot()
            assert await asyncio.to_thread(started.wait, 2)
            try:
                app.open_session(replacement)
            finally:
                release.set()
            await pilot.pause(0.2)
            assert app._connection_failures == 0
            assert app._connection_error == ""
            assert app._next_refresh_at == 0
            app.refresh_snapshot()
            await pilot.pause(0.2)
            assert app.snapshot["session"]["id"] == "replacement-session"
            assert not app.query_one("#run", Button).disabled
    asyncio.run(scenario())


def test_new_workspace_starts_with_its_own_connection_retry_state():
    async def scenario():
        old = RemoteWorkspace()
        replacement = RemoteWorkspace()
        replacement.connection.name = "SECOND-PC"
        app = DDHApp(old, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            old.fail = True
            app.refresh_snapshot()
            await pilot.pause(0.2)
            assert app._connection_failures == 1
            assert app._connection_error

            replacement.fail = True
            app.open_session(replacement)
            await pilot.pause(0.2)
            assert app._connection_failures == 1
            assert "SECOND-PC" in str(app.query_one("#connection", Button).label)
            assert app.snapshot == {}
    asyncio.run(scenario())


def test_failed_workspace_refresh_blocks_changes_even_when_transport_stays_connected():
    async def scenario():
        service = RemoteWorkspace()
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            previous = copy.deepcopy(app.snapshot)
            original_snapshot = service.snapshot

            def denied_snapshot(**kwargs):
                raise ValueError("Session is no longer available")

            service.snapshot = denied_snapshot
            app.refresh_snapshot()
            await pilot.pause(0.2)
            assert service.connection.state == "connected"
            assert app.snapshot == previous
            assert app.query_one("#run", Button).disabled
            assert "STALE" in str(app.query_one("#connection", Button).label)
            await pilot.press("m", "space")
            await pilot.pause()
            assert service.calls == []

            service.snapshot = original_snapshot
            app._next_refresh_at = 0
            app.refresh_snapshot()
            await pilot.pause(0.2)
            assert not app.query_one("#run", Button).disabled
            assert "STALE" not in str(app.query_one("#connection", Button).label)
    asyncio.run(scenario())


def test_missing_ssd_keeps_connection_and_cancel_available():
    async def scenario():
        service = RemoteWorkspace()
        service.data["storage"] = {"available": False, "message": "Drive missing"}
        service.data["active_job"] = {"state": "running", "job_type": "scan"}
        service.cancel_pipeline = lambda: service.calls.append(("cancel",))
        app = DDHApp(service, image_capability=ImageCapabilities("off", "Preview off"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert service.connection.state == "connected"
            assert "DRIVE MISSING" in str(app.query_one("#connection", Button).label)
            assert app.query_one("#metadata", Button).disabled
            assert app.query_one("#open-image", Button).disabled
            assert not app.query_one("#cancel-run", Button).disabled
            await pilot.press("m")
            await pilot.pause()
            assert service.calls == []
            await pilot.click("#cancel-run")
            await pilot.pause(0.2)
            assert service.calls == [("cancel",)]
    asyncio.run(scenario())

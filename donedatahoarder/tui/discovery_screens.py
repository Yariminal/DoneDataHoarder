"""Nearby and saved workstations in the existing terminal design language."""
from __future__ import annotations

import asyncio
import socket

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static


class NearbyScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Back")]
    DEFAULT_CSS = """
    NearbyScreen { align: center middle; background: $background 90%; }
    NearbyScreen > VerticalScroll { width: 92%; max-width: 108; height: auto; max-height: 96%; border: solid $primary; padding: 0 1; background: $surface; }
    NearbyScreen Label { height: 1; color: $primary; }
    NearbyScreen Static { height: auto; }
    NearbyScreen Horizontal { height: 3; }
    NearbyScreen Button { min-width: 10; margin-right: 1; }
    NearbyScreen #nearby-message { color: $warning; }
    """

    def __init__(self, manager, *, initial=False, active_server_id=None):
        super().__init__()
        self.manager, self.initial, self.active_server_id = manager, initial, active_server_id
        self.opening = False
        self._choices = []
        self._signature = None
        self._picker_closed = False
        self._refreshing = False
        self._auto_attempted = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("DDH / NEARBY WORKSTATIONS", markup=False)
            yield Static("Your terminal stays here. Processing and files stay on the workstation.", markup=False)
            yield Select([], id="nearby-devices", prompt="Looking for workstations…")
            yield Static("", id="nearby-details", markup=False)
            yield Label("First connection · paste the workstation's one-use invitation", markup=False)
            yield Input(password=True, id="nearby-invitation", placeholder="Generate with remote-serve --discoverable --pair")
            yield Label("This laptop's name", markup=False)
            yield Input(socket.gethostname(), id="nearby-device-name")
            yield Checkbox("Reconnect automatically to this workstation", id="nearby-auto")
            yield Label("Manual HTTPS address (optional)", markup=False)
            yield Input(id="nearby-url", placeholder="https://192.168.1.20:8765")
            yield Static("Discovery does not grant access. An invitation is needed only for first use.", markup=False)
            yield Static("", id="nearby-message", markup=False)
            with Horizontal():
                yield Button("Connect", id="nearby-connect", variant="primary")
                yield Button("Forget", id="nearby-forget", disabled=True)
                yield Button("Quit" if self.initial else "Back", id="nearby-close")

    def on_mount(self):
        self.refresh_devices(start=True)
        self.set_interval(1.0, self.refresh_devices)
        self.query_one("#nearby-devices", Select).focus()

    @work(group="nearby-list", exit_on_error=False)
    async def refresh_devices(self, *, start=False):
        if self._refreshing or self.opening:
            return
        self._refreshing = True
        try:
            if start:
                await asyncio.to_thread(self.manager.start)
            choices = await asyncio.to_thread(self.manager.choices)
            if self._picker_closed or not self.is_mounted:
                return
            signature = [(item["server_id"], item["name"], item["saved"], item["nearby"], item["url"]) for item in choices]
            self._choices = choices
            selector = self.query_one("#nearby-devices", Select)
            if signature != self._signature:
                selected = selector.value
                selector.set_options([(Text(f"{item['name']} · {item['server_id'][:8]} · {'Nearby' if item['nearby'] else 'Saved / offline'}{' · paired' if item['saved'] else ''}"), item["server_id"]) for item in choices])
                if any(item["server_id"] == selected for item in choices):
                    selector.value = selected
                selector.prompt = "Choose a workstation" if choices else "No nearby workstations yet"
                self._signature = signature
            if self.manager.error:
                self.query_one("#nearby-message", Static).update(self.manager.error)
            # Only a previously opted-in profile may connect automatically, and
            # only before the user has selected or typed anything in this picker.
            automatic = [item for item in choices if item["saved"] and item["nearby"] and item["auto_reconnect"]]
            if self.initial and not self._auto_attempted and len(automatic) == 1 and selector.value is Select.NULL:
                if not self.query_one("#nearby-invitation", Input).value and not self.query_one("#nearby-url", Input).value:
                    self._auto_attempted = True
                    selector.value = automatic[0]["server_id"]
                    self.query_one("#nearby-auto", Checkbox).value = True
                    self.connect_selected()
        except Exception as exc:
            if self.is_mounted and not self._picker_closed:
                self.query_one("#nearby-message", Static).update(str(exc))
        finally:
            self._refreshing = False

    @on(Select.Changed, "#nearby-devices")
    def selected(self, event):
        item = next((item for item in self._choices if item["server_id"] == event.value), None)
        self.query_one("#nearby-details", Static).update(f"{item['name']} · {item['url']}" if item else "")
        already_open = bool(item and item["server_id"] == self.active_server_id)
        self.query_one("#nearby-connect", Button).disabled = self.opening or already_open
        if already_open:
            self.query_one("#nearby-message", Static).update("This workstation is already open. Use Back, then Reconnect in the connection panel.")
        self.query_one("#nearby-forget", Button).disabled = not item or not item["saved"] or item["server_id"] == self.active_server_id or self.opening
        self.query_one("#nearby-auto", Checkbox).value = item["auto_reconnect"] if item else False

    @on(Button.Pressed, "#nearby-connect")
    def connect_selected(self):
        if self.opening:
            return
        if self.query_one("#nearby-devices", Select).value == self.active_server_id:
            return
        self.opening = True
        self._auto_attempted = True
        selector = self.query_one("#nearby-devices", Select)
        values = dict(server_id=None if selector.value is Select.NULL else selector.value,
                      url=self.query_one("#nearby-url", Input).value.strip(),
                      invitation=self.query_one("#nearby-invitation", Input).value.strip(),
                      device_name=self.query_one("#nearby-device-name", Input).value.strip(),
                      auto_reconnect=self.query_one("#nearby-auto", Checkbox).value)
        self.query_one("#nearby-invitation", Input).value = ""
        self.query_one("#nearby-connect", Button).disabled = True
        self.query_one("#nearby-forget", Button).disabled = True
        self.query_one("#nearby-message", Static).update("Verifying workstation and connecting…")
        self._connect(values)

    @work(group="nearby-connect", exit_on_error=False)
    async def _connect(self, values):
        # Shield the thread and clean up its connection if this dialog closes.
        task = asyncio.create_task(asyncio.to_thread(self.manager.connect, **values))
        try:
            connection = await asyncio.shield(task)
            if self._picker_closed or not self.is_mounted:
                connection.close()
            else:
                self.dismiss(connection)
        except asyncio.CancelledError:
            def clean_up(future):
                if not future.cancelled() and future.exception() is None:
                    future.result().close()
            task.add_done_callback(clean_up)
            raise
        except Exception as exc:
            if self.is_mounted and not self._picker_closed:
                # A third-party exception must never reflect the pasted secret.
                message = str(exc)
                if values.get("invitation"):
                    message = "Pairing could not be completed. Check the workstation, address, and invitation; request a fresh invitation if needed."
                self.query_one("#nearby-message", Static).update(message)
        finally:
            self.opening = False
            if self.is_mounted and not self._picker_closed:
                selected = self.query_one("#nearby-devices", Select).value
                self.query_one("#nearby-connect", Button).disabled = selected == self.active_server_id
                item = next((item for item in self._choices if item["server_id"] == selected), None)
                self.query_one("#nearby-forget", Button).disabled = not item or not item["saved"] or selected == self.active_server_id

    @on(Button.Pressed, "#nearby-forget")
    def forget(self):
        selected = self.query_one("#nearby-devices", Select).value
        if self.opening or selected is Select.NULL or selected == self.active_server_id:
            return
        try:
            self.manager.store.forget(selected)
            self.query_one("#nearby-message", Static).update("Saved credential removed. Command recovery state is retained. Revoke the device on the workstation with ddh remote-devices --revoke.")
            self._signature = None
            self.refresh_devices()
        except Exception as exc:
            self.query_one("#nearby-message", Static).update(str(exc))

    @on(Button.Pressed, "#nearby-close")
    def action_close(self):
        self._picker_closed = True
        self.dismiss(None)

    def on_unmount(self):
        self._picker_closed = True

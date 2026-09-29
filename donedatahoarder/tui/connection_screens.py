"""Connection details and session settings without replacing the workspace."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ConnectionScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    ConnectionScreen { align: center middle; background: $background 90%; }
    ConnectionScreen > VerticalScroll { width: 90%; max-width: 100; height: auto; max-height: 95%; border: solid $primary; padding: 0 1; background: $surface; }
    ConnectionScreen Static { height: auto; }
    ConnectionScreen Label { height: 1; color: $primary; }
    ConnectionScreen Horizontal { height: 3; }
    ConnectionScreen Button { min-width: 12; margin-right: 1; }
    """

    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        session = self.workspace.snapshot.get("session") or {}
        with VerticalScroll():
            yield Label("DDH / CONNECTION & SESSION", markup=False)
            yield Static("", id="connection-details", markup=False)
            yield Label("Ollama model", markup=False)
            yield Input(session.get("model", ""), id="connection-model")
            yield Label("Analysis workers (1–32)", markup=False)
            yield Input(str(session.get("workers", 1)), id="connection-workers", type="integer")
            yield Static("Settings apply to the next new run. Finish the current saved plan before changing them.", markup=False)
            yield Static("", id="connection-message", markup=False)
            with Horizontal():
                yield Button("Save settings", id="connection-save", variant="primary")
                yield Button("Reconnect", id="connection-retry")
                yield Button("Close", id="connection-close")
            yield Button("Nearby workstations", id="connection-nearby")

    def on_mount(self) -> None:
        self.update_details()
        self.set_interval(0.5, self.update_details)
        self.query_one("#connection-close", Button).focus()

    def update_details(self) -> None:
        connection = self.workspace.remote_connection
        session = self.workspace.snapshot.get("session") or {}
        if connection is None:
            text = "LOCAL · This computer owns processing and files."
        else:
            latency = getattr(connection, "latency_ms", None)
            text = f"{getattr(connection, 'name', 'Workstation')} · {connection.state.replace('_', ' ').upper()}"
            if latency is not None:
                text += f" · {latency:.0f} ms"
            text += f"\n{getattr(connection, 'url', '')}\nProcessing and files stay on the workstation. Quitting this TUI disconnects only."
            if getattr(connection, "pending_request_id", None):
                text += f"\nPending command: {connection.pending_request_id}\nNew commands are blocked until its outcome is known."
            if getattr(connection, "error", None):
                text += f"\n{connection.error}"
        text += f"\nSession: {session.get('id', 'Choose a session')}\nFolder: {session.get('root_path', '—')}"
        if not self.workspace.storage_available:
            text += "\nDRIVE MISSING · Session state remains available. Reconnect the collection drive before continuing."
        self.query_one("#connection-details", Static).update(text)
        plan = self.workspace.snapshot.get("plan") or {}
        blocked = (self.workspace.service is None or self.workspace.busy or self.workspace.remote_blocked
                   or not self.workspace.storage_available
                   or self.workspace.snapshot.get("has_live_workers")
                   or bool(plan and plan.get("state") != "completed"))
        self.query_one("#connection-save", Button).disabled = bool(blocked)
        self.query_one("#connection-retry", Button).display = connection is not None

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "connection-close":
            self.action_close()
        elif event.button.id == "connection-retry":
            self.workspace.reconnect_remote()
        elif event.button.id == "connection-nearby":
            self.dismiss({"nearby": True})
        elif event.button.id == "connection-save":
            model = self.query_one("#connection-model", Input).value.strip()
            try:
                workers = int(self.query_one("#connection-workers", Input).value)
                if not model or len(model) > 200 or not 1 <= workers <= 32:
                    raise ValueError
            except ValueError:
                self.query_one("#connection-message", Static).update("Choose a model and 1–32 workers.")
                return
            self.dismiss({"model": model, "workers": workers})

    def action_close(self) -> None:
        self.dismiss(None)

"""Folder/session selection and keyboard reference for the terminal workspace."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static


HELP = """WORKSPACES
1 Pipeline · 2 Review · 3 Collections · 4 History
Tab / Shift+Tab move focus · arrows navigate the focused control
j / k move the file or focused table selection · Enter activates a control
o choose a folder or reopen a session · F2 connection and session settings

PIPELINE
Space run / pause / resume · m start a metadata-only run
Metadata only scans, extracts metadata, finds duplicates, and prepares a preview.
It works without Ollama. Opening a session never starts processing.

REVIEW
a approve · r reject · e edit destination (in the Review workspace)
p preview approved changes · Apply requires explicit confirmation
Use History → Preview undo to inspect and confirm recovery.

IMAGES
i preview selected image · c compare duplicate candidates
In the image viewer: + / = zoom in · − zoom out · 0 fit · Escape close
The arrow buttons pan both images together. Open original uses your local viewer.
Remote images are prepared on the workstation and displayed in this terminal.

HELP AND EXIT
? / F1 this reference · Escape close a dialog
q / Ctrl+Q / Ctrl+C quit safely; local processing must stop first
Remote quit disconnects; workstation processing continues.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,question_mark", "close", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 90%; }
    HelpScreen > Vertical { width: 90%; max-width: 100; height: 95%; max-height: 38; border: solid $primary; padding: 0 1; background: $surface; }
    HelpScreen .help-body { height: 1fr; }
    HelpScreen Static { height: auto; }
    HelpScreen Button { height: 3; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("DDH / KEYBOARD REFERENCE", markup=False)
            with VerticalScroll(classes="help-body"):
                yield Static(HELP, markup=False)
            yield Button("Close", id="help-close")

    def on_mount(self) -> None:
        self.query_one("#help-close", Button).focus()

    @on(Button.Pressed, "#help-close")
    def action_close(self) -> None:
        self.dismiss(None)


class SessionScreen(ModalScreen[Any]):
    """Selections create/open a service; merely visiting this screen is read-only."""

    BINDINGS = [Binding("escape", "cancel", "Back"), Binding("f1,question_mark", "help", "Help")]
    DEFAULT_CSS = """
    SessionScreen { align: center middle; background: $background 95%; }
    SessionScreen > VerticalScroll { width: 92%; max-width: 112; height: auto; max-height: 98%; border: solid $primary; background: $surface; padding: 0 1; }
    SessionScreen Label { height: 1; color: $primary; }
    SessionScreen Input, SessionScreen Select { height: 3; }
    SessionScreen Static { height: auto; }
    SessionScreen .session-actions { height: 3; }
    SessionScreen Button { min-width: 12; margin-right: 1; padding: 0 1; }
    SessionScreen #session-model { width: 1fr; }
    SessionScreen #session-message { color: $warning; }
    SessionScreen #session-readiness { color: $text-muted; }
    """

    def __init__(self, catalog: Any, *, initial: bool = False):
        super().__init__()
        self.catalog, self.initial = catalog, initial
        self.sessions: list[dict] = []
        self.opening = False
        self._reconnecting = False
        self._readiness_revision = 0
        self._readiness_running = False
        self._readiness_requested: tuple[str, int, bool] | None = None

    def compose(self) -> ComposeResult:
        downloads = Path.home() / "Downloads"
        default_root = getattr(self.catalog, "default_root", str(downloads if downloads.is_dir() else Path.home()))
        path_label = getattr(self.catalog, "path_label", "New collection folder")
        with VerticalScroll():
            yield Label("DDH / CHOOSE YOUR COLLECTION", markup=False)
            yield Static("Open a folder or continue saved work. You decide when processing starts.", markup=False)
            yield Label(path_label, markup=False)
            yield Input(default_root, id="session-folder", placeholder=default_root)
            with Horizontal(classes="session-actions"):
                yield Input(self.catalog.model, id="session-model", placeholder="Ollama model")
                yield Button("Check model", id="session-check")
                yield Button("Open folder", id="session-new", variant="primary")
            yield Static("Checking Ollama… Metadata only is always available.", id="session-readiness", markup=False)
            yield Label("Recent sessions", markup=False)
            yield Select([], id="session-recent", prompt="Loading saved sessions…", disabled=True)
            yield Static("", id="session-detail", markup=False)
            with Horizontal(classes="session-actions"):
                yield Button("Open session", id="session-resume", disabled=True)
                retry = Button("Reconnect", id="session-reconnect")
                retry.display = getattr(self.catalog, "connection", None) is not None
                yield retry
                yield Button("Help", id="session-help")
                yield Button("Quit" if self.initial else "Back", id="session-cancel")
            yield Static("", id="session-message", markup=False)

    def on_mount(self) -> None:
        self.query_one("#session-folder", Input).focus()
        self.load_sessions()
        self.check_model()

    @work(exit_on_error=False)
    async def load_sessions(self) -> None:
        try:
            sessions = await asyncio.to_thread(self.catalog.list_sessions)
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#session-message", Static).update(f"Cannot load saved sessions: {exc}")
            return
        if not self.is_mounted:
            return
        self.sessions = sessions
        eligible = [item for item in sessions if item.get("backend") == "ollama"]
        selector = self.query_one("#session-recent", Select)
        selector.set_options([(f"{item.get('root_path')} · {item.get('status')} · {item['id'][:8]}", item["id"]) for item in eligible])
        selector.disabled = not eligible
        selector.prompt = "Choose saved work" if eligible else "No sessions yet"
        clouds = len(sessions) - len(eligible)
        if clouds:
            self.query_one("#session-message", Static).update(f"{clouds} cloud-backed session(s) stay available in the CLI/web interface.")

    @on(Select.Changed, "#session-recent")
    def recent_changed(self, event: Select.Changed) -> None:
        item = next((item for item in self.sessions if item["id"] == event.value), None)
        self.query_one("#session-resume", Button).disabled = self.opening or item is None
        self.query_one("#session-detail", Static).update(
            f"{item['root_path']}\nSaved model: {item['model']} · {item.get('workers', 1)} worker(s) · {item['id']}" if item else "")
        if item:
            self._readiness_revision += 1
            self._check_model(item["id"], self._readiness_revision, session=True)

    def check_model(self) -> None:
        self._readiness_revision += 1
        self._check_model(self.query_one("#session-model", Input).value.strip(), self._readiness_revision)

    def _check_model(self, model: str, revision: int, *, session: bool = False) -> None:
        # Selection changes may arrive while a network check is in flight.
        # Coalesce them into the newest choice rather than filling the shared
        # thread pool with requests that could delay opening a workspace.
        self._readiness_requested = (model, revision, session)
        self.query_one("#session-readiness", Static).update("Checking Ollama… Metadata only is always available.")
        if not self._readiness_running:
            self._readiness_running = True
            self.query_one("#session-check", Button).disabled = True
            self._run_model_checks()

    @work(group="model-readiness", exit_on_error=False)
    async def _run_model_checks(self) -> None:
        try:
            while self.is_mounted and self._readiness_requested is not None:
                model, revision, session = self._readiness_requested
                self._readiness_requested = None
                try:
                    checker = self.catalog.session_readiness if session else self.catalog.readiness
                    message = await asyncio.to_thread(checker, model)
                except Exception:
                    message = "Cannot check Ollama right now. Metadata only remains available."
                if self.is_mounted and revision == self._readiness_revision:
                    self.query_one("#session-readiness", Static).update(message)
        finally:
            self._readiness_running = False
            if self.is_mounted:
                self.query_one("#session-check", Button).disabled = False

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        identifier = event.button.id
        if identifier == "session-cancel":
            self.action_cancel()
        elif identifier == "session-help":
            self.action_help()
        elif identifier == "session-check":
            self.check_model()
        elif identifier == "session-reconnect":
            self.reconnect()
        elif identifier == "session-new":
            model = self.query_one("#session-model", Input).value.strip()
            if not model:
                self.query_one("#session-message", Static).update("Enter a model name for this session. It is only used when you run AI stages.")
                return
            self.open_choice(root=self.query_one("#session-folder", Input).value, model=model)
        elif identifier == "session-resume":
            selected = self.query_one("#session-recent", Select).value
            if isinstance(selected, str):
                self.open_choice(session_id=selected)

    def open_choice(self, **choice: Any) -> None:
        if self.opening:
            return
        self.opening = True
        for identifier in ("session-new", "session-resume", "session-cancel", "session-help", "session-reconnect"):
            self.query_one("#" + identifier, Button).disabled = True
        self.query_one("#session-message", Static).update("Opening workspace…")
        self._open_choice(choice)

    @work(group="session-open", exit_on_error=False)
    async def _open_choice(self, choice: dict) -> None:
        try:
            service = await asyncio.to_thread(self.catalog.open, **choice)
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#session-message", Static).update(str(exc))
        else:
            if self.is_mounted:
                self.dismiss(service)
        finally:
            self.opening = False
            if self.is_mounted:
                self.query_one("#session-new", Button).disabled = False
                self.query_one("#session-cancel", Button).disabled = False
                self.query_one("#session-help", Button).disabled = False
                self.query_one("#session-reconnect", Button).disabled = self._reconnecting
                self.query_one("#session-resume", Button).disabled = not isinstance(self.query_one("#session-recent", Select).value, str)

    def action_cancel(self) -> None:
        if not self.opening:
            self.dismiss(None)

    @work(group="session-reconnect", exit_on_error=False)
    async def reconnect(self) -> None:
        connection = getattr(self.catalog, "connection", None)
        if self.opening or self._reconnecting or connection is None:
            return
        self._reconnecting = True
        self.query_one("#session-reconnect", Button).disabled = True
        try:
            await asyncio.to_thread(connection.connect)
            if self.is_mounted:
                self.query_one("#session-message", Static).update("Connected. Choose a saved session or collection folder.")
                self.load_sessions()
                self.check_model()
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#session-message", Static).update(str(exc))
        finally:
            self._reconnecting = False
            if self.is_mounted:
                self.query_one("#session-reconnect", Button).disabled = self.opening

    def action_help(self) -> None:
        # A completing open must dismiss this screen, not a Help screen above
        # it. Keep both the button and keyboard route blocked until it finishes.
        if not self.opening:
            self.app.push_screen(HelpScreen())

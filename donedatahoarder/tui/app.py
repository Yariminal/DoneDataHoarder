"""Keyboard-first workspace over DDH's persisted, scope-checked services."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
from typing import Any, Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, Static, TabbedContent, TabPane, Tree

from .images import create_image_preview, open_external
from .onboarding_screens import HelpScreen, SessionScreen
from .connection_screens import ConnectionScreen


STAGES = ("scan", "enrich", "analyze", "dedup", "relate", "propose", "organize", "preview")


def readable(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def human_size(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def photo_dimensions(photo: dict | None) -> str:
    photo = photo or {}
    width, height = photo.get("display_width") or photo.get("width"), photo.get("display_height") or photo.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
        return f"{width:g}×{height:g} px · {width * height / 1_000_000:.1f} MP"
    return "Pixel dimensions unknown"


def photo_value(value: Any) -> str:
    text = " ".join(readable(value).split())
    return text if len(text) <= 512 else text[:512] + "…"


def photo_metadata_summary(photo: dict | None) -> str:
    """Render indexed capture evidence, keeping an unknown inventory explicit."""
    photo = photo or {}
    if photo.get("is_photo") is False or photo.get("status") == "not_photo":
        return ""
    lines = [photo_dimensions(photo)]
    status = photo.get("status", "unknown")
    fields = photo.get("fields") or {}
    if status != "complete":
        lines.append(f"Capture metadata inventory: {status}; absence is not established.")
    elif not fields:
        lines.append("No valid capture metadata found.")
    for name, value in fields.items():
        lines.append(f"{name.replace('_', ' ').capitalize()}: {photo_value(value)}")
    lines.extend(photo_value(value) for value in photo.get("warnings", []))
    return "\n".join(lines)


def photo_quality_summary(quality: dict | None) -> str:
    if not quality:
        return ""
    labels = {
        "recommended": "PHOTO PRESERVATION · Keeper recommendation",
        "tradeoff": "PHOTO TRADEOFF · Keep both for review",
        "equivalent": "PHOTO EVIDENCE · Equivalent recorded evidence",
        "unknown": "PHOTO EVIDENCE UNKNOWN · Keep both for review",
        "variant": "PHOTO VARIANTS · Keep both for review",
    }
    candidate, keeper = quality.get("candidate") or {}, quality.get("keeper") or {}
    lines = [labels.get(quality.get("status"), labels["unknown"]),
             f"Candidate: {photo_dimensions(candidate)}",
             f"Keeper: {photo_dimensions(keeper)}"]
    lines.extend(photo_value(reason) for reason in quality.get("reasons", []))
    for label, key, evidence in (("Only candidate retains", "candidate_unique_fields", candidate),
                                 ("Only keeper retains", "keeper_unique_fields", keeper)):
        for field in quality.get(key, []):
            lines.append(f"{label} {field.replace('_', ' ')}: {photo_value((evidence.get('fields') or {}).get(field))}")
    for field in quality.get("conflicting_fields", []):
        lines.append(f"Conflicting {field.replace('_', ' ')}: candidate {photo_value((candidate.get('fields') or {}).get(field))}; keeper {photo_value((keeper.get('fields') or {}).get(field))}")
    if quality.get("requires_review"):
        lines.append("Inspect both originals. Pixel dimensions alone do not establish image quality or interchangeability.")
    return "\n".join(lines)


def duplicate_summary(evidence: dict) -> str:
    parts = [f"{str(evidence.get('type', 'Duplicate')).title()} candidate"]
    if evidence.get("exact_bytes") is True:
        parts.append("Stored SHA-256 matches keeper")
    elif evidence.get("exact_bytes") is False:
        parts.append("Stored SHA-256 differs — not an exact copy")
    elif evidence.get("matching_indexed_md5") is True:
        parts.append("Indexed MD5 matches; apply rechecks the files")
    else:
        parts.append("Exact byte match not established")
    if evidence.get("distance_to_keeper") is not None:
        parts.append(f"Distance to keeper: {evidence['distance_to_keeper']}")
    if evidence.get("keeper_path"):
        parts.append(f"Keeper: {evidence['keeper_path']}")
    if evidence.get("photo_quality"):
        parts.append(photo_quality_summary(evidence["photo_quality"]))
    return "\n".join(parts)


class ConfirmScreen(ModalScreen[bool]):
    """A confirmation has no global Enter binding; the cancel button starts focused."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; background: $background 85%; }
    ConfirmScreen > Vertical { width: 86%; max-width: 110; height: auto; max-height: 90%; border: solid $primary; padding: 1 2; background: $surface; }
    ConfirmScreen .confirmation-body { height: auto; max-height: 24; margin: 1 0; }
    ConfirmScreen Horizontal { height: 3; align-horizontal: right; }
    ConfirmScreen Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, confirm: str = "Apply changes", *, confirm_enabled: bool = True) -> None:
        super().__init__()
        self.heading, self.body, self.confirm_label = title, body, confirm
        self.confirm_enabled = confirm_enabled

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, markup=False)
            with VerticalScroll(classes="confirmation-body"):
                yield Static(self.body, markup=False)
            with Horizontal():
                yield Button("Cancel", id="confirm-cancel")
                yield Button(self.confirm_label, id="confirm-apply", variant="warning", disabled=not self.confirm_enabled)

    def on_mount(self) -> None:
        self.query_one("#confirm-cancel", Button).focus()

    @on(Button.Pressed, "#confirm-cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-apply")
    def confirm(self) -> None:
        self.dismiss(True)


class EditScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    EditScreen { align: center middle; background: $background 85%; }
    EditScreen > Vertical { width: 82%; max-width: 100; height: auto; border: solid $primary; background: $surface; padding: 1 2; }
    EditScreen Input { margin: 1 0; }
    EditScreen Horizontal { height: 3; align-horizontal: right; }
    """

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Edit proposal destination", markup=False)
            yield Input(self.value, id="destination")
            yield Static("The destination will be validated before saving and again before applying.", markup=False)
            with Horizontal():
                yield Button("Cancel", id="edit-cancel")
                yield Button("Save for review", id="edit-save", variant="primary")

    @on(Button.Pressed, "#edit-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#edit-save")
    def save(self) -> None:
        self.dismiss(self.query_one("#destination", Input).value)


class ImageScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("plus,equals", "zoom_in", "Zoom in"), Binding("minus", "zoom_out", "Zoom out"), Binding("0", "fit", "Fit")]
    DEFAULT_CSS = """
    ImageScreen { align: center middle; background: $background 90%; }
    ImageScreen > Vertical { width: 100%; height: 100%; border: solid $primary; background: $surface; padding: 0 1; }
    ImageScreen #image-heading { height: 1; color: $primary; }
    ImageScreen #image-evidence-scroll { height: auto; max-height: 7; }
    ImageScreen #image-evidence { height: auto; }
    ImageScreen #image-panes { height: 1fr; }
    ImageScreen .image-column { width: 1fr; height: 1fr; border: solid $panel; }
    ImageScreen .image-name { height: auto; max-height: 5; padding: 0 1; }
    ImageScreen .image-preview { height: 1fr; }
    ImageScreen .image-photo-scroll { height: auto; max-height: 6; padding: 0 1; }
    ImageScreen .image-photo { height: auto; }
    ImageScreen #image-controls, ImageScreen #image-open-controls { height: 3; layout: horizontal; }
    ImageScreen Button { min-width: 5; margin-right: 1; padding: 0 1; }
    ImageScreen Select { width: 1fr; }
    ImageScreen #image-note { height: 1; color: $text-muted; }
    """

    def __init__(self, file: dict, candidates: list[dict], *, capability: Any = None, evidence: str = "", source_factory: Callable | None = None, remote: bool = False, keeper_callback: Callable | None = None) -> None:
        super().__init__()
        self.file, self.candidates, self.capability, self.evidence = file, candidates, capability, evidence
        self.candidate = candidates[0] if candidates else None
        self.zoom = 1.0
        self.center = (0.5, 0.5)
        self.source_factory = source_factory or (lambda item: item["path"])
        self.remote = remote
        self.keeper_callback = keeper_callback

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("IMAGE COMPARISON" if self.candidates else "IMAGE PREVIEW", id="image-heading", markup=False)
            if self.candidates:
                yield Select([(item.get("filename", str(item["id"])), str(item["id"])) for item in self.candidates], value=str(self.candidates[0]["id"]), allow_blank=False, id="image-candidate")
            with VerticalScroll(id="image-evidence-scroll"):
                yield Static(self.evidence or "Visual inspection only. Review decisions are made in the Review workspace.", id="image-evidence", markup=False)
            with Horizontal(id="image-panes"):
                with Vertical(classes="image-column"):
                    yield Static(f"A · {self.file['path']}\n{human_size(self.file.get('size_bytes'))}", classes="image-name", id="image-a-name", markup=False)
                    with VerticalScroll(classes="image-photo-scroll"):
                        yield Static(photo_metadata_summary(self.file.get("photo_metadata")), classes="image-photo", id="image-a-photo", markup=False)
                    image = create_image_preview(self.source_factory(self.file), id="image-a", capability=self.capability)
                    image.add_class("image-preview")
                    yield image
                if self.candidate:
                    with Vertical(classes="image-column"):
                        yield Static(f"B · {self.candidate['path']}\n{human_size(self.candidate.get('size_bytes'))}", classes="image-name", id="image-b-name", markup=False)
                        with VerticalScroll(classes="image-photo-scroll"):
                            yield Static(photo_metadata_summary(self.candidate.get("photo_metadata")), classes="image-photo", id="image-b-photo", markup=False)
                        image = create_image_preview(self.source_factory(self.candidate), id="image-b", capability=self.capability)
                        image.add_class("image-preview")
                        yield image
            with Horizontal(id="image-controls"):
                yield Button("Fit", id="image-fit")
                yield Button("−", id="image-minus")
                yield Button("+", id="image-plus")
                yield Button("←", id="image-left")
                yield Button("→", id="image-right")
                yield Button("↑", id="image-up")
                yield Button("↓", id="image-down")
            with Horizontal(id="image-open-controls"):
                yield Button("Open original", id="image-external", disabled=self.remote, tooltip="Original stays on the workstation; use inline preview." if self.remote else None)
                if self.candidates:
                    yield Button("Open candidate", id="image-external-b", disabled=self.remote)
                    yield Button("Keep A", id="image-keep-a", disabled=True)
                    yield Button("Keep B", id="image-keep-b", disabled=True)
                yield Button("Close", id="image-close")
            yield Static("1× fit · linked zoom/pan · original colors" + (" · workstation preview" if self.remote else ""), id="image-note", markup=False)

    def on_mount(self) -> None:
        self.query_one("#image-close", Button).focus()
        self.set_interval(0.3, self.update_dimensions)
        self.update_keeper_controls()

    def update_keeper_controls(self) -> None:
        if not self.candidate:
            return
        enabled = bool(self.keeper_callback and self.candidate.get("comparison_group_id") is not None)
        current = self.candidate.get("comparison_keeper_id")
        for side, file in (("a", self.file), ("b", self.candidate)):
            self.query_one(f"#image-keep-{side}", Button).disabled = not enabled or str(file["id"]) == str(current)

    def choose_keeper(self, file: dict) -> None:
        if not self.candidate or self.keeper_callback is None or self.candidate.get("comparison_group_id") is None:
            return
        group_id, file_id = self.candidate["comparison_group_id"], file["id"]
        current_keeper = self.candidate.get("comparison_keeper_id")
        if str(file_id) == str(current_keeper):
            return

        def confirmed(value: bool) -> None:
            if value and self.is_mounted:
                self.dismiss(None)
                self.keeper_callback(group_id, file_id, expected_keeper_id=current_keeper)

        self.app.push_screen(ConfirmScreen("Choose this photo as keeper?",
            f"Keep: {file['path']}\n\n{photo_metadata_summary(file.get('photo_metadata'))}\n\n"
            "Changing the keeper resets affected duplicate decisions for fresh review. Both files remain in place; this does not approve or apply any trash action.",
            "Choose keeper"), confirmed)

    def update_dimensions(self) -> None:
        for side, file in (("a", self.file), ("b", self.candidate)):
            if file:
                size = getattr(self.query_one(f"#image-{side}"), "original_size", None)
                dimensions = f" · {size[0]}×{size[1]} px" if size else ""
                self.query_one(f"#image-{side}-name", Static).update(f"{side.upper()} · {file['path']}\n{human_size(file.get('size_bytes'))}{dimensions}")

    def action_close(self) -> None:
        self.dismiss(None)

    def update_zoom(self) -> None:
        self.query_one("#image-a").set_zoom(self.zoom, center=self.center)
        if self.candidate:
            self.query_one("#image-b").set_zoom(self.zoom, center=self.center)
        self.query_one("#image-note", Static).update(f"{self.zoom:g}× fit · linked zoom/pan · original colors")

    def action_zoom_in(self) -> None:
        self.zoom = min(8.0, self.zoom * 2)
        self.update_zoom()

    def action_zoom_out(self) -> None:
        self.zoom = max(1.0, self.zoom / 2)
        self.update_zoom()

    def action_fit(self) -> None:
        self.zoom, self.center = 1.0, (0.5, 0.5)
        self.update_zoom()

    @on(Select.Changed, "#image-candidate")
    def candidate_changed(self, event: Select.Changed) -> None:
        selected = next((item for item in self.candidates if str(item["id"]) == event.value), None)
        if selected and self.is_mounted:
            self.candidate = selected
            self.query_one("#image-b").set_source(self.source_factory(selected))
            self.update_dimensions()
            self.query_one("#image-evidence", Static).update(selected.get("comparison_evidence") or self.evidence)
            self.query_one("#image-b-photo", Static).update(photo_metadata_summary(selected.get("photo_metadata")))
            self.update_keeper_controls()
            self.update_zoom()

    @on(Button.Pressed)
    async def image_button(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "image-close":
            self.action_close()
        elif action == "image-fit":
            self.action_fit()
        elif action == "image-plus":
            self.action_zoom_in()
        elif action in {"image-keep-a", "image-keep-b"}:
            self.choose_keeper(self.file if action == "image-keep-a" else self.candidate)
        elif action == "image-minus":
            self.action_zoom_out()
        elif action in {"image-left", "image-right", "image-up", "image-down"}:
            dx = -0.1 if action == "image-left" else 0.1 if action == "image-right" else 0
            dy = -0.1 if action == "image-up" else 0.1 if action == "image-down" else 0
            self.center = (max(0, min(1, self.center[0] + dx)), max(0, min(1, self.center[1] + dy)))
            self.update_zoom()
        elif action in {"image-external", "image-external-b"} and not self.remote:
            try:
                file = self.candidate if action == "image-external-b" else self.file
                await asyncio.to_thread(open_external, file["path"])
            except OSError as exc:
                self.notify(str(exc), severity="error")


class ReviewTable(DataTable):
    """Fit the evidence labels after the table receives its actual pane size."""

    def on_resize(self) -> None:
        self.app.update_review_table()


class DDHApp(App[None]):
    """The app never mutates files directly; WorkspaceService owns every operation."""

    TITLE = "DoneDataHoarder"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "workspace('pipeline')", "Pipeline"), Binding("2", "workspace('review')", "Review"),
        Binding("3", "workspace('collections')", "Collections"), Binding("4", "workspace('history')", "History"),
        Binding("space", "run_pause", "Run/pause"), Binding("m", "metadata", "Metadata only", show=False),
        Binding("j", "next_file", "Down", show=False), Binding("k", "previous_file", "Up", show=False),
        Binding("a", "approve", "Approve", show=False), Binding("r", "reject", "Reject", show=False),
        Binding("e", "edit", "Edit", show=False), Binding("p", "preview", "Preview"),
        Binding("i", "image", "Image"), Binding("c", "compare", "Compare"),
        Binding("o", "sessions", "Sessions"), Binding("question_mark,f1", "help", "Help"),
        Binding("f2", "connection", "Connection", show=False),
        Binding("q", "safe_quit", "Quit"), Binding("ctrl+c,ctrl+q", "safe_quit", "Quit", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $background; color: $foreground; }
    Button { border: solid $panel; }
    #heading { height: 2; }
    #titlebar { width: 1fr; height: 2; padding: 0 1; color: $primary; text-style: bold; }
    #connection { width: auto; max-width: 40%; min-width: 10; height: 2; border: none; padding: 0 1; color: $success; }
    #connection.disconnected { color: $warning; }
    #stages { height: 3; padding: 0 1; }
    #stages Button { width: 1fr; min-width: 8; height: 3; border: solid $panel; margin: 0; padding: 0; }
    #stages .complete { color: $success; }
    #stages .running { color: $primary; border: solid $primary; }
    #toolbar { height: 3; padding: 0 1; }
    #toolbar Button { min-width: 9; margin-right: 1; }
    #page-controls { height: 3; padding: 0 1; }
    #page-label { width: 1fr; padding-top: 1; }
    #page-controls Button { min-width: 9; }
    #workspace { height: 1fr; }
    TabPane { padding: 0 1; }
    #pipeline-layout { height: 1fr; }
    .pane { border: solid $panel; }
    #file-pane { width: 3fr; }
    #inspector-pane { width: 2fr; }
    .pane-heading { height: 1; color: $primary; padding: 0 1; }
    DataTable { height: 1fr; }
    #files { height: 1fr; padding: 0 1; }
    #inspector-text, #review-detail, #collection-detail { height: auto; padding: 1; }
    #inspector-image { height: 13; margin: 0 1; }
    #inspector-actions { height: 3; }
    #inspector-actions Button { min-width: 10; }
    #review-layout { height: 1fr; }
    #review-table { width: 3fr; }
    #review-evidence { width: 2fr; }
    .actions { height: 3; }
    .actions Button { min-width: 10; margin-right: 1; }
    #collections-layout { height: 1fr; }
    #collections-table { width: 1fr; }
    #collection-detail { width: 1fr; }
    #activity { height: 7; margin: 0 1; border: solid $panel; }
    #status { height: 2; padding: 0 1; color: $text-muted; }
    Footer { height: 1; }
    Screen.compact #activity { height: 3; }
    Screen.compact #status { height: 1; }
    Screen.narrow #stages { height: 1; padding: 0 1; }
    Screen.narrow #stages Button { width: auto; height: 1; min-width: 0; border: none; padding: 0; }
    """

    def __init__(self, service: Any = None, *, image_capability: Any = None, catalog: Any = None,
                 discover: bool = False, remote_root=None, remote_session=None, nearby_manager=None) -> None:
        super().__init__()
        self.service = service
        self.catalog = catalog
        self.image_capability = image_capability
        self.snapshot: dict = {}
        self.selected_file_id: int | str | None = None
        self.selected_proposal_id: int | str | None = None
        self.selected_collection_id: int | str | None = None
        self.busy = False
        self._generation = 0
        self._signatures: dict[str, str] = {}
        self._image_path: str | None = None
        self._last_job_message = ""
        self._theme_fingerprint: Any = None
        self._theme_revision = 0
        self._quitting = False
        self._refreshing = False
        self.inspected_stage: str | None = None
        self.offset = 0
        self.page_limit = 250
        self._next_refresh_at = 0.0
        self._connection_failures = 0
        self._connection_error = ""
        self._reconnecting = False
        self._discover = discover
        self._remote_root, self._remote_session = remote_root, remote_session
        self._nearby_manager = nearby_manager
        self._choosing_connection = False
        self._owned_connections = []

    @property
    def remote_connection(self):
        return getattr(self.service, "connection", None) or getattr(self.catalog, "connection", None)

    @property
    def remote_blocked(self) -> bool:
        connection = self.remote_connection
        return bool(connection and (connection.state != "connected" or connection.pending_request_id
                                    or self._connection_error))

    @property
    def storage_available(self) -> bool:
        return (self.snapshot.get("storage") or {}).get("available", True)

    def display_path(self, value: str):
        flavor = self.snapshot.get("path_flavor") or getattr(self.service, "path_flavor", None)
        return PureWindowsPath(value) if flavor == "windows" else PurePosixPath(value) if flavor == "posix" else Path(value)

    def preview_source(self, file: dict):
        if getattr(self.service, "is_remote", False):
            from donedatahoarder.remote.previews import remote_source
            return remote_source(self.service.connection, self.service.session_id, file)
        return file["path"]

    def compose(self) -> ComposeResult:
        with Horizontal(id="heading"):
            yield Static("DDH / loading workspace", id="titlebar", markup=False)
            yield Button("● LOCAL", id="connection")
        with Horizontal(id="stages"):
            for index, stage in enumerate(STAGES, 1):
                yield Button(f"{index} {stage.title()}", id=f"stage-{stage}")
            yield Button("Review →", id="review-gate")
        with Horizontal(id="toolbar"):
            yield Button("Run pipeline", id="run", variant="primary")
            yield Button("Metadata only", id="metadata")
            yield Button("Cancel run", id="cancel-run")
            yield Button("Compare images", id="compare-images")
        with TabbedContent(id="workspace"):
            with TabPane("1 Pipeline", id="pipeline"):
                with Horizontal(id="pipeline-layout"):
                    with Vertical(id="file-pane", classes="pane"):
                        yield Static("FILES / indexed collection", classes="pane-heading", markup=False)
                        yield Tree("Collection", id="files")
                    with VerticalScroll(id="inspector-pane", classes="pane"):
                        yield Static("CURRENT FILE / EVIDENCE", classes="pane-heading", markup=False)
                        yield Static("Start a scan or open an existing session.", id="inspector-text", markup=False)
                        yield create_image_preview(None, id="inspector-image", capability=self.image_capability)
                        with Horizontal(id="inspector-actions"):
                            yield Button("Open image [i]", id="open-image")
                            yield Button("Compare [c]", id="compare-selected")
            with TabPane("2 Review", id="review"):
                with Horizontal(classes="actions"):
                    yield Button("Approve [a]", id="approve")
                    yield Button("Reject [r]", id="reject")
                    yield Button("Edit [e]", id="edit")
                    yield Button("Approve clear", id="approve-clear")
                    yield Button("Preview [p]", id="preview", variant="primary")
                with Horizontal(id="review-layout"):
                    yield ReviewTable(id="review-table", cursor_type="row", classes="pane")
                    with VerticalScroll(id="review-evidence", classes="pane"):
                        yield Static("Select a proposal to inspect its evidence.", id="review-detail", markup=False)
            with TabPane("3 Collections", id="collections"):
                with Horizontal(id="collections-layout"):
                    yield DataTable(id="collections-table", cursor_type="row", classes="pane")
                    yield Static("Related files appear here after Relate.", id="collection-detail", markup=False, classes="pane")
            with TabPane("4 History", id="history"):
                with Horizontal(classes="actions"):
                    yield Button("Preview undo", id="undo")
                yield DataTable(id="history-table", cursor_type="row", classes="pane")
        with Horizontal(id="page-controls"):
            yield Static("", id="page-label", markup=False)
            yield Button("← Previous", id="page-previous")
            yield Button("Next →", id="page-next")
        yield RichLog(id="activity", highlight=False, markup=False, wrap=True, max_lines=100)
        yield Static("No files changed by opening this workspace.", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        for selector, columns in {
            "#collections-table": ("Collection", "Files"), "#history-table": ("Operation", "State", "Details"),
        }.items():
            self.query_one(selector, DataTable).add_columns(*columns)
        self.query_one("#inspector-image").display = False
        self.call_after_refresh(self.update_review_table)
        self.reload_theme()
        self.refresh_snapshot()
        self.set_interval(1.5, self.refresh_snapshot)
        self.set_interval(2.0, self.reload_theme)
        self.query_one("#run", Button).focus()
        self.screen.set_class(self.size.height < 32, "compact")
        self.screen.set_class(self.size.width < 110, "narrow")
        self.update_stages()
        self.update_connection()
        if self._discover:
            self.action_nearby()
        elif self.service is None:
            self.action_sessions()

    def on_resize(self, event) -> None:
        # Keep the workspace responsive even when a comparison modal is open.
        workspace_screen = self.screen_stack[0]
        workspace_screen.set_class(event.size.height < 32, "compact")
        workspace_screen.set_class(event.size.width < 110, "narrow")
        if self.is_mounted:
            self.update_stages()
            self.update_title()

    def reload_theme(self) -> None:
        from .theme import load_palette, theme_fingerprint
        fingerprint = theme_fingerprint()
        if fingerprint == self._theme_fingerprint:
            return
        palette = load_palette()
        colors = palette.colors
        self._theme_revision = 1 - self._theme_revision
        theme_name = f"ddh-omarchy-{self._theme_revision}"
        self.register_theme(Theme(name=theme_name, primary=colors["accent"], foreground=colors["foreground"],
                                  background=colors["background"], surface=colors["background"], panel=colors["selection"],
                                  accent=colors["accent"], success=colors["green"], warning=colors["yellow"], error=colors["red"], dark=palette.dark))
        self.theme = theme_name
        self._theme_fingerprint = fingerprint

    @work(group="snapshot", exit_on_error=False)
    async def refresh_snapshot(self) -> None:
        if self._refreshing or self._choosing_connection or self.service is None or time.monotonic() < self._next_refresh_at:
            return
        self._refreshing = True
        generation = self._generation
        try:
            snapshot = await asyncio.to_thread(self.service.snapshot, limit=self.page_limit, offset=self.offset)
        except Exception as exc:
            if generation != self._generation or not self.is_mounted:
                return
            if self.remote_connection:
                self._connection_failures += 1
                self._next_refresh_at = time.monotonic() + min(30, 1.5 * 2 ** min(self._connection_failures, 5))
                message = f"Workstation unavailable; showing last received state. {exc}"
                if message != self._connection_error:
                    self._connection_error = message
                    self.report(message)
                self.update_connection()
                self.update_controls()
            else:
                self.report(f"Cannot refresh workspace: {exc}", error=True)
            return
        finally:
            self._refreshing = False
        if generation == self._generation and self.is_mounted:
            self._next_refresh_at = 0
            self._connection_failures = 0
            if self._connection_error:
                self.report("Reconnected. Workspace refreshed from the workstation.")
                self._connection_error = ""
            self.update_snapshot(snapshot)

    def report(self, message: str, *, error: bool = False) -> None:
        self.query_one("#status", Static).update(message)
        self.query_one("#activity", RichLog).write(Text(message, style="red" if error else None))
        if error:
            self.notify(message, severity="error", timeout=8)

    def operate(self, method: str, *args: Any, completed: Callable | None = None, **kwargs: Any) -> None:
        if self.service is None:
            return
        if self.remote_blocked:
            self.notify("Connection unavailable or a command outcome is pending. Open Connection [F2] for details.", severity="warning")
            return
        if not self.storage_available and method not in {"pause_pipeline", "cancel_pipeline", "history"}:
            self.notify("Reconnect the workstation collection drive before making changes.", severity="warning")
            return
        if self.busy:
            self.notify("Wait for the current operation to finish.")
            return
        self.busy = True
        self._generation += 1
        self.update_controls()
        self._operation(method, args, kwargs, completed)

    @work(group="operation", exit_on_error=False)
    async def _operation(self, method: str, args: tuple, kwargs: dict, completed: Callable | None) -> None:
        try:
            result = await asyncio.to_thread(getattr(self.service, method), *args, **kwargs)
            if self.is_mounted:
                summary = method.replace("_", " ").capitalize()
                if isinstance(result, dict):
                    counts = [f"{key.replace('_', ' ')}: {value}" for key, value in result.items() if isinstance(value, int) and not isinstance(value, bool) and key not in {"id", "file_id"}]
                    summary += (" · " + ", ".join(counts)) if counts else " complete"
                self.report(summary)
                if completed:
                    completed(result)
        except Exception as exc:
            if self.is_mounted:
                self.report(str(exc), error=True)
                self._quitting = False
        finally:
            self.busy = False
            if self.is_mounted:
                self.update_controls()
                self.update_connection()
                self.refresh_snapshot()

    def _table(self, selector: str, rows: list[tuple[str, tuple]], signature: Any,
               *, columns: tuple[tuple[str, int], ...] | None = None) -> None:
        encoded = json.dumps([signature, columns], sort_keys=True, default=str)
        if self._signatures.get(selector) == encoded:
            return
        table = self.query_one(selector, DataTable)
        old_row = table.cursor_row
        old_key = str(self.selected_proposal_id) if selector == "#review-table" else str(self.selected_collection_id) if selector == "#collections-table" else None
        table.clear(columns=columns is not None)
        if columns is not None:
            for label, width in columns:
                table.add_column(label, width=width)
        for key, cells in rows:
            table.add_row(*(Text(readable(cell), no_wrap=True, overflow="ellipsis") for cell in cells), key=key)
        if rows:
            selected_row = next((index for index, (key, _) in enumerate(rows) if key == old_key), min(old_row, len(rows) - 1))
            table.move_cursor(row=selected_row, animate=False)
        self._signatures[selector] = encoded

    def relative_display_path(self, value: str | None) -> str:
        if not value:
            return "—"
        root = (self.snapshot.get("session") or {}).get("root_path")
        if root:
            try:
                return str(self.display_path(value).relative_to(self.display_path(root)))
            except ValueError:
                pass
        return value

    def update_review_table(self) -> None:
        if not self.is_mounted:
            return
        proposals = self.snapshot.get("proposals", [])
        table = self.query_one("#review-table", DataTable)
        labels = {"mark_duplicate": "Duplicate", "rename_folder": "Folder rename"}
        types = [labels.get(item.get("proposal_type"), item.get("proposal_type") or "—") for item in proposals]
        type_width = max([4, *(Text(value).cell_len for value in types)])
        decision_width = max([8, *(Text(item.get("status") or "—").cell_len for item in proposals)])
        path_width = max(1, table.content_size.width - type_width - decision_width
                         - 6 * table.cell_padding - table.scrollbar_size_vertical)
        rows = [(str(item["id"]),
                 (f"{self.relative_display_path(item.get('current_path'))} → {self.relative_display_path(item.get('proposed_path'))}",
                  proposal_type, item.get("status")))
                for item, proposal_type in zip(proposals, types)]
        self._table("#review-table", rows, proposals,
                    columns=(("Current → proposed", path_width), ("Type", type_width), ("Decision", decision_width)))

    def update_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        session = snapshot.get("session") or {}
        self.update_title()
        self.update_connection()
        files = snapshot.get("files", [])
        if not any(str(item["id"]) == str(self.selected_file_id) for item in files):
            self.selected_file_id = None
        self.update_file_tree(files, session.get("root_path", ""))
        if self.selected_file_id is None and files:
            self.selected_file_id = files[0]["id"]
        proposals = snapshot.get("proposals", [])
        if not any(str(item["id"]) == str(self.selected_proposal_id) for item in proposals):
            self.selected_proposal_id = proposals[0]["id"] if proposals else None
        self.update_review_table()
        collections = snapshot.get("collections", [])
        if not any(str(item["id"]) == str(self.selected_collection_id) for item in collections):
            self.selected_collection_id = None
            self.query_one("#collection-detail", Static).update("Related files appear here after Relate.")
        self._table("#collections-table", [(str(item["id"]), (item.get("label"), item.get("member_count", len(item.get("members", []))))) for item in collections], collections)
        history = snapshot.get("history", [])
        self._table("#history-table", [(str(index), (item.get("operation", "Operation"), item.get("state", ""), f"{item.get('source', '')} → {item.get('destination', '')}")) for index, item in enumerate(history)], history)
        self.update_inspector()
        self.update_review_detail()
        self.update_stages()
        self.update_controls()
        page = snapshot.get("page") or {}
        counts = snapshot.get("counts") or {}
        paged = self.offset > 0 or page.get("files_truncated") or page.get("proposals_truncated") or page.get("collections_truncated")
        self.query_one("#page-controls").display = bool(paged)
        shown = max(len(files), len(proposals), len(collections))
        self.query_one("#page-label", Static).update(f"Rows {self.offset + 1}–{self.offset + shown} · {counts.get('files', len(files))} files · {counts.get('proposals', len(proposals))} proposals · {counts.get('collections', len(collections))} collections")
        self.query_one("#page-previous", Button).disabled = self.offset == 0 or self.busy
        self.query_one("#page-next", Button).disabled = not (page.get("files_truncated") or page.get("proposals_truncated") or page.get("collections_truncated")) or self.busy
        job = snapshot.get("active_job") or snapshot.get("job") or {}
        message = json.dumps({"state": job.get("state"), "progress": job.get("progress"), "error": job.get("error")}, sort_keys=True, default=str) if job else ""
        if message and message != self._last_job_message:
            self._last_job_message = message
            self.report(self.job_summary(job), error=bool(job.get("error")))

    def update_title(self) -> None:
        session = self.snapshot.get("session") or {}
        heading = Text(f"DDH / {session.get('root_path', '')} · {session.get('model', '')}")
        heading.truncate(max(1, self.query_one("#titlebar").size.width - 2), overflow="ellipsis")
        heading.append(f"\nSession {session.get('id', getattr(self.service, 'session_id', ''))} · {session.get('status', 'active')}")
        self.query_one("#titlebar", Static).update(heading)

    def update_connection(self) -> None:
        if not self.is_mounted:
            return
        connection = self.remote_connection
        button = self.query_one("#connection", Button)
        if connection is None:
            button.label = "● LOCAL"
            button.tooltip = "Connection and session settings [F2]"
        else:
            pending = bool(connection.pending_request_id)
            state = "COMMAND PENDING" if pending else connection.state.replace("_", " ").upper()
            if not pending and connection.state == "connected":
                if self._connection_error:
                    state = "STALE"
                elif not self.storage_available:
                    state = "DRIVE MISSING"
            button.label = Text(f"{'●' if connection.state == 'connected' else '○'} {connection.name} · {state}")
            button.tooltip = f"{state} · Connection and session settings [F2]"
        button.set_class(self.remote_blocked or not self.storage_available, "disconnected")

    def action_connection(self) -> None:
        self.push_screen(ConnectionScreen(self), self.connection_choice)

    def connection_choice(self, values):
        if values == {"nearby": True}:
            self.action_nearby()
        elif values:
            self.operate("update_settings", **values)

    def action_nearby(self):
        job = self.snapshot.get("active_job") or {}
        local_running = not getattr(self.service, "is_remote", False) and (self.snapshot.get("has_live_workers") or job.get("state") in {"running", "paused", "cancelling"})
        if self.busy or self._quitting or self._reconnecting or local_running:
            self.notify("Wait for the current operation, or stop the local pipeline, before changing workstations.")
            return
        try:
            if self._nearby_manager is None:
                from donedatahoarder.remote.nearby import NearbyManager
                self._nearby_manager = NearbyManager()
        except (ValueError, OSError, ImportError) as exc:
            self.notify(str(exc), severity="error")
            return
        from .discovery_screens import NearbyScreen
        self._choosing_connection = True
        self.push_screen(NearbyScreen(self._nearby_manager, initial=self.service is None,
                                      active_server_id=getattr(self.remote_connection, "server_id", None)), self.use_nearby)

    @work(group="nearby-open", exit_on_error=False)
    async def use_nearby(self, connection):
        if connection is None:
            self._choosing_connection = False
            if self.service is None:
                self.exit()
            return
        from donedatahoarder.remote.client import RemoteSessionCatalog
        old_connection = self.remote_connection
        self._owned_connections.append(connection)
        try:
            catalog = RemoteSessionCatalog(connection)
            service = None
            if self._remote_root is not None or self._remote_session:
                service = await asyncio.to_thread(catalog.open, root=self._remote_root, session_id=self._remote_session)
            self._generation += 1
            self.service, self.catalog = None, catalog
            self.snapshot = {}
            self._signatures.clear()
            self.selected_file_id = self.selected_proposal_id = self.selected_collection_id = None
            self.offset = 0
            self._next_refresh_at = 0
            self._connection_failures = 0
            self._connection_error = ""
            self.query_one("#inspector-image").set_source(None)
            self.query_one("#inspector-image").display = False
            self.query_one("#inspector-text", Static).update("Choose a workstation collection.")
            self.update_snapshot({})
            self._remote_root = self._remote_session = None
            if old_connection is not None:
                old_connection.close()
            self._choosing_connection = False
            if service is not None:
                self.open_session(service)
            else:
                self.action_sessions()
        except Exception as exc:
            connection.close()
            self.report(f"Cannot open workstation: {exc}", error=True)
        finally:
            self._choosing_connection = False

    def on_unmount(self):
        for connection in self._owned_connections:
            connection.close()
        if self._nearby_manager is not None:
            self._nearby_manager.close()

    @work(group="reconnect", exit_on_error=False)
    async def reconnect_remote(self) -> None:
        connection = self.remote_connection
        if connection is None or self._reconnecting:
            return
        self._reconnecting = True
        try:
            await asyncio.to_thread(connection.connect)
            self._next_refresh_at = 0
            self.refresh_snapshot()
        except Exception as exc:
            self.report(f"Cannot connect: {exc}")
        finally:
            self._reconnecting = False
            self.update_connection()
            self.update_controls()

    @staticmethod
    def job_summary(job: dict) -> str:
        progress = job.get("progress") or {}
        details = [f"{key.replace('_', ' ')} {value}" for key, value in progress.items() if isinstance(value, (int, float)) and not isinstance(value, bool)]
        for key in ("message", "current_file", "filename", "file", "phase"):
            if progress.get(key):
                details.append(str(progress[key]))
        if job.get("error"):
            details.append(str(job["error"]))
        phase = str(job.get("job_type", "Pipeline")).replace("execute_dry", "Preview").replace("_", " ").capitalize()
        return f"{phase} · {job.get('state', '')}" + (" · " + " · ".join(details) if details else "")

    def update_file_tree(self, files: list[dict], root_path: str) -> None:
        signature = json.dumps([(file["id"], file.get("path"), file.get("status"), file.get("size_bytes")) for file in files])
        if self._signatures.get("#files") == signature:
            return
        tree = self.query_one("#files", Tree)
        expanded = set()
        def remember(node):
            if node.data and node.data.get("folder") and node.is_expanded:
                expanded.add(node.data["folder"])
            for child in node.children:
                remember(child)
        remember(tree.root)
        first = "#files" not in self._signatures
        tree.root.remove_children()
        total = self.snapshot.get("counts", {}).get("files", len(files))
        tree.root.set_label(Text(f"{self.display_path(root_path).name or 'Collection'}/  {len(files)} of {total} files", style="bold"))
        tree.root.expand()
        folders = {"": tree.root}
        selected = None
        for file in files:
            try:
                parent = self.display_path(file["path"]).parent.relative_to(self.display_path(root_path))
                parts = parent.parts
            except ValueError:
                parts = ("Outside root — review required",)
            folder_key = ""
            node = tree.root
            for part in parts:
                folder_key += "/" + part
                if folder_key not in folders:
                    folders[folder_key] = node.add(Text(part + "/", style="green"), data={"folder": folder_key}, expand=first or folder_key in expanded)
                node = folders[folder_key]
            leaf = node.add_leaf(Text(f"{file.get('filename')}  {human_size(file.get('size_bytes'))}  {file.get('status', '')}"), data={"file_id": file["id"]})
            if str(file["id"]) == str(self.selected_file_id):
                selected = leaf
        if selected and not self.inspected_stage:
            self.call_after_refresh(tree.move_cursor, selected)
        self._signatures["#files"] = signature

    @on(Tree.NodeHighlighted, "#files")
    def tree_file_selected(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data or {}
        if "file_id" in data:
            self.selected_file_id = data["file_id"]
            self.inspected_stage = None
            self.update_inspector()
            self.update_controls()

    def update_stages(self) -> None:
        narrow = self.screen_stack[0].has_class("narrow")
        plan = self.snapshot.get("plan") or {}
        steps = ["preview" if step == "execute_dry" else step for step in plan.get("steps") or []]
        index = int(plan.get("current_index") or 0)
        completed = set(steps[:index])
        job = self.snapshot.get("active_job") or {}
        active = job.get("job_type") or (steps[index] if index < len(steps) and plan.get("state") == "running" else None)
        if active == "execute_dry":
            active = "preview"
        skipped = {"preview" if step == "execute_dry" else step for step in (plan.get("options") or {}).get("skipped_steps", [])}
        for stage in STAGES:
            button = self.query_one(f"#stage-{stage}", Button)
            button.set_class(stage in completed, "complete")
            button.set_class(stage == active, "running")
            prefix = '✓' if stage in completed else '▶' if stage == active else '−' if stage in skipped else '·'
            button.label = stage.title() if narrow else f"{prefix} {stage.title()}"
            button.tooltip = f"{stage.title()}: {'completed' if stage in completed else 'running' if stage == active else 'skipped' if stage in skipped else 'waiting'}"
        self.query_one("#review-gate", Button).label = "Review" if narrow else "Review →"

    def update_controls(self) -> None:
        if not self.is_mounted:
            return
        job = self.snapshot.get("active_job") or {}
        live = job.get("state") in {"running", "paused", "cancelling"}
        blocked = live or self.snapshot.get("other_session_busy") or self.snapshot.get("has_live_workers")
        unavailable = self.remote_blocked or self.service is None
        blocked = blocked or unavailable or not self.storage_available
        plan = self.snapshot.get("plan") or {}
        retry = plan.get("state") == "failed" or (plan.get("state") in {"interrupted", "cancelled"} and self.snapshot.get("counts", {}).get("file_statuses", {}).get("error", 0))
        self.query_one("#run", Button).label = "Pause" if job.get("state") == "running" else "Retry run" if retry else "Resume" if plan and plan.get("state") != "completed" else "Run pipeline"
        self.query_one("#run", Button).disabled = self.busy or unavailable or job.get("state") == "cancelling" or (not self.storage_available and job.get("state") != "running")
        self.query_one("#metadata", Button).disabled = bool(self.busy or blocked)
        self.query_one("#cancel-run", Button).disabled = self.busy or unavailable or not live
        proposal = self.selected_proposal()
        for selector in ("#approve", "#reject", "#edit"):
            self.query_one(selector, Button).disabled = bool(self.busy or blocked or not proposal or proposal.get("status") == "applied")
        self.query_one("#approve-clear", Button).disabled = bool(self.busy or blocked or not self.snapshot.get("proposals"))
        self.query_one("#preview", Button).disabled = bool(self.busy or blocked or not self.snapshot.get("proposals"))
        self.query_one("#undo", Button).disabled = bool(self.busy or blocked or not self.snapshot.get("history"))
        image = self.selected_file()
        for selector in ("#open-image", "#compare-selected"):
            self.query_one(selector, Button).disabled = not self.is_image(image) or not self.storage_available
        self.query_one("#compare-images", Button).disabled = not self.storage_available

    def selected_file(self) -> dict | None:
        selected_id = self.selected_file_id
        if self.is_mounted and self.query_one("#workspace", TabbedContent).active == "review":
            proposal = self.selected_proposal()
            if proposal:
                selected_id = proposal["file_id"]
        file = next((item for item in self.snapshot.get("files", []) if str(item["id"]) == str(selected_id)), None)
        if file is None:
            proposal = next((item for item in self.snapshot.get("proposals", []) if str(item["file_id"]) == str(selected_id)), None)
            if proposal and proposal.get("file_path"):
                quality = (proposal.get("duplicate_evidence") or {}).get("photo_quality") or {}
                file = {"id": proposal["file_id"], "path": proposal["file_path"], "filename": proposal.get("filename"), "mime_type": proposal.get("mime_type"), "status": proposal.get("status"), "preview_revision": proposal.get("preview_revision"), "photo_metadata": proposal.get("photo_metadata") or quality.get("candidate")}
        return file

    def selected_proposal(self) -> dict | None:
        return next((item for item in self.snapshot.get("proposals", []) if str(item["id"]) == str(self.selected_proposal_id)), None)

    @staticmethod
    def is_image(file: dict | None) -> bool:
        return bool(file and (str(file.get("mime_type") or "").startswith("image/") or Path(file.get("path", "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}))

    def update_inspector(self) -> None:
        if self.inspected_stage:
            self.query_one("#inspector-image").display = False
            plan = self.snapshot.get("plan") or {}
            done = plan.get("completed_steps") or []
            self.query_one("#inspector-text", Static).update(f"{self.inspected_stage.upper()}\n\nRun: {plan.get('state', 'not started')}\nCompleted: {', '.join(done) or 'none yet'}\n\n{self.job_summary(self.snapshot.get('job') or {})}\n\nSelect a file to return to its evidence.")
            return
        file = self.selected_file()
        if not file:
            self.query_one("#inspector-text", Static).update("No indexed file selected. Run the pipeline or choose Metadata only to get started.")
            image = self.query_one("#inspector-image")
            image.set_source(None)
            image.display = False
            self._image_path = None
            return
        text = f"{file.get('filename')}\n{file.get('path')}\n\nType: {file.get('mime_type') or 'unknown'}\nSize: {human_size(file.get('size_bytes'))}\nState: {file.get('status')}\n\n{file.get('ai_description') or file.get('text') or 'No extracted description yet.'}"
        if self.is_image(file):
            text = f"{file.get('filename')}\n{file.get('path')}\n\nPHOTO EVIDENCE\n{photo_metadata_summary(file.get('photo_metadata'))}\n\n" + text.split("\n\n", 1)[1]
        self.query_one("#inspector-text", Static).update(text[:5000])
        image = self.query_one("#inspector-image")
        image.display = self.is_image(file) and self.storage_available
        if image.display:
            # The widget skips unchanged identities itself. Keep passing the
            # source so edits or replacements at the same path are refreshed.
            self._image_path = file["path"]
            image.set_source(self.preview_source(file))
        elif not self.storage_available:
            image.set_source(None)

    def update_review_detail(self) -> None:
        proposal = self.selected_proposal()
        if proposal:
            detail = f"{proposal.get('proposal_type', '').upper()} · {proposal.get('status', '')}\n\n{proposal.get('current_path', '')}\n→ {proposal.get('proposed_path', '')}\n\n{proposal.get('reasoning') or 'No explanation recorded.'}"
            if proposal.get("protection_reason"):
                detail += f"\n\nPROTECTED: {proposal['protection_reason']}"
            if proposal.get("duplicate_evidence"):
                detail += "\n\nDUPLICATE EVIDENCE\n" + duplicate_summary(proposal["duplicate_evidence"])
            self.query_one("#review-detail", Static).update(detail[:7000])
        else:
            self.query_one("#review-detail", Static).update("Select a proposal to inspect its evidence.")

    @on(DataTable.RowHighlighted)
    def row_selected(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if event.data_table.id == "files":
            self.selected_file_id = key
            self.inspected_stage = None
            self.update_inspector()
        elif event.data_table.id == "review-table":
            self.selected_proposal_id = key
            self.update_review_detail()
        elif event.data_table.id == "collections-table":
            item = next((item for item in self.snapshot.get("collections", []) if str(item["id"]) == key), None)
            if item:
                self.selected_collection_id = key
                self.query_one("#collection-detail", Static).update(f"{item.get('label')}\n\n{item.get('reason') or ''}\n\n" + "\n".join(f"{member.get('filename')} · {member.get('role', 'related')}" for member in item.get("members", [])))
                if item.get("members_truncated"):
                    self.query_one("#collection-detail", Static).update(f"{item.get('label')}\nShowing {len(item.get('members', []))} of {item.get('member_count')} members\n\n{item.get('reason') or ''}\n\n" + "\n".join(f"{member.get('filename')} · {member.get('role', 'related')}" for member in item.get("members", [])))
        self.update_controls()

    @on(TabbedContent.TabActivated, "#workspace")
    def workspace_changed(self) -> None:
        self.update_controls()
        self.call_after_refresh(self.update_review_table)

    def action_workspace(self, name: str) -> None:
        self.query_one("#workspace", TabbedContent).active = name

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # App shortcuts must not mutate a workspace hidden by a dialog. Inputs
        # and modal-specific bindings (e.g. image zoom) remain owned by screens.
        if isinstance(self.screen, ModalScreen) and action != "safe_quit":
            return False
        if self.service is None and action not in {"sessions", "help", "safe_quit", "connection"}:
            return False
        return True

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_sessions(self) -> None:
        if self.catalog is None:
            self.notify("Reopen with ddh tui to choose another collection.")
            return
        job = self.snapshot.get("active_job") or {}
        local_running = not getattr(self.service, "is_remote", False) and (self.snapshot.get("has_live_workers") or job.get("state") in {"running", "paused", "cancelling"})
        if self.busy or self._quitting or local_running:
            self.notify("Stop the current pipeline and wait for its worker before switching sessions.")
            return
        self.push_screen(SessionScreen(self.catalog, initial=self.service is None), self.open_session)

    def open_session(self, service: Any) -> None:
        if service is None:
            if self.service is None:
                self.exit()
            return
        self._generation += 1
        self.service = service
        self.snapshot = {}
        self.offset = 0
        self.selected_file_id = self.selected_proposal_id = self.selected_collection_id = None
        self._signatures.clear()
        self._image_path = None
        self._last_job_message = ""
        self._next_refresh_at = 0
        self._connection_failures = 0
        self._connection_error = ""
        self.inspected_stage = None
        self.query_one("#inspector-image").set_source(None)
        self.query_one("#inspector-image").display = False
        self.query_one("#inspector-text", Static).update("Ready. Run the pipeline or choose Metadata only to get started.")
        self.query_one("#review-detail", Static).update("Select a proposal to inspect its evidence.")
        self.query_one("#collection-detail", Static).update("Related files appear here after Relate.")
        self.query_one("#activity", RichLog).clear()
        self.action_workspace("pipeline")
        self.update_snapshot({})
        self.report("Workspace opened. Processing starts only when you run it.")
        self.refresh_snapshot()
        self.query_one("#run", Button).focus()

    def action_next_file(self) -> None:
        table = self.focused if isinstance(self.focused, (DataTable, Tree)) else self.query_one("#files", Tree)
        table.action_cursor_down()

    def action_previous_file(self) -> None:
        table = self.focused if isinstance(self.focused, (DataTable, Tree)) else self.query_one("#files", Tree)
        table.action_cursor_up()

    def action_run_pause(self) -> None:
        state = (self.snapshot.get("active_job") or {}).get("state")
        if state == "running":
            self.operate("pause_pipeline")
        elif self.snapshot.get("plan") and self.snapshot["plan"].get("state") != "completed":
            plan = self.snapshot["plan"]
            retry = plan.get("state") == "failed" or (plan.get("state") in {"interrupted", "cancelled"} and self.snapshot.get("counts", {}).get("file_statuses", {}).get("error", 0))
            self.operate("resume_pipeline", retry_errors=bool(retry))
        else:
            self.operate("start_pipeline")

    def action_metadata(self) -> None:
        self.operate("start_pipeline", metadata_only=True)

    def action_approve(self) -> None:
        if self.query_one("#workspace", TabbedContent).active != "review":
            return
        proposal = self.selected_proposal()
        if proposal:
            self.operate("approve", proposal["id"], review_token=proposal.get("review_token"))

    def action_reject(self) -> None:
        if self.query_one("#workspace", TabbedContent).active != "review":
            return
        proposal = self.selected_proposal()
        if proposal:
            self.operate("reject", proposal["id"])

    def action_edit(self) -> None:
        if self.query_one("#workspace", TabbedContent).active != "review":
            return
        proposal = self.selected_proposal()
        if proposal:
            proposal_id = proposal["id"]
            value = proposal.get("proposed_path") or ""
            if proposal.get("proposal_type") == "rename":
                value = self.display_path(value).name
            self.push_screen(EditScreen(value), lambda value: self.operate("edit", proposal_id, value) if value is not None else None)

    @staticmethod
    def preview_text(preview: dict) -> str:
        return "\n\n".join(f"{item.get('type', 'Change').upper()}\n{item.get('source', '')}\n→ {item.get('destination', '')}" + (f"\nKeep: {item['keeper']}" if item.get("keeper") else "") + (f"\nERROR: {item['error']}" if item.get("error") else "") for item in preview.get("items", [])) or "No operations in this plan."

    def action_preview(self) -> None:
        def show(preview: dict) -> None:
            if preview.get("errors"):
                self.push_screen(ConfirmScreen("Resolve preview errors before applying", self.preview_text(preview), "Apply blocked", confirm_enabled=False))
                return
            if not preview.get("total", len(preview.get("items", []))):
                self.report("Approve a proposal before previewing changes.")
                return
            token = preview["token"]
            target = f" on {self.remote_connection.name}" if self.remote_connection else ""
            self.push_screen(ConfirmScreen(f"Review filesystem changes{target}", self.preview_text(preview)), lambda confirmed: self.operate("apply", token, confirmed=True) if confirmed else None)
        self.operate("preview", completed=show)

    def action_undo(self) -> None:
        def show(preview: dict) -> None:
            if preview.get("errors"):
                self.push_screen(ConfirmScreen("Resolve recovery errors before undoing", self.preview_text(preview), "Undo blocked", confirm_enabled=False))
                return
            if not preview.get("total", len(preview.get("items", []))):
                self.report("No applied operations available to undo.")
                return
            self.push_screen(ConfirmScreen("Review undo operations", self.preview_text(preview), "Undo changes"), lambda confirmed: self.operate("undo", preview["token"], confirmed=True) if confirmed else None)
        self.operate("undo_preview", completed=show)

    def image_candidates(self, file: dict) -> tuple[list[dict], str]:
        candidates: dict[str, dict] = {}
        evidence: list[str] = []
        proposal = self.selected_proposal() if self.query_one("#workspace", TabbedContent).active == "review" else None
        proposal_evidence = (proposal or {}).get("duplicate_evidence") if proposal and str(proposal.get("file_id")) == str(file["id"]) else None
        preferred_group = (proposal_evidence or {}).get("group_id")
        for group in self.snapshot.get("duplicates", []):
            if preferred_group is not None and str(group.get("id")) != str(preferred_group):
                continue
            members = group.get("members", [])
            if any(str(item.get("id", item.get("file_id"))) == str(file["id"]) for item in members):
                selected_member = next(item for item in members if str(item.get("id", item.get("file_id"))) == str(file["id"]))
                for member in members:
                    member_id = member.get("id", member.get("file_id"))
                    candidate = next((item for item in self.snapshot.get("files", []) if str(item["id"]) == str(member_id)), member)
                    if str(member_id) != str(file["id"]) and self.is_image(candidate):
                        keeper = group.get("keep_file_id")
                        pair_member = member if str(file["id"]) == str(keeper) else selected_member if str(member_id) == str(keeper) else None
                        pair_evidence = {"type": group.get("type"), "keeper_path": group.get("keeper_path")}
                        if pair_member:
                            pair_evidence.update({key: pair_member.get(key) for key in ("exact_bytes", "matching_indexed_md5", "distance_to_keeper", "photo_quality")})
                        elif member.get("exact_bytes") is True and selected_member.get("exact_bytes") is True:
                            pair_evidence["exact_bytes"] = True
                        summary = duplicate_summary(pair_evidence)
                        candidates[str(member_id)] = {**candidate, "photo_metadata": candidate.get("photo_metadata") or member.get("photo_metadata"), "comparison_evidence": summary, "comparison_group_id": group["id"], "comparison_keeper_id": keeper}
                        evidence.append(summary)
        if not candidates and proposal_evidence and proposal_evidence.get("keeper_path"):
            candidate = {"id": proposal_evidence.get("keeper_id"), "path": proposal_evidence["keeper_path"],
                         "filename": self.display_path(proposal_evidence["keeper_path"]).name,
                         "mime_type": proposal_evidence.get("keeper_mime_type"),
                         "preview_revision": proposal_evidence.get("keeper_preview_revision"),
                         "photo_metadata": (proposal_evidence.get("photo_quality") or {}).get("keeper"),
                         "size_bytes": proposal_evidence.get("keeper_size_bytes")}
            if self.is_image(candidate) and str(candidate["id"]) != str(file["id"]):
                summary = duplicate_summary(proposal_evidence)
                candidates[str(candidate["id"])] = {**candidate, "comparison_evidence": summary, "comparison_group_id": proposal_evidence.get("group_id"), "comparison_keeper_id": proposal_evidence.get("keeper_id")}
                evidence.append(summary)
        return list(candidates.values()), evidence[0] if evidence else ""

    def action_image(self) -> None:
        if not self.storage_available:
            self.notify("Reconnect the workstation collection drive to preview images.")
            return
        file = self.selected_file()
        if self.is_image(file):
            self.push_screen(ImageScreen(file, [], capability=self.image_capability, source_factory=self.preview_source, remote=bool(self.remote_connection)))
        else:
            self.notify("Select an image in the file list first.")

    def action_compare(self) -> None:
        if not self.storage_available:
            self.notify("Reconnect the workstation collection drive to compare images.")
            return
        file = self.selected_file()
        if not self.is_image(file):
            self.notify("Select an image in the file list first.")
            return
        candidates, evidence = self.image_candidates(file)
        if not candidates:
            self.notify("No duplicate image candidates recorded. Run the Dedup stage first.")
            self.push_screen(ImageScreen(file, [], capability=self.image_capability, source_factory=self.preview_source, remote=bool(self.remote_connection)))
            return
        self.push_screen(ImageScreen(file, candidates, capability=self.image_capability, evidence=evidence, source_factory=self.preview_source, remote=bool(self.remote_connection), keeper_callback=self.choose_photo_keeper if hasattr(self.service, "set_keeper") else None))

    def choose_photo_keeper(self, group_id: int, file_id: int, *, expected_keeper_id: int | None = None) -> None:
        self.operate("set_keeper", group_id, file_id, expected_keeper_id=expected_keeper_id,
                     completed=lambda _: self.action_workspace("review"))

    def action_safe_quit(self) -> None:
        if self.remote_connection and not self.busy and not any(isinstance(screen, SessionScreen) and screen.opening for screen in self.screen_stack):
            self.exit()
            return
        if any(isinstance(screen, SessionScreen) and screen.opening for screen in self.screen_stack):
            self.notify("Wait for the workspace to finish opening before quitting.")
            return
        if self.busy:
            self.notify("Wait for the current filesystem operation to finish before quitting.")
            return
        job = self.snapshot.get("active_job") or {}
        if job.get("state") in {"running", "paused", "cancelling"}:
            self.push_screen(ConfirmScreen("Stop safely and quit?", "The current run will stop at a checkpoint. Saved results and review decisions remain available when you reopen this session.", "Stop and quit"), self._confirm_quit)
        elif self.snapshot.get("owned_live_workers"):
            self._quitting = True
            self._wait_for_shutdown()
        else:
            self.exit()

    def _confirm_quit(self, confirmed: bool) -> None:
        if confirmed:
            self._quitting = True
            self.operate("cancel_pipeline", completed=lambda _: self._wait_for_shutdown())

    @work(group="shutdown", exit_on_error=False)
    async def _wait_for_shutdown(self) -> None:
        self.report("Waiting for the pipeline worker to stop safely…")
        while self._quitting:
            try:
                snapshot = await asyncio.to_thread(self.service.snapshot)
            except Exception as exc:
                self.report(f"Cannot verify worker shutdown: {exc}", error=True)
                self._quitting = False
                return
            job = snapshot.get("active_job") or {}
            if job.get("state") not in {"running", "paused", "cancelling"} and not snapshot.get("owned_live_workers"):
                self.exit()
                return
            await asyncio.sleep(0.5)

    @on(Button.Pressed)
    def toolbar_button(self, event: Button.Pressed) -> None:
        actions = {"connection": self.action_connection, "run": self.action_run_pause, "metadata": self.action_metadata, "approve": self.action_approve,
                   "reject": self.action_reject, "edit": self.action_edit, "preview": self.action_preview,
                   "undo": self.action_undo, "open-image": self.action_image, "compare-selected": self.action_compare,
                   "compare-images": self.action_compare, "review-gate": lambda: self.action_workspace("review"),
                   "page-previous": lambda: self.change_page(-1), "page-next": lambda: self.change_page(1),
                   "cancel-run": lambda: self.operate("cancel_pipeline"), "approve-clear": lambda: self.operate("approve_clear")}
        if event.button.id in actions:
            actions[event.button.id]()
        elif event.button.id and event.button.id.startswith("stage-"):
            stage = event.button.id.removeprefix("stage-")
            if stage == "preview":
                self.action_preview()
            else:
                self.inspected_stage = stage
                self.update_inspector()
                self.action_workspace("pipeline")

    def change_page(self, direction: int) -> None:
        self.offset = max(0, self.offset + direction * self.page_limit)
        self.selected_file_id = self.selected_proposal_id = None
        self._generation += 1
        self.refresh_snapshot()

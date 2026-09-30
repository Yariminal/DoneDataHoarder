"""Headless keyboard and confirmation regressions for the optional terminal UI."""
import asyncio
import copy
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, Tree

from donedatahoarder.tui.app import ConfirmScreen, DDHApp, ImageScreen
from donedatahoarder.tui.onboarding_screens import HelpScreen, SessionScreen


class FakeWorkspace:
    def __init__(self, root: Path):
        self.calls = []
        self.session_id = "test-session"
        files = [{"id": index, "path": str(root / name), "filename": name,
                  "mime_type": "image/png", "size_bytes": 42, "status": "enriched",
                  "ai_description": "A real indexed image"}
                 for index, name in ((1, "original.png"), (2, "copy.png"))]
        self.data = {
            "session": {"id": self.session_id, "root_path": str(root), "backend": "ollama", "model": "test-model"},
            "files": files,
            "proposals": [{"id": 7, "file_id": 1, "proposal_type": "rename", "current_path": "original.png",
                           "proposed_path": "renamed.png", "status": "pending", "reasoning": "Readable name",
                           "review_token": "review-7"}],
            "collections": [], "history": [], "plan": None, "job": None, "active_job": None,
            "duplicates": [{"id": 9, "type": "exact", "keep_file_id": 1, "members": files}],
            "counts": {"files": 2, "proposals": 1}, "page": {},
        }

    def snapshot(self, **kwargs):
        return copy.deepcopy(self.data)

    def start_pipeline(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"job_id": "job-1"}

    def resume_pipeline(self, **kwargs):
        self.calls.append(("resume", kwargs))
        return {"job_id": "job-retry"}

    def approve(self, proposal_id, review_token=None):
        self.calls.append(("approve", proposal_id, review_token))
        self.data["proposals"][0]["status"] = "approved"
        return {"approved": 1}

    def preview(self):
        self.calls.append(("preview",))
        return {"token": "exact-preview-token", "total": 1, "errors": 0,
                "items": [{"id": 7, "type": "rename", "source": "original.png", "destination": "renamed.png"}]}

    def apply(self, token, confirmed=False):
        self.calls.append(("apply", token, confirmed))
        self.data["proposals"][0]["status"] = "applied"
        self.data["history"] = [{"operation": "rename", "source": "original.png", "destination": "renamed.png", "state": "outstanding"}]
        return {"applied": 1}

    def cancel_pipeline(self):
        self.calls.append(("cancel",))
        self.data["active_job"] = None


async def settled(pilot):
    await pilot.pause(0.2)


class FakeCatalog:
    model = "test-model"

    def __init__(self, root):
        self.root, self.calls = root, []

    def list_sessions(self):
        return [{"id": "recent-session", "root_path": str(self.root), "status": "new",
                 "model": "saved-model", "backend": "ollama"},
                {"id": "cloud-session", "root_path": str(self.root), "status": "new",
                 "model": "cloud-model", "backend": "gemini"}]

    def readiness(self, model):
        return f"Ollama unavailable · {model} · Metadata only remains available."

    def session_readiness(self, session_id):
        return f"Saved plan for {session_id} · Metadata only remains available."

    def open(self, **kwargs):
        self.calls.append(kwargs)
        return FakeWorkspace(self.root)


@pytest.mark.parametrize("size", [(80, 24), (120, 42)])
def test_startup_picker_is_read_only_and_offline_metadata_is_available(tmp_path, size):
    async def scenario():
        catalog = FakeCatalog(tmp_path)
        app = DDHApp(catalog=catalog)
        async with app.run_test(size=size) as pilot:
            await settled(pilot)
            assert isinstance(app.screen, SessionScreen)
            assert app.service is None
            assert catalog.calls == []
            assert "Metadata only" in str(app.screen.query_one("#session-readiness", Static).render())
            assert "cloud-backed" in str(app.screen.query_one("#session-message", Static).render())
            app.screen.query_one("#session-folder", Input).value = str(tmp_path)
            await pilot.click("#session-new")
            await settled(pilot)
            assert catalog.calls == [{"root": str(tmp_path), "model": "test-model"}]
            assert not isinstance(app.screen, SessionScreen)
            assert app.service.calls == []
            await pilot.press("m")
            await settled(pilot)
            assert app.service.calls == [("start", {"metadata_only": True})]
    asyncio.run(scenario())


def test_help_blocks_workspace_shortcuts_and_closes_without_side_effects(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await settled(pilot)
            await pilot.press("question_mark")
            assert isinstance(app.screen, HelpScreen)
            assert app.screen.query_one("#help-close", Button).region.bottom <= 24
            await pilot.press("m", "2", "a", "p", "c")
            assert isinstance(app.screen, HelpScreen)
            assert app.query_one("#workspace", TabbedContent).active == "pipeline"
            assert service.calls == []
            await pilot.press("escape")
            assert not isinstance(app.screen, HelpScreen)
            assert service.calls == []
            await pilot.press("f1", "space")
            assert service.calls == []
    asyncio.run(scenario())


def test_session_switch_resumes_saved_choice_and_waits_for_worker_drain(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        catalog = FakeCatalog(tmp_path)
        app = DDHApp(service, catalog=catalog)
        async with app.run_test(size=(120, 42)) as pilot:
            await settled(pilot)
            service.data["has_live_workers"] = True
            app.update_snapshot(service.snapshot())
            await pilot.press("o")
            assert not isinstance(app.screen, SessionScreen)
            service.data["has_live_workers"] = False
            app.update_snapshot(service.snapshot())
            await pilot.press("o")
            await settled(pilot)
            assert isinstance(app.screen, SessionScreen)
            app.screen.query_one("#session-recent", Select).value = "recent-session"
            await settled(pilot)
            assert "Saved plan for recent-session" in str(app.screen.query_one("#session-readiness", Static).render())
            await pilot.click("#session-resume")
            await settled(pilot)
            assert catalog.calls == [{"session_id": "recent-session"}]
            assert app.service is not service
            assert app.service.calls == []
            assert service.calls == []
    asyncio.run(scenario())


def test_navigation_does_not_start_work_and_metadata_is_explicit(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(120, 42)) as pilot:
            await settled(pilot)
            assert service.calls == []
            assert len(app.query_one("#files", Tree).root.children) == 2
            await pilot.press("2")
            assert app.query_one("#workspace", TabbedContent).active == "review"
            await pilot.press("3")
            assert app.query_one("#workspace", TabbedContent).active == "collections"
            await pilot.press("1")
            await pilot.click("#metadata")
            await settled(pilot)
            assert service.calls == [("start", {"metadata_only": True})]
    asyncio.run(scenario())


def test_apply_requires_fresh_preview_and_explicit_confirmation(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(120, 42)) as pilot:
            await settled(pilot)
            await pilot.press("2")
            await settled(pilot)
            await pilot.click("#approve")
            await settled(pilot)
            assert ("approve", 7, "review-7") in service.calls
            await pilot.click("#preview")
            await settled(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            assert app.focused.id == "confirm-cancel"
            await pilot.press("enter")
            await settled(pilot)
            assert not any(call[0] == "apply" for call in service.calls)
            await pilot.click("#preview")
            await settled(pilot)
            await pilot.click("#confirm-apply")
            await settled(pilot)
            assert ("apply", "exact-preview-token", True) in service.calls
            assert app.query_one("#history-table", DataTable).row_count == 1
    asyncio.run(scenario())


def test_image_comparison_uses_candidates_without_changing_review(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(120, 42)) as pilot:
            await settled(pilot)
            await pilot.press("c")
            await settled(pilot)
            assert isinstance(app.screen, ImageScreen)
            assert app.screen.candidate["id"] == 2
            assert app.screen.query_one("#image-a") is not None
            assert app.screen.query_one("#image-b") is not None
            await pilot.click("#image-plus")
            assert app.screen.zoom == 2
            await pilot.click("#image-right")
            assert app.screen.center[0] > 0.5
            await pilot.press("escape")
            assert not isinstance(app.screen, ImageScreen)
            assert service.calls == []
            assert service.data["proposals"][0]["status"] == "pending"
    asyncio.run(scenario())


def test_quit_waits_for_cancellation_instead_of_abandoning_worker(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        service.data["active_job"] = {"job_type": "analyze", "state": "running", "progress": {"done": 1, "total": 2}}
        app = DDHApp(service)
        async with app.run_test(size=(120, 42)) as pilot:
            await settled(pilot)
            await pilot.press("q")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click("#confirm-apply")
            await settled(pilot)
            assert ("cancel",) in service.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_compact_layout_keeps_image_actions_reachable(tmp_path, size):
    async def scenario():
        app = DDHApp(FakeWorkspace(tmp_path))
        async with app.run_test(size=size) as pilot:
            await settled(pilot)
            assert app.query_one("#files", Tree).size.height >= 3
            if size[0] == 80:
                for stage in ("scan", "enrich", "analyze", "dedup", "relate", "propose", "organize", "preview"):
                    button = app.query_one(f"#stage-{stage}", Button)
                    assert str(button.label) == stage.title()
                    assert len(str(button.label)) <= button.content_size.width
                    assert stage.title() in button.render_line(0).text
            await pilot.press("c")
            await settled(pilot)
            for identifier in ("image-close", "image-external", "image-external-b", "image-plus"):
                region = app.screen.query_one("#" + identifier, Button).region
                assert region.right <= size[0], (identifier, region)
                assert region.bottom <= size[1], (identifier, region)
            await pilot.press("escape")
    asyncio.run(scenario())


def test_resizing_with_image_modal_updates_underlying_workspace(tmp_path):
    async def scenario():
        app = DDHApp(FakeWorkspace(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            await pilot.press("c")
            await pilot.resize_terminal(80, 24)
            await pilot.press("escape")
            await settled(pilot)
            assert app.screen.has_class("narrow")
            assert app.screen.has_class("compact")
            assert str(app.query_one("#stage-organize", Button).label) == "Organize"
    asyncio.run(scenario())


def test_tree_preserves_selected_file_when_rows_reorder(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            tree = app.query_one("#files", Tree)
            tree.move_cursor(tree.root.children[1])
            await settled(pilot)
            assert str(app.selected_file_id) == "2"
            service.data["files"].reverse()
            app.update_snapshot(service.snapshot())
            await settled(pilot)
            assert str(app.selected_file_id) == "2"
            assert tree.cursor_node.data["file_id"] == 2
    asyncio.run(scenario())


def test_refresh_clears_evidence_and_pixels_when_rows_disappear(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        service.data["collections"] = [{"id": 20, "label": "Previous collection",
                                        "members": [], "reason": "Previous evidence"}]
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            assert "original.png" in str(app.query_one("#inspector-text", Static).render())
            assert "renamed.png" in str(app.query_one("#review-detail", Static).render())
            preview = app.query_one("#inspector-image")
            assert preview.source is not None
            await pilot.press("3")
            await settled(pilot)
            assert "Previous evidence" in str(app.query_one("#collection-detail", Static).render())

            service.data["files"] = []
            service.data["proposals"] = []
            service.data["collections"] = []
            app.update_snapshot(service.snapshot())
            await settled(pilot)

            assert app.selected_file_id is None
            assert app.selected_proposal_id is None
            assert app.selected_collection_id is None
            assert "original.png" not in str(app.query_one("#inspector-text", Static).render())
            assert "renamed.png" not in str(app.query_one("#review-detail", Static).render())
            assert "Previous evidence" not in str(app.query_one("#collection-detail", Static).render())
            assert preview.source is None
            assert not preview.display
            assert app.query_one("#approve", Button).disabled
    asyncio.run(scenario())


def test_review_table_keeps_type_and_decision_visible_with_long_paths(tmp_path):
    async def scenario():
        root = tmp_path / ("long collection path " * 5)
        service = FakeWorkspace(root)
        source, destination = root / "trip" / "source.png", root / "trip" / "destination.png"
        service.data["proposals"][0].update(current_path=str(source), proposed_path=str(destination))
        app = DDHApp(service)
        async with app.run_test(size=(140, 42)) as pilot:
            await settled(pilot)
            await pilot.press("2")
            await settled(pilot)
            table = app.query_one("#review-table", DataTable)
            path_cell = table.get_row_at(0)[0].plain
            assert str(root) not in path_cell
            assert "trip" in path_cell and "source.png" in path_cell and "destination.png" in path_cell
            detail = str(app.query_one("#review-detail", Static).render())
            assert str(source) in detail and str(destination) in detail
            for size in ((140, 42), (80, 24)):
                await pilot.resize_terminal(*size)
                await settled(pilot)
                assert sum(column.get_render_width(table) for column in table.ordered_columns) <= table.content_size.width
                assert table.ordered_columns[1].label.plain == "Type"
                assert table.ordered_columns[2].label.plain == "Decision"
            assert service.calls == []
    asyncio.run(scenario())


def test_ctrl_q_uses_safe_quit_and_bad_preview_cannot_apply(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        service.preview = lambda: {"token": "bad", "total": 1, "errors": 1, "items": [{"type": "move", "source": "a", "destination": "b", "error": "Target already exists"}]}
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            app.action_preview()
            await settled(pilot)
            assert app.screen.query_one("#confirm-apply", Button).disabled
            await pilot.press("enter")
            assert not any(call[0] == "apply" for call in service.calls)
            service.data["active_job"] = {"job_type": "analyze", "state": "running"}
            app.update_snapshot(service.snapshot())
            await pilot.press("ctrl+q")
            assert isinstance(app.screen, ConfirmScreen)
            assert "Stop safely" in app.screen.heading
            await pilot.press("escape")
    asyncio.run(scenario())


def test_live_theme_reload_updates_actual_styles(tmp_path, monkeypatch):
    from dataclasses import replace
    from donedatahoarder.tui import theme

    original = theme.load_palette()
    light_colors = {**original.colors, "background": "#fffcf0", "foreground": "#403e3c"}
    current = [replace(original, fingerprint=("dark-test",), dark=True)]
    monkeypatch.setattr(theme, "theme_fingerprint", lambda: current[0].fingerprint)
    monkeypatch.setattr(theme, "load_palette", lambda: current[0])

    async def scenario():
        app = DDHApp(FakeWorkspace(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            current[0] = replace(original, colors=light_colors, fingerprint=("light-test",), dark=False)
            app.reload_theme()
            await settled(pilot)
            assert app.screen.styles.background.hex.lower() == "#fffcf0"
            assert app.has_class("-light-mode")
    asyncio.run(scenario())


def test_review_shortcuts_ignore_unseen_proposals(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            await pilot.press("a", "r", "e")
            await settled(pilot)
            assert service.calls == []
            assert not isinstance(app.screen, ConfirmScreen)
    asyncio.run(scenario())


def test_off_page_image_proposal_compares_its_own_keeper(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        service.data["proposals"] = [{"id": 88, "file_id": 99, "file_path": str(tmp_path / "offpage.png"), "filename": "offpage.png", "mime_type": "image/png", "proposal_type": "mark_duplicate", "status": "pending", "duplicate_evidence": {"group_id": 77, "type": "exact", "keeper_id": 100, "keeper_path": str(tmp_path / "keeper.png"), "keeper_mime_type": "image/png", "keeper_size_bytes": 128, "exact_bytes": True}}]
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            await pilot.press("2", "c")
            await settled(pilot)
            assert isinstance(app.screen, ImageScreen)
            assert app.screen.file["id"] == 99
            assert app.screen.candidate["id"] == 100
            assert "SHA-256 matches" in app.screen.evidence
            await pilot.press("escape")
    asyncio.run(scenario())


def test_failed_run_offers_explicit_provider_retry(tmp_path):
    async def scenario():
        service = FakeWorkspace(tmp_path)
        service.data["plan"] = {"state": "failed", "steps": ["scan", "enrich", "analyze"], "current_index": 2}
        app = DDHApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(pilot)
            assert str(app.query_one("#run", Button).label) == "Retry run"
            await pilot.click("#run")
            await settled(pilot)
            assert ("resume", {"retry_errors": True}) in service.calls
    asyncio.run(scenario())


def test_inspector_snapshot_refreshes_same_path_replacements_without_redrawing_unchanged_pixels(tmp_path, monkeypatch):
    import os
    from PIL import Image
    from donedatahoarder.tui import images

    class NativeImage(Static):
        def __init__(self, **kwargs):
            kwargs.pop("on_error", None)
            super().__init__("", **kwargs)
            self.image = None

    monkeypatch.setattr(images, "_native_widgets", {"sixel": NativeImage})
    path = tmp_path / "original.png"
    with Image.new("RGB", (128, 64), "red") as source:
        # Stored PNG blocks keep red/blue fixture lengths equal across zlib
        # versions, so this test isolates same-size, same-mtime replacement.
        source.save(path, compress_level=0)

    async def wait_for_pixels(pilot, preview, color):
        previous, stable_frames = None, 0
        for _ in range(100):
            await pilot.pause(0.05)
            prepared = preview.prepared
            pixels = preview._owned_pixels
            ready = (prepared is not None and prepared.path == path.resolve()
                     and pixels is prepared.image and preview._image_widget.image is pixels
                     and preview.original_size == (128, 64)
                     and preview._active_worker.is_finished
                     and pixels.getpixel((0, 0)) == color)
            if ready:
                stable_frames = stable_frames + 1 if pixels is previous else 1
                previous = pixels
                if stable_frames >= 3:
                    return pixels
            else:
                previous, stable_frames = None, 0
        pytest.fail(f"Inspector did not settle on pixels {color}; prepared={preview.prepared!r}")

    async def scenario():
        service = FakeWorkspace(tmp_path)
        app = DDHApp(service, image_capability=images.ImageCapabilities("sixel"))
        async with app.run_test(size=(120, 42)) as pilot:
            preview = app.query_one("#inspector-image")
            # Initial layout may supersede a preview worker through resize.
            # Observe the accepted pixels, not unrelated/cancelled workers.
            pixels = await wait_for_pixels(pilot, preview, (255, 0, 0, 255))
            app.update_snapshot(service.snapshot())
            await pilot.pause(0.2)
            assert preview._owned_pixels is pixels
            before = path.stat()
            replacement = tmp_path / "replacement.png"
            with Image.new("RGB", (128, 64), "blue") as source:
                source.save(replacement, compress_level=0)
            assert replacement.stat().st_size == before.st_size
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, path)
            app.update_snapshot(service.snapshot())
            await wait_for_pixels(pilot, preview, (0, 0, 255, 255))
            with pytest.raises(ValueError, match="closed"):
                pixels.getpixel((0, 0))

    asyncio.run(scenario())

"""Photo preservation evidence and explicit keeper selection in the terminal UI."""
import asyncio
import copy

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Select, Static, TabbedContent

from donedatahoarder.tui.app import (
    ConfirmScreen, DDHApp, ImageScreen, duplicate_summary,
    photo_metadata_summary, photo_quality_summary,
)
from donedatahoarder.tui.images import ImageCapabilities
from test_tui_app import FakeWorkspace


FULL = {"is_photo": True, "status": "complete", "width": 6000, "height": 4000, "fields": {}}
RICH = {"is_photo": True, "status": "complete", "width": 3000, "height": 2000,
        "fields": {"capture_time": "2020-06-07T12:30:00", "camera_model": "Camera [red]", "lens": "50 mm"}}
TRADEOFF = {"status": "tradeoff", "candidate": RICH, "keeper": FULL,
            "reasons": ["The larger copy lacks capture information retained by the smaller copy."],
            "candidate_unique_fields": ["capture_time", "camera_model", "lens"],
            "conflicting_fields": [], "requires_review": True}


class PhotoWorkspace(FakeWorkspace):
    def __init__(self, root):
        super().__init__(root)
        self.data["files"][0]["photo_metadata"] = copy.deepcopy(FULL)
        self.data["files"][1]["photo_metadata"] = copy.deepcopy(RICH)
        self.data["files"][1]["photo_quality"] = copy.deepcopy(TRADEOFF)
        group = self.data["duplicates"][0]
        group["type"] = "perceptual"
        group["keeper_path"] = self.data["files"][0]["path"]
        self.data["proposals"] = [{
            "id": 7, "file_id": 2, "filename": "copy.png", "file_path": str(root / "copy.png"),
            "current_path": str(root / "copy.png"), "proposed_path": str(root / ".ddh_trash/copy.png"),
            "mime_type": "image/png", "proposal_type": "mark_duplicate", "status": "pending",
            "reasoning": "Visual similarity candidate", "review_token": "photo-review-7",
            "duplicate_evidence": {"group_id": 9, "type": "perceptual", "keeper_id": 1,
                "keeper_path": str(root / "original.png"), "keeper_mime_type": "image/png",
                "photo_quality": copy.deepcopy(TRADEOFF)},
        }]

    def set_keeper(self, group_id, file_id, expected_keeper_id=None):
        self.calls.append(("set_keeper", group_id, file_id, expected_keeper_id))
        self.data["duplicates"][0]["keep_file_id"] = file_id
        self.data["proposals"][0]["status"] = "pending"
        return {"review_reset": 1}


def rendered(widget):
    return str(widget.render())


async def wait_for(pilot, predicate):
    for _ in range(100):
        if predicate():
            return
        await pilot.pause(0.03)
    assert predicate(), "Photo comparison did not reach the expected state"


def test_unknown_metadata_is_not_rendered_as_confirmed_absence():
    assert "No valid capture metadata found" in photo_metadata_summary(FULL)
    for status in ("partial", "unavailable", "unsupported", "unknown"):
        text = photo_metadata_summary({"status": status, "fields": {}})
        assert "absence is not established" in text
        assert "No valid capture metadata found" not in text
    assert "Pixel dimensions unknown" in photo_metadata_summary(None)


def test_tradeoffs_display_dimensions_unique_values_and_conflicting_values():
    quality = copy.deepcopy(TRADEOFF)
    quality["keeper"]["fields"] = {"capture_time": "2021-01-01T00:00:00"}
    quality["conflicting_fields"] = ["capture_time"]
    text = photo_quality_summary(quality)
    assert "PHOTO TRADEOFF" in text and "Keep both for review" in text
    assert "3000×2000 px · 6.0 MP" in text and "6000×4000 px · 24.0 MP" in text
    assert "Only candidate retains lens: 50 mm" in text
    assert "Conflicting capture time: candidate 2020-06-07T12:30:00; keeper 2021-01-01T00:00:00" in text
    assert "dimensions alone do not establish image quality" in text
    combined = duplicate_summary({"type": "exact", "exact_bytes": True, "photo_quality": quality})
    assert "SHA-256 matches" in combined and "PHOTO TRADEOFF" in combined


def test_photo_evidence_is_visible_in_inspector_proposal_and_both_comparison_panes(tmp_path):
    async def scenario():
        service = PhotoWorkspace(tmp_path)
        app = DDHApp(service, image_capability=ImageCapabilities("off"))
        async with app.run_test(size=(130, 45)) as pilot:
            await pilot.pause(0.2)
            app.selected_file_id = "2"
            app.update_inspector()
            inspector = rendered(app.query_one("#inspector-text", Static))
            assert "6.0 MP" in inspector and "Camera [red]" in inspector
            app.action_workspace("review")
            app.selected_proposal_id = "7"
            app.update_review_detail()
            detail = rendered(app.query_one("#review-detail", Static))
            assert "PHOTO TRADEOFF" in detail and "Only candidate retains lens: 50 mm" in detail
            app.action_compare()
            await pilot.pause(0.2)
            comparison = app.screen
            assert isinstance(comparison, ImageScreen)
            assert "6.0 MP" in rendered(comparison.query_one("#image-a-photo", Static))
            assert "Camera [red]" in rendered(comparison.query_one("#image-a-photo", Static))
            assert "24.0 MP" in rendered(comparison.query_one("#image-b-photo", Static))
            assert "No valid capture metadata found" in rendered(comparison.query_one("#image-b-photo", Static))
            assert "PHOTO TRADEOFF" in rendered(comparison.query_one("#image-evidence", Static))
            assert service.calls == []
    asyncio.run(scenario())


def test_keeper_choice_requires_confirmation_then_passes_displayed_keeper_without_approval(tmp_path):
    async def scenario():
        service = PhotoWorkspace(tmp_path)
        app = DDHApp(service, image_capability=ImageCapabilities("off"))
        async with app.run_test(size=(130, 45)) as pilot:
            await pilot.pause(0.2)
            app.action_workspace("review")
            app.selected_proposal_id = "7"
            app.action_compare()
            await pilot.pause(0.2)
            comparison = app.screen
            assert not comparison.query_one("#image-keep-a", Button).disabled
            assert comparison.query_one("#image-keep-b", Button).disabled
            await pilot.click("#image-keep-a")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert isinstance(app.screen, ConfirmScreen)
            assert app.focused.id == "confirm-cancel"
            assert "does not approve or apply" in app.screen.body
            assert service.calls == []
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.screen is comparison)
            assert app.screen is comparison and service.calls == []
            # Textual suppresses button clicks during its short active-effect
            # animation. Wait for the actual control state after cancelling.
            await wait_for(pilot, lambda: not comparison.query_one("#image-keep-a", Button).has_class("-active"))
            await pilot.pause()
            await pilot.click("#image-keep-a")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.pause()
            await pilot.click("#confirm-apply")
            await wait_for(pilot, lambda: bool(service.calls))
            await wait_for(pilot, lambda: not app.busy and not isinstance(app.screen, ImageScreen))
            assert service.calls == [("set_keeper", 9, 2, 1)]
            assert service.data["proposals"][0]["status"] == "pending"
            assert app.query_one("#workspace", TabbedContent).active == "review"
            assert not isinstance(app.screen, ImageScreen)
    asyncio.run(scenario())


def test_switching_comparison_refreshes_metadata_and_keeper_context(tmp_path):
    async def scenario():
        service = PhotoWorkspace(tmp_path)
        unknown = {"id": 3, "path": str(tmp_path / "unavailable.png"), "filename": "unavailable.png",
                   "mime_type": "image/png", "photo_metadata": {"status": "unavailable", "fields": {}},
                   "photo_quality": {"status": "unknown", "candidate": {"status": "unavailable"},
                                     "keeper": FULL, "requires_review": True}}
        service.data["files"].append(unknown)
        app = DDHApp(service, image_capability=ImageCapabilities("off"))
        async with app.run_test(size=(130, 45)) as pilot:
            await pilot.pause(0.2)
            app.selected_file_id = "1"
            app.action_compare()
            await pilot.pause(0.2)
            comparison = app.screen
            comparison.query_one("#image-candidate", Select).value = "3"
            await pilot.pause(0.2)
            assert "unavailable" in rendered(comparison.query_one("#image-b-photo", Static))
            assert "Camera [red]" not in rendered(comparison.query_one("#image-b-photo", Static))
            assert "PHOTO EVIDENCE UNKNOWN" in rendered(comparison.query_one("#image-evidence", Static))
            assert comparison.query_one("#image-keep-a", Button).disabled
            assert not comparison.query_one("#image-keep-b", Button).disabled
    asyncio.run(scenario())

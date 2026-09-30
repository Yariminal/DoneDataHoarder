"""Exercise the real TUI → persisted pipeline → review → filesystem → recovery."""
import asyncio

import pytest

pytest.importorskip("textual")

from PIL import Image
from textual.widgets import Button, DataTable, Static, TabbedContent

from donedatahoarder.core import jobs
from donedatahoarder.core.jobs import JobManager
from donedatahoarder.db.session import init_db
from donedatahoarder.tui import service as service_module
from donedatahoarder.tui.app import ConfirmScreen, DDHApp
from donedatahoarder.tui.images import ImageCapabilities
from donedatahoarder.tui.service import WorkspaceService


def test_metadata_duplicate_review_and_undo_through_real_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    init_db(tmp_path / "index.db")
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    monkeypatch.setattr(jobs, "job_manager", manager)
    monkeypatch.setattr(service_module, "job_manager", manager)
    from donedatahoarder.ai import router

    def no_provider(*args, **kwargs):
        pytest.fail("The metadata-only workflow must not initialize an AI provider")
    monkeypatch.setattr(router, "init_ai", no_provider)
    root = tmp_path / "collection"
    root.mkdir()
    with Image.new("RGB", (48, 32), (60, 120, 180)) as photo:
        photo.save(root / "original.png")
    original_bytes = (root / "original.png").read_bytes()
    (root / "copy.png").write_bytes(original_bytes)
    workspace = WorkspaceService(root)

    async def wait_for(pilot, predicate, message, *, workspace_app=None, action_state=None):
        for _ in range(160):
            if predicate():
                return
            await pilot.pause(0.1)
        current = workspace.snapshot()
        ui_state = None
        if workspace_app is not None:
            table = workspace_app.query_one("#review-table", DataTable)
            ui_state = {
                "busy": workspace_app.busy,
                "selected_proposal_id": workspace_app.selected_proposal_id,
                "selected_proposal": workspace_app.selected_proposal(),
                "approve_disabled": workspace_app.query_one("#approve", Button).disabled,
                "workspace": workspace_app.query_one("#workspace", TabbedContent).active,
                "review_row_count": table.row_count,
                "review_cursor_row": table.cursor_row,
                "status": str(workspace_app.query_one("#status", Static).render()),
                "snapshot": workspace_app.snapshot,
            }
        pytest.fail(f"{message}: plan={current.get('plan')!r}; job={current.get('job')!r}; ui={ui_state!r}; action={action_state!r}")

    async def scenario():
        app = DDHApp(workspace, image_capability=ImageCapabilities(renderer="off"))

        def confirmation_ready():
            # push_screen updates the stack before the modal mounts and runs
            # its focus handler. Verify the actual safe default before use.
            return (isinstance(app.screen, ConfirmScreen) and app.screen.is_mounted
                    and not app.busy and app.focused is not None
                    and app.focused.id == "confirm-cancel")

        async with app.run_test(size=(140, 45)) as pilot:
            await wait_for(pilot, lambda: bool(app.snapshot), "Initial session did not load")
            assert app.snapshot["counts"]["files"] == 0
            await pilot.click("#metadata")
            await wait_for(pilot, lambda: (app.snapshot.get("plan") or {}).get("state") == "completed"
                           and not app.snapshot.get("has_live_workers") and not app.busy,
                           "Metadata pipeline failed to complete")
            assert app.snapshot["counts"]["files"] == 2
            assert app.snapshot["counts"]["duplicates"] >= 1
            assert app.snapshot["proposals"]
            assert (root / "original.png").exists() and (root / "copy.png").exists()
            await pilot.press("2")
            assert app.query_one("#workspace", TabbedContent).active == "review"
            approve = app.query_one("#approve", Button)
            action_state = {
                "disabled": approve.disabled, "mounted": approve.is_mounted,
                "display": approve.display, "visible": approve.visible,
                "region": str(approve.region), "selected_proposal_id": app.selected_proposal_id,
                "busy": app.busy, "has_live_workers": app.snapshot.get("has_live_workers"),
                "active_job": app.snapshot.get("active_job"),
            }

            def approve_ready():
                # Tab activation can precede layout. Pilot resolves click
                # coordinates before its own pause, so an enabled button with
                # a stale region can silently send the click to another widget.
                region = approve.region
                return (not approve.disabled and region.width > 1 and region.height > 1
                        and app.get_widget_at(region.x + 1, region.y + 1)[0] is approve)

            await wait_for(pilot, approve_ready, "Review button did not become visible",
                           workspace_app=app, action_state=action_state)
            action_state["ready_region"] = str(approve.region)
            action_state["click_hit"] = await pilot.click("#approve", offset=(1, 1))
            assert action_state["click_hit"], f"Review click missed its button: {action_state!r}"
            await wait_for(pilot, lambda: not app.busy and any(
                proposal["status"] == "approved" for proposal in app.snapshot["proposals"]),
                "Review decision was not persisted", workspace_app=app, action_state=action_state)
            await pilot.click("#preview")
            await wait_for(pilot, confirmation_ready,
                           "Execution preview was not shown")
            assert app.focused.id == "confirm-cancel"
            assert len(list(root.glob("*.png"))) == 2
            await pilot.click("#confirm-apply")
            await wait_for(pilot, lambda: not app.busy and len(list(root.glob("*.png"))) == 1,
                           "Approved duplicate was not moved to the recoverable trash")
            assert len(list((root / ".ddh_trash").glob("*.png"))) == 1
            app.refresh_snapshot()
            await wait_for(pilot, lambda: bool(app.snapshot.get("history")), "History did not refresh")
            await pilot.press("4")
            await pilot.click("#undo")
            await wait_for(pilot, confirmation_ready,
                           "Recovery preview was not shown")
            assert app.focused.id == "confirm-cancel"
            await pilot.click("#confirm-apply")
            await wait_for(pilot, lambda: not app.busy and len(list(root.glob("*.png"))) == 2,
                           "Recovery did not restore the duplicate")
            assert (root / "original.png").read_bytes() == original_bytes
            assert (root / "copy.png").read_bytes() == original_bytes

    try:
        asyncio.run(scenario())
    finally:
        for job in list(manager._jobs.values()):
            if job.state.value in {"running", "paused", "cancelling"}:
                manager.force_cancel(job.job_id)
        for thread in list(manager._worker_threads.values()):
            thread.join(timeout=5)

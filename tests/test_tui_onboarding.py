"""Startup discovery does not create sessions, process files, or pull models."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session

from donedatahoarder.db.models import RunPlan, UserSession
from donedatahoarder.db.session import init_db
from donedatahoarder.tui import onboarding


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    engine = init_db(tmp_path / "index.db")
    manager = SimpleNamespace(get_active=lambda: None, has_live_workers=lambda *args: False,
                              reconcile_startup=lambda: None)
    monkeypatch.setattr(onboarding, "job_manager", manager)
    from donedatahoarder.tui import service
    monkeypatch.setattr(service, "job_manager", manager)
    result = onboarding.SessionCatalog(model="default-model", workers=2, ollama_host="http://localhost:11434")
    yield result
    engine.dispose()


def test_catalog_is_read_only_until_folder_chosen(catalog, tmp_path):
    assert catalog.list_sessions() == []
    with Session(catalog.engine) as db:
        assert db.query(UserSession).count() == 0
    with pytest.raises(ValueError, match="folder"):
        catalog.open(root="")
    with pytest.raises(ValueError, match="folder"):
        catalog.open(root=str(tmp_path / "missing"))
    assert catalog.list_sessions() == []
    root = tmp_path / "collection"
    root.mkdir()
    (root / "photo.txt").write_text("untouched")
    service = catalog.open(root=str(root), model="chosen-model")
    snapshot = service.snapshot()
    assert snapshot["session"]["model"] == "chosen-model"
    assert snapshot["files"] == []
    assert snapshot["plan"] is None
    assert (root / "photo.txt").read_text() == "untouched"
    resumed = catalog.open(session_id=service.session_id, model="ignored")
    assert resumed.snapshot()["session"]["model"] == "chosen-model"
    assert len(catalog.list_sessions()) == 1


def test_catalog_blocks_selection_until_workers_drain(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(onboarding.job_manager, "has_live_workers", lambda: True)
    with pytest.raises(ValueError, match="worker"):
        catalog.open(root=str(tmp_path))
    assert catalog.list_sessions() == []


@pytest.mark.parametrize(("installed", "expected"), [
    ([{"name": "chosen:latest"}], "is installed"),
    ([{"model": "chosen:latest"}], "is installed"),
    ([{"name": "other:latest"}], "not installed"),
])
def test_readiness_only_requests_installed_model_list(monkeypatch, installed, expected):
    requests = []
    actual_client = httpx.Client
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"models": installed})
    monkeypatch.setattr(onboarding.httpx, "Client", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(response)))
    assert expected in onboarding.model_readiness("http://localhost:11434", "chosen")
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/tags"
    assert requests[0].content == b""


def test_readiness_failure_preserves_metadata_path(monkeypatch):
    actual_client = httpx.Client
    monkeypatch.setattr(onboarding.httpx, "Client", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    assert "Metadata only" in onboarding.model_readiness("http://localhost:11434", "chosen")
    assert "Metadata only" in onboarding.model_readiness("http://[broken", "chosen")


@pytest.mark.parametrize("state", ["failed", "completed"])
def test_saved_session_checks_actual_plan_or_next_run_settings(catalog, tmp_path, monkeypatch, state):
    service = catalog.open(root=str(tmp_path))
    with Session(catalog.engine) as db:
        owner = db.get(UserSession, service.session_id)
        owner.analyze_model, owner.propose_model = "session-vision", "session-text"
        db.add(RunPlan(id="saved", session_id=service.session_id, state=state,
                       current_index=0, steps_json=json.dumps(["analyze", "propose"]),
                       options_json=json.dumps({"ollama_host": "http://saved-server:12400",
                                                "analyze_model": "saved-vision", "propose_model": "saved-text"})))
        db.commit()
    requests = []
    actual_client = httpx.Client
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"models": [{"name": name + ":latest"}
            for name in ("saved-vision", "saved-text", "session-vision", "session-text")]})
    monkeypatch.setattr(onboarding.httpx, "Client", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(response)))
    result = catalog.session_readiness(service.session_id)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/tags"
    if state == "failed":
        assert requests[0].url.host == "saved-server"
        assert requests[0].url.port == 12400
        assert "Resume saved plan" in result
        assert "saved-vision, saved-text" in result
        assert "session-vision" not in result
    else:
        assert requests[0].url.host == "localhost"
        assert "Next new run" in result
        assert "session-vision, session-text" in result
        assert "saved-vision" not in result


def test_metadata_only_saved_plan_needs_no_model_request(catalog, tmp_path, monkeypatch):
    service = catalog.open(root=str(tmp_path))
    with Session(catalog.engine) as db:
        db.add(RunPlan(id="metadata", session_id=service.session_id, state="cancelled",
                       current_index=1, steps_json=json.dumps(["scan", "enrich", "dedup", "execute_dry"]),
                       options_json="{}"))
        db.commit()
    monkeypatch.setattr(onboarding, "model_readiness", lambda *args: pytest.fail("No AI stages need checking"))
    assert "Ollama is not required" in catalog.session_readiness(service.session_id)


def test_help_and_quit_cannot_interrupt_opening_a_real_session(catalog, tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input
    from donedatahoarder.tui.app import DDHApp
    from donedatahoarder.tui.onboarding_screens import HelpScreen, SessionScreen

    root = tmp_path / "collection"
    root.mkdir()
    ready, release = threading.Event(), threading.Event()
    original_open = catalog.open

    def slow_open(**kwargs):
        ready.set()
        release.wait(5)
        return original_open(**kwargs)

    monkeypatch.setattr(catalog, "open", slow_open)
    monkeypatch.setattr(catalog, "readiness", lambda model: "Metadata only is available.")

    async def exercise():
        app = DDHApp(catalog=catalog)
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            picker = app.screen
            picker.query_one("#session-folder", Input).value = str(root)
            await pilot.click("#session-new")
            assert await asyncio.to_thread(ready.wait, 2)
            assert picker.opening
            assert picker.query_one("#session-help", Button).disabled
            await pilot.press("f1", "escape", "ctrl+q")
            assert app.screen is picker
            assert app.service is None
            assert catalog.list_sessions() == []
            release.set()
            await app.workers.wait_for_complete([worker for worker in app.workers if not worker.is_cancelled])
            await pilot.pause(0.2)
            assert not isinstance(app.screen, SessionScreen)
            assert app.service.snapshot()["plan"] is None
            assert len(catalog.list_sessions()) == 1
            await pilot.press("f1")
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, HelpScreen)
            assert len(app.screen_stack) == 1

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_readiness_coalesces_pending_choices_without_concurrent_requests(catalog, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Static
    from donedatahoarder.tui.app import DDHApp

    ready, release = threading.Event(), threading.Event()
    calls = []

    def slow_readiness(model):
        calls.append(model)
        if len(calls) == 1:
            ready.set()
            release.wait(5)
        return f"Readiness for {model}"

    monkeypatch.setattr(catalog, "readiness", slow_readiness)

    async def exercise():
        app = DDHApp(catalog=catalog)
        async with app.run_test() as pilot:
            assert await asyncio.to_thread(ready.wait, 2)
            picker = app.screen
            assert picker.query_one("#session-check", Button).disabled
            for model in ("ignored-a", "ignored-b", "newest"):
                picker.query_one("#session-model", Input).value = model
                picker.check_model()
            await pilot.pause(0.1)
            assert calls == ["default-model"]
            release.set()
            await app.workers.wait_for_complete([worker for worker in app.workers if not worker.is_cancelled])
            await pilot.pause(0.2)
            assert calls == ["default-model", "newest"]
            assert not picker.query_one("#session-check", Button).disabled
            assert str(picker.query_one("#session-readiness", Static).render()) == "Readiness for newest"

    try:
        asyncio.run(exercise())
    finally:
        release.set()

"""Authenticated remote sessions reuse the real engine without replaying writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import io
from pathlib import Path
import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from donedatahoarder.core import jobs
from donedatahoarder.core.jobs import JobManager
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus, Proposal,
    ProposalType, RunPlan,
)
from donedatahoarder.db.session import get_engine
from donedatahoarder.remote.receipts import ReceiptConflict, ReceiptStore
from donedatahoarder.remote.server import MAX_COMMAND_BODY_BYTES, PREFIX, create_app
from donedatahoarder.tui import onboarding, service as service_module
from donedatahoarder.tui.service import WorkspaceService

TOKEN = "remote-test-" + "a" * 48


@pytest.fixture
def workstation(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    for module in (jobs, service_module, onboarding):
        monkeypatch.setattr(module, "job_manager", manager)
    monkeypatch.setattr(onboarding, "model_readiness", lambda host, model: "Ollama ready on workstation")
    root = tmp_path / "collection"
    root.mkdir()
    database = tmp_path / "index.db"
    app = create_app(database, token=TOKEN, allowed_roots=[root], name="home-workstation")
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield client, app, root, database, manager
    for job in list(manager._jobs.values()):
        if job.state.value in {"running", "paused", "cancelling"}:
            manager.force_cancel(job.job_id)
    for thread in list(manager._worker_threads.values()):
        thread.join(timeout=10)


def open_session(client, **kwargs):
    response = client.post(PREFIX + "/sessions", json={"request_id": str(uuid4()), **kwargs})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["state"] == "completed", receipt
    return receipt["result"]["session_id"]


def command(client, session_id, command, params=None, request_id=None):
    return client.post(PREFIX + f"/sessions/{session_id}/commands", json={
        "request_id": request_id or str(uuid4()), "command": command, "params": params or {},
    })


def add_file(session_id, path):
    path.write_text("sample", encoding="utf-8")
    with Session(get_engine()) as db:
        file = File(session_id=session_id, path=str(path), filename=path.name,
                    size_bytes=6, mime_type="text/plain", status=FileStatus.PENDING)
        db.add(file)
        db.commit()
        return file.id


def test_auth_precedes_every_route_and_unrelated_routes_do_not_exist(workstation):
    client, app, root, database, _ = workstation
    with TestClient(app) as unauthenticated:
        for method, path in (("get", "/hello"), ("get", "/sessions"),
                             ("post", "/sessions"), ("get", "/commands/" + str(uuid4()))):
            response = getattr(unauthenticated, method)(PREFIX + path)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
        assert unauthenticated.get("/does-not-exist").status_code == 401
        assert unauthenticated.get(PREFIX + "/hello", headers={"Authorization": "Bearer wrong"}).status_code == 401
    hello = client.get(PREFIX + "/hello").json()
    assert hello["protocol"] == 1
    assert hello["name"] == "home-workstation"
    assert hello["server_id"]
    assert hello["default_root"] == str(root)
    assert hello["path_flavor"] in {"windows", "posix"}
    for path in ("/docs", "/openapi.json", "/api/files", PREFIX + "/hello/"):
        assert client.get(path, follow_redirects=False).status_code == 404


def test_remote_keeper_change_is_scoped_receipted_and_rejects_stale_comparison(workstation):
    client, _, root, _, _ = workstation
    session_id = open_session(client)
    first = add_file(session_id, root / "first.jpg")
    second = add_file(session_id, root / "second.jpg")
    with Session(get_engine()) as db:
        group = DuplicateGroup(session_id=session_id, dupe_type=DupeType.EXACT,
                               group_hash="keeper-command", keep_file_id=first)
        db.add(group)
        db.flush()
        db.add_all([DuplicateMember(group_id=group.id, file_id=first),
                    DuplicateMember(group_id=group.id, file_id=second)])
        db.commit()
        group_id = group.id
    params = {"group_id": group_id, "file_id": second, "expected_keeper_id": first}
    request_id = str(uuid4())
    result = command(client, session_id, "set_keeper", params, request_id)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["keep_file_id"] == second
    replay = command(client, session_id, "set_keeper", params, request_id)
    assert replay.json() == result.json()
    stale = command(client, session_id, "set_keeper",
                    {"group_id": group_id, "file_id": first, "expected_keeper_id": first})
    assert stale.json()["state"] == "failed"
    assert "changed" in str(stale.json())
    other = open_session(client)
    foreign = command(client, other, "set_keeper", params)
    assert foreign.json()["state"] == "failed"
    with Session(get_engine()) as db:
        assert db.get(DuplicateGroup, group_id).keep_file_id == second
    assert (root / "first.jpg").read_text() == "sample"
    assert (root / "second.jpg").read_text() == "sample"


def test_bad_configuration_fails_before_creating_database(tmp_path):
    database = tmp_path / "never-created.db"
    with pytest.raises(ValueError, match="32"):
        create_app(database, token="short", allowed_roots=[tmp_path])
    with pytest.raises(ValueError, match="allowed"):
        create_app(database, token=TOKEN, allowed_roots=[])
    assert not database.exists()


def test_database_and_recovery_journal_cannot_live_in_collection(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    inside_database = root / "index.db"
    with pytest.raises(ValueError, match="database"):
        create_app(inside_database, token=TOKEN, allowed_roots=[root])
    assert not inside_database.exists()
    monkeypatch.setenv("DDH_DATA_DIR", str(root / "journal"))
    outside_database = tmp_path / "index.db"
    with pytest.raises(ValueError, match="recovery journal"):
        create_app(outside_database, token=TOKEN, allowed_roots=[root])
    assert not outside_database.exists()
    assert not (root / "journal").exists()


@pytest.mark.parametrize("token", ["a" * 32 + "\x7f", "a" * 32 + "é", "a" * 4097])
def test_header_incompatible_token_is_rejected_before_creating_database(tmp_path, token):
    database = tmp_path / "never-created.db"
    with pytest.raises(ValueError, match="printable ASCII"):
        create_app(database, token=token, allowed_roots=[tmp_path])
    assert not database.exists()


def test_sessions_and_file_metadata_are_scoped_to_allowed_roots(workstation):
    client, app, root, _, _ = workstation
    session_id = open_session(client)
    private = root.parent / "private"
    private.mkdir()
    other = WorkspaceService(private)
    own_file = add_file(session_id, root / "own.txt")
    private_file = add_file(other.session_id, private / "secret.txt")
    assert [row["id"] for row in client.get(PREFIX + "/sessions").json()] == [session_id]
    assert client.get(PREFIX + f"/sessions/{other.session_id}/snapshot").status_code == 403
    assert client.post(PREFIX + "/sessions", json={"request_id": str(uuid4()), "root": str(private)}).status_code == 403
    assert client.post(PREFIX + "/sessions", json={"request_id": str(uuid4()), "root": "../private"}).status_code == 403
    assert client.get(PREFIX + f"/sessions/{session_id}/files/{own_file}").json()["filename"] == "own.txt"
    assert client.get(PREFIX + f"/sessions/{session_id}/files/{private_file}").status_code == 404
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot?limit=1").json()["page"]["limit"] == 1
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot?limit=2001").status_code == 422
    assert "workstation" in client.get(PREFIX + f"/sessions/{session_id}/readiness").json()["message"]
    # An inconsistent index must not turn a scoped file endpoint into arbitrary access.
    with Session(get_engine()) as db:
        db.get(File, own_file).path = str(private / "secret.txt")
        db.commit()
    assert client.get(PREFIX + f"/sessions/{session_id}/files/{own_file}").status_code == 403
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot").status_code == 403


def test_replaced_root_and_link_ancestors_are_rejected(workstation, monkeypatch):
    client, _, root, _, _ = workstation
    session_id = open_session(client)
    child = root / "child"
    child.mkdir()
    from donedatahoarder.remote import server
    original = server._is_link_or_reparse
    monkeypatch.setattr(server, "_is_link_or_reparse", lambda path: path == root or original(path))
    assert client.get(PREFIX + "/sessions").json()[0]["storage"]["available"] is False
    snapshot = client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()
    assert snapshot["storage"]["available"] is False
    assert client.post(PREFIX + "/sessions", json={"request_id": str(uuid4()), "root": str(child)}).status_code == 403


def test_missing_drive_preserves_metadata_control_and_receipt_access(workstation, monkeypatch):
    client, _, root, database, _ = workstation
    session_id = open_session(client)
    file_id = add_file(session_id, root / "sample.txt")
    with Session(get_engine()) as db:
        db.add(Proposal(file_id=file_id, proposal_type=ProposalType.RENAME,
                        current_value=str(root / "sample.txt"), proposed_value=str(root / "renamed.txt")))
        db.add(RunPlan(id=str(uuid4()), session_id=session_id, state="ready"))
        db.commit()
    key = str(uuid4())
    receipt = command(client, session_id, "history", request_id=key).json()
    root.rename(root.parent / "disconnected-drive")
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline snapshots must not inspect collection files")
    from donedatahoarder.remote import server
    monkeypatch.setattr(service_module, "cached_protection_index", forbidden)
    monkeypatch.setattr(service_module, "duplicate_evidence", forbidden)
    monkeypatch.setattr(service_module, "proposal_review_token", forbidden)
    monkeypatch.setattr(server, "preview_revision", forbidden)
    assert client.get(PREFIX + "/sessions").json()[0]["storage"]["available"] is False
    assert open_session(client, session_id=session_id) == session_id
    snapshot = client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()
    assert snapshot["storage"]["available"] is False
    assert snapshot["counts"]["files"] == 1
    assert snapshot["files"][0]["preview_revision"] == "unavailable"
    assert snapshot["proposals"][0]["review_token"] is None
    assert snapshot["proposals"][0]["duplicate_evidence"] is None
    assert client.get(PREFIX + f"/sessions/{session_id}/files/{file_id}").status_code == 200
    assert client.get(PREFIX + "/commands/" + key).json() == receipt
    assert command(client, session_id, "history").json()["state"] == "completed"
    pauses = []
    monkeypatch.setattr(WorkspaceService, "pause_pipeline", lambda self: pauses.append(self.session_id))
    assert command(client, session_id, "pause_pipeline").json()["state"] == "completed"
    assert pauses == [session_id]
    assert command(client, session_id, "cancel_pipeline").json()["state"] == "completed"
    for action, params in (("start_pipeline", {"metadata_only": True}), ("preview", {}),
                           ("apply", {"token": "old", "confirmed": True}),
                           ("undo", {"token": "old", "confirmed": True})):
        assert command(client, session_id, action, params).status_code == 403
    assert client.get(PREFIX + f"/sessions/{session_id}/files/{file_id}/preview").status_code == 403
    # Restarting the workstation service while the drive is detached also
    # preserves access to stored work and cancellation controls.
    restarted = create_app(database, token=TOKEN, allowed_roots=[root])
    with TestClient(restarted, headers={"Authorization": "Bearer " + TOKEN}) as new_client:
        assert new_client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()["storage"]["available"] is False
        assert new_client.get(PREFIX + "/commands/" + key).json() == receipt


def test_missing_allowed_folder_does_not_block_another_collection(workstation):
    client, _, first, database, _ = workstation
    first_session = open_session(client)
    second = first.parent / "second-collection"
    second.mkdir()
    app = create_app(database, token=TOKEN, allowed_roots=[first, second])
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as other_client:
        second_session = open_session(other_client, root=str(second))
        file_id = add_file(second_session, second / "available.txt")
        first.rename(first.parent / "disconnected-drive")
        listed = {row["id"]: row for row in other_client.get(PREFIX + "/sessions").json()}
        assert listed[first_session]["storage"]["available"] is False
        assert listed[second_session]["storage"]["available"] is True
        snapshot = other_client.get(PREFIX + f"/sessions/{second_session}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["storage"]["available"] is True
        assert snapshot.json()["files"][0]["id"] == file_id
        opened = open_session(other_client, root=str(second))
        assert other_client.get(PREFIX + f"/sessions/{opened}/snapshot").json()["storage"]["available"] is True
        settings = command(other_client, second_session, "update_settings", {"workers": 2}).json()
        assert settings["state"] == "completed", settings
        assert (second / "available.txt").read_text() == "sample"


def test_create_and_command_receipts_replay_without_duplicate_execution(workstation, monkeypatch):
    client, app, root, _, _ = workstation
    request_id = str(uuid4())
    body = {"request_id": request_id, "root": str(root)}
    first = client.post(PREFIX + "/sessions", json=body).json()
    assert client.post(PREFIX + "/sessions", json=body).json() == first
    assert len(client.get(PREFIX + "/sessions").json()) == 1
    changed = client.post(PREFIX + "/sessions", json={**body, "model": "different:8b"})
    assert changed.status_code == 409
    session_id = first["result"]["session_id"]
    calls = []
    monkeypatch.setattr(WorkspaceService, "preflight", lambda self, **kwargs: calls.append(kwargs) or {"files": 2})
    key = str(uuid4())
    response = command(client, session_id, "preflight", request_id=key)
    assert response.json()["result"] == {"files": 2}
    assert command(client, session_id, "preflight", request_id=key).json() == response.json()
    assert len(calls) == 1
    assert client.get(PREFIX + "/commands/" + key).json() == response.json()
    assert command(client, session_id, "preflight", {"metadata_only": True}, request_id=key).status_code == 409


def test_command_allowlist_types_and_failed_receipts(workstation):
    client, _, _, _, _ = workstation
    session_id = open_session(client)
    for action, params in (("__getattribute__", {}), ("start_pipeline", {"metadata_only": "true"}),
                           ("approve", {"proposal_id": True}), ("apply", {"confirmed": True}),
                           ("preview", {"arbitrary_path": "C:/private"}),
                           ("update_settings", {"workers": 0}),
                           ("update_settings", {"ollama_host": "http://other"})):
        assert command(client, session_id, action, params).status_code == 422
    key = str(uuid4())
    failed = command(client, session_id, "apply", {"token": "stale", "confirmed": False}, key).json()
    assert failed["state"] == "failed" and failed["error"]["status_code"] == 400
    assert command(client, session_id, "apply", {"token": "stale", "confirmed": False}, key).json() == failed
    assert command(client, session_id, "reject", {"proposal_id": 999}).json()["error"]["status_code"] == 404


@pytest.mark.parametrize("chunked", [False, True])
def test_command_body_limit_precedes_validation_and_dispatch(workstation, monkeypatch, chunked):
    client, app, _, _, _ = workstation
    session_id = open_session(client)
    calls = []
    monkeypatch.setattr(WorkspaceService, "update_settings", lambda self, **kwargs: calls.append(kwargs))
    key = str(uuid4())
    body = ('{"request_id":"' + key + '","command":"update_settings","params":{"workers":2}}').encode()
    body += b" " * (MAX_COMMAND_BODY_BYTES + 1 - len(body))
    content = (body[index:index + 1024] for index in range(0, len(body), 1024)) if chunked else body
    response = client.post(PREFIX + f"/sessions/{session_id}/commands", content=content,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413, response.text
    assert calls == []
    assert app.state.remote_receipts.get(key) is None
    response = client.post(PREFIX + "/sessions", content=body,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    with TestClient(app) as unauthenticated:
        assert unauthenticated.post(PREFIX + "/sessions", content=body).status_code == 401


def test_real_metadata_pipeline_remains_owned_by_workstation(workstation):
    client, _, root, _, manager = workstation
    (root / "one.txt").write_text("remote sample content", encoding="utf-8")
    (root / "two.txt").write_text("remote sample content", encoding="utf-8")
    session_id = open_session(client)
    started = command(client, session_id, "start_pipeline", {"metadata_only": True}).json()
    assert started["state"] == "completed", started
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()
        if snapshot["plan"]["state"] in {"completed", "failed", "interrupted"} and not manager.has_live_workers():
            break
        time.sleep(.05)
    assert snapshot["plan"]["state"] == "completed", snapshot
    assert snapshot["counts"]["files"] == 2
    assert snapshot["plan"]["steps"] == ["scan", "enrich", "dedup", "execute_dry"]
    assert (root / "one.txt").read_text(encoding="utf-8") == "remote sample content"
    assert (root / "two.txt").is_file()


def test_settings_are_validated_and_cannot_change_unfinished_plan(workstation):
    client, _, _, _, _ = workstation
    session_id = open_session(client)
    response = command(client, session_id, "update_settings", {"model": "gemma3:4b", "workers": 2}).json()
    assert response["state"] == "completed"
    snapshot = client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()
    assert snapshot["session"]["model"] == "gemma3:4b"
    assert snapshot["session"]["workers"] == 2
    with Session(get_engine()) as db:
        db.add(RunPlan(id=str(uuid4()), session_id=session_id, state="interrupted",
                       options_json='{"analyze_model":"gemma3:4b","workers":2}'))
        db.commit()
    refused = command(client, session_id, "update_settings", {"model": "different:8b"}).json()
    assert refused["state"] == "failed" and refused["error"]["status_code"] == 409
    assert "unfinished" in refused["error"]["detail"]
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()["session"]["model"] == "gemma3:4b"


def test_restart_preserves_identity_and_marks_inflight_receipt_uncertain(workstation):
    client, app, root, database, _ = workstation
    session_id = open_session(client)
    server_id = client.get(PREFIX + "/hello").json()["server_id"]
    key = str(uuid4())
    payload = {"action": "update_settings", "session_id": session_id, "root": str(root),
               "params": {"model": None, "workers": 2}, "mutation": True}
    app.state.remote_receipts.claim(key, payload)
    restarted = create_app(database, token=TOKEN, allowed_roots=[root])
    with TestClient(restarted, headers={"Authorization": "Bearer " + TOKEN}) as new_client:
        assert new_client.get(PREFIX + "/hello").json()["server_id"] == server_id
        receipt = new_client.get(PREFIX + "/commands/" + key).json()
        assert receipt["state"] == "uncertain"
        assert command(new_client, session_id, "update_settings", {"workers": 2}, request_id=key).json() == receipt
        blocked = command(new_client, session_id, "update_settings", {"workers": 2}).json()
        assert blocked["state"] == "failed" and "uncertain" in blocked["error"]["detail"]
        assert command(new_client, session_id, "history").json()["state"] == "completed"


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_unexpected_mutation_failure_is_uncertain_and_gates_other_clients(workstation, monkeypatch, error_type):
    client, app, root, _, _ = workstation
    session_id = open_session(client)
    def interrupted_apply(self, **kwargs):
        (root / "partial.txt").write_text("write completed before process failure")
        raise error_type("simulated journal write failure")
    monkeypatch.setattr(WorkspaceService, "apply", interrupted_apply)
    key = str(uuid4())
    receipt = command(client, session_id, "apply", {"token": "example", "confirmed": True}, key).json()
    assert receipt["state"] == "uncertain"
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as other_client:
        blocked = command(other_client, session_id, "update_settings", {"workers": 2}).json()
        assert blocked["state"] == "failed" and "uncertain" in blocked["error"]["detail"]
        assert other_client.get(PREFIX + f"/sessions/{session_id}/snapshot").status_code == 200
    assert command(client, session_id, "apply", {"token": "example", "confirmed": True}, key).json() == receipt


@pytest.mark.parametrize("invalid_result", [{"applied": float("inf")}, {"applied": {1}}])
def test_unrecordable_mutation_result_is_uncertain_and_blocks_more_changes(workstation, monkeypatch, invalid_result):
    client, app, root, _, _ = workstation
    session_id = open_session(client)
    calls = []

    def unrecordable_apply(self, **kwargs):
        calls.append(self.session_id)
        (root / "completed.txt").write_text("mutation completed before receipt encoding")
        return invalid_result

    monkeypatch.setattr(WorkspaceService, "apply", unrecordable_apply)
    key = str(uuid4())
    receipt = command(client, session_id, "apply", {"token": "example", "confirmed": True}, key).json()
    assert receipt["state"] == "uncertain", receipt
    assert app.state.remote_receipts.get(key) == receipt
    assert (root / "completed.txt").read_text() == "mutation completed before receipt encoding"
    assert command(client, session_id, "apply", {"token": "example", "confirmed": True}, key).json() == receipt
    assert calls == [session_id]
    blocked = command(client, session_id, "update_settings", {"workers": 2}).json()
    assert blocked["state"] == "failed" and "uncertain" in blocked["error"]["detail"]
    assert command(client, session_id, "history").json()["state"] == "completed"


def test_preview_route_is_authenticated_and_returns_actual_bounded_png(workstation):
    from PIL import Image
    client, app, root, _, _ = workstation
    session_id = open_session(client)
    path = root / "preview.png"
    Image.new("RGB", (800, 600), "green").save(path)
    with Session(get_engine()) as db:
        file = File(session_id=session_id, path=str(path), filename=path.name,
                    size_bytes=path.stat().st_size, mime_type="image/png", status=FileStatus.PENDING)
        db.add(file)
        db.commit()
        file_id = file.id
    route = PREFIX + f"/sessions/{session_id}/files/{file_id}/preview"
    with TestClient(app) as unauthenticated:
        assert unauthenticated.get(route).status_code == 401
    response = client.get(route + "?width=120&height=90")
    assert response.status_code == 200, response.text
    image = Image.open(io.BytesIO(base64.b64decode(response.json()["image"])))
    assert image.format == "PNG" and image.size == (120, 90)
    detail = client.get(PREFIX + f"/sessions/{session_id}/files/{file_id}").json()
    assert detail["preview_revision"] != "unavailable"
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()["files"][0]["preview_revision"] == detail["preview_revision"]


def test_snapshot_and_file_detail_bound_descriptive_utf8_text(workstation):
    client, _, root, _, _ = workstation
    session_id = open_session(client)
    file_id = add_file(session_id, root / "text.txt")
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        file.ai_transcript = "π" * 50_000
        file.ai_description = "description " * 50_000
        db.commit()
    snapshot = client.get(PREFIX + f"/sessions/{session_id}/snapshot").json()
    detail = client.get(PREFIX + f"/sessions/{session_id}/files/{file_id}").json()
    for row in (snapshot["files"][0], detail):
        assert len(row["text"].encode("utf-8")) <= 16 * 1024
        assert len(row["ai_description"].encode("utf-8")) <= 16 * 1024
        assert row["text_truncated"] and row["ai_description_truncated"]


def test_offpage_proposal_and_keeper_have_scoped_live_preview_revisions(workstation):
    client, _, root, _, _ = workstation
    session_id = open_session(client)
    first_id = add_file(session_id, root / "a.txt")
    candidate_id = add_file(session_id, root / "z-candidate.png")
    keeper_id = add_file(session_id, root / "zz-keeper.png")
    with Session(get_engine()) as db:
        for file_id in (candidate_id, keeper_id):
            db.get(File, file_id).mime_type = "image/png"
        group = DuplicateGroup(session_id=session_id, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="offpage", keep_file_id=keeper_id)
        db.add(group)
        db.flush()
        db.add_all([DuplicateMember(group_id=group.id, file_id=candidate_id),
                    DuplicateMember(group_id=group.id, file_id=keeper_id)])
        db.add(Proposal(file_id=candidate_id, proposal_type=ProposalType.MARK_DUPLICATE,
                        current_value=str(root / "z-candidate.png"),
                        proposed_value=str(root / "zz-keeper.png"), duplicate_group_id=group.id))
        db.commit()
    before = client.get(PREFIX + f"/sessions/{session_id}/snapshot?limit=1").json()
    assert [file["id"] for file in before["files"]] == [first_id]
    proposal = before["proposals"][0]
    assert proposal["file_id"] == candidate_id and proposal["preview_revision"] != "unavailable"
    evidence = proposal["duplicate_evidence"]
    assert evidence["keeper_preview_revision"] != "unavailable"
    (root / "z-candidate.png").write_bytes(b"changed candidate bytes")
    (root / "zz-keeper.png").write_bytes(b"changed keeper bytes")
    after = client.get(PREFIX + f"/sessions/{session_id}/snapshot?limit=1").json()["proposals"][0]
    assert after["preview_revision"] != proposal["preview_revision"]
    assert after["duplicate_evidence"]["keeper_preview_revision"] != evidence["keeper_preview_revision"]
    # An off-page proposal must not authorize a file from a different root.
    with Session(get_engine()) as db:
        db.get(File, candidate_id).path = str(root.parent / "private.png")
        db.commit()
    assert client.get(PREFIX + f"/sessions/{session_id}/snapshot?limit=1").status_code == 403


def test_concurrent_claims_have_only_one_owner_and_do_not_replay(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    key, payload = str(uuid4()), {"session_id": "one", "action": "apply"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim(key, payload), range(12)))
    assert sum(claimed for _, claimed in claims) == 1
    assert {receipt["state"] for receipt, _ in claims} == {"running"}
    store.finish(key, result={"applied": 1})
    assert store.claim(key, payload)[0]["result"] == {"applied": 1}
    with pytest.raises(ReceiptConflict):
        store.claim(key, {"session_id": "one", "action": "undo"})

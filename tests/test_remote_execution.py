"""Real remote review, filesystem commits, lost replies, and recovery."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from donedatahoarder.core import jobs
from donedatahoarder.core.jobs import JobManager
from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType
from donedatahoarder.db.session import get_engine
from donedatahoarder.remote.client import RemoteConnection, RemoteError, RemoteSessionCatalog
from donedatahoarder.remote.server import create_app
from donedatahoarder.tui import onboarding, service as service_module

TOKEN = "remote-filesystem-test-" + "a" * 48


@pytest.fixture
def remote_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(JobManager, "_instance", None)
    manager = JobManager()
    for module in (jobs, onboarding, service_module):
        monkeypatch.setattr(module, "job_manager", manager)
    allowed = tmp_path / "workstation-files"
    allowed.mkdir()
    app = create_app(tmp_path / "workstation.db", token=TOKEN, allowed_roots=[allowed], name="HOME-PC")
    state = SimpleNamespace(root=allowed, posts=[], drop_apply_reply=False, guard=tmp_path / "laptop" / "endpoint.json", connections=[])
    with TestClient(app) as server:
        def forward(request):
            body = json.loads(request.content) if request.method == "POST" else None
            if body is not None:
                state.posts.append(body)
            response = server.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            if state.drop_apply_reply and body and body.get("command") == "apply":
                state.drop_apply_reply = False
                receipt = response.json()
                assert receipt["state"] == "completed", receipt
                assert receipt["result"]["applied"] == 1
                # The real endpoint, engine, filesystem mutation, and durable
                # receipt have all completed before the laptop loses its reply.
                raise httpx.ReadTimeout("connection lost after workstation commit", request=request)
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        def connect():
            connection = RemoteConnection("https://home-workstation.test", TOKEN,
                pending_file=state.guard, transport=httpx.MockTransport(forward))
            connection.connect()
            state.connections.append(connection)
            return connection

        state.connect = connect
        state.connection = connect()
        state.catalog = RemoteSessionCatalog(state.connection)
        yield state
    for connection in state.connections:
        connection.close()
    for thread in list(manager._worker_threads.values()):
        thread.join(timeout=5)


def proposed_rename(workspace, root, *, name="original.txt", destination="renamed.txt", content=b"original bytes survive remote review"):
    original, target = root / name, root / destination
    original.write_bytes(content)
    with Session(get_engine()) as db:
        file = File(session_id=workspace.session_id, path=str(original), filename=name,
                    size_bytes=len(content), mime_type="text/plain", status=FileStatus.PROPOSED,
                    hash_md5=hashlib.md5(content).hexdigest(), hash_sha256=hashlib.sha256(content).hexdigest(),
                    analysis_outcome="content_verified", analysis_evidence_source="text")
        db.add(file)
        db.flush()
        proposal = Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                            current_value=str(original), proposed_value=str(target),
                            confidence=.98, status=ProposalStatus.PENDING)
        db.add(proposal)
        db.commit()
        return SimpleNamespace(file_id=file.id, proposal_id=proposal.id,
                               source=original, target=target, content=content)


def approve_displayed(workspace, proposal_id):
    displayed = next(proposal for proposal in workspace.snapshot()["proposals"] if proposal["id"] == proposal_id)
    workspace.approve(proposal_id, review_token=displayed["review_token"])


def test_remote_apply_undo_require_fresh_confirmations_and_preserve_session_scope(remote_files):
    env = remote_files
    first_root, second_root = env.root / "first", env.root / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = env.catalog.open(root=str(first_root))
    second = env.catalog.open(root=str(second_root))
    own = proposed_rename(first, first_root)
    foreign = proposed_rename(second, second_root, content=b"another session stays untouched")

    for operation in (lambda: first.get_file(foreign.file_id), lambda: first.approve(foreign.proposal_id),
                      lambda: first.reject(foreign.proposal_id), lambda: first.edit(foreign.proposal_id, "stolen.txt")):
        with pytest.raises(RemoteError) as caught:
            operation()
        assert caught.value.status_code == 404
    assert all(row["file_id"] != foreign.file_id for row in first.snapshot()["proposals"])
    approve_displayed(first, own.proposal_id)
    preview = first.preview()
    assert preview["total"] == 1 and preview["errors"] == 0
    with pytest.raises(RemoteError, match="Confirm"):
        first.apply(preview["token"])
    with pytest.raises(RemoteError, match="changed"):
        second.apply(preview["token"], confirmed=True)

    # Change a reviewed destination after its preview; the old authorization
    # must not commit either the original or the newly edited operation.
    first.edit(own.proposal_id, "edited.txt")
    with pytest.raises(RemoteError, match="changed"):
        first.apply(preview["token"], confirmed=True)
    assert own.source.read_bytes() == own.content
    assert not own.target.exists()
    actual = first.preview()
    assert actual["token"] != preview["token"]
    assert actual["items"][0]["destination"] == str(first_root / "edited.txt")
    assert first.apply(actual["token"], confirmed=True)["applied"] == 1
    renamed = first_root / "edited.txt"
    assert renamed.read_bytes() == own.content
    assert not own.source.exists()
    history = first.history()
    assert len(history) == 1 and history[0]["state"] == "outstanding"

    recovery = first.undo_preview()
    with pytest.raises(RemoteError, match="Confirm"):
        first.undo(recovery["token"])
    with pytest.raises(RemoteError, match="changed"):
        second.undo(recovery["token"], confirmed=True)
    assert first.undo(recovery["token"], confirmed=True)["undone"] == 1
    assert own.source.read_bytes() == own.content
    assert not renamed.exists()
    assert first.history()[0]["state"] == "undone"
    with pytest.raises(RemoteError, match="changed"):
        first.undo(recovery["token"], confirmed=True)
    assert foreign.source.read_bytes() == foreign.content
    assert not foreign.target.exists()
    assert second.history() == []
    assert second.snapshot()["proposals"][0]["status"] == "pending"


def test_lost_apply_reply_after_actual_rename_recovers_without_repeating_post(remote_files):
    env = remote_files
    workspace = env.catalog.open(root=str(env.root))
    row = proposed_rename(workspace, env.root)
    approve_displayed(workspace, row.proposal_id)
    preview = workspace.preview()
    env.drop_apply_reply = True
    with pytest.raises(RemoteError, match="Cannot reach"):
        workspace.apply(preview["token"], confirmed=True)

    assert row.target.read_bytes() == row.content
    assert not row.source.exists()
    pending = env.connection.pending_request_id
    assert pending
    assert json.loads(env.guard.read_text())["request_id"] == pending
    with pytest.raises(RemoteError):
        workspace.apply(preview["token"], confirmed=True)
    assert len([post for post in env.posts if post.get("command") == "apply"]) == 1

    env.connection.close()
    posts_before_reconnect = len(env.posts)
    reconnected = env.connect()
    # Constructor + handshake recovered the persisted receipt using only GET.
    assert len(env.posts) == posts_before_reconnect
    assert reconnected.pending_request_id is None
    assert reconnected.last_receipt["request_id"] == pending
    assert reconnected.last_receipt["result"]["applied"] == 1
    assert json.loads(env.guard.read_text())["request_id"] is None
    resumed = RemoteSessionCatalog(reconnected).open(session_id=workspace.session_id)
    assert len(env.posts) == posts_before_reconnect
    history = resumed.snapshot()["history"]
    assert len(history) == 1
    assert history[0]["source"] == str(row.source)
    assert history[0]["destination"] == str(row.target)
    assert history[0]["state"] == "outstanding"
    assert resumed.get_file(row.file_id)["path"] == str(row.target)
    assert len([post for post in env.posts if post.get("command") == "apply"]) == 1

    recovery = resumed.undo_preview()
    assert resumed.undo(recovery["token"], confirmed=True)["undone"] == 1
    assert row.source.read_bytes() == row.content
    assert not row.target.exists()
    assert resumed.history()[0]["state"] == "undone"

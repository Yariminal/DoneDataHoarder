"""
Regression tests for the AI-provider thread-context bug.

The job system binds the AI provider to a contextvar via init_ai() in the
job's thread, but the *_with_progress wrappers run the actual work in an
inner worker thread. Contextvars do not propagate into new threads on their
own, so the wrappers must copy the caller's context into the worker —
otherwise get_client() raises (Organize) or silently degrades to non-LLM
fallbacks (Relate, Propose translation).
"""
import pytest

from donedatahoarder.ai import provider as provider_mod
from donedatahoarder.ai.router import get_client


class _FakeClient:
    def generate(self, *args, **kwargs):
        return "ok"

    def generate_json(self, *args, **kwargs):
        return []


class _FakeProvider:
    def __init__(self):
        self.client = _FakeClient()

    def get_client(self, failover: bool = True):
        return self.client


@pytest.fixture
def bound_provider():
    """Bind a fake AI provider to the current context, like init_ai() does."""
    fake = _FakeProvider()
    token = provider_mod._ai_provider_var.set(fake)
    yield fake
    provider_mod._ai_provider_var.reset(token)


def test_relate_worker_sees_provider(bound_provider, monkeypatch):
    import donedatahoarder.core.relate as relate_mod

    captured = {}

    def fake_relate(session_id, scope="per_directory", client=None, model=None,
                    progress_cb=None):
        captured["client"] = client
        return {"directories": 0, "groups": 0, "members": 0,
                "llm_groups": 0, "backstop_groups": 0}

    monkeypatch.setattr(relate_mod, "relate", fake_relate)
    events = list(relate_mod.relate_with_progress(session_id="test-session"))
    assert events[-1].get("done") is True
    assert captured["client"] is bound_provider.client


def test_propose_worker_sees_provider(bound_provider, monkeypatch):
    from donedatahoarder.proposals.namer import core as namer_core

    seen = {}

    def fake_generate_proposals(session_id=None, **kwargs):
        seen["client"] = get_client()
        return {"rename": 0, "tags": 0, "skipped": 0}

    monkeypatch.setattr(namer_core, "generate_proposals", fake_generate_proposals)
    events = list(namer_core.generate_proposals_with_progress(session_id="test-session"))
    assert events[-1].get("done") is True
    assert seen["client"] is bound_provider.client


def test_organize_worker_sees_provider(bound_provider, monkeypatch):
    from donedatahoarder.proposals.organizer import core as organizer_core

    seen = {}

    def fake_reorg(session_id=None):
        seen["client"] = get_client()
        return {"move": 0, "rename_folder": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(organizer_core, "generate_reorg_proposals", fake_reorg)
    events = list(organizer_core.generate_reorg_proposals_with_progress(session_id="test-session"))
    assert events[-1].get("done") is True
    assert seen["client"] is bound_provider.client

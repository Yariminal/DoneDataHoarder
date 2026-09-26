"""Worker threads only see the AI provider when the context is copied in."""
from __future__ import annotations

import contextvars
import threading

from donedatahoarder.ai.provider import _ai_provider_var, get_client, set_provider


class _StubProvider:
    def __init__(self) -> None:
        self.client = object()

    def get_client(self, failover: bool = True):
        return self.client


def _start(target, *, copy: bool) -> threading.Thread:
    """Start a daemon thread the same way the progress workers do."""
    if copy:
        worker = threading.Thread(
            target=contextvars.copy_context().run,
            args=(target,),
            daemon=True,
            name="provider-context-worker",
        )
    else:
        worker = threading.Thread(
            target=target,
            daemon=True,
            name="provider-context-bare",
        )
    worker.start()
    return worker


def test_copied_worker_thread_keeps_provider():
    """set_provider sticks when the worker is started via copy_context().run.

    A bare thread does not inherit the contextvar, so get_client must fail there.
    """
    provider = _StubProvider()
    previous = _ai_provider_var.get()
    set_provider(provider)
    try:
        copied: dict = {}
        bare: dict = {}

        def _in_copied_thread():
            copied["client"] = get_client()

        def _in_bare_thread():
            try:
                bare["client"] = get_client()
            except RuntimeError as exc:
                bare["error"] = exc

        copied_worker = _start(_in_copied_thread, copy=True)
        bare_worker = _start(_in_bare_thread, copy=False)
        copied_worker.join(timeout=5)
        bare_worker.join(timeout=5)

        assert not copied_worker.is_alive()
        assert not bare_worker.is_alive()
        assert copied["client"] is provider.client
        assert "client" not in bare
        assert isinstance(bare["error"], RuntimeError)
    finally:
        _ai_provider_var.set(previous)

"""
System wake lock — prevents the host OS from sleeping while pipeline jobs run.

Why this exists: long unattended runs (Analyze can take many hours on large
trees) silently stall when Windows hits its idle-sleep timer. CPU/network
activity does not reset that timer; the OS only watches user input. The
browser-side `navigator.wakeLock` we already use is released the moment the
tab is hidden / laptop is locked — exactly the "step away" scenario — so the
authoritative wake lock has to live in the server process.

Windows-specific: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`
keeps the system awake, but the state is bound to the calling thread and is
cleared when that thread exits. So we spawn a dedicated holder thread that
owns the state for the full duration of the wake session.

On non-Windows platforms this module is a no-op (acquire/release succeed
silently). macOS would use `caffeinate -i`, Linux varies — add when needed.
"""
from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_count = 0
_holder_thread: threading.Thread | None = None
_release_event: threading.Event | None = None


def _holder(release_event: threading.Event) -> None:
    if sys.platform != "win32":
        release_event.wait()
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        prev = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if prev == 0:
            logger.warning("Wake lock: SetThreadExecutionState returned 0 (acquire may have failed)")
        else:
            logger.info("Wake lock acquired — system sleep inhibited")
        try:
            release_event.wait()
        finally:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("Wake lock released")
    except Exception as exc:
        logger.warning("Wake lock holder error: %s", exc)
        release_event.wait()


def acquire() -> None:
    """Increment the ref count. Spawns the holder thread on the first acquire."""
    global _count, _holder_thread, _release_event
    with _lock:
        _count += 1
        if _count == 1:
            _release_event = threading.Event()
            _holder_thread = threading.Thread(
                target=_holder,
                args=(_release_event,),
                daemon=True,
                name="wake-lock-holder",
            )
            _holder_thread.start()


def release() -> None:
    """Decrement the ref count. Stops the holder thread when it reaches zero."""
    global _count, _holder_thread, _release_event
    with _lock:
        if _count <= 0:
            return
        _count -= 1
        if _count == 0 and _release_event is not None:
            _release_event.set()
            _holder_thread = None
            _release_event = None


def is_active() -> bool:
    with _lock:
        return _count > 0

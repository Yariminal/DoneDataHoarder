"""A database-scoped writer lock shared by CLI and web processes.

The lock file is intentionally persistent. The OS releases its byte lock when
the owning process exits, including after a crash; deleting the file would
allow two processes to lock different inodes at once.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class OperationBusyError(RuntimeError):
    """Another process or thread is changing this database's collection."""


_thread_state = threading.local()
_local_guard = threading.Lock()
_local_owners: dict[Path, int] = {}


def _lock_path(db_path: Path | None) -> Path:
    if db_path is None:
        from donedatahoarder.db.session import get_engine

        database = get_engine().url.database
        if not database:
            raise ValueError("An on-disk database is required for operation locking")
        db_path = Path(database)
    return Path(str(Path(db_path).resolve()) + ".operations.lock")


def _acquire_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def operation_lock(
    phase: str, *, db_path: Path | None = None, timeout: float = 0,
) -> Iterator[None]:
    """Serialize filesystem/database writer phases across processes.

    A nested call on the same thread and database shares its outer lock.
    ``timeout=0`` fails immediately; a positive timeout retries briefly.
    """
    path = _lock_path(db_path)
    held = getattr(_thread_state, "held", None)
    if held is None:
        held = _thread_state.held = {}
    if path in held:
        held[path] += 1
        try:
            yield
        finally:
            held[path] -= 1
        return

    deadline = time.monotonic() + max(timeout, 0)
    handle = None
    owner = threading.get_ident()
    while True:
        with _local_guard:
            locally_busy = path in _local_owners
            if not locally_busy:
                _local_owners[path] = owner
        if not locally_busy:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                _acquire_os_lock(handle)
                break
            except (OSError, BlockingIOError):
                if handle is not None:
                    handle.close()
                    handle = None
                with _local_guard:
                    _local_owners.pop(path, None)
        if time.monotonic() >= deadline:
            raise OperationBusyError(
                f"Cannot start {phase}: another operation holds {path}"
            )
        time.sleep(min(0.05, max(deadline - time.monotonic(), 0)))

    held[path] = 1
    try:
        yield
    finally:
        held.pop(path, None)
        try:
            _release_os_lock(handle)
        finally:
            handle.close()
            with _local_guard:
                _local_owners.pop(path, None)

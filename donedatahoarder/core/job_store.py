"""SQLite-backed job/run-plan checkpoints shared by server processes."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from donedatahoarder.db.models import BackgroundJob, RunPlan
from donedatahoarder.db.session import get_engine
from donedatahoarder.timeutils import utcnow


LIVE_STATES = ("running", "paused", "cancelling")


def process_started_at(pid: int | None) -> datetime | None:
    """OS creation time, used to reject a recycled PID during recovery."""
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FileTime)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            created, exited, kernel_time, user_time = (FileTime() for _ in range(4))
            ok = kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel_time), ctypes.byref(user_time))
            if not ok:
                return None
            ticks = (created.high << 32) | created.low
            return datetime.fromtimestamp((ticks - 116444736000000000) / 10_000_000,
                                          tz=timezone.utc).replace(tzinfo=None)
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
        start_ticks = int(stat.rsplit(") ", 1)[1].split()[19])
        with open("/proc/stat", encoding="ascii") as stream:
            boot_time = next(int(line.split()[1]) for line in stream if line.startswith("btime "))
        epoch = boot_time + start_ticks / os.sysconf("SC_CLK_TCK")
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def pid_alive(pid: int | None, expected_start: datetime | None = None) -> bool:
    actual_start = process_started_at(pid)
    if actual_start is not None:
        return expected_start is None or abs((actual_start - expected_start).total_seconds()) < 2
    if not pid or pid <= 0:
        return False
    # Creation metadata may be inaccessible on macOS or an OS-protected
    # process. Mark interrupted only when process absence is established.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                exit_code = wintypes.DWORD()
                if kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == 259
                return True  # metadata denied/unknown: do not interrupt
            finally:
                kernel.CloseHandle(handle)
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def job_dict(row: BackgroundJob) -> dict:
    return {
        "job_id": row.id, "job_type": row.job_type, "session_id": row.session_id,
        "state": row.state, "progress": json.loads(row.progress_json or "{}"),
        "error": row.error, "run_plan_id": row.run_plan_id,
        "cancel_requested": bool(row.cancel_requested),
        "owner_pid": row.owner_pid,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def plan_dict(row: RunPlan) -> dict:
    return {
        "plan_id": row.id, "session_id": row.session_id, "state": row.state,
        "steps": json.loads(row.steps_json or "[]"),
        "options": json.loads(row.options_json or "{}"),
        "completed_steps": json.loads(row.completed_steps_json or "[]"),
        "checkpoint": json.loads(row.checkpoint_json or "{}"),
        "current_index": row.current_index, "active_job_id": row.active_job_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def reconcile(owner_token: str) -> None:
    """Only declare work interrupted when its owning OS process is dead."""
    with Session(get_engine()) as db:
        for row in db.query(BackgroundJob).filter(BackgroundJob.state.in_(LIVE_STATES)):
            if row.owner_token == owner_token or pid_alive(row.owner_pid, row.owner_started_at):
                continue
            row.state = "interrupted"
            row.error = "Worker process exited before recording completion"
            row.finished_at = utcnow()
            if row.run_plan_id:
                plan = db.get(RunPlan, row.run_plan_id)
                if plan and plan.active_job_id == row.id:
                    plan.active_job_id = None
                    plan.state = "interrupted"
                    plan.updated_at = utcnow()
        db.commit()


def request_cancel(job_id: str) -> bool:
    with Session(get_engine()) as db:
        row = db.get(BackgroundJob, job_id)
        if not row or row.state not in LIVE_STATES:
            return False
        row.cancel_requested = True
        row.state = "cancelling"
        db.commit()
        return True


def cancel_requested(job_id: str) -> bool:
    with Session(get_engine()) as db:
        row = db.get(BackgroundJob, job_id)
        return bool(row and row.cancel_requested)


def heartbeat(job_id: str, owner_token: str) -> None:
    with Session(get_engine()) as db:
        db.execute(update(BackgroundJob).where(
            BackgroundJob.id == job_id, BackgroundJob.owner_token == owner_token,
            BackgroundJob.state.in_(LIVE_STATES),
        ).values(heartbeat_at=utcnow()))
        db.commit()

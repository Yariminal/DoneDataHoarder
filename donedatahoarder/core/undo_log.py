"""
Transaction logging and undo support for executor operations.

Logs every applied change to ~/.datahoarder/undo.log with:
- operation type (MOVE, RENAME, DELETE)
- original path → new path
- timestamp
- sha256 hash of content (for verification)
"""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from donedatahoarder.core.process_lock import operation_lock


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_datahoarder_dir(*, create: bool = True) -> Path:
    """Get the DoneDataHoarder data directory (~/.datahoarder)."""
    override = os.environ.get("DDH_DATA_DIR")
    if override:
        dh_dir = Path(override).expanduser().resolve()
        if create:
            dh_dir.mkdir(parents=True, exist_ok=True)
        return dh_dir
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path.home()
    dh_dir = base / ".datahoarder"
    if create:
        dh_dir.mkdir(parents=True, exist_ok=True)
    return dh_dir


def get_undo_log_path(session_id: Optional[str] = None) -> Path:
    """Get path to the undo log file."""
    dh_dir = get_datahoarder_dir()
    if session_id:
        if Path(session_id).name != session_id or any(c in session_id for c in ("/", "\\")):
            raise ValueError("Invalid session_id for undo log")
        # Session-specific log for isolated undo
        return dh_dir / f"undo_{session_id}.log"
    return dh_dir / "undo.log"


def _compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of file contents for verification."""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except (OSError, IOError):
        return ""


# ---------------------------------------------------------------------------
# Log entries
# ---------------------------------------------------------------------------

def log_operation(
    operation: str,
    original_path: str,
    new_path: str,
    session_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """
    Log a single operation to the undo log.

    Returns the log entry dict for potential use.
    """
    original = Path(original_path)
    sha256 = ""
    source_type = ("symlink" if original.is_symlink() else
                   "file" if original.is_file() else
                   "directory" if original.is_dir() else "missing")
    directory_identity = None
    if source_type == "directory":
        stat = original.stat()
        directory_identity = [stat.st_dev, stat.st_ino]
    if operation != "TAGS" and original.exists() and original.is_file():
        sha256 = _compute_sha256(original)
        if not sha256:
            raise OSError(f"Cannot verify file content before {operation}: {original}")

    entry = {
        "operation_id": str(uuid.uuid4()),
        "phase": "intent",
        "operation": operation,
        "original_path": original_path,
        "new_path": new_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256,
        "source_type": source_type,
        "directory_identity": directory_identity,
        "session_id": session_id,
        "extra": extra or {},
    }

    log_path = get_undo_log_path(session_id)
    _append_entry(log_path, entry)

    return entry


def _append_entry(log_path: Path, entry: dict) -> None:
    """Flush a journal event before the next filesystem transition."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def complete_operation(entry: dict) -> None:
    """Record that the filesystem transition finished."""
    _append_entry(get_undo_log_path(entry.get("session_id")), {
        "operation": "OP_COMPLETE",
        "operation_id": entry["operation_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": entry.get("session_id"),
    })


def complete_database_operation(entry: dict) -> None:
    """Record that the indexed state was committed after the disk change."""
    _append_entry(get_undo_log_path(entry.get("session_id")), {
        "operation": "DB_COMPLETE",
        "operation_id": entry["operation_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": entry.get("session_id"),
    })


def _safe_path(path_str: str) -> Path:
    """Safely create a Path object, handling escaped backslashes."""
    # Handle Windows paths with backslashes in JSON
    return Path(path_str)


def parse_undo_log(session_id: Optional[str] = None) -> list[dict]:
    """Parse all entries from the undo log file."""
    log_path = get_undo_log_path(session_id)
    entries = []
    if not log_path.exists():
        return entries

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return entries


def get_last_session_entries(session_id: Optional[str] = None) -> list[dict]:
    """
    Get outstanding operations for one session. Without a session ID, return
    the most recent five-minute group in the legacy global log.
    """
    events = parse_undo_log(session_id)
    operations: list[dict] = []
    by_id: dict[str, dict] = {}
    undone: set[str] = set()
    legacy_pending: list[dict] = []
    for event in events:
        op = event.get("operation")
        op_id = event.get("operation_id")
        if op == "OP_COMPLETE":
            if op_id in by_id:
                by_id[op_id]["phase"] = "complete"
        elif op == "DB_COMPLETE":
            if op_id in by_id:
                by_id[op_id]["_db_complete"] = True
        elif op == "UNDO_COMPLETE":
            undone.add(op_id)
        elif op == "UNDO_MARKER":
            # Legacy markers contain a count only. They cannot identify which
            # operations succeeded in a partial undo, so exclude that many
            # preceding legacy entries rather than replaying possible restores.
            for _ in range(min(int(event.get("undone_count", 0)), len(legacy_pending))):
                legacy_pending.pop()["_legacy_undone"] = True
        elif op in {"MOVE", "RENAME", "RENAME_FOLDER", "TRASH", "DELETE", "JUNK_TRASH", "TAGS"}:
            operation = event.copy()
            if not operation.get("phase"):
                operation["phase"] = "complete"  # legacy post-move log
            operations.append(operation)
            if op_id:
                by_id[op_id] = operation
            else:
                legacy_pending.append(operation)
    if session_id:
        return [e for e in operations if e.get("session_id") == session_id
                and _entry_id(e) not in undone and not e.get("_legacy_undone")]
    if not operations:
        return []
    last_ts = datetime.fromisoformat(operations[-1]["timestamp"]).timestamp()
    return [e for e in operations
            if datetime.fromisoformat(e["timestamp"]).timestamp() >= last_ts - 300
            and _entry_id(e) not in undone and not e.get("_legacy_undone")]


# ---------------------------------------------------------------------------
# Undo operations
# ---------------------------------------------------------------------------

def _entry_id(entry: dict) -> str:
    """Stable identity for new and pre-journal legacy operations."""
    return entry.get("operation_id") or hashlib.sha256(
        json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _replace_prefix(value: str, old: str, new: str) -> str:
    if value == old:
        return new
    for sep in ("/", "\\"):
        if value.startswith(old + sep):
            return new + value[len(old):]
    return value


def _remove_created_empty_dirs(entry: dict) -> None:
    """Remove only directories the journal says this operation created."""
    created = (entry.get("extra") or {}).get("created_dirs") or []
    for raw in reversed(created):
        directory = Path(raw)
        try:
            directory.rmdir()
        except OSError:
            # A non-empty directory may contain another operation or a user
            # file, so it must be left in place.
            pass


def _verify_restore_source(entry: dict, path: Path) -> None:
    """Require journaled identity before restoring any filesystem entry.

    Legacy entries with recorded hashes remain recoverable. An absent hash or
    directory identity cannot prove that a replacement belongs to the journal.
    """
    if entry.get("source_type") == "directory" or entry["operation"] == "RENAME_FOLDER":
        identity = entry.get("directory_identity")
        if identity is None:
            raise ValueError(f"No recorded directory identity for {path}; manual recovery is required")
        if not path.is_dir() or [path.stat().st_dev, path.stat().st_ino] != identity:
            raise ValueError(f"Folder identity changed at {path}")
        return
    expected = entry.get("sha256")
    if not expected:
        raise ValueError(f"No recorded SHA-256 for {path}; manual recovery is required")
    if entry.get("source_type") not in (None, "file") or not path.is_file():
        raise ValueError(f"Source is no longer a regular file: {path}")
    if _compute_sha256(path) != expected:
        raise ValueError(f"File hash mismatch for {path.name}")


def _assert_proposal_state(entry: dict, *, allow_restored: bool = False) -> None:
    """Refuse to overwrite review edits made after this operation committed."""
    expected = (entry.get("extra") or {}).get("proposal_expected") or []
    if not expected:
        return  # older journals did not record post-operation review state
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import Proposal
    from donedatahoarder.db.session import get_engine

    extra = entry.get("extra") or {}
    prior = {snapshot["id"]: snapshot for snapshot in extra.get("proposal_snapshots", [])}
    prior_status = extra.get("proposal_status")
    with Session(get_engine()) as db:
        current = {row["id"]: db.get(Proposal, row["id"]) for row in expected}
        if any(proposal is None for proposal in current.values()):
            raise ValueError("A journaled proposal is missing; review undo manually")

        def matches(row: dict, *, restored: bool) -> bool:
            proposal = current[row["id"]]
            prior_row = prior.get(row["id"])
            if restored and prior_row is not None:
                target = prior_row
            elif restored and row["id"] == extra.get("proposal_id"):
                target = {**row, "status": prior_status}
            else:
                target = row
            return (proposal.current_value == target["current_value"]
                    and proposal.proposed_value == target["proposed_value"]
                    and proposal.status.value == target["status"])

        if all(matches(row, restored=False) for row in expected):
            return
        if allow_restored and all(matches(row, restored=True) for row in expected):
            return
    raise ValueError("Proposal changed after execution; undo would overwrite a review edit")


def _restore_database(entry: dict) -> None:
    """Reconcile indexed paths and review state after filesystem restoration."""
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus
    from donedatahoarder.db.session import get_engine

    extra = entry.get("extra") or {}
    source = entry["original_path"]
    destination = entry["new_path"]
    session_id = entry.get("session_id")
    with Session(get_engine()) as db:
        if entry["operation"] == "RENAME_FOLDER":
            files = db.query(File).filter(File.path.like(f"{destination}%"))
            if session_id:
                files = files.filter(File.session_id == session_id)
            for file in files.all():
                restored = _replace_prefix(file.path, destination, source)
                if restored != file.path:
                    file.path = restored
                    file.filename = Path(restored).name
            if not extra.get("proposal_snapshots"):
                proposals = db.query(Proposal).join(File)
                if session_id:
                    proposals = proposals.filter(File.session_id == session_id)
                for proposal in proposals.all():
                    if proposal.id == extra.get("proposal_id"):
                        continue
                    if proposal.current_value:
                        proposal.current_value = _replace_prefix(proposal.current_value, destination, source)
                    if proposal.proposed_value:
                        proposal.proposed_value = _replace_prefix(proposal.proposed_value, destination, source)
        else:
            file = db.get(File, extra["file_id"]) if extra.get("file_id") else None
            if file and file.path == destination:
                file.path = source
                file.filename = Path(source).name
            # Legacy entries lack file_id; use the unique indexed path.
            elif not file:
                files = db.query(File).filter(File.path == destination)
                if session_id:
                    files = files.filter(File.session_id == session_id)
                matches = files.all()
                if len(matches) > 1:
                    raise ValueError("Ambiguous legacy undo path across sessions")
                file = matches[0] if matches else None
                if file:
                    file.path = source
                    file.filename = Path(source).name
            if entry["operation"] == "RENAME" and extra.get("file_id") and not extra.get("proposal_snapshots"):
                for related in db.query(Proposal).filter(Proposal.file_id == extra["file_id"]).all():
                    if related.id == extra.get("proposal_id"):
                        continue
                    if related.current_value:
                        related.current_value = _replace_prefix(related.current_value, destination, source)
                    if related.proposed_value:
                        # A rename cascade also substituted the new basename
                        # into the destination of a later MOVE proposal.
                        proposed = Path(related.proposed_value)
                        if proposed.name == Path(destination).name:
                            related.proposed_value = str(proposed.with_name(Path(source).name))
        if extra.get("file_id"):
            file = db.get(File, extra["file_id"])
            if file and extra.get("file_status"):
                file.status = FileStatus(extra["file_status"])
        if extra.get("proposal_id"):
            proposal = db.get(Proposal, extra["proposal_id"])
            if proposal:
                proposal.status = ProposalStatus(extra.get("proposal_status") or "approved")
                proposal.applied_at = None
        for snapshot in extra.get("proposal_snapshots", []):
            proposal = db.get(Proposal, snapshot["id"])
            if proposal:
                proposal.current_value = snapshot["current_value"]
                proposal.proposed_value = snapshot["proposed_value"]
                proposal.status = ProposalStatus(snapshot["status"])
                proposal.applied_at = (datetime.fromisoformat(snapshot["applied_at"])
                                       if snapshot.get("applied_at") else None)
        db.commit()

def _undo_operations_unlocked(
    session_id: Optional[str] = None,
    force: bool = False,
    console = None,
) -> dict:
    """
    Reverse outstanding operations one at a time, newest first. Successful
    restores are recorded individually; failures remain available for retry.

    Returns a summary dict with results.
    """
    from rich.console import Console

    con = console or Console()

    entries = get_last_session_entries(session_id)
    if not entries:
        con.print("[yellow]No operations to undo.[/yellow]")
        return {"undone": 0, "failed": 0, "skipped": 0, "entries": []}

    if not force:
        con.print(f"[bold yellow]Found {len(entries)} operations to undo:[/bold yellow]")
        for entry in entries:
            op = entry["operation"]
            orig = entry["original_path"]
            new = entry["new_path"]
            con.print(f"  [{op}] {new} → {orig}")

        import typer

        confirm = typer.confirm("Undo these operations?", default=False)
        if not confirm:
            con.print("[yellow]Undo cancelled.[/yellow]")
            return {"undone": 0, "failed": 0, "skipped": 0, "cancelled": True}

    # Reverse order so dependent moves return before their earlier renames.
    counts = {"undone": 0, "failed": 0, "skipped": 0}
    undone_entries = []

    con.print(f"\n[bold]Undoing {len(entries)} operations (in reverse order)...[/bold]")

    for entry in reversed(entries):
        op = entry["operation"]
        original = _safe_path(entry["original_path"])
        new = _safe_path(entry["new_path"])
        sha256_expected = entry.get("sha256", "")

        try:
            _assert_proposal_state(
                entry,
                allow_restored=(not entry.get("_db_complete") or op == "TAGS"
                                or (not new.exists() and original.exists())),
            )
            if op == "TAGS":
                _restore_database(entry)
                _append_entry(get_undo_log_path(session_id), {
                    "operation": "UNDO_COMPLETE", "operation_id": _entry_id(entry),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "session_id": session_id,
                })
                counts["undone"] += 1
                undone_entries.append(entry)
                continue
            if entry.get("operation_id") and not new.exists() and original.exists():
                # Crash before mutation, or crash after restoring files but
                # before marking the undo complete. Only a matching file hash
                # proves that this is the journaled source rather than an
                # unrelated replacement at the same path.
                if entry.get("source_type") == "directory":
                    identity = entry.get("directory_identity")
                    verified = (original.is_dir() and identity is not None
                                and [original.stat().st_dev, original.stat().st_ino] == identity)
                else:
                    verified = (entry.get("source_type") == "file" and original.is_file()
                                and bool(sha256_expected)
                                and _compute_sha256(original) == sha256_expected)
                if not verified:
                    con.print(f"  [red]✗[/red] {op}: Cannot verify restored source {original}")
                    counts["failed"] += 1
                    continue
                if entry.get("phase") == "complete":
                    _restore_database(entry)
                _remove_created_empty_dirs(entry)
                _append_entry(get_undo_log_path(session_id), {
                    "operation": "UNDO_COMPLETE", "operation_id": _entry_id(entry),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "session_id": session_id,
                })
                counts["undone"] += 1
                undone_entries.append(entry)
                continue
            if op in ("MOVE", "RENAME"):
                # Reverse: move from new_path back to original_path
                if not new.exists():
                    con.print(f"  [red]✗[/red] {op}: Source not found {new}")
                    counts["failed"] += 1
                    continue

                _verify_restore_source(entry, new)

                # Check if destination already exists
                if original.exists() and original != new:
                    con.print(
                        f"  [red]✗[/red] {op}: Destination already exists {original}"
                    )
                    counts["failed"] += 1
                    continue

                # Ensure parent directory exists
                original.parent.mkdir(parents=True, exist_ok=True)

                # Perform the reverse move
                import shutil
                shutil.move(str(new), str(original))
                con.print(f"  [green]✓[/green] {op}: {new.name} → {original.parent}/{original.name}")

            elif op in ("DELETE", "TRASH", "JUNK_TRASH"):
                # Reverse: move from trash back to original location.
                # The log's new_path records the exact trash destination
                # (session-root .ddh_trash, possibly with a collision suffix)
                # — prefer it over re-deriving the location.
                trash_path = new
                if not trash_path.exists():
                    # Fallback for old log entries: look next to the original
                    trash_dir = original.parent / ".ddh_trash"
                    trash_path = trash_dir / original.name
                    if not trash_path.exists():
                        # Look for numbered variants
                        stem, suffix = original.stem, original.suffix
                        for i in range(1, 100):
                            alt = trash_dir / f"{stem}_{i}{suffix}"
                            if alt.exists():
                                trash_path = alt
                                break

                if not trash_path.exists():
                    con.print(f"  [red]✗[/red] {op}: File not in trash {original.name}")
                    counts["failed"] += 1
                    continue

                if original.exists():
                    con.print(f"  [red]✗[/red] {op}: Destination already exists {original}")
                    counts["failed"] += 1
                    continue
                _verify_restore_source(entry, trash_path)

                # Ensure original directory exists
                original.parent.mkdir(parents=True, exist_ok=True)

                import shutil
                shutil.move(str(trash_path), str(original))
                con.print(f"  [green]✓[/green] {op}: Restored {original.name} from trash")

            elif op == "RENAME_FOLDER":
                # Reverse: rename folder back
                if not new.exists():
                    con.print(f"  [red]✗[/red] {op}: Folder not found {new}")
                    counts["failed"] += 1
                    continue

                _verify_restore_source(entry, new)

                if original.exists() and original != new:
                    con.print(
                        f"  [red]✗[/red] {op}: Destination folder already exists {original}"
                    )
                    counts["failed"] += 1
                    continue

                new.rename(original)
                con.print(f"  [green]✓[/green] {op}: {new.name} → {original.name}")

            else:
                con.print(f"  [yellow]⚠[/yellow] Unknown operation type: {op}")
                counts["skipped"] += 1
                continue

            _restore_database(entry)
            _remove_created_empty_dirs(entry)
            _append_entry(get_undo_log_path(session_id), {
                "operation": "UNDO_COMPLETE", "operation_id": _entry_id(entry),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
            })
            counts["undone"] += 1
            undone_entries.append(entry)

        except Exception as exc:
            con.print(f"  [red]✗[/red] {op} failed: {exc}")
            counts["failed"] += 1

    con.print(
        f"\n[bold]Done:[/bold] {counts['undone']} undone, {counts['failed']} failed, {counts['skipped']} skipped"
    )

    return {
        "undone": counts["undone"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "entries": undone_entries,
    }


def undo_operations(
    session_id: Optional[str] = None,
    force: bool = False,
    console=None,
) -> dict:
    """Undo under the same cross-process writer lock used by execution."""
    with operation_lock("undo"):
        return _undo_operations_unlocked(session_id=session_id, force=force, console=console)


def _mark_entries_undone(entries: list[dict], session_id: Optional[str] = None) -> None:
    """Mark log entries as undone by appending an undo marker."""
    log_path = get_undo_log_path(session_id)
    marker = {
        "operation": "UNDO_MARKER",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "undone_count": len(entries),
        "session_id": session_id,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(marker, ensure_ascii=False) + "\n")


def latest_undo_session_id() -> Optional[str]:
    """Find the session with the most recent outstanding journal operation."""
    latest: tuple[str, str] | None = None
    for path in get_datahoarder_dir().glob("undo_*.log"):
        session_id = path.stem.removeprefix("undo_")
        entries = get_last_session_entries(session_id)
        if entries:
            timestamp = entries[-1].get("timestamp", "")
            if latest is None or timestamp > latest[0]:
                latest = (timestamp, session_id)
    return latest[1] if latest else None


def list_undo_sessions() -> list[dict]:
    """List available undo sessions from the log."""
    all_entries = parse_undo_log()

    # Group by time windows
    sessions = []
    current_session = []
    last_ts = None

    for entry in all_entries:
        if entry["operation"] == "UNDO_MARKER":
            continue

        entry_ts = datetime.fromisoformat(entry["timestamp"])

        if last_ts is None or (entry_ts.timestamp() - last_ts.timestamp()) > 300:
            # New session (5+ min gap)
            if current_session:
                sessions.append(_summarize_session(current_session))
            current_session = [entry]
        else:
            current_session.append(entry)

        last_ts = entry_ts

    if current_session:
        sessions.append(_summarize_session(current_session))

    for path in get_datahoarder_dir().glob("undo_*.log"):
        session_id = path.stem.removeprefix("undo_")
        outstanding = get_last_session_entries(session_id)
        if outstanding:
            summary = _summarize_session(outstanding)
            summary["session_id"] = session_id
            sessions.append(summary)

    sessions.sort(key=lambda item: item["timestamp"])

    return sessions


def _summarize_session(entries: list[dict]) -> dict:
    """Create a summary of a session from its entries."""
    if not entries:
        return {}

    first_ts = datetime.fromisoformat(entries[0]["timestamp"])
    last_ts = datetime.fromisoformat(entries[-1]["timestamp"])

    op_counts = {}
    for e in entries:
        op = e["operation"]
        op_counts[op] = op_counts.get(op, 0) + 1

    return {
        "timestamp": first_ts.isoformat(),
        "operation_count": len(entries),
        "operations": op_counts,
        "duration_seconds": (last_ts - first_ts).total_seconds(),
    }


# ---------------------------------------------------------------------------
# Clear log
# ---------------------------------------------------------------------------

def clear_undo_log(session_id: Optional[str] = None, keep_last_n: int = 0) -> int:
    """
    Clear the undo log file.

    If keep_last_n > 0, keep the last N operations.
    Returns the number of entries removed.
    """
    log_path = get_undo_log_path(session_id)
    if not log_path.exists():
        return 0

    entries = parse_undo_log(session_id)

    if keep_last_n > 0:
        entries = entries[-keep_last_n:]
    else:
        entries = []

    # Rewrite the file
    with open(log_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return len(entries)

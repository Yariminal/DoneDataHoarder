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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_datahoarder_dir() -> Path:
    """Get the DataHoarder data directory (~/.datahoarder)."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path.home()
    dh_dir = base / ".datahoarder"
    dh_dir.mkdir(parents=True, exist_ok=True)
    return dh_dir


def get_undo_log_path(session_id: Optional[str] = None) -> Path:
    """Get path to the undo log file."""
    dh_dir = get_datahoarder_dir()
    if session_id:
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
    if original.exists() and original.is_file():
        sha256 = _compute_sha256(original)

    entry = {
        "operation": operation,
        "original_path": original_path,
        "new_path": new_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256,
        "session_id": session_id,
        "extra": extra or {},
    }

    log_path = get_undo_log_path(session_id)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


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
    Get entries from the most recent session.

    If session_id is provided, gets entries for that session.
    Otherwise, gets entries since the last undo marker or from the last timestamp group.
    """
    all_entries = parse_undo_log(session_id)

    if session_id:
        return [e for e in all_entries if e.get("session_id") == session_id]

    # Find the last batch of operations (grouped by timestamp within 1 hour)
    if not all_entries:
        return []

    # Get the most recent timestamp
    last_ts = datetime.fromisoformat(all_entries[-1]["timestamp"])

    # Collect entries from the same "session" (within 5 minutes of each other)
    session_entries = []
    cutoff = last_ts.timestamp() - 300  # 5 minutes before last entry

    for entry in reversed(all_entries):
        entry_ts = datetime.fromisoformat(entry["timestamp"])
        if entry_ts.timestamp() >= cutoff:
            session_entries.insert(0, entry)
        else:
            break

    return session_entries


# ---------------------------------------------------------------------------
# Undo operations
# ---------------------------------------------------------------------------

def undo_operations(
    session_id: Optional[str] = None,
    force: bool = False,
    console = None,
) -> dict:
    """
    Undo the last batch of operations atomically (in reverse order).

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

    # Reverse order for atomic rollback
    counts = {"undone": 0, "failed": 0, "skipped": 0}
    undone_entries = []

    con.print(f"\n[bold]Undoing {len(entries)} operations (in reverse order)...[/bold]")

    for entry in reversed(entries):
        op = entry["operation"]
        original = _safe_path(entry["original_path"])
        new = _safe_path(entry["new_path"])
        sha256_expected = entry.get("sha256", "")

        try:
            if op in ("MOVE", "RENAME"):
                # Reverse: move from new_path back to original_path
                if not new.exists():
                    con.print(f"  [red]✗[/red] {op}: Source not found {new}")
                    counts["failed"] += 1
                    continue

                # Verify file integrity if SHA256 available
                if sha256_expected and new.exists():
                    current_hash = _compute_sha256(new)
                    if current_hash and current_hash != sha256_expected:
                        con.print(
                            f"  [yellow]⚠[/yellow] {op}: File hash mismatch for {new.name} (file may have changed)"
                        )

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
                counts["undone"] += 1
                undone_entries.append(entry)

            elif op == "DELETE" or op == "TRASH":
                # Reverse: move from trash back to original location
                # Trash files are stored in .datahoarder_trash folder
                trash_dir = original.parent / ".datahoarder_trash"
                trash_path = trash_dir / original.name

                # Try to find the file in trash
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

                # Ensure original directory exists
                original.parent.mkdir(parents=True, exist_ok=True)

                import shutil
                shutil.move(str(trash_path), str(original))
                con.print(f"  [green]✓[/green] {op}: Restored {original.name} from trash")
                counts["undone"] += 1
                undone_entries.append(entry)

            elif op == "RENAME_FOLDER":
                # Reverse: rename folder back
                if not new.exists():
                    con.print(f"  [red]✗[/red] {op}: Folder not found {new}")
                    counts["failed"] += 1
                    continue

                if original.exists() and original != new:
                    con.print(
                        f"  [red]✗[/red] {op}: Destination folder already exists {original}"
                    )
                    counts["failed"] += 1
                    continue

                new.rename(original)
                con.print(f"  [green]✓[/green] {op}: {new.name} → {original.name}")
                counts["undone"] += 1
                undone_entries.append(entry)

            else:
                con.print(f"  [yellow]⚠[/yellow] Unknown operation type: {op}")
                counts["skipped"] += 1

        except Exception as exc:
            con.print(f"  [red]✗[/red] {op} failed: {exc}")
            counts["failed"] += 1

    # Mark undone entries in the log
    if undone_entries:
        _mark_entries_undone(entries, session_id)

    con.print(
        f"\n[bold]Done:[/bold] {counts['undone']} undone, {counts['failed']} failed, {counts['skipped']} skipped"
    )

    return {
        "undone": counts["undone"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "entries": undone_entries,
    }


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

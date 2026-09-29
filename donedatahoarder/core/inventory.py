"""Read-only, streaming reconciliation of a collection with one indexed session.

The JSONL ledger is private collection evidence. It records names and database
provenance, but never reads file contents or changes the index.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import TextIO

from donedatahoarder.analyzers.format_policy import disposition
from donedatahoarder.core.ignore import load_ddhignore
from donedatahoarder.core.scanner import (
    SKIP_DIRS, _is_link_or_reparse, directory_exclusion, file_exclusion,
)


_COLUMNS = (
    "id, status, analysis_outcome, analysis_reason, analysis_evidence_source, "
    "analysis_model_tag, analysis_model_digest, analysis_prompt_version, "
    "analysis_extractor_version, analysis_cache_hit"
)


def _write(ledger: TextIO, record: dict) -> None:
    ledger.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def reconcile_inventory(root: Path, database: Path, session_id: str,
                        ledger: TextIO, *, extra_skip_dirs: set[str] | None = None) -> dict:
    """Stream one record per regular file, plus explicit walk/read errors.

    ``database`` must be an existing SQLite database. This function uses a
    read-only URI rather than the application's migrating ``init_db`` path.
    The caller should place the ledger outside ``root`` so it cannot affect
    its own inventory.
    """
    original_root = Path(root)
    if _is_link_or_reparse(original_root):
        raise ValueError("Collection root is absent, unreadable, or a reparse point")
    root = original_root.resolve()
    database = Path(database).resolve()
    if not root.is_dir():
        raise ValueError("Collection root is absent, unreadable, or a reparse point")
    if not database.is_file():
        raise ValueError("Existing SQLite database required")
    stream_name = getattr(ledger, "name", None)
    if isinstance(stream_name, (str, os.PathLike)) and not str(stream_name).startswith("<"):
        if Path(stream_name).resolve().is_relative_to(root):
            raise ValueError("Inventory ledger must be outside the collection root")
    ddhignore = load_ddhignore(root)
    active_db = {database, Path(f"{database}-wal"), Path(f"{database}-shm"),
                 Path(f"{database}-journal")}
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    bytes_by_decision: Counter[str] = Counter()
    errors: list[dict] = []
    error_count = 0

    def record_error(error: dict) -> None:
        nonlocal error_count
        _write(ledger, error)
        error_count += 1
        if len(errors) < 20:
            errors.append(error)

    # mode=ro forbids schema migration or writes; query_only is a second guard.
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        # A read transaction pins one WAL snapshot for all per-path lookups
        # and final totals while a separate writer may commit new rows.
        connection.execute("BEGIN")
        owner = connection.execute(
            "SELECT root_path FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if owner is None or Path(owner[0]).resolve() != root:
            raise ValueError("Session is absent or belongs to a different collection root")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
        needed = {item.strip() for item in _COLUMNS.split(",")} | {"path", "session_id"}
        if not needed <= columns:
            raise ValueError("Database lacks analysis provenance columns; migrate a copy first")
        usable_index = False
        for index in connection.execute("PRAGMA index_list(files)"):
            if len(index) > 4 and index[4]:
                # A partial index might omit the very path being reconciled.
                continue
            indexed_columns = [row[2] for row in
                               connection.execute(f"PRAGMA index_info('{index[1]}')")]
            if indexed_columns[:2] == ["session_id", "path"] or indexed_columns[:1] == ["path"]:
                usable_index = True
                break
        if not usable_index:
            raise ValueError("Files table lacks a path index; refusing per-file lookup")

        if ddhignore.load_error:
            error = {"kind": "policy_error", "path": ".ddhignore",
                     "reason": "ddhignore_unreadable", "detail": ddhignore.load_error}
            record_error(error)

        def walk_error(exc: OSError) -> None:
            path = str(getattr(exc, "filename", "unknown"))
            error = {"kind": "walk_error", "path": path,
                     "reason": "directory_unreadable", "detail": str(exc)}
            record_error(error)

        inherited: dict[Path, str | None] = {root: None}
        skip = SKIP_DIRS | (extra_skip_dirs or set())
        for base_name, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            base = Path(base_name)
            parent_reason = inherited.pop(base, None)
            descend = []
            for name in dirs:
                path = base / name
                if _is_link_or_reparse(path):
                    _write(ledger, {"kind": "non_regular_entry", "path": path.relative_to(root).as_posix(),
                                    "reason": "link_or_reparse"})
                    counts["non_regular_entry"] += 1
                    continue
                inherited[path] = parent_reason or directory_exclusion(name, path, skip, ddhignore)
                descend.append(name)
            dirs[:] = descend

            for name in names:
                path = base / name
                relative = path.relative_to(root).as_posix()
                try:
                    info = path.lstat()
                except OSError as exc:
                    error = {"kind": "stat_error", "path": relative,
                             "reason": "file_unreadable", "detail": str(exc)}
                    record_error(error)
                    continue
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) &
                        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                    _write(ledger, {"kind": "non_regular_entry", "path": relative,
                                    "reason": "link_or_reparse"})
                    counts["non_regular_entry"] += 1
                    continue
                if not stat.S_ISREG(info.st_mode):
                    _write(ledger, {"kind": "non_regular_entry", "path": relative,
                                    "reason": "not_regular_file"})
                    counts["non_regular_entry"] += 1
                    continue

                reason = parent_reason or file_exclusion(name, path, active_db, ddhignore)
                indexed = connection.execute(
                    f"SELECT {_COLUMNS} FROM files WHERE session_id=? AND path=?",
                    (session_id, str(path.resolve())),
                ).fetchone()
                if reason:
                    decision = "excluded_but_indexed" if indexed else "excluded"
                else:
                    decision = "indexed" if indexed else "eligible_unindexed"
                record = {"kind": "regular_file", "path": relative,
                          "size_bytes": info.st_size, "decision": decision,
                          "format_disposition": disposition(path)}
                if reason:
                    record["exclusion_reason"] = reason
                    reasons[reason] += 1
                if indexed:
                    record["index"] = dict(zip(
                        [field.strip() for field in _COLUMNS.split(",")], indexed
                    ))
                _write(ledger, record)
                counts[decision] += 1
                bytes_by_decision[decision] += info.st_size

        db_total = connection.execute(
            "SELECT COUNT(*) FROM files WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        outcome_counts = dict(connection.execute(
            "SELECT COALESCE(analysis_outcome, 'unprocessed'), COUNT(*) "
            "FROM files WHERE session_id=? GROUP BY COALESCE(analysis_outcome, 'unprocessed')",
            (session_id,),
        ).fetchall())
        reason_counts = dict(connection.execute(
            "SELECT analysis_reason, COUNT(*) FROM files WHERE session_id=? "
            "AND analysis_reason IS NOT NULL GROUP BY analysis_reason "
            "ORDER BY COUNT(*) DESC, analysis_reason LIMIT 50",
            (session_id,),
        ).fetchall())
        all_reason_rows = connection.execute(
            "SELECT COUNT(*) FROM files WHERE session_id=? AND analysis_reason IS NOT NULL",
            (session_id,),
        ).fetchone()[0]
        status_counts = dict(connection.execute(
            "SELECT status, COUNT(*) FROM files WHERE session_id=? GROUP BY status",
            (session_id,),
        ).fetchall())
        # A second, streaming index pass identifies stale or escaped rows.
        # It performs only lstat checks and never opens content bytes.
        discrepancy_count = 0
        for row_id, row_path in connection.execute(
                "SELECT id, path FROM files WHERE session_id=?", (session_id,)):
            indexed_path = Path(row_path)
            issue = None
            try:
                if not indexed_path.is_relative_to(root):
                    issue = "indexed_path_outside_root"
                elif indexed_path.resolve() != indexed_path:
                    issue = "indexed_path_changed_or_reparse"
                else:
                    row_stat = indexed_path.lstat()
                    if not stat.S_ISREG(row_stat.st_mode):
                        issue = "indexed_path_not_regular"
            except OSError:
                issue = "indexed_path_missing_or_unreadable"
            if issue:
                discrepancy_count += 1
                _write(ledger, {"kind": "indexed_row_discrepancy", "file_id": row_id,
                                "path": row_path, "reason": issue})
        connection.rollback()
    finally:
        connection.close()

    seen_indexed = counts["indexed"] + counts["excluded_but_indexed"]
    inventory_complete = (not error_count and not discrepancy_count
                          and not counts["eligible_unindexed"]
                          and not counts["excluded_but_indexed"]
                          and db_total == seen_indexed)
    terminal_outcomes = {"content_verified", "context_only", "metadata_only",
                         "sampled", "skipped"}
    analysis_incomplete = (
        sum(count for outcome, count in outcome_counts.items()
            if outcome not in terminal_outcomes)
        + sum(status_counts.get(state, 0) for state in
              ("PENDING", "ENRICHED", "ERROR"))
    )
    return {
        "root": str(root), "database": str(database), "session_id": session_id,
        "physical_regular_files": sum(counts[key] for key in
                                      ("indexed", "excluded", "excluded_but_indexed",
                                       "eligible_unindexed")),
        "physical_regular_bytes": sum(bytes_by_decision.values()),
        "decisions": dict(counts), "bytes_by_decision": dict(bytes_by_decision),
        "exclusion_reasons": dict(reasons), "analysis_outcomes": outcome_counts,
        "analysis_reasons_top_50": reason_counts,
        "analysis_reasons_other_rows": all_reason_rows - sum(reason_counts.values()),
        "file_statuses": status_counts,
        "analysis_complete": analysis_incomplete == 0,
        "indexed_database_rows": db_total,
        "indexed_rows_not_seen_as_regular_files": db_total - seen_indexed,
        "indexed_row_discrepancies": discrepancy_count,
        "errors": errors, "error_count": error_count,
        "inventory_complete": inventory_complete,
        "complete": inventory_complete,
    }

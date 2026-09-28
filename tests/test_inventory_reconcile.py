"""Inventory proof covers policy parity and failure disclosure without AI calls."""
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from donedatahoarder.core import inventory
from donedatahoarder.analyzers.pipeline import _get_analyzer
from donedatahoarder.core.scanner import walk_files
from scripts.inventory_reconcile import main as report_main


def _database(path: Path, root: Path, rows: list[tuple[str, str, str | None]]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, root_path TEXT)")
        db.execute("INSERT INTO sessions VALUES (?, ?)", ("run", str(root.resolve())))
        db.execute("""CREATE TABLE files (
            id INTEGER PRIMARY KEY, session_id TEXT, path TEXT, status TEXT,
            analysis_outcome TEXT, analysis_reason TEXT, analysis_evidence_source TEXT,
            analysis_model_tag TEXT, analysis_model_digest TEXT,
            analysis_prompt_version TEXT, analysis_extractor_version TEXT,
            analysis_cache_hit INTEGER)""")
        db.execute("CREATE UNIQUE INDEX file_session_path ON files(session_id, path)")
        for number, (filename, status, outcome) in enumerate(rows, 1):
            db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (number, "run", str((root / filename).resolve()), status,
                        outcome, "unsupported_type" if outcome == "skipped" else None,
                        "none" if outcome == "skipped" else "metadata",
                        None, None, None, None, 0))


def _report(root: Path, database: Path) -> tuple[dict, list[dict]]:
    output = io.StringIO()
    summary = inventory.reconcile_inventory(root, database, "run", output)
    return summary, [json.loads(line) for line in output.getvalue().splitlines()]


def test_every_regular_file_has_scanner_consistent_decision_and_provenance(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "visible.txt").write_text("content")
    (root / "font.shx").write_bytes(b"opaque resource")
    (root / "unindexed.txt").write_text("later")
    (root / "Thumbs.db").write_bytes(b"metadata")
    (root / "temporary.tmp").write_bytes(b"transient")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "saved.dat").write_bytes(b"private")
    (root / "ignored").mkdir()
    (root / "ignored" / "kept.dat").write_bytes(b"ignored by policy")
    (root / ".ddhignore").write_text("ignored/\n", encoding="utf-8")
    db = tmp_path / "index.sqlite"
    _database(db, root, [("visible.txt", "PROPOSED", "content_verified"),
                         ("font.shx", "SKIPPED", "skipped")])

    summary, records = _report(root, db)
    files = {record["path"]: record for record in records
             if record["kind"] == "regular_file"}
    assert set(files) == {"visible.txt", "font.shx", "unindexed.txt",
                          "Thumbs.db", "temporary.tmp", ".hidden/saved.dat",
                          "ignored/kept.dat", ".ddhignore"}
    assert {item["path"] for item in files.values() if item["decision"] == "indexed"} == {
        "visible.txt", "font.shx"}
    assert files["font.shx"]["format_disposition"] == "preserve_opaque_font_or_cad_resource"
    assert files["font.shx"]["index"]["analysis_outcome"] == "skipped"
    assert files["Thumbs.db"]["exclusion_reason"] == "system_metadata_name"
    assert files["temporary.tmp"]["exclusion_reason"] == "transient_extension"
    assert files[".hidden/saved.dat"]["exclusion_reason"] == "scanner_dot_directory"
    assert files["ignored/kept.dat"]["exclusion_reason"] == "ddhignore_directory"
    assert files["unindexed.txt"]["decision"] == "eligible_unindexed"
    assert not summary["complete"]
    assert summary["analysis_outcomes"] == {"content_verified": 1, "skipped": 1}
    assert summary["analysis_complete"]
    assert summary["physical_regular_bytes"] == sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    assert {str(p.relative_to(root)).replace("\\", "/") for p in walk_files(root)} == {
        "visible.txt", "font.shx", "unindexed.txt", ".ddhignore"}


def test_indexed_excluded_row_and_missing_row_are_not_counted_complete(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "Thumbs.db").write_bytes(b"metadata")
    db = tmp_path / "index.sqlite"
    _database(db, root, [("Thumbs.db", "SKIPPED", "skipped"),
                         ("removed.txt", "ANALYZED", "content_verified")])
    summary, records = _report(root, db)
    assert summary["decisions"]["excluded_but_indexed"] == 1
    assert summary["indexed_rows_not_seen_as_regular_files"] == 1
    assert summary["indexed_row_discrepancies"] == 1
    assert not summary["complete"]
    assert records[0]["decision"] == "excluded_but_indexed"
    assert records[-1]["kind"] == "indexed_row_discrepancy"
    assert records[-1]["reason"] == "indexed_path_missing_or_unreadable"


def test_unreadable_walk_and_policy_error_are_explicit_and_bounded(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    real_walk = inventory.os.walk

    def broken_walk(*args, **kwargs):
        kwargs["onerror"](PermissionError(13, "denied", str(root / "inaccessible")))
        yield from real_walk(*args, **kwargs)

    monkeypatch.setattr(inventory.os, "walk", broken_walk)
    summary, records = _report(root, db)
    assert summary["error_count"] == 1
    assert records[0]["kind"] == "walk_error"
    assert not summary["complete"]


def test_unreadable_file_and_ignore_policy_failure_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "unreadable.dat").write_bytes(b"keep")
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    original_lstat = Path.lstat

    def denied_lstat(path):
        if path.name == "unreadable.dat":
            raise PermissionError("inaccessible fixture")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    monkeypatch.setattr(inventory, "load_ddhignore", lambda _:
                        type("BrokenIgnore", (), {"load_error": "denied",
                                                  "should_ignore": lambda *_args, **_kwargs: False})())
    summary, records = _report(root, db)
    assert summary["error_count"] == 2
    assert {record["kind"] for record in records} == {"policy_error", "stat_error"}
    assert not summary["complete"]


def test_read_only_reconciliation_sees_committed_wal_rows(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "active.txt").write_text("active")
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO files VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("run", str((root / "active.txt").resolve()), "ANALYZED",
                        "content_verified", None, "text", "local", "digest", "v1", "v1", 0))
        writer.commit()
        summary, records = _report(root, db)
        assert summary["complete"]
        assert summary["analysis_complete"]
        assert records[0]["index"]["analysis_model_digest"] == "digest"
    finally:
        writer.close()


def test_complete_inventory_does_not_imply_completed_analysis(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "waiting.txt").write_text("waiting")
    db = tmp_path / "index.sqlite"
    _database(db, root, [("waiting.txt", "ENRICHED", None)])
    summary, _ = _report(root, db)
    assert summary["inventory_complete"]
    assert not summary["analysis_complete"]
    assert summary["analysis_outcomes"] == {"unprocessed": 1}


def test_unknown_analysis_outcome_is_not_claimed_complete(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "odd.txt").write_text("odd")
    db = tmp_path / "index.sqlite"
    _database(db, root, [("odd.txt", "ANALYZED", "unknown_future_outcome")])
    summary, _ = _report(root, db)
    assert summary["inventory_complete"]
    assert not summary["analysis_complete"]
    assert summary["analysis_outcomes"] == {"unknown_future_outcome": 1}


def test_opaque_resource_mime_misclassification_cannot_route_to_model():
    class GreedyAnalyzer:
        def can_handle(self, mime, ext):
            return True

    for ext in (".shx", ".ctb", ".ttf", ".woff2"):
        assert _get_analyzer([GreedyAnalyzer()], "image/png", ext) is None


def test_wrong_session_root_rejected_before_ledger_output(tmp_path):
    root = tmp_path / "collection"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    db = tmp_path / "index.sqlite"
    _database(db, other, [])
    output = io.StringIO()
    with pytest.raises(ValueError, match="different collection root"):
        inventory.reconcile_inventory(root, db, "run", output)
    assert output.getvalue() == ""


def test_cli_rejects_report_inside_collection_before_creating_files(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    ledger = root / "self.jsonl"
    summary = tmp_path / "summary.json"
    with pytest.raises(SystemExit):
        report_main([str(root), "--db", str(db), "--session", "run",
                     "--ledger", str(ledger), "--summary", str(summary)])
    assert not ledger.exists()
    assert not summary.exists()


def test_missing_path_index_fails_before_walk(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX file_session_path")
    output = io.StringIO()
    with pytest.raises(ValueError, match="lacks a path index"):
        inventory.reconcile_inventory(root, db, "run", output)
    assert output.getvalue() == ""


def test_partial_path_index_is_not_accepted_for_full_ledger(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    db = tmp_path / "index.sqlite"
    _database(db, root, [])
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX file_session_path")
        connection.execute("CREATE INDEX only_analyzed_paths ON files(session_id, path) "
                           "WHERE analysis_outcome='content_verified'")
    with pytest.raises(ValueError, match="lacks a path index"):
        inventory.reconcile_inventory(root, db, "run", io.StringIO())


def test_script_uses_its_checkout_from_other_cwd_without_pythonpath(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "font.shx").write_bytes(b"opaque")
    db = tmp_path / "index.sqlite"
    _database(db, root, [("font.shx", "SKIPPED", "skipped")])
    ledger = tmp_path / "ledger.jsonl"
    summary = tmp_path / "summary.json"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = Path(__file__).resolve().parents[1] / "scripts" / "inventory_reconcile.py"
    result = subprocess.run(
        [sys.executable, str(script), str(root), "--db", str(db),
         "--session", "run", "--ledger", str(ledger), "--summary", str(summary)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(summary.read_text())["complete"]
    assert json.loads(ledger.read_text())["format_disposition"] == (
        "preserve_opaque_font_or_cad_resource")

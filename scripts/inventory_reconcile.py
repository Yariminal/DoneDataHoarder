"""Export a read-only per-file inventory ledger and reconciliation summary.

Example: python scripts/inventory_reconcile.py ROOT --db SNAPSHOT.sqlite
         --session SESSION_ID --ledger /private/report/files.jsonl
         --summary /private/report/summary.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from donedatahoarder.core.inventory import reconcile_inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--db", type=Path, required=True, help="Existing SQLite database or backup")
    parser.add_argument("--session", required=True, help="Session ID whose index is reconciled")
    parser.add_argument("--ledger", type=Path, required=True, help="Private JSONL output outside root")
    parser.add_argument("--summary", type=Path, required=True, help="Private JSON output outside root")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    db = args.db.resolve()
    ledger = args.ledger.resolve()
    summary = args.summary.resolve()
    if ledger == summary or ledger in {db, root} or summary in {db, root}:
        parser.error("Output paths must be distinct from each other, root, and database")
    if ledger.is_relative_to(root) or summary.is_relative_to(root):
        parser.error("Reports must be placed outside the collection root")
    if ledger.exists() or summary.exists():
        parser.error("Reports already exist; choose new names to preserve prior evidence")
    with ledger.open("x", encoding="utf-8") as output:
        result = reconcile_inventory(args.root, db, args.session, output)
    with summary.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, ensure_ascii=False, sort_keys=True)
        output.write("\n")
    print(json.dumps({"inventory_complete": result["inventory_complete"],
                      "analysis_complete": result["analysis_complete"],
                      "physical_regular_files": result["physical_regular_files"],
                      "physical_regular_bytes": result["physical_regular_bytes"],
                      "decisions": result["decisions"],
                      "error_count": result["error_count"]}, sort_keys=True))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

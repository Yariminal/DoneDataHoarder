r"""Disposable, model-free mixed-library organization proof.

Labels in tests/fixtures/mixed_organization_labels.json were authored before
running the planner. This is a regression control, not an independent holdout.

Examples:
  python scripts/prove_mixed_organization.py prepare --output-parent D:\Test
  python scripts/prove_mixed_organization.py cycle --run-dir <printed-run-dir> \
      --selected-path Inbox/invoice_alpha.pdf

The cycle targets one reviewed MOVE and audits all original bytes/paths after
undo. No original collection or external model is accessed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if sys.path[0] != str(REPO_ROOT):
    sys.path.insert(0, str(REPO_ROOT))

LABELS = REPO_ROOT / "tests" / "fixtures" / "mixed_organization_labels.json"


def _assert_source_checkout() -> str:
    from donedatahoarder.proposals.organizer import core

    source = Path(core.__file__).resolve()
    if not source.is_relative_to(REPO_ROOT):
        raise RuntimeError("organizer imported from another checkout")
    return str(source)


def _json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> dict:
    return {
        "files": {p.relative_to(root).as_posix(): _hash(p)
                  for p in sorted(root.rglob("*")) if p.is_file()},
        "directories": sorted(p.relative_to(root).as_posix()
                              for p in root.rglob("*") if p.is_dir()),
    }


def _db_evidence(session_id: str, root: Path) -> dict:
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File
    from donedatahoarder.db.session import get_engine

    with Session(get_engine()) as db:
        rows = db.query(File).filter(File.session_id == session_id).order_by(File.id).all()
        files = [{"id": row.id, "path": Path(row.path).relative_to(root).as_posix(),
                  "filename": row.filename, "sha256": row.hash_sha256,
                  "status": row.status.value} for row in rows]
        quick_check = db.execute(text("PRAGMA quick_check")).scalars().all()
        foreign_key_errors = db.execute(text("PRAGMA foreign_key_check")).all()
    return {"files": files, "quick_check": quick_check,
            "foreign_key_errors": len(foreign_key_errors)}


def _records(run_dir: Path, labels: dict) -> tuple[str, dict]:
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, UserSession
    from donedatahoarder.db.session import get_engine, init_db

    root = run_dir / "collection"
    engine = init_db(run_dir / "fixture.sqlite")
    with Session(engine) as db:
        owner = UserSession(root_path=str(root), name="authored-mixed-organization")
        db.add(owner)
        db.flush()
        for item in labels["files"]:
            relative = Path(item["path"])
            if (relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or relative.parts[0] == ""):
                raise ValueError("fixture label path must stay beneath collection")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = item["content"].encode("utf-8")
            path.write_bytes(payload)
            db.add(File(
                session_id=owner.id, path=str(path), filename=path.name,
                extension=path.suffix, mime_type=item["mime"],
                size_bytes=len(payload), hash_md5=hashlib.md5(payload).hexdigest(),
                hash_sha256=hashlib.sha256(payload).hexdigest(),
                status=FileStatus.ANALYZED, analysis_outcome="metadata_only",
                analysis_evidence_source="authored_fixture",
                date_exif=(datetime.fromisoformat(item["exif_date"])
                           if item.get("exif_date") else None),
                date_modified=(datetime.fromisoformat(item["modified_date"])
                               if item.get("modified_date") else None),
            ))
        db.commit()
        return owner.id, _manifest(root)


def _plan(run_dir: Path, labels: dict, session_id: str, baseline: dict) -> dict:
    from sqlalchemy.orm import Session
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.db.models import File, Proposal, ProposalType
    from donedatahoarder.db.session import get_engine
    from donedatahoarder.proposals.organizer.core import (
        _emit_standalone_moves, _inside_project, _loose_source, _project_roots,
    )
    from donedatahoarder.proposals.organizer.tree import _file_category

    root = run_dir / "collection"
    protection = ProtectionIndex(root)
    with Session(get_engine()) as db:
        files = db.query(File).filter(File.session_id == session_id).all()
        project_roots = _project_roots(root, files, protection)
        boundaries = {}
        for file_rec in files:
            path = Path(file_rec.path)
            relative = path.relative_to(root).as_posix()
            if _inside_project(path, project_roots):
                kind, reason = "preserved", "project_manifest_or_resource_bundle"
            elif protection.assess(path).protected:
                kind, reason = "retained", "linked_resource_protected"
            elif not _loose_source(file_rec, root, {}, project_roots):
                kind, reason = "retained", "named_folder_context_unverified"
            elif _file_category(file_rec.mime_type, file_rec.extension) == "other":
                kind, reason = "needs_review", "unsupported_category"
            else:
                kind, reason = "eligible", "loose_supported_type"
            boundaries[relative] = {"boundary": kind, "reason": reason}

    made = _emit_standalone_moves(session_id, str(root))
    with Session(get_engine()) as db:
        proposals = db.query(Proposal).join(File).filter(
            File.session_id == session_id, Proposal.proposal_type == ProposalType.MOVE,
        ).all()
        destinations = {
            Path(p.current_value).relative_to(root).as_posix(): {
                "proposal_id": p.id,
                "destination": Path(p.proposed_value).relative_to(root).as_posix(),
                "status": p.status.value,
            } for p in proposals
        }
    by_path = {item["path"]: item for item in labels["files"]}
    rows = []
    for path, item in by_path.items():
        expected = item["expected_move"]
        actual = destinations.get(path)
        rows.append({
            "path": path, "label": item["role"], "expected_move": expected,
            "actual_move": actual, **boundaries[path],
            "matches_label": ((actual is None and expected is None) or
                              (actual is not None and actual["destination"] == expected)),
        })
    counts = Counter(row["boundary"] for row in rows)
    expected_eligible = sum(item["expected_move"] is not None for item in by_path.values())
    correct_eligible = sum(row["matches_label"] and row["expected_move"] is not None
                           for row in rows)
    correct_no_move = sum(row["matches_label"] and row["expected_move"] is None
                          for row in rows)
    report = {
        "kind": "authored_regression_not_independent_holdout",
        "planner_scope": "deterministic_standalone_moves_only_no_model",
        "source_root": str(REPO_ROOT),
        "organizer_source": _assert_source_checkout(),
        "session_id": session_id, "run_dir": str(run_dir),
        "labels_sha256": _hash(LABELS), "baseline": baseline,
        "db_baseline": _db_evidence(session_id, root),
        "counts": dict(counts), "suppression_reasons": dict(Counter(
            row["reason"] for row in rows if row["actual_move"] is None)),
        "proposal_coverage": {"eligible": expected_eligible, "proposed": made,
                              "correct_destination": correct_eligible,
                              "expected_no_move": len(rows) - expected_eligible,
                              "correct_no_move": correct_no_move},
        "rows": rows,
        "passed": correct_eligible == expected_eligible
                  and correct_no_move == len(rows) - expected_eligible
                  and made == expected_eligible,
    }
    _json(run_dir / "plan.json", report)
    return report


def prepare(output_parent: Path) -> dict:
    _assert_source_checkout()
    output_parent = output_parent.resolve()
    if not output_parent.is_dir():
        raise ValueError("output parent must already exist")
    run_dir = Path(tempfile.mkdtemp(prefix="DDH-mixed-organization-", dir=output_parent))
    os.environ["DDH_DATA_DIR"] = str(run_dir / "state")
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    session_id, baseline = _records(run_dir, labels)
    return _plan(run_dir, labels, session_id, baseline)


def cycle(run_dir: Path, selected_path: str) -> dict:
    _assert_source_checkout()
    from sqlalchemy.orm import Session
    from donedatahoarder.core.undo_log import undo_operations
    from donedatahoarder.db.models import Proposal, ProposalStatus
    from donedatahoarder.db.session import get_engine, init_db
    from donedatahoarder.executor import execute

    run_dir = run_dir.resolve()
    if not run_dir.is_dir() or not run_dir.name.startswith("DDH-mixed-organization-"):
        raise ValueError("run directory must be a disposable mixed-organization fixture")
    os.environ["DDH_DATA_DIR"] = str(run_dir / "state")
    report_path = run_dir / "plan.json"
    plan = json.loads(report_path.read_text(encoding="utf-8"))
    if (not plan["passed"] or _hash(LABELS) != plan["labels_sha256"]
            or Path(plan["run_dir"]).resolve() != run_dir):
        raise ValueError("plan or frozen labels failed validation")
    root = run_dir / "collection"
    if _manifest(root) != plan["baseline"]:
        raise ValueError("fixture files changed since planning")
    selected = next((row for row in plan["rows"] if row["path"] == selected_path), None)
    if selected is None or selected["expected_move"] is None or not selected["actual_move"]:
        raise ValueError("select one labeled eligible proposal")
    if (selected["actual_move"]["destination"] != selected["expected_move"]):
        raise ValueError("selected proposal differs from frozen expected destination")
    init_db(run_dir / "fixture.sqlite")
    if (_db_evidence(plan["session_id"], root) != plan["db_baseline"]
            or plan["db_baseline"]["quick_check"] != ["ok"]
            or plan["db_baseline"]["foreign_key_errors"] != 0):
        raise ValueError("fixture database changed or failed integrity before cycle")
    proposal_id = selected["actual_move"]["proposal_id"]
    with Session(get_engine()) as db:
        if db.query(Proposal).filter(Proposal.status.in_([
            ProposalStatus.APPROVED, ProposalStatus.MODIFIED, ProposalStatus.APPLIED,
        ])).count():
            raise ValueError("fixture already has reviewed or applied proposals")
        proposal = db.get(Proposal, proposal_id)
        if (proposal is None or proposal.status != ProposalStatus.PENDING
                or Path(proposal.current_value).relative_to(root).as_posix() != selected_path
                or Path(proposal.proposed_value).relative_to(root).as_posix()
                != selected["expected_move"]):
            raise ValueError("proposal changed since planning")
        proposal.status = ProposalStatus.APPROVED
        db.commit()
    result = {"selected_path": selected_path, "proposal_id": proposal_id}
    try:
        result["dry_run"] = execute(dry_run=True, session_id=plan["session_id"],
                                    proposal_ids=[proposal_id])
        if result["dry_run"].get("applied") != 1 or result["dry_run"].get("failed"):
            raise RuntimeError("selected dry run failed")
        result["commit"] = execute(dry_run=False, session_id=plan["session_id"],
                                   proposal_ids=[proposal_id])
        if result["commit"].get("applied") != 1 or result["commit"].get("failed"):
            raise RuntimeError("selected commit failed")
        destination = root / selected["expected_move"]
        source = root / selected_path
        if source.exists() or not destination.is_file():
            raise RuntimeError("committed destination missing or source still exists")
        if _hash(destination) != plan["baseline"]["files"][selected_path]:
            raise RuntimeError("committed bytes differ from baseline")
        result["undo"] = undo_operations(session_id=plan["session_id"], force=True)
        if result["undo"].get("undone") != 1 or result["undo"].get("failed"):
            raise RuntimeError("selected undo failed")
        result["restored"] = _manifest(root) == plan["baseline"]
        if not result["restored"]:
            raise RuntimeError("undo did not restore original paths, bytes and directories")
        result["db_after_undo"] = _db_evidence(plan["session_id"], root)
        result["db_restored"] = result["db_after_undo"] == plan["db_baseline"]
        if not result["db_restored"]:
            raise RuntimeError("undo did not restore indexed paths, names, hashes or SQLite integrity")
        result["passed"] = True
    except Exception as exc:
        result["passed"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _json(run_dir / "cycle.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = commands.add_parser("prepare")
    prepare_cmd.add_argument("--output-parent", type=Path, required=True)
    cycle_cmd = commands.add_parser("cycle")
    cycle_cmd.add_argument("--run-dir", type=Path, required=True)
    cycle_cmd.add_argument("--selected-path", required=True)
    args = parser.parse_args()
    report = prepare(args.output_parent) if args.command == "prepare" else cycle(
        args.run_dir, args.selected_path)
    print(json.dumps({"run_dir": report.get("run_dir", str(getattr(args, "run_dir", ""))),
                      "passed": report["passed"],
                      "coverage": report.get("proposal_coverage"),
                      "counts": report.get("counts")}, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

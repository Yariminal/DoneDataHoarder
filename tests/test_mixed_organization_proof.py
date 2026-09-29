"""Authored mixed-library fixture and selected disposable move cycle."""
import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prove_mixed_organization.py"


def _run(cwd: Path, *args: str) -> dict:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=cwd, check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_labeled_mixed_library_plan_and_selected_move_undo(tmp_path):
    prepared = _run(tmp_path, "prepare", "--output-parent", str(tmp_path))
    assert prepared["passed"]
    assert prepared["coverage"] == {
        "eligible": 6, "proposed": 6, "correct_destination": 6,
        "expected_no_move": 9, "correct_no_move": 9,
    }
    assert prepared["counts"] == {
        "eligible": 6, "needs_review": 1, "preserved": 3, "retained": 5,
    }
    run_dir = Path(prepared["run_dir"])
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["planner_scope"] == "deterministic_standalone_moves_only_no_model"
    assert Path(plan["source_root"]) == SCRIPT.parents[1]
    assert Path(plan["organizer_source"]).is_relative_to(SCRIPT.parents[1])
    assert plan["db_baseline"]["quick_check"] == ["ok"]
    assert plan["db_baseline"]["foreign_key_errors"] == 0
    assert plan["suppression_reasons"] == {
        "project_manifest_or_resource_bundle": 3,
        "linked_resource_protected": 3,
        "named_folder_context_unverified": 2,
        "unsupported_category": 1,
    }
    assert all(row["matches_label"] for row in plan["rows"])

    cycled = _run(
        tmp_path,
        "cycle", "--run-dir", str(run_dir),
        "--selected-path", "Inbox/invoice_alpha.pdf",
    )
    assert cycled["passed"]
    cycle = json.loads((run_dir / "cycle.json").read_text(encoding="utf-8"))
    assert cycle["dry_run"]["applied"] == 1
    assert cycle["commit"]["applied"] == 1
    assert cycle["undo"]["undone"] == 1
    assert cycle["restored"]
    assert cycle["db_restored"]
    assert cycle["db_after_undo"] == plan["db_baseline"]

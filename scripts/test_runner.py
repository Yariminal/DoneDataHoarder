#!/usr/bin/env python3
"""
DataHoarder Automated Test Runner

Runs the full DataHoarder pipeline on zipped test fixtures, applies changes,
captures the resulting directory structure, and uses AI to critique the results.

Usage:
    python scripts/test_runner.py /path/to/test_fixtures/

Test fixtures:
    Place .zip files in the test fixtures folder. Each zip should contain a
    realistic messy folder that DataHoarder should organize.

Outputs:
    test_dbs/       — SQLite databases from each test run (for later inspection)
    test_reports/   — Markdown reports with AI critique and directory trees
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from datahoarder.ai.router import init_ai, generate_json
from datahoarder.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = _PROJECT_ROOT / "test_outputs"
TEST_DBS_DIR = DEFAULT_OUTPUT_DIR / "test_dbs"
TEST_REPORTS_DIR = DEFAULT_OUTPUT_DIR / "test_reports"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("DATAHOARDER_MODEL", "gemma3:12b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    """Create output directories if they don't exist."""
    TEST_DBS_DIR.mkdir(parents=True, exist_ok=True)
    TEST_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _unzip_fixture(zip_path: Path, dest_dir: Path) -> Path:
    """Unzip a test fixture into *dest_dir* and return the root folder."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

    # If the zip contains a single top-level directory, use that as root
    entries = [e for e in dest_dir.iterdir() if not e.name.startswith(".")]
    dirs = [e for e in entries if e.is_dir()]
    if len(dirs) == 1 and len(entries) == 1:
        return dirs[0]
    return dest_dir


def _run_cli(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run a datahoarder CLI command and return the result."""
    full_cmd = [sys.executable, "-m", "datahoarder"] + cmd
    logger.info("Running CLI command: %s", " ".join(full_cmd))
    result = subprocess.run(
        full_cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result


def _capture_tree(root: Path) -> str:
    """Capture a textual tree of the directory structure (like `tree` or `find`)."""
    lines: list[str] = []
    lines.append(str(root.resolve()))

    def _walk(path: Path, prefix: str = "") -> None:
        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension)

    _walk(root)
    return "\n".join(lines)


def _query_db_stats(db_path: Path) -> dict[str, Any]:
    """Run `datahoarder stats --db <db>` and parse the output."""
    result = _run_cli(["stats", "--db", str(db_path)])
    # stats prints a Rich table; we can't easily parse it.
    # Instead, query the DB directly with SQLAlchemy.
    from datahoarder.db.session import init_db, get_engine
    from datahoarder.db.models import File, Proposal, DuplicateGroup, RelationGroup
    from sqlalchemy.orm import Session
    from sqlalchemy import func

    init_db(db_path)
    engine = get_engine()
    with Session(engine) as session:
        total_files = session.query(func.count(File.id)).scalar() or 0
        statuses = dict(
            session.query(File.status, func.count(File.id)).group_by(File.status).all()
        )
        proposals = dict(
            session.query(Proposal.status, func.count(Proposal.id))
            .group_by(Proposal.status)
            .all()
        )
        dupes = session.query(func.count(DuplicateGroup.id)).scalar() or 0
        relations = session.query(func.count(RelationGroup.id)).scalar() or 0

    return {
        "total_files": total_files,
        "by_status": {k.value: v for k, v in statuses.items()},
        "proposals": {k.value: v for k, v in proposals.items()},
        "duplicate_groups": dupes,
        "relation_groups": relations,
    }


def _query_proposals(db_path: Path, limit: int = 50) -> list[dict]:
    """Fetch pending proposals from the test DB for critique context."""
    from datahoarder.db.session import init_db, get_engine
    from datahoarder.db.models import Proposal, ProposalStatus
    from sqlalchemy.orm import Session

    init_db(db_path)
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .limit(limit)
            .all()
        )
        return [
            {
                "type": p.proposal_type.value,
                "current": p.current_value,
                "proposed": p.proposed_value,
                "confidence": p.confidence,
                "reasoning": p.reasoning,
            }
            for p in rows
        ]


# ---------------------------------------------------------------------------
# AI Critic
# ---------------------------------------------------------------------------

class AICritic:
    """Uses an LLM to evaluate DataHoarder's organization results."""

    SYSTEM_PROMPT = (
        "You are a meticulous file organization critic. "
        "Evaluate how well an AI file organizer performed on a test folder. "
        "Be specific, point out both strengths and weaknesses. "
        "Rate on a scale of 1-10 for each category."
    )

    def __init__(self, backend: str = "ollama", model: str = OLLAMA_MODEL):
        self.backend = backend
        self.model = model
        init_ai(backend=backend, text_model=model, vision_model=model)

    def critique(
        self,
        test_name: str,
        original_tree: str,
        final_tree: str,
        stats: dict,
        proposals: list[dict],
    ) -> dict[str, Any]:
        """Send results to AI and return structured critique."""

        prompt = f"""Evaluate the performance of an AI file organizer on the test case "{test_name}".

## Before (original messy structure)
```
{original_tree}
```

## After (organized structure)
```
{final_tree}
```

## Database Stats
{json.dumps(stats, indent=2, ensure_ascii=False)}

## Sample Proposals Generated ({len(proposals)} shown)
{json.dumps(proposals, indent=2, ensure_ascii=False)}

## Instructions
Provide a structured evaluation with the following fields in JSON:

- "overall_score": int (1-10) — How well did the organizer do overall?
- "naming_quality": int (1-10) — Are filenames descriptive, consistent, free of junk?
- "folder_structure": int (1-10) — Is the folder hierarchy logical and useful?
- "duplicate_handling": int (1-10) — Were duplicates detected and handled appropriately?
- "metadata_quality": int (1-10) — Did AI descriptions and tags add value?
- "missed_opportunities": list[str] — What obvious improvements were missed?
- "strengths": list[str] — What did the organizer do well?
- "regressions": list[str] — Did it make anything worse?
- "recommendations": list[str] — Specific actionable suggestions to improve the tool
- "summary": str — One-paragraph overall assessment

Respond with valid JSON only. No markdown fences, no explanations outside the JSON.
"""

        from datahoarder.ai.json_utils import LooseDict

        try:
            result = generate_json(
                prompt=prompt,
                model_cls=LooseDict,
                system=self.SYSTEM_PROMPT,
                temperature=0.2,
            )
            return result.model_dump()
        except Exception as exc:
            logger.warning("AI critique failed: %s", exc)
            return {
                "overall_score": 0,
                "summary": f"AI critique failed: {exc}",
                "error": str(exc),
            }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_report(
    test_name: str,
    original_tree: str,
    final_tree: str,
    stats: dict,
    critique: dict,
    log_output: str,
) -> str:
    """Generate a Markdown report for a single test run."""
    now = datetime.datetime.now().isoformat()
    lines = [
        f"# Test Report: {test_name}",
        f"",
        f"**Date:** {now}",
        f"",
        f"---",
        f"",
        f"## Original Directory Tree",
        f"",
        f"```",
        original_tree,
        f"```",
        f"",
        f"## Final Directory Tree (after execute --commit)",
        f"",
        f"```",
        final_tree,
        f"```",
        f"",
        f"## Database Stats",
        f"",
        f"```json",
        json.dumps(stats, indent=2, ensure_ascii=False),
        f"```",
        f"",
        f"## AI Critique",
        f"",
    ]

    if "error" in critique:
        lines.extend([
            f"> **Critique failed:** {critique['error']}",
            f"",
        ])
    else:
        lines.extend([
            f"- **Overall Score:** {critique.get('overall_score', 'N/A')}/10",
            f"- **Naming Quality:** {critique.get('naming_quality', 'N/A')}/10",
            f"- **Folder Structure:** {critique.get('folder_structure', 'N/A')}/10",
            f"- **Duplicate Handling:** {critique.get('duplicate_handling', 'N/A')}/10",
            f"- **Metadata Quality:** {critique.get('metadata_quality', 'N/A')}/10",
            f"",
            f"### Strengths",
            f"",
        ])
        for s in critique.get("strengths", []):
            lines.append(f"- {s}")
        lines.append("")

        lines.extend([
            f"### Missed Opportunities",
            f"",
        ])
        for m in critique.get("missed_opportunities", []):
            lines.append(f"- {m}")
        lines.append("")

        lines.extend([
            f"### Regressions",
            f"",
        ])
        for r in critique.get("regressions", []):
            lines.append(f"- {r}")
        lines.append("")

        lines.extend([
            f"### Recommendations",
            f"",
        ])
        for rec in critique.get("recommendations", []):
            lines.append(f"- {rec}")
        lines.append("")

        lines.extend([
            f"### Summary",
            f"",
            f"> {critique.get('summary', 'No summary provided.')}",
            f"",
        ])

    lines.extend([
        f"---",
        f"",
        f"## CLI Log",
        f"",
        f"```",
        log_output,
        f"```",
        f"",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

class TestRunner:
    """Orchestrates the automated test sequence."""

    def __init__(
        self,
        fixtures_dir: Path,
        backend: str = "ollama",
        model: str = OLLAMA_MODEL,
        keep_artifacts: bool = True,
        skip_execute: bool = False,
    ):
        self.fixtures_dir = Path(fixtures_dir).resolve()
        self.backend = backend
        self.model = model
        self.keep_artifacts = keep_artifacts
        self.skip_execute = skip_execute
        self.critic = AICritic(backend=backend, model=model)
        self._log_buffer: list[str] = []

    def _log(self, msg: str) -> None:
        self._log_buffer.append(msg)
        print(msg)

    def run_all(self) -> list[Path]:
        """Discover .zip fixtures and run each one. Returns list of report paths."""
        _ensure_dirs()
        zips = sorted(self.fixtures_dir.glob("*.zip"))
        if not zips:
            logger.warning("No .zip fixtures found in %s", self.fixtures_dir)
            return []

        self._log(f"Found {len(zips)} test fixture(s) in {self.fixtures_dir}")
        reports: list[Path] = []
        for zip_path in zips:
            report_path = self._run_single(zip_path)
            if report_path:
                reports.append(report_path)
        return reports

    def _run_single(self, zip_path: Path) -> Optional[Path]:
        """Run a single test fixture end-to-end."""
        test_name = zip_path.stem
        self._log(f"\n{'=' * 60}")
        self._log(f"Running test: {test_name}")
        self._log(f"{'=' * 60}")

        db_path = TEST_DBS_DIR / f"{test_name}.db"
        # Remove old DB if it exists
        if db_path.exists():
            db_path.unlink()

        with tempfile.TemporaryDirectory(prefix=f"dh_test_{test_name}_") as tmp:
            tmp_path = Path(tmp)
            unzip_dir = tmp_path / "fixture"
            unzip_dir.mkdir()

            # 1. Unzip
            self._log("[1/7] Unzipping fixture...")
            root_dir = _unzip_fixture(zip_path, unzip_dir)
            original_tree = _capture_tree(root_dir)
            self._log(f"Fixture root: {root_dir}")

            # 2. Run pipeline
            self._log("[2/7] Running pipeline (scan → enrich → dedup → analyze → propose)...")
            pipeline_result = _run_cli(
                [
                    "pipeline", str(root_dir),
                    "--db", str(db_path),
                    "--backend", self.backend,
                    "--model", self.model,
                    "--workers", "1",
                ],
                env={"DATAHOARDER_DB": str(db_path)},
            )
            self._log(pipeline_result.stdout)
            if pipeline_result.returncode != 0:
                self._log(f"[ERROR] Pipeline failed: {pipeline_result.stderr}")
                # Continue anyway to capture partial results

            # 3. Approve all proposals via bulk-approve (dry-run first to see counts)
            self._log("[3/7] Reviewing proposals...")
            review_result = _run_cli(
                ["review", "--db", str(db_path), "--limit", "10"],
            )
            self._log(review_result.stdout)

            # 4. Execute (if not skipped)
            if not self.skip_execute:
                self._log("[4/7] Executing proposals (--commit --force)...")
                exec_result = _run_cli(
                    ["execute", "--db", str(db_path), "--commit", "--force"],
                )
                self._log(exec_result.stdout)
                if exec_result.returncode != 0:
                    self._log(f"[WARN] Execute stderr: {exec_result.stderr}")
            else:
                self._log("[4/7] Skipping execute (--skip-execute)")

            # 5. Capture final tree
            self._log("[5/7] Capturing final directory tree...")
            final_tree = _capture_tree(root_dir)

            # 6. Query DB stats
            self._log("[6/7] Querying database stats...")
            stats = _query_db_stats(db_path)
            proposals = _query_proposals(db_path, limit=30)

            # 7. AI Critique
            self._log("[7/7] Requesting AI critique...")
            critique = self.critic.critique(
                test_name=test_name,
                original_tree=original_tree,
                final_tree=final_tree,
                stats=stats,
                proposals=proposals,
            )

            # Generate report
            log_output = "\n".join(self._log_buffer)
            report_md = _generate_report(
                test_name=test_name,
                original_tree=original_tree,
                final_tree=final_tree,
                stats=stats,
                critique=critique,
                log_output=log_output,
            )

            report_path = TEST_REPORTS_DIR / f"{test_name}_report.md"
            report_path.write_text(report_md, encoding="utf-8")
            self._log(f"\n[OK] Report saved: {report_path}")
            self._log(f"[OK] Database saved: {db_path}")

            # If keeping artifacts, copy the final folder too
            if self.keep_artifacts:
                artifact_dir = DEFAULT_OUTPUT_DIR / "trees" / test_name
                if artifact_dir.exists():
                    shutil.rmtree(artifact_dir)
                shutil.copytree(root_dir, artifact_dir, ignore=shutil.ignore_patterns("*.db"))
                self._log(f"[OK] Final tree artifact: {artifact_dir}")

            return report_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automated DataHoarder test runner with AI critique.",
    )
    parser.add_argument(
        "fixtures_dir",
        nargs="?",
        default="test_fixtures",
        help="Directory containing .zip test fixtures (default: test_fixtures/)",
    )
    parser.add_argument(
        "--backend",
        default="ollama",
        choices=["ollama", "gemini", "auto"],
        help="AI backend for analysis and critique (default: ollama)",
    )
    parser.add_argument(
        "--model",
        default=OLLAMA_MODEL,
        help=f"Model name for AI analysis (default: {OLLAMA_MODEL})",
    )
    parser.add_argument(
        "--skip-execute",
        action="store_true",
        help="Run pipeline but skip execute (dry-run only, preserves fixtures)",
    )
    parser.add_argument(
        "--no-artifacts",
        action="store_true",
        help="Don't copy final tree artifacts to test_outputs/trees/",
    )
    args = parser.parse_args()

    runner = TestRunner(
        fixtures_dir=args.fixtures_dir,
        backend=args.backend,
        model=args.model,
        keep_artifacts=not args.no_artifacts,
        skip_execute=args.skip_execute,
    )

    reports = runner.run_all()
    if not reports:
        print("\nNo tests run. Place .zip files in the fixtures directory.")
        return 1

    print(f"\n{'=' * 60}")
    print(f"All tests complete. {len(reports)} report(s) generated:")
    for r in reports:
        print(f"  • {r}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

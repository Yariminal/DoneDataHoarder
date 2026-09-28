"""Metadata-only relation write/idempotence profile with real frame groups.

No image bytes are created, hashed, decoded, or sent to a provider. A stub
returns no LLM groups; the production frame backstop and DB writes run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import run_step, write_json


class EmptyProvider:
    def generate_json(self, *_args, **_kwargs):
        return []

    def generate(self, *_args, **_kwargs):
        return "[]"


def _source_fingerprint() -> dict[str, str]:
    return {name: hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
            for name in ("donedatahoarder/core/relate.py", "scripts/bench_relate_groups.py")}


def benchmark(output: Path, directories: int, frames_per_directory: int) -> dict:
    if directories < 1 or frames_per_directory < 8:
        raise ValueError("Require at least one directory and eight frames per directory")
    from sqlalchemy import func, insert, select, text
    from sqlalchemy.orm import Session
    from donedatahoarder.core.relate import relate
    from donedatahoarder.db.models import File, FileStatus, RelationGroup, RelationMember, UserSession
    from donedatahoarder.db.session import get_engine, init_db

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "fixture_type": "metadata-only; zero physical image bytes; empty local provider stub",
        "directories": directories,
        "frames_per_directory": frames_per_directory,
        "expected_frame_rows": directories * frames_per_directory,
        "expected_companion_rows": directories,
        "logical_file_bytes_per_frame": 5 * 1024 * 1024,
        "physically_hashed_bytes": 0,
        "source_fingerprint_start": _source_fingerprint(),
        "steps": {},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    with tempfile.TemporaryDirectory(prefix="ddh-relate-groups-") as temp:
        scratch = Path(temp)
        os.environ["DDH_DATA_DIR"] = str(scratch / "state")
        init_db(scratch / "index.sqlite")
        engine = get_engine()
        with Session(engine) as db:
            owner = UserSession(name="relation-scale", root_path=str(scratch / "logical"))
            db.add(owner)
            db.commit()
            sid = owner.id

        def populate() -> dict:
            inserted = 0
            with engine.begin() as conn:
                for directory in range(directories):
                    parent = scratch / "logical" / f"project_{directory:05d}"
                    rows = [
                        {"session_id": sid, "path": str(parent / f"{frame:05d}.jpg"),
                         "filename": f"{frame:05d}.jpg", "extension": ".jpg",
                         "mime_type": "image/jpeg", "size_bytes": 5 * 1024 * 1024,
                         "status": FileStatus.ENRICHED}
                        for frame in range(28, 28 + frames_per_directory)
                    ]
                    rows.append({"session_id": sid,
                                 "path": str(parent / "frame_notes.txt"),
                                 "filename": "frame_notes.txt", "extension": ".txt",
                                 "mime_type": "text/plain", "size_bytes": 1024,
                                 "status": FileStatus.ENRICHED})
                    conn.execute(insert(File), rows)
                    inserted += len(rows)
            return {"rows": inserted, "sqlite_bytes": (scratch / "index.sqlite").stat().st_size}

        report["population"] = run_step(report, output, "populate", populate)
        for label in ("first", "rerun"):
            summary = run_step(report, output, label, lambda: relate(
                sid, client=EmptyProvider()))
            with Session(engine) as db:
                groups = db.scalar(select(func.count()).select_from(RelationGroup)
                                   .where(RelationGroup.session_id == sid))
                members = db.scalar(select(func.count()).select_from(RelationMember)
                                    .join(RelationGroup)
                                    .where(RelationGroup.session_id == sid))
                companions = db.execute(text("""
                    SELECT f.path, g.dir_path FROM files f
                    JOIN relation_members m ON m.file_id = f.id
                    JOIN relation_groups g ON g.id = m.group_id
                    WHERE f.session_id = :sid AND f.filename = 'frame_notes.txt'
                """), {"sid": sid}).all()
                foreign_key_issues = db.execute(text("PRAGMA foreign_key_check")).all()
            misplaced_companions = sum(
                str(Path(file_path).parent) != group_dir
                for file_path, group_dir in companions
            )
            report[label + "_audit"] = {
                "summary": summary, "groups": groups, "members": members,
                "linked_companions": len(companions),
                "misplaced_companions": misplaced_companions,
                "foreign_key_issues": len(foreign_key_issues),
            }
            write_json(output, report)
        engine.dispose()
    report["source_fingerprint_end"] = _source_fingerprint()
    expected_members = directories * (frames_per_directory + 1)
    report["pass"] = bool(
        report["source_fingerprint_start"] == report["source_fingerprint_end"]
        and all(report[label + "_audit"]["groups"] == directories
                and report[label + "_audit"]["members"] == expected_members
                and report[label + "_audit"]["linked_companions"] == directories
                and report[label + "_audit"]["misplaced_companions"] == 0
                and report[label + "_audit"]["foreign_key_issues"] == 0
                for label in ("first", "rerun"))
    )
    write_json(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directories", type=int, default=500)
    parser.add_argument("--frames-per-directory", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark(args.output, args.directories, args.frames_per_directory)
    print(json.dumps({"pass": result["pass"], "output": str(args.output),
                      "first": result["first_audit"], "rerun": result["rerun_audit"]},
                     ensure_ascii=False, indent=2))
    if not result["pass"]:
        raise SystemExit(1)

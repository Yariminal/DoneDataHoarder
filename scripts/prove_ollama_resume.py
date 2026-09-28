"""Isolated two-process real Ollama interruption/resume proof on six text files.

This script creates a unique D:\\Test/DDH-real-resume-* fixture. It kills only
its own child after two committed analyses and the third call starts, then a
fresh interpreter explicitly resumes the durable analyze run plan.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import write_json, read_json

MODEL = "gemma4:26b"
HOST = "http://127.0.0.1:11434"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        return [dict(id=row[0], filename=row[1], status=row[2],
                     outcome=row[3], digest=row[4], cache_hit=bool(row[5]))
                for row in conn.execute(
                    "SELECT id, filename, status, analysis_outcome, "
                    "analysis_model_digest, analysis_cache_hit FROM files ORDER BY id"
                )]


def _worker(root: Path, action: str) -> None:
    os.environ["DDH_DATA_DIR"] = str(root / "state")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.pop("GEMINI_API_KEY", None)
    from donedatahoarder.db.session import init_db
    from donedatahoarder.core.jobs import job_manager
    init_db(root / "state" / "index.sqlite")

    if action == "start":
        from donedatahoarder.analyzers.document import DocumentAnalyzer
        original = DocumentAnalyzer.analyze
        calls = root / "reports" / "calls-a.jsonl"
        def marked(self, file_rec, context):
            with calls.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": _utc(), "file_id": file_rec.id,
                                         "filename": file_rec.filename}) + "\n")
                stream.flush()
            return original(self, file_rec, context)
        DocumentAnalyzer.analyze = marked
        from sqlalchemy.orm import Session
        from donedatahoarder.db.models import UserSession
        from donedatahoarder.db.session import get_engine
        with Session(get_engine()) as db:
            sid = db.query(UserSession.id).first()[0]
        plan_id = job_manager.create_run_plan(sid, ["analyze"], {
            "backend": "ollama", "analyze_model": MODEL, "workers": 1,
            "sequence_sample_stride": 0, "use_cache": True,
        })
        job_id = job_manager.advance_run_plan(plan_id)
        write_json(root / "reports" / "started.json", {
            "pid": os.getpid(), "at": _utc(), "session_id": sid,
            "plan_id": plan_id, "job_id": job_id,
        })
    elif action == "resume":
        started = json.loads((root / "reports" / "started.json").read_text(encoding="utf-8"))
        job_manager.reconcile_startup()
        before = job_manager.get_run_plan(started["plan_id"])
        job_id = job_manager.resume_run_plan(started["plan_id"])
        write_json(root / "reports" / "resumed.json", {
            "pid": os.getpid(), "at": _utc(), "before_plan": before,
            "job_id": job_id,
        })
    else:
        raise ValueError(action)

    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        plan = job_manager.get_run_plan(plan_id if action == "start" else started["plan_id"])
        if plan["state"] in {"completed", "failed", "cancelled"}:
            write_json(root / "reports" / f"{action}-final.json", {
                "at": _utc(), "pid": os.getpid(), "plan": plan,
                "rows": _rows(root / "state" / "index.sqlite"),
            })
            return
        time.sleep(0.2)
    raise TimeoutError("analyze plan did not finish within 20 minutes")


def _spawn(root: Path, action: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["DDH_DATA_DIR"] = str(root / "state")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env.pop("GEMINI_API_KEY", None)
    log = (root / "reports" / f"worker-{action}.log").open("w", encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "worker", action, str(root)],
        cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=log,
        stderr=subprocess.STDOUT, env=env, creationflags=flags,
    )
    log.close()
    return process


def _controller(base: Path) -> None:
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=HOST, text_model=MODEL, vision_model=MODEL)
    if MODEL not in local.list_models():
        raise RuntimeError(f"required local model unavailable: {MODEL}")
    model_digest = local.model_digest(MODEL)
    if not model_digest:
        raise RuntimeError("local model digest unavailable")
    root = base / f"DDH-real-resume-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    root.mkdir(exist_ok=False)
    data = root / "data"
    state = root / "state"
    reports = root / "reports"
    for path in (data, state, reports):
        path.mkdir()
    baseline = []
    for i in range(6):
        path = data / f"note_{i+1:02}.txt"
        payload = (f"Document {i+1}: this is an isolated archival review fixture. "
                   f"It records a distinct numbered observation about garden tools, "
                   f"paper records, and labeled storage shelves. "
                   f"Its content is intentionally longer than the verified text threshold.\n")
        path.write_text(payload, encoding="utf-8")
        baseline.append({"path": path.name, "size": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json(reports / "baseline.json", baseline)
    os.environ["DDH_DATA_DIR"] = str(state)
    os.environ.pop("GEMINI_API_KEY", None)
    from donedatahoarder.db.session import init_db, get_engine
    from donedatahoarder.db.models import UserSession, SessionStatus
    from sqlalchemy.orm import Session
    init_db(state / "index.sqlite")
    with Session(get_engine()) as db:
        us = UserSession(name="real-ollama-resume", root_path=str(data),
                         backend="ollama", model=MODEL, analyze_model=MODEL,
                         workers=1, status=SessionStatus.ACTIVE)
        db.add(us)
        db.commit()
        sid = us.id
    from donedatahoarder.core.scanner import scan
    from donedatahoarder.core.enricher import enrich
    scan(data, session_id=sid, workers=1)
    enrich(session_id=sid, workers=1)
    db_path = state / "index.sqlite"
    before_rows = _rows(db_path)
    if len(before_rows) != 6 or any(r["status"] != "ENRICHED" for r in before_rows):
        raise RuntimeError("fixture scan/enrich incomplete")
    report = {"run_dir": str(root), "started_utc": _utc(), "model": MODEL,
              "model_digest": model_digest, "workers": 1,
              "baseline": baseline, "before": before_rows}
    write_json(reports / "proof.json", report)

    a = _spawn(root, "start")
    try:
        deadline = time.monotonic() + 600
        started_file = reports / "started.json"
        while not started_file.exists() and a.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if not started_file.exists():
            raise RuntimeError("process A did not start plan")
        report["process_a"] = read_json(started_file)
        write_json(reports / "proof.json", report)
        calls = reports / "calls-a.jsonl"
        while time.monotonic() < deadline:
            rows = _rows(db_path)
            call_rows = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []
            if sum(r["status"] == "ANALYZED" for r in rows) == 2 and len(call_rows) >= 3:
                report["kill_at_utc"] = _utc()
                report["calls_before_kill"] = call_rows
                report["rows_before_kill"] = rows
                a.kill()
                a.wait(timeout=15)
                report["process_a_exit_code"] = a.returncode
                break
            if a.poll() is not None:
                raise RuntimeError("process A exited before third call")
            time.sleep(0.05)
        else:
            raise TimeoutError("did not reach two committed rows and third started call")
    finally:
        if a.poll() is None:
            a.kill()
            a.wait(timeout=15)
    report["rows_after_kill"] = _rows(db_path)
    write_json(reports / "proof.json", report)
    if sum(r["status"] == "ANALYZED" for r in report["rows_after_kill"]) != 2:
        raise RuntimeError("kill did not leave exactly two committed analyses")
    b = _spawn(root, "resume")
    try:
        b.wait(timeout=1200)
    finally:
        if b.poll() is None:
            b.kill()
            b.wait(timeout=15)
    report["process_b"] = read_json(reports / "resumed.json") if (reports / "resumed.json").exists() else None
    report["process_b_exit_code"] = b.returncode
    report["final"] = read_json(reports / "resume-final.json") if (reports / "resume-final.json").exists() else None
    report["finished_utc"] = _utc()
    report["file_hashes_unchanged"] = all(
        hashlib.sha256((data / row["path"]).read_bytes()).hexdigest() == row["sha256"]
        for row in baseline
    )
    report["pass"] = bool(
        b.returncode == 0 and report["final"]
        and report["process_b"]["before_plan"]["state"] == "interrupted"
        and report["final"]["plan"]["state"] == "completed"
        and report["final"]["plan"]["current_index"] == 1
        and all(row["status"] == "ANALYZED" for row in report["final"]["rows"])
        and all(row["digest"] == model_digest for row in report["final"]["rows"])
        and report["file_hashes_unchanged"]
        and [row["id"] for row in report["rows_after_kill"] if row["status"] == "ANALYZED"]
        == [row["id"] for row in report["final"]["rows"] if row["id"] in {
            r["id"] for r in report["rows_after_kill"] if r["status"] == "ANALYZED"}]
    )
    write_json(reports / "proof.json", report)
    print(json.dumps({"run_dir": str(root), "pass": report["pass"],
                      "proof": str(reports / "proof.json")}, indent=2))
    if not report["pass"]:
        raise RuntimeError("real Ollama resume proof failed; inspect retained reports")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run")
    start.add_argument("--base", type=Path, default=Path("D:/Test"))
    worker = sub.add_parser("worker")
    worker.add_argument("action", choices=("start", "resume"))
    worker.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.command == "worker":
        _worker(args.root, args.action)
    else:
        _controller(args.base)


if __name__ == "__main__":
    main()

"""Compare saved corpus analysis with a new local model without updating files/DB.

The baseline is the existing full-corpus run. Each comparison result is saved
immediately, so a long model run can be inspected or resumed after interruption.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import read_json, run_paths, write_json

DEFAULT_IDS = [1, 2, 7, 8, 10, 11]


def model_details(host: str, names: set[str]) -> dict:
    import httpx

    response = httpx.get(host.rstrip("/") + "/api/tags", timeout=15)
    response.raise_for_status()
    models = {row.get("name"): row for row in response.json().get("models", [])}
    missing = names - models.keys()
    if missing:
        raise RuntimeError(f"local Ollama model(s) unavailable: {sorted(missing)}")
    return {name: {
        "name": name,
        "size_bytes": models[name].get("size"),
        "digest": models[name].get("digest"),
        "details": models[name].get("details", {}),
    } for name in names}


def result_error(description: str | None) -> bool:
    return (description or "").lower().startswith("ai inference failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--model", default="gemma4:26b")
    parser.add_argument("--ids", type=int, nargs="+", default=DEFAULT_IDS)
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    root, data, state = run_paths(args.run_dir)
    pipeline = read_json(root / "reports" / "pipeline.json")
    if pipeline.get("ai_scope") != "full" or pipeline.get("corpus") != "Small.zip":
        raise SystemExit("comparison requires a completed full Small.zip baseline")
    if args.timeout < 30 or args.timeout > 3600:
        raise SystemExit("timeout must be 30..3600 seconds")
    ids = list(dict.fromkeys(args.ids))
    if not ids or any(fid < 1 for fid in ids):
        raise SystemExit("provide positive file IDs")
    endpoint = urlparse(args.ollama_host)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Ollama host must be local HTTP loopback")

    # Prevent ancillary model downloads and keep app state in this run's
    # directory. Analysis is read-only; no app DB initialization/migration.
    os.environ["DDH_DATA_DIR"] = str(state / "datahoarder")
    os.environ["USERPROFILE"] = str(state / "home")
    os.environ["APPDATA"] = str(state / "roaming")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(state / "cache" / "huggingface")
    os.environ["DATAHOARDER_OLLAMA_TIMEOUT"] = str(args.timeout)
    os.environ.pop("GEMINI_API_KEY", None)

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File
    from donedatahoarder.core.context import build_context
    from donedatahoarder.analyzers.pipeline import _get_analyzer
    from donedatahoarder.analyzers.image import ImageAnalyzer
    from donedatahoarder.analyzers.video import VideoAnalyzer
    from donedatahoarder.analyzers.document import DocumentAnalyzer
    from donedatahoarder.analyzers.archive import ArchiveAnalyzer
    from donedatahoarder.analyzers.threedmodel import ThreeDModelAnalyzer
    from donedatahoarder.ai.ollama_client import OllamaClient

    db_path = state / "corpus.sqlite"
    if not db_path.is_file():
        raise SystemExit("baseline database missing")
    engine = create_engine("sqlite+pysqlite:///file:" + db_path.as_posix() + "?mode=ro&uri=true")

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA query_only=ON")

    with Session(engine) as db:
        files = db.query(File).filter(File.id.in_(ids), File.session_id == pipeline["session_id"]).all()
        selected = {file.id: file for file in files}
        if set(selected) != set(ids):
            raise SystemExit(f"baseline lacks selected IDs: {sorted(set(ids) - set(selected))}")
        baseline = {}
        for fid in ids:
            file = selected[fid]
            if not Path(file.path).is_file() or not Path(file.path).resolve().is_relative_to(data.resolve()):
                raise SystemExit(f"selected file is missing or outside copy: {file.path}")
            if not file.analyzed_at or not file.ai_description:
                raise SystemExit(f"file {fid} has no saved e4b analysis")
            baseline[str(fid)] = {
                "path": file.path, "filename": file.filename,
                "extension": file.extension, "mime_type": file.mime_type,
                "status": file.status.value, "analyzed_at": file.analyzed_at.isoformat(),
                "description": file.ai_description,
                "suggested_name": file.ai_suggested_name,
                "confidence": file.ai_confidence,
                "tags": file.tags_list(),
                "content_available": not file.ai_description.startswith("[UNVERIFIED"),
                "error": result_error(file.ai_description),
            }
        # Detach after read-only session closes; analyzers only read attributes.
        for file in files:
            db.expunge(file)

    details = model_details(args.ollama_host, {pipeline["model"], args.model})
    output = root / "reports" / "model-comparison.json"
    if output.exists():
        report = read_json(output)
        if (report.get("baseline_model") != pipeline["model"]
                or report.get("candidate_model") != args.model
                or report.get("file_ids") != ids):
            raise SystemExit("existing comparison has different model/IDs")
    else:
        report = {
            "run_dir": str(root), "baseline_model": pipeline["model"],
            "candidate_model": args.model, "file_ids": ids,
            "model_details": details, "baseline": baseline,
            "candidate": {},
            "baseline_corpus_analyze_seconds": pipeline["steps"]["analyze"]["seconds"],
            "baseline_corpus_analyzed_count": pipeline["steps"]["analyze"]["result"].get("analyzed"),
            "note": "Baseline is saved e4b output; no per-file baseline timings were recorded. Candidate times are measured now.",
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(output, report)

    client = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    analyzers = [ImageAnalyzer(client), VideoAnalyzer(client), DocumentAnalyzer(client),
                 ArchiveAnalyzer(client), ThreeDModelAnalyzer(client)]
    for fid in ids:
        if str(fid) in report["candidate"]:
            print(f"{fid}: existing result retained", flush=True)
            continue
        file = selected[fid]
        analyzer = _get_analyzer(analyzers, file.mime_type, file.extension)
        start = time.perf_counter()
        if analyzer is None:
            result = {"error": "no analyzer available", "content_available": False}
        else:
            try:
                analysis = analyzer.analyze(file, build_context(file))
                result = {
                    "analyzer": type(analyzer).__name__,
                    "description": analysis.description,
                    "suggested_name": analysis.suggested_name,
                    "confidence": analysis.confidence,
                    "tags": analysis.tags,
                    "content_available": analysis.content_available,
                    "error": result_error(analysis.description),
                }
            except Exception as exc:
                result = {"analyzer": type(analyzer).__name__,
                          "error": f"{type(exc).__name__}: {exc}",
                          "content_available": False}
        result["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        report["candidate"][str(fid)] = result
        write_json(output, report)
        print(f"{fid}: {result['elapsed_seconds']}s, error={result.get('error', False)}", flush=True)

    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(output, report)
    print(f"Saved {output}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    main()

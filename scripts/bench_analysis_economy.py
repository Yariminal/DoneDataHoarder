"""Real local Ollama full vs sampled vs cache benchmark on 20 Medium frames."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import digest, run_step, write_json
from donedatahoarder.proposals.sequence_identity import numbered_frame_identity

SOURCE = Path("D:/Test/Medium.zip")
MODEL = "gemma4:26b"
SOURCE_FILES = (
    "scripts/bench_analysis_economy.py",
    "donedatahoarder/analyzers/pipeline.py",
    "donedatahoarder/analyzers/cache.py",
    "donedatahoarder/analyzers/image.py",
    "donedatahoarder/proposals/sequence_identity.py",
)


def source_fingerprint() -> dict[str, str]:
    return {name: digest(REPO_ROOT / name) for name in SOURCE_FILES}


def select_frame_window(archive: zipfile.ZipFile, start: int, count: int) -> list[zipfile.ZipInfo]:
    """Choose a stable window from the longest contiguous padded JPEG family."""
    if start < 0 or count < 4:
        raise ValueError("frame start must be non-negative and count at least four")
    families: dict[tuple[str, int], list[tuple[int, zipfile.ZipInfo]]] = defaultdict(list)
    for info in archive.infolist():
        if info.is_dir():
            continue
        path = Path(info.filename)
        identity = numbered_frame_identity(path)
        if identity and identity[0] == "" and path.suffix.lower() == ".jpg":
            families[(str(path.parent), identity[2])].append((identity[1], info))
    runs = []
    for (parent, _width), entries in families.items():
        entries.sort(key=lambda item: (item[0], item[1].filename))
        current = []
        for number, info in entries:
            if current and number != current[-1][0] + 1:
                runs.append((parent, current))
                current = []
            current.append((number, info))
        if current:
            runs.append((parent, current))
    runs.sort(key=lambda item: (-len(item[1]), item[0].casefold(), item[1][0][0]))
    if not runs or len(runs[0][1]) < start + count:
        raise RuntimeError("no contiguous padded JPEG family covers requested window")
    return [info for _, info in runs[0][1][start:start + count]]


def run(frame_start_index: int = 0, frame_count: int = 20) -> dict:
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host="http://127.0.0.1:11434", text_model=MODEL,
                         vision_model=MODEL)
    if MODEL not in local.list_models():
        raise RuntimeError("selected local Ollama model unavailable")
    model_digest = local.model_digest(MODEL)
    if not model_digest:
        raise RuntimeError("model digest unavailable")
    source_hash_before = digest(SOURCE)
    root = SOURCE.parent / ("DDH-analysis-economy-" +
                            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") +
                            uuid.uuid4().hex[:8])
    root.mkdir(exist_ok=False)
    data, state, reports = (root / name for name in ("data", "state", "reports"))
    for path in (data, state, reports):
        path.mkdir()
    baseline = []
    with zipfile.ZipFile(SOURCE) as archive:
        frames = select_frame_window(archive, frame_start_index, frame_count)
        for info in frames:
            name = Path(info.filename).name
            payload = archive.read(info)
            path = data / name
            path.write_bytes(payload)
            baseline.append({"path": name, "size": len(payload),
                             "sha256": hashlib.sha256(payload).hexdigest()})
    os.environ["DDH_DATA_DIR"] = str(state)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.pop("GEMINI_API_KEY", None)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, UserSession, SessionStatus
    from donedatahoarder.db.session import init_db, get_engine
    init_db(state / "index.sqlite")
    with Session(get_engine()) as db:
        us = UserSession(name=f"analysis-economy-{frame_count}-frames", root_path=str(data),
                         backend="ollama", model=MODEL, analyze_model=MODEL,
                         workers=1, status=SessionStatus.ACTIVE)
        db.add(us)
        db.commit()
        sid = us.id
    from donedatahoarder.core.scanner import scan
    from donedatahoarder.core.enricher import enrich
    from donedatahoarder.ai.provider import init_ai
    from donedatahoarder.analyzers.pipeline import analyze
    from donedatahoarder.proposals.sequence_identity import is_confirmed_frame
    assert all(is_confirmed_frame(data / row["path"]) for row in baseline)
    report = {"run_dir": str(root), "source_zip": str(SOURCE),
              "source_sha256_before": source_hash_before, "model": MODEL,
              "model_digest": model_digest, "frames": len(baseline),
              "frame_selection": {"start_index": frame_start_index, "count": frame_count,
                                  "strategy": "longest contiguous padded JPEG family"},
              "source_fingerprint_before": source_fingerprint(),
              "baseline": baseline, "steps": {}, "runs": {}}
    report_path = reports / "benchmark.json"
    write_json(report_path, report)
    init_ai(backend="ollama", ollama_host="http://127.0.0.1:11434",
            text_model=MODEL, vision_model=MODEL)
    for label, stride, use_cache in (
        ("full", 0, True),
        ("representative_stride_10", 10, False),
        ("same_context_cache_repeat", 0, True),
    ):
        run_step(report, report_path, f"{label}_scan", lambda: scan(
            data, session_id=sid, force_rescan=label != "full"))
        run_step(report, report_path, f"{label}_enrich", lambda: enrich(session_id=sid))
        result = run_step(report, report_path, f"{label}_analyze", lambda: analyze(
            workers=1, min_size_kb=0, session_id=sid,
            sequence_sample_stride=stride, use_cache=use_cache))
        with Session(get_engine()) as db:
            rows = db.query(File).filter(File.session_id == sid).order_by(File.filename).all()
            evidence = [{"filename": row.filename, "status": row.status.value,
                         "outcome": row.analysis_outcome, "reason": row.analysis_reason,
                         "evidence_source": row.analysis_evidence_source,
                         "cache_hit": row.analysis_cache_hit,
                         "model_digest": row.analysis_model_digest,
                         "description_present": bool(row.ai_description)} for row in rows]
        report["runs"][label] = {"counts": result, "evidence": evidence,
                                  "analyze_seconds": report["steps"][f"{label}_analyze"]["seconds"],
                                  "peak_process_rss_bytes": report["steps"][f"{label}_analyze"]["peak_process_rss_bytes"]}
        write_json(report_path, report)
        if result["errors"]:
            raise RuntimeError(f"{label} had provider errors")
    report["source_sha256_after"] = digest(SOURCE)
    report["source_fingerprint_after"] = source_fingerprint()
    report["copied_hashes_unchanged"] = all(
        hashlib.sha256((data / row["path"]).read_bytes()).hexdigest() == row["sha256"]
        for row in baseline)
    full = report["runs"]["full"]["counts"]
    representative = report["runs"]["representative_stride_10"]
    cached = report["runs"]["same_context_cache_repeat"]["counts"]
    report["pass"] = bool(
        source_hash_before == report["source_sha256_after"]
        and report["source_fingerprint_before"] == report["source_fingerprint_after"]
        and report["copied_hashes_unchanged"]
        and full["analyzed"] == frame_count and full["cached"] == 0 and full["sampled"] == 0
        and representative["counts"]["sampled"] > 0
        and representative["counts"]["analyzed"] + representative["counts"]["sampled"] == frame_count
        and all(not row["description_present"] and row["outcome"] == "sampled"
                for row in representative["evidence"] if row["status"] == "skipped")
        and cached["cached"] == frame_count and cached["analyzed"] == 0
    )
    write_json(report_path, report)
    print(json.dumps({"run_dir": str(root), "pass": report["pass"],
                      "full": report["runs"]["full"]["counts"],
                      "representative": representative["counts"],
                      "cache_repeat": cached,
                      "timings_seconds": {name: value["analyze_seconds"]
                                          for name, value in report["runs"].items()},
                      "report": str(report_path)}, ensure_ascii=False, indent=2))
    if not report["pass"]:
        raise RuntimeError("analysis economy benchmark did not meet evidence checks")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame-start-index", type=int, default=0)
    parser.add_argument("--frame-count", type=int, default=20)
    options = parser.parse_args()
    run(options.frame_start_index, options.frame_count)

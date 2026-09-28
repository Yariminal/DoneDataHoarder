"""Bounded file-count benchmark; no synthetic large byte allocation or AI calls."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import _process_rss_bytes, write_json, run_step


PHASE_SOURCES = (
    "donedatahoarder/core/dedup.py",
    "donedatahoarder/core/relate.py",
    "donedatahoarder/core/dependency_protection.py",
    "donedatahoarder/proposals/sequence_identity.py",
    "donedatahoarder/proposals/sequence_detector.py",
    "donedatahoarder/proposals/namer/core.py",
    "donedatahoarder/proposals/namer/postpass.py",
    "donedatahoarder/proposals/namer/naming.py",
    "donedatahoarder/proposals/namer/llm.py",
    "donedatahoarder/proposals/organizer/core.py",
    "donedatahoarder/proposals/organizer/tree.py",
    "donedatahoarder/proposals/organizer/backstops.py",
    "donedatahoarder/proposals/organizer/text_utils.py",
    "donedatahoarder/proposals/organizer/prompts.py",
    "donedatahoarder/core/scanner.py",
    "donedatahoarder/core/enricher.py",
    "donedatahoarder/core/preflight.py",
    "scripts/scale_benchmark.py",
)


def source_fingerprint() -> dict:
    files = {}
    combined = hashlib.sha256()
    for name in PHASE_SOURCES:
        content = (REPO_ROOT / name).read_bytes()
        files[name] = hashlib.sha256(content).hexdigest()
        combined.update(name.encode("utf-8"))
        combined.update(bytes.fromhex(files[name]))
    return {"combined_sha256": combined.hexdigest(), "files": files}


def bounded_step(report: dict, path: Path, name: str, fn):
    try:
        return run_step(report, path, name, fn)
    except Exception:
        step = report.get("steps", {}).get(name)
        if step and len(step.get("error", "")) > 500:
            step["error"] = step["error"][:500] + "… [truncated; see stderr log]"
            write_json(path, report)
        raise


def benchmark(*, counts: list[int], real_files: int, output: Path,
              logical_file_bytes: int, phase_rows: int) -> dict:
    from sqlalchemy import func, insert, select
    from sqlalchemy.orm import Session
    from donedatahoarder.core.preflight import estimate_collection
    from donedatahoarder.core.scanner import scan
    from donedatahoarder.core.enricher import enrich
    from donedatahoarder.db.models import File, FileStatus, UserSession
    from donedatahoarder.db.session import get_engine, init_db

    report = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "counts": counts, "real_files": real_files,
              "logical_file_bytes_per_db_row": logical_file_bytes,
              "physical_fixture_bytes": real_files * 32,
              "metadata_only_rows": [], "real_fixture": {}, "phase_fixture": {"steps": {}},
              "source_fingerprint_start": source_fingerprint()}
    with tempfile.TemporaryDirectory(prefix="ddh-scale-") as scratch_name, ExitStack() as cleanup:
        scratch = Path(scratch_name)
        init_db(scratch / "scale.sqlite")
        engine = get_engine()
        cleanup.callback(engine.dispose)
        with Session(engine) as db:
            session = UserSession(name="scale-metadata", root_path=str(scratch))
            db.add(session)
            db.commit()
            sid = session.id
        populated = 0
        for target in counts:
            started = time.perf_counter()
            with engine.begin() as conn:
                for start in range(populated, target, 1000):
                    end = min(start + 1000, target)
                    conn.execute(insert(File), [
                        {"session_id": sid, "path": str(scratch / "logical" / f"f{i:07}.bin"),
                         "filename": f"f{i:07}.bin", "extension": ".bin",
                         "size_bytes": logical_file_bytes, "status": FileStatus.ENRICHED}
                        for i in range(start, end)
                    ])
            insert_seconds = time.perf_counter() - started
            populated = target
            started = time.perf_counter()
            with Session(engine) as db:
                counted = db.scalar(select(func.count(File.id)).where(File.session_id == sid))
                streamed = sum(1 for _ in db.execute(select(File.id).where(
                    File.session_id == sid).order_by(File.id)).yield_per(500))
                first_batch = db.execute(select(File.id).where(
                    File.session_id == sid, File.status == FileStatus.ENRICHED,
                    File.id > 0).order_by(File.id).limit(50)).all()
            query_seconds = time.perf_counter() - started
            report["metadata_only_rows"].append({
                "rows": target, "logical_bytes": target * logical_file_bytes,
                "physically_hashed_bytes": 0, "insert_seconds": round(insert_seconds, 3),
                "stream_count_seconds": round(query_seconds, 3),
                "counted": counted, "streamed": streamed,
                "first_batch": len(first_batch), "process_rss_bytes": _process_rss_bytes(),
                "db_bytes": (scratch / "scale.sqlite").stat().st_size,
            })
            write_json(output, report)

        root = scratch / "real-files"
        root.mkdir()
        for i in range(real_files):
            (root / f"file_{i:06}.txt").write_bytes(f"scale fixture {i:06d}".encode().ljust(32, b" "))
        report["real_fixture"]["preflight"] = estimate_collection(root)
        with Session(engine) as db:
            session = UserSession(name="scale-real", root_path=str(root))
            db.add(session)
            db.commit()
            real_sid = session.id
        started = time.perf_counter()
        report["real_fixture"]["scan"] = scan(root, session_id=real_sid)
        report["real_fixture"]["scan_seconds"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        report["real_fixture"]["enrich"] = enrich(session_id=real_sid)
        report["real_fixture"]["enrich_seconds"] = round(time.perf_counter() - started, 3)
        report["real_fixture"]["physically_hashed_bytes"] = real_files * 32
        report["real_fixture"]["process_rss_bytes"] = _process_rss_bytes()
        write_json(output, report)

        # Dense synthetic DB relations exercise actual phase algorithms with
        # a deterministic in-process provider. These rows have no disk bytes.
        phase_sid = None
        with Session(engine) as db:
            session = UserSession(name="scale-phases", root_path=str(scratch / "phase"))
            db.add(session)
            db.commit()
            phase_sid = session.id
        with engine.begin() as conn:
            for start in range(0, phase_rows, 500):
                end = min(start + 500, phase_rows)
                conn.execute(insert(File), [
                    {"session_id": phase_sid,
                     "path": str(scratch / "phase" / f"folder_{i // 100:04}" / f"photo_{i:06}.jpg"),
                     "filename": f"photo_{i:06}.jpg", "extension": ".jpg",
                     "mime_type": "image/jpeg", "size_bytes": 32,
                     "hash_md5": f"{i // 100:032x}",
                     "ai_description": (
                         f"Synthetic fixture image {i}: a documented storage view "
                         "with shelves, labeled bins, neutral light, and a clearly "
                         "visible numbered card used for archive workflow review. "
                         "This description is benchmark data only."
                     ),
                     "ai_tags": '["synthetic_fixture","archive_workflow","labeled_bins","storage_shelves","neutral_light","numbered_card"]',
                     "ai_confidence": 0.9,
                     "analysis_outcome": "content_verified",
                     "analysis_evidence_source": "vision",
                     "analysis_model_tag": "stub:fixture",
                     "analysis_model_digest": "fixture-digest",
                     "status": FileStatus.ANALYZED}
                    for i in range(start, end)
                ])
        phase = report["phase_fixture"]
        phase["source_fingerprint_start"] = report["source_fingerprint_start"]
        phase["rows"] = phase_rows
        phase["logical_bytes"] = phase_rows * 32
        phase["physically_hashed_bytes"] = 0
        phase["provider"] = "deterministic stub returning no LLM groups/proposals"
        phase["text_payload_note"] = "Descriptions ~190 chars; tag JSON ~110 chars per row"
        class StubProvider:
            text_model = "stub:fixture"
            vision_model = "stub:fixture"
            def generate_json(self, *_args, **_kwargs):
                return []
            def generate(self, *_args, **_kwargs):
                return "[]"
        import donedatahoarder.ai.router as router
        old_get_client = router.get_client
        router.get_client = lambda: StubProvider()
        try:
            from donedatahoarder.core.dedup import (
                find_exact_duplicates, find_perceptual_duplicates,
                find_text_near_duplicates, find_semantic_duplicates,
                generate_dedup_proposals,
            )
            from donedatahoarder.core.relate import relate
            from donedatahoarder.proposals.namer import generate_proposals
            from donedatahoarder.proposals.organizer import generate_reorg_proposals
            for name, fn in (
                ("dedup_exact", find_exact_duplicates),
                ("dedup_perceptual", find_perceptual_duplicates),
                ("dedup_text", find_text_near_duplicates),
                ("dedup_semantic", find_semantic_duplicates),
                ("dedup_proposals", generate_dedup_proposals),
            ):
                bounded_step(phase, output, name, lambda fn=fn: fn(session_id=phase_sid))
            bounded_step(phase, output, "relate", lambda: relate(
                session_id=phase_sid, client=StubProvider()))
            bounded_step(phase, output, "propose", lambda: generate_proposals(
                session_id=phase_sid))
            bounded_step(phase, output, "organize", lambda: generate_reorg_proposals(
                session_id=phase_sid))
        finally:
            router.get_client = old_get_client
        write_json(output, report)
    report["scratch_removed"] = True
    report["source_fingerprint_end"] = source_fingerprint()
    report["source_unchanged_during_run"] = (
        report["source_fingerprint_start"] == report["source_fingerprint_end"]
    )
    write_json(output, report)
    return report


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[10_000, 50_000, 100_000])
    parser.add_argument("--real-files", type=int, default=2_000)
    parser.add_argument("--logical-file-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--phase-rows", type=int, default=10_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.counts or sorted(set(args.counts)) != args.counts or min(args.counts) < 1:
        raise ValueError("counts must increase and be positive")
    if (not 0 <= args.real_files <= 20_000 or args.logical_file_bytes < 0
            or not 1 <= args.phase_rows <= 100_000):
        raise ValueError("fixture limits exceeded")
    result = benchmark(counts=args.counts, real_files=args.real_files,
                       output=args.output.resolve(), logical_file_bytes=args.logical_file_bytes,
                       phase_rows=args.phase_rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

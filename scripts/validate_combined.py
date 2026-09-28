"""One isolated six-ZIP validation collection under D:\\Test.

Commands are explicit. `prepare` safely extracts into archive namespaces;
`pipeline` uses one database session; `audit` checks files, ZIPs and DB.
No command deletes an existing run or source archive.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_corpus import (
    CORPUS_DIR, CHUNK, archive_path, digest, safe_members, within,
    write_json, read_json, isolate, db_session, run_step, run_ai_step,
    require_local_provider, status_report, current_manifest, directory_manifest,
)

STANDARD_ARCHIVES = (
    ("Small", "Small.zip"),
    ("Medium", "Medium.zip"),
    ("Photo", "Medium - With Photo.zip"),
    ("Links", "Medium - With Links.zip"),
    ("Large", "Large.zip"),
)
ADDITIONAL_NAMESPACE = "Corpus"
PREFIX = "DDH-combined-"
MAX_COMBINED_FILES = 20_000
MAX_COMBINED_BYTES = 12 * 1024**3


def selected_archives(additional_zip: str | None = None) -> tuple[tuple[str, Path], ...]:
    """Resolve the five named test ZIPs and exactly one additional source.

    A basename supplied at runtime disambiguates D:\\Test folders containing
    other ZIPs. No private source basename is stored in repository code.
    """
    sources = [(namespace, archive_path(name))
               for namespace, name in STANDARD_ARCHIVES]
    known_names = {source.name.casefold() for _, source in sources}
    if additional_zip:
        additional = archive_path(additional_zip)
        if additional.name.casefold() in known_names:
            raise ValueError("additional ZIP duplicates a standard source")
    else:
        candidates = []
        for path in CORPUS_DIR.iterdir():
            if path.suffix.lower() != ".zip" or path.name.casefold() in known_names:
                continue
            try:
                candidates.append(archive_path(path.name))
            except ValueError:
                continue
        if len(candidates) != 1:
            raise ValueError(
                f"found {len(candidates)} additional top-level ZIPs; "
                "specify exactly one with --corpus-zip"
            )
        additional = candidates[0]
    return (*sources, (ADDITIONAL_NAMESPACE, additional))


def combined_paths(value: str) -> tuple[Path, Path, Path]:
    root = Path(value).resolve(strict=True)
    if root.parent != CORPUS_DIR.resolve(strict=True) or not root.name.startswith(PREFIX):
        raise ValueError("run-dir must be a direct DDH-combined-* child of D:\\Test")
    meta = read_json(root / "run.json")
    if meta.get("schema") != 1 or meta.get("run_dir") != str(root):
        raise ValueError("invalid combined run metadata")
    data, state = root / "data", root / "state"
    if not all(p.is_dir() and not p.is_symlink() and within(p, root) for p in (data, state)):
        raise ValueError("combined data/state is missing or redirected")
    return root, data, state


def _indexed_rows(engine, session_id: str, data: Path) -> dict[str, tuple[str, str | None, bool]]:
    """Return indexed paths with durable status/outcome/cache provenance."""
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File

    rows = {}
    with Session(engine) as db:
        for path, status, outcome, cache_hit in db.query(
            File.path, File.status, File.analysis_outcome, File.analysis_cache_hit,
        ).filter(File.session_id == session_id).yield_per(500):
            try:
                relative = Path(path).relative_to(data).as_posix()
            except ValueError as exc:
                raise RuntimeError("indexed path escaped combined data root") from exc
            if relative in rows:
                raise RuntimeError("duplicate indexed path in combined session")
            rows[relative] = (status.value, outcome, bool(cache_hit))
    return rows


def _scan_coverage(engine, session_id: str, data: Path, result: dict) -> dict:
    from donedatahoarder.core.scanner import walk_files

    if result.get("errors", 0):
        raise RuntimeError(f"scan had {result['errors']} errors")
    expected = {path.relative_to(data).as_posix() for path in walk_files(data)}
    indexed = _indexed_rows(engine, session_id, data)
    if expected != indexed.keys():
        raise RuntimeError(
            f"scan indexed {len(indexed)} of {len(expected)} index-eligible paths "
            f"(missing={len(expected - indexed.keys())}, extra={len(indexed.keys() - expected)})"
        )
    if any(status != "pending" for status, _, _ in indexed.values()):
        raise RuntimeError("scan left indexed rows outside pending status")
    extracted = int(read_json(data.parent / "run.json")["files"])
    if len(expected) > extracted:
        raise RuntimeError("index eligibility exceeds extracted file baseline")
    return {"extracted": extracted, "index_eligible": len(expected),
            "indexed": len(indexed), "excluded_from_index": extracted - len(expected)}


def _preflight_coverage(estimate: dict, metadata: dict) -> dict:
    """Reject unreadable subtrees before scan can mistake them for exclusions."""
    if not estimate.get("size_estimate_complete"):
        raise RuntimeError("preflight could not read the whole extracted collection")
    if (estimate["full_collection_files"] != metadata["files"]
            or estimate["logical_bytes"] != metadata["logical_bytes"]):
        raise RuntimeError("preflight files/bytes differ from extraction baseline")
    return {"extracted": metadata["files"],
            "index_eligible": estimate["files"],
            "excluded_from_index": metadata["files"] - estimate["files"],
            "logical_bytes": estimate["logical_bytes"],
            "physically_hashed_bytes": estimate["physically_hashed_bytes"]}


def _enrich_coverage(engine, session_id: str, data: Path,
                     indexed: set[str], result: dict) -> dict:
    if result.get("errors", 0):
        raise RuntimeError(f"enrichment had {result['errors']} errors")
    rows = _indexed_rows(engine, session_id, data)
    if rows.keys() != indexed or any(status != "enriched" for status, _, _ in rows.values()):
        raise RuntimeError("enrichment left missing or unprocessed indexed rows")
    return {"indexed": len(rows), "enriched": len(rows), "errors": 0}


def _final_coverage(engine, session_id: str, data: Path, indexed: set[str],
                    mode: str, analysis_result: dict | None,
                    *, compare_run_counters: bool = True) -> dict:
    rows = _indexed_rows(engine, session_id, data)
    if rows.keys() != indexed:
        raise RuntimeError("final database paths differ from indexed baseline")
    counts = {name: 0 for name in ("fresh", "cached", "sampled", "skipped",
                                   "errors", "unprocessed", "metadata_only_unanalyzed")}
    for status, outcome, cache_hit in rows.values():
        if status == "error" or outcome == "failed":
            counts["errors"] += 1
        elif mode == "metadata_only":
            if status in {"enriched", "proposed"} and outcome is None:
                counts["metadata_only_unanalyzed"] += 1
            else:
                counts["unprocessed"] += 1
        elif outcome == "sampled" and status == "skipped":
            counts["sampled"] += 1
        elif outcome == "skipped" and status == "skipped":
            counts["skipped"] += 1
        elif status in {"analyzed", "proposed"} and outcome in {
            "content_verified", "context_only", "metadata_only",
        }:
            counts["cached" if cache_hit else "fresh"] += 1
        else:
            counts["unprocessed"] += 1
    counts["indexed"] = len(rows)
    if counts["errors"] or counts["unprocessed"]:
        raise RuntimeError(f"final coverage incomplete: {counts}")
    if mode != "metadata_only" and compare_run_counters:
        expected = {"fresh": "analyzed", "cached": "cached", "sampled": "sampled",
                    "skipped": "skipped", "errors": "errors"}
        if analysis_result is None or any(
            counts[name] != analysis_result.get(source, 0)
            for name, source in expected.items()
        ):
            raise RuntimeError("persisted analysis coverage differs from run counters")
    return counts


def _pipeline_report(root: Path, basename: str = "pipeline.json") -> tuple[Path, dict]:
    """Select an explicit completed pipeline or continuation report in this run."""
    if (not basename or Path(basename).name != basename
            or basename not in {"pipeline.json", "continuation-1.json"}):
        raise ValueError("unsupported pipeline report basename")
    path = root / "reports" / basename
    report = read_json(path)
    original = read_json(root / "reports" / "pipeline.json")
    if "final" not in report:
        raise ValueError("pipeline incomplete")
    if report.get("session_id") != original.get("session_id"):
        raise ValueError("selected pipeline report session differs from original")
    if basename != "pipeline.json":
        if (report.get("status") != "complete" or report.get("run_dir") != str(root)
                or report.get("original_pipeline_sha256") != digest(root / "reports" / "pipeline.json")
                or report.get("original_source_report_sha256") != digest(
                    root / "reports" / "astra-source-fingerprint-start.json")
                or report.get("indexed_records_sha256") != digest(root / "reports" / "indexed-records.json")):
            raise ValueError("continuation provenance differs from original run")
    return path, report


def prepare(_args) -> None:
    inventory = []
    total_files = 0
    total_bytes = 0
    for namespace, source in selected_archives(getattr(_args, "corpus_zip", None)):
        with zipfile.ZipFile(source) as zf:
            members = safe_members(zf)
            files = sum(not info.is_dir() for info, _ in members)
            size = sum(info.file_size for info, _ in members if not info.is_dir())
        total_files += files
        total_bytes += size
        inventory.append((namespace, source, files, size))
    if total_files > MAX_COMBINED_FILES or total_bytes > MAX_COMBINED_BYTES:
        raise ValueError("combined archive exceeds configured file/byte cap")
    free = shutil.disk_usage(CORPUS_DIR).free
    if free < total_bytes + 2 * 1024**3:
        raise ValueError("insufficient free disk for extraction plus state reserve")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = CORPUS_DIR / f"{PREFIX}{stamp}-{uuid.uuid4().hex[:8]}"
    root.mkdir(exist_ok=False)
    data = root / "data"
    for folder in (data, root / "state", root / "reports"):
        folder.mkdir()
    archives_report = []
    baseline = []
    for namespace, source, expected_files, expected_bytes in inventory:
        ns_root = data / namespace
        ns_root.mkdir()
        before = digest(source)
        n_files = 0
        n_bytes = 0
        with zipfile.ZipFile(source) as zf:
            for info, rel in safe_members(zf):
                target = ns_root / rel
                if not within(target, ns_root):
                    raise ValueError(f"archive path escaped namespace: {rel}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                import hashlib
                h = hashlib.sha256()
                size = 0
                with zf.open(info) as src, target.open("xb") as dst:
                    for block in iter(lambda: src.read(CHUNK), b""):
                        size += len(block)
                        n_bytes += len(block)
                        if size > info.file_size or n_bytes > expected_bytes:
                            raise ValueError("ZIP entry exceeded declared size")
                        dst.write(block)
                        h.update(block)
                if size != info.file_size:
                    raise ValueError("ZIP entry size mismatch")
                modified = datetime(*info.date_time).timestamp()
                os.utime(target, (modified, modified))
                baseline.append({"archive": namespace,
                                 "path": (Path(namespace) / rel).as_posix(),
                                 "size": size, "sha256": h.hexdigest()})
                n_files += 1
        after = digest(source)
        if before != after or n_files != expected_files or n_bytes != expected_bytes:
            raise RuntimeError(f"archive integrity changed: {source.name}")
        archives_report.append({"namespace": namespace, "source": str(source.resolve()),
                                "source_sha256": before, "files": n_files,
                                "logical_bytes": n_bytes})
    metadata = {"schema": 1, "run_dir": str(root.resolve()),
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "files": len(baseline), "logical_bytes": total_bytes,
                "archives": archives_report}
    write_json(root / "run.json", metadata)
    write_json(root / "reports" / "baseline.json", baseline)
    write_json(root / "reports" / "directories.json", directory_manifest(data))
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


def pipeline(args) -> None:
    root, data, state = combined_paths(args.run_dir)
    report_path = root / "reports" / "pipeline.json"
    if report_path.exists():
        raise ValueError("pipeline already started on this copy")
    if args.workers < 1 or args.workers > 4:
        raise ValueError("workers must be 1..4")
    if args.mode not in {"full", "representative", "metadata_only"}:
        raise ValueError("invalid analysis mode")
    from urllib.parse import urlparse
    endpoint = urlparse(args.ollama_host)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama must be local loopback")
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import UserSession, SessionStatus
    session_id = str(uuid.uuid4())
    with Session(engine) as db:
        db.add(UserSession(id=session_id, name="combined-six-archives", root_path=str(data),
                           backend="ollama", model=args.model, analyze_model=args.model,
                           propose_model=args.model, workers=args.workers,
                           status=SessionStatus.ACTIVE))
        db.commit()
    report = {"run_dir": str(root), "session_id": session_id, "model": args.model,
              "mode": args.mode, "workers": args.workers, "steps": {}}
    write_json(report_path, report)
    from donedatahoarder.core.scanner import scan
    from donedatahoarder.core.preflight import estimate_collection
    from donedatahoarder.core.enricher import enrich
    from donedatahoarder.core.dedup import (
        find_exact_duplicates, find_perceptual_duplicates,
        find_text_near_duplicates, find_semantic_duplicates,
        generate_dedup_proposals,
    )
    preflight = run_step(report, report_path, "preflight", lambda: estimate_collection(
        data, mode=args.mode, sequence_sample_stride=args.sequence_sample_stride))
    run_step(report, report_path, "preflight_coverage", lambda: _preflight_coverage(
        preflight, read_json(root / "run.json")))
    scan_result = run_step(report, report_path, "scan", lambda: scan(
        data, session_id=session_id, workers=args.workers))
    run_step(report, report_path, "scan_coverage", lambda: _scan_coverage(
        engine, session_id, data, scan_result))
    from donedatahoarder.db.models import File
    with Session(engine) as db:
        indexed_records = sorted(
            ({"id": file_id, "path": Path(path).relative_to(data).as_posix()}
             for file_id, path in db.query(File.id, File.path)
             .filter(File.session_id == session_id).yield_per(500)),
            key=lambda row: row["id"],
        )
        indexed = sorted(row["path"] for row in indexed_records)
    write_json(root / "reports" / "indexed-baseline.json", indexed)
    write_json(root / "reports" / "indexed-records.json", indexed_records)
    enrich_result = run_step(report, report_path, "enrich", lambda: enrich(
        session_id=session_id, workers=args.workers))
    run_step(report, report_path, "enrich_coverage", lambda: _enrich_coverage(
        engine, session_id, data, set(indexed), enrich_result))
    for name, fn in (("dedup_exact", find_exact_duplicates),
                     ("dedup_perceptual", find_perceptual_duplicates),
                     ("dedup_text", find_text_near_duplicates)):
        run_step(report, report_path, name, lambda fn=fn: fn(session_id=session_id))
    analysis_result = None
    if args.mode != "metadata_only":
        from donedatahoarder.ai.ollama_client import OllamaClient
        local = OllamaClient(host=args.ollama_host, text_model=args.model,
                             vision_model=args.model)
        if args.model not in local.list_models():
            raise RuntimeError(f"local model unavailable: {args.model}")
        from donedatahoarder.ai.provider import init_ai
        init_ai(backend="ollama", ollama_host=args.ollama_host,
                text_model=args.model, vision_model=args.model)
        require_local_provider(args.model)
        from donedatahoarder.analyzers.pipeline import analyze
        stride = args.sequence_sample_stride if args.mode == "representative" else 0
        if args.mode == "representative" and stride < 2:
            raise ValueError("representative mode requires stride >= 2")
        result = run_step(report, report_path, "analyze", lambda: analyze(
            workers=args.workers, min_size_kb=0, session_id=session_id,
            sequence_sample_stride=stride, use_cache=args.cache))
        if result["errors"]:
            raise RuntimeError("analysis errors persist; downstream stages blocked")
        analysis_result = result
        run_step(report, report_path, "analysis_coverage", lambda: _final_coverage(
            engine, session_id, data, set(indexed), args.mode, analysis_result))
        run_step(report, report_path, "dedup_semantic",
                 lambda: find_semantic_duplicates(session_id=session_id))
    run_step(report, report_path, "dedup_proposals",
             lambda: generate_dedup_proposals(session_id=session_id))
    if args.mode != "metadata_only":
        from donedatahoarder.core.relate import relate
        run_ai_step(report, report_path, "relate",
                    lambda: relate(session_id=session_id, scope="per_directory",
                                   model=args.model), state / "logs" / "donedatahoarder.log")
    from donedatahoarder.proposals.namer import generate_proposals
    run_step(report, report_path, "propose",
             lambda: generate_proposals(session_id=session_id))
    if args.mode != "metadata_only":
        from donedatahoarder.proposals.organizer import generate_reorg_proposals
        run_ai_step(report, report_path, "organize",
                    lambda: generate_reorg_proposals(session_id=session_id),
                    state / "logs" / "donedatahoarder.log")
    from donedatahoarder.executor import execute, _make_quiet_console
    run_step(report, report_path, "dry_run", lambda: execute(
        dry_run=True, session_id=session_id, _console=_make_quiet_console()))
    report["coverage"] = run_step(report, report_path, "final_coverage", lambda: _final_coverage(
        engine, session_id, data, set(indexed), args.mode, analysis_result))
    report["final"] = status_report(engine, session_id)
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _file_evidence_hash(file) -> str:
    """Hash every persisted File field so retained rows can be checked after retry."""
    values = {column.name: getattr(file, column.name)
              for column in file.__table__.columns}
    payload = json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_sqlite_integrity(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("combined database quick_check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("combined database foreign_key_check failed")


def continue_analysis(args) -> None:
    """One guarded continuation of a failed full run, on its original indexed IDs."""
    root, data, state = combined_paths(args.run_dir)
    report_path = root / "reports" / "continuation-1.json"
    snapshot_path = root / "reports" / "continuation-before.json"
    database_snapshot = root / "reports" / "continuation-before.sqlite"
    if any(path.exists() for path in (report_path, snapshot_path, database_snapshot)):
        raise ValueError("combined continuation already attempted")
    original_path = root / "reports" / "pipeline.json"
    original = read_json(original_path)
    stages = original.get("steps", {})
    if (original.get("mode") != "full" or original.get("model") != args.model
            or original.get("workers") != 1 or "final" in original
            or "analyze" not in stages or stages["analyze"].get("result", {}).get("errors", 0) < 1
            or any(name in stages for name in ("dedup_semantic", "dedup_proposals", "relate", "propose", "organize"))):
        raise ValueError("original pipeline is not a stopped full-analysis failure")
    if (root / "reports" / "execution.json").exists() or list(
            (state / "datahoarder").glob("undo*.log")):
        raise ValueError("execution report or undo journal already exists")
    if not all(name in stages for name in (
            "preflight_coverage", "scan_coverage", "enrich_coverage",
            "dedup_exact", "dedup_perceptual", "dedup_text")):
        raise ValueError("original pre-analysis stages are incomplete")
    if any(stages[name].get("error") for name in stages if name != "analyze"):
        raise ValueError("original pipeline has an earlier stage failure")
    if args.original_commit == args.recovery_commit:
        raise ValueError("continuation must identify both original and recovery commits")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value)
               for value in (args.original_commit, args.recovery_commit)):
        raise ValueError("continuation commits must be full lowercase SHA-1 IDs")
    actual_commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    if actual_commit != args.recovery_commit:
        raise ValueError("recovery commit differs from checked-out source")
    tracked_changes = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True).stdout.strip()
    if tracked_changes:
        raise ValueError("recovery source has uncommitted tracked changes")
    source_report_name = getattr(
        args, "original_source_report", "astra-source-fingerprint-start.json")
    if (Path(source_report_name).name != source_report_name
            or source_report_name != "astra-source-fingerprint-start.json"):
        raise ValueError("unsupported original source report basename")
    source_report_path = root / "reports" / source_report_name
    source_report = read_json(source_report_path)
    source_hashes = source_report.get("source_sha256")
    if (source_report.get("commit") != args.original_commit
            or not isinstance(source_hashes, dict)
            or "scripts/validate_combined.py" not in source_hashes):
        raise ValueError("original commit differs from recorded source provenance")
    source_match_counts = {"raw_git_blob": 0, "crlf_checkout": 0}
    for relative, expected_hash in source_hashes.items():
        parts = Path(relative).parts
        if (not parts or Path(relative).is_absolute() or ".." in parts
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise ValueError("invalid original source fingerprint entry")
        original_bytes = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{args.original_commit}:{relative}"],
            check=True, capture_output=True).stdout
        if hashlib.sha256(original_bytes).hexdigest() == expected_hash:
            source_match_counts["raw_git_blob"] += 1
        else:
            try:
                original_bytes.decode("utf-8")
                is_text = b"\x00" not in original_bytes
            except UnicodeDecodeError:
                is_text = False
            checkout_bytes = original_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if not is_text or hashlib.sha256(checkout_bytes).hexdigest() != expected_hash:
                raise ValueError("recorded source bytes differ from original commit")
            source_match_counts["crlf_checkout"] += 1
    from urllib.parse import urlparse
    endpoint = urlparse(args.ollama_host)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama must be local loopback")
    baseline = read_json(root / "reports" / "baseline.json")
    expected = sorted(({key: row[key] for key in ("path", "size", "sha256")}
                       for row in baseline), key=lambda row: row["path"])
    if current_manifest(data) != expected:
        raise ValueError("combined data differs from original extraction baseline")
    if directory_manifest(data) != read_json(root / "reports" / "directories.json"):
        raise ValueError("combined directories differ from original extraction baseline")
    metadata = read_json(root / "run.json")
    if any(digest(Path(item["source"])) != item["source_sha256"]
           for item in metadata["archives"]):
        raise ValueError("a source ZIP differs from original prepare")
    indexed_records = read_json(root / "reports" / "indexed-records.json")
    indexed_paths = read_json(root / "reports" / "indexed-baseline.json")
    if (len(indexed_records) != len(indexed_paths)
            or sorted(row["path"] for row in indexed_records) != indexed_paths
            or len({row["id"] for row in indexed_records}) != len(indexed_records)):
        raise ValueError("original indexed baseline is inconsistent")
    _check_sqlite_integrity(state / "corpus.sqlite")
    # Preserve the failed database before init_db can apply any migration.
    with sqlite3.connect(state / "corpus.sqlite") as source, sqlite3.connect(database_snapshot) as target:
        source.backup(target)
    database_snapshot_sha256 = digest(database_snapshot)
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal, UserSession
    with Session(engine) as db:
        owner = db.get(UserSession, original["session_id"])
        files = db.query(File).filter_by(session_id=original["session_id"]).order_by(File.id).all()
        if db.query(Proposal).join(File).filter(File.session_id == original["session_id"]).count():
            raise ValueError("session already has proposals before continuation")
        actual_records = sorted(({"id": f.id, "path": Path(f.path).relative_to(data).as_posix()}
                                 for f in files), key=lambda row: row["id"])
        if (owner is None or owner.root_path != str(data) or owner.backend != "ollama"
                or owner.model != args.model or actual_records != indexed_records):
            raise ValueError("database session or indexed IDs differ from original")
        if any(f.status not in {FileStatus.ANALYZED, FileStatus.SKIPPED, FileStatus.ERROR}
               for f in files):
            raise ValueError("original analysis has unprocessed or unexpected statuses")
        provider_errors = [f for f in files if f.status == FileStatus.ERROR
                           and ((f.analysis_reason or "").startswith("provider_")
                                or (f.error_message or "").startswith("AI inference failed"))]
        if len(provider_errors) != sum(f.status == FileStatus.ERROR for f in files):
            raise ValueError("non-provider error requires separate review")
        skipped_ids = sorted(set(args.reviewed_skipped_ids))
        if len(skipped_ids) != len(args.reviewed_skipped_ids):
            raise ValueError("reviewed skipped IDs must be unique")
        by_id = {f.id: f for f in files}
        if any(fid not in by_id or by_id[fid].status != FileStatus.SKIPPED
               or by_id[fid].analysis_outcome == "sampled" for fid in skipped_ids):
            raise ValueError("reviewed IDs must be non-sampled skipped rows")
        selected_ids = sorted([f.id for f in provider_errors] + skipped_ids)
        if not selected_ids:
            raise ValueError("continuation has no reviewed rows to process")
        retained_hashes = {str(f.id): _file_evidence_hash(f)
                           for f in files if f.id not in selected_ids}
        prior_states = {str(fid): {"status": by_id[fid].status.value,
                                   "outcome": by_id[fid].analysis_outcome,
                                   "reason": by_id[fid].analysis_reason}
                        for fid in selected_ids}
        observed_digests = {f.analysis_model_digest for f in files if f.analysis_model_digest}
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    model_digest = local.model_digest(args.model)
    if (args.model not in local.list_models() or not model_digest
            or observed_digests != {model_digest}):
        raise ValueError("local model digest differs from original analyzed rows")
    from donedatahoarder.ai.provider import init_ai
    init_ai(backend="ollama", ollama_host=args.ollama_host,
            text_model=args.model, vision_model=args.model)
    require_local_provider(args.model)
    before = {"run_dir": str(root), "session_id": original["session_id"],
              "original_commit": args.original_commit,
              "recovery_commit": args.recovery_commit,
              "original_pipeline_sha256": digest(original_path),
              "original_source_report_sha256": digest(source_report_path),
              "original_source_match_counts": source_match_counts,
              "baseline_sha256": digest(root / "reports" / "baseline.json"),
              "indexed_records_sha256": digest(root / "reports" / "indexed-records.json"),
              "model": args.model, "model_digest": model_digest,
              "selected_ids": selected_ids, "reviewed_skipped_ids": skipped_ids,
              "prior_states": prior_states, "retained_evidence_hashes": retained_hashes,
              "indexed_count": len(indexed_records),
              "created_utc": datetime.now(timezone.utc).isoformat()}
    before["database_snapshot_sha256"] = database_snapshot_sha256
    write_json(snapshot_path, before)
    report = {key: before[key] for key in (
        "run_dir", "session_id", "original_commit", "recovery_commit",
        "original_pipeline_sha256", "indexed_records_sha256", "model", "model_digest",
        "original_source_report_sha256",
        "original_source_match_counts",
        "selected_ids", "reviewed_skipped_ids", "indexed_count")}
    report.update({"status": "running", "pre_retry_snapshot": str(snapshot_path),
                   "steps": {}, "started_utc": datetime.now(timezone.utc).isoformat()})
    write_json(report_path, report)
    try:
        with Session(engine) as db:
            for fid in skipped_ids:
                row = db.get(File, fid)
                row.status = FileStatus.ENRICHED
                row.analysis_outcome = None
                row.analysis_reason = None
            db.commit()
        from donedatahoarder.analyzers.pipeline import _eligible_for_analysis, analyze
        with Session(engine) as db:
            eligible_ids = sorted(row[0] for row in db.query(File.id).filter(
                File.session_id == original["session_id"],
                _eligible_for_analysis(True, include_sampled=True)).all())
        if eligible_ids != selected_ids:
            raise RuntimeError("analysis retry selection differs from reviewed IDs")
        result = run_step(report, report_path, "analyze_retry", lambda: analyze(
            workers=1, min_size_kb=0, session_id=original["session_id"],
            retry_errors=True, use_cache=True))
        with Session(engine) as db:
            retained = db.query(File).filter(
                File.session_id == original["session_id"],
                File.id.notin_(selected_ids)).all()
            after_hashes = {str(f.id): _file_evidence_hash(f) for f in retained}
            selected_after = {f.id: f for f in db.query(File).filter(
                File.session_id == original["session_id"],
                File.id.in_(selected_ids)).all()}
        report["retained_evidence_unchanged"] = after_hashes == retained_hashes
        report["selected_final"] = {str(fid): {
            "status": selected_after[fid].status.value,
            "outcome": selected_after[fid].analysis_outcome,
            "reason": selected_after[fid].analysis_reason,
            "evidence_source": selected_after[fid].analysis_evidence_source,
            "model_tag": selected_after[fid].analysis_model_tag,
            "model_digest": selected_after[fid].analysis_model_digest,
            "cache_hit": bool(selected_after[fid].analysis_cache_hit),
        } for fid in selected_ids if fid in selected_after}
        write_json(report_path, report)
        if not report["retained_evidence_unchanged"]:
            raise RuntimeError("previously processed file evidence changed during retry")
        if (set(selected_after) != set(selected_ids)
                or any(f.status != FileStatus.ANALYZED
                       or f.analysis_outcome not in {
                           "content_verified", "context_only", "metadata_only"}
                       or f.analysis_model_digest != model_digest
                       or (f.ai_description or "").lower().startswith("ai inference failed")
                       for f in selected_after.values())):
            raise RuntimeError("selected retry IDs did not finish with accepted analysis")
        report["coverage"] = run_step(report, report_path, "analysis_coverage", lambda:
            _final_coverage(engine, original["session_id"], data, set(indexed_paths),
                            "full", None, compare_run_counters=False))
        if result["errors"] or sum(result.values()) != len(selected_ids):
            raise RuntimeError("selected analysis retry failed or processed the wrong count")
        _check_sqlite_integrity(state / "corpus.sqlite")
        from donedatahoarder.core.dedup import find_semantic_duplicates, generate_dedup_proposals
        run_step(report, report_path, "dedup_semantic",
                 lambda: find_semantic_duplicates(session_id=original["session_id"]))
        run_step(report, report_path, "dedup_proposals",
                 lambda: generate_dedup_proposals(session_id=original["session_id"]))
        from donedatahoarder.core.relate import relate
        require_local_provider(args.model)
        run_ai_step(report, report_path, "relate",
                    lambda: relate(session_id=original["session_id"],
                                   scope="per_directory", model=args.model),
                    state / "logs" / "donedatahoarder.log")
        if report["steps"]["relate"].get("warning_count"):
            raise RuntimeError("relation stage logged warnings")
        from donedatahoarder.proposals.namer import generate_proposals
        run_step(report, report_path, "propose",
                 lambda: generate_proposals(session_id=original["session_id"]))
        from donedatahoarder.proposals.organizer import generate_reorg_proposals
        require_local_provider(args.model)
        run_ai_step(report, report_path, "organize",
                    lambda: generate_reorg_proposals(session_id=original["session_id"]),
                    state / "logs" / "donedatahoarder.log")
        if report["steps"]["organize"].get("warning_count"):
            raise RuntimeError("organization stage logged warnings")
        from donedatahoarder.executor import execute, _make_quiet_console
        dry_run = run_step(report, report_path, "dry_run", lambda: execute(
            dry_run=True, session_id=original["session_id"],
            _console=_make_quiet_console()))
        if dry_run.get("failed", 0):
            raise RuntimeError("continuation dry run reported failed proposals")
        report["coverage"] = run_step(report, report_path, "final_coverage", lambda:
            _final_coverage(engine, original["session_id"], data, set(indexed_paths),
                            "full", None, compare_run_counters=False))
        report["final"] = status_report(engine, original["session_id"])
        if report["final"]["provider_errors"]:
            raise RuntimeError("provider errors remain in final session")
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def audit(args) -> dict:
    root, data, state = combined_paths(args.run_dir)
    meta = read_json(root / "run.json")
    baseline = read_json(root / "reports" / "baseline.json")
    expected = sorted(({key: row[key] for key in ("path", "size", "sha256")}
                       for row in baseline), key=lambda row: row["path"])
    current = current_manifest(data)
    sources = {item["namespace"]: digest(Path(item["source"])) == item["source_sha256"]
               for item in meta["archives"]}
    if args.organized:
        content_match = Counter((r["size"], r["sha256"]) for r in current) == Counter(
            (r["size"], r["sha256"]) for r in expected)
        paths_match = None
        dirs_match = None
    else:
        content_match = current == expected
        paths_match = content_match
        dirs_match = directory_manifest(data) == read_json(root / "reports" / "directories.json")
    db_match = None
    foreign_keys = None
    pipeline_path = root / "reports" / "pipeline.json"
    if pipeline_path.exists():
        selected_report = (_pipeline_report(root, args.pipeline_report)[1]
                           if getattr(args, "pipeline_report", "pipeline.json") != "pipeline.json"
                           else read_json(pipeline_path))
        isolate(root, state)
        engine = db_session(state)
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from donedatahoarder.db.models import File
        session_id = selected_report["session_id"]
        with Session(engine) as db:
            rows = [(file_id, Path(path).relative_to(data).as_posix())
                    for file_id, path in db.query(File.id, File.path)
                    .filter(File.session_id == session_id).yield_per(500)]
            paths = {path for _, path in rows}
            baseline_records = read_json(root / "reports" / "indexed-records.json")
            baseline_ids = {row["id"] for row in baseline_records}
            db_match = (len(rows) == len(baseline_records) == len(paths)
                        and {file_id for file_id, _ in rows} == baseline_ids
                        and paths.issubset({r["path"] for r in current})
                        and (args.organized or paths == set(read_json(
                            root / "reports" / "indexed-baseline.json"))))
            foreign_keys = db.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    result = {"run_dir": str(root), "mode": "organized" if args.organized else "restored",
              "files": len(current), "bytes": sum(r["size"] for r in current),
              "content_match": content_match, "paths_match": paths_match,
              "directories_match": dirs_match, "db_paths_on_disk": db_match,
              "foreign_keys_clean": foreign_keys, "source_zip_unchanged": sources}
    result["pass"] = bool(content_match and all(sources.values()) and
                          db_match is not False and foreign_keys is not False and
                          (args.organized or dirs_match))
    path = root / "reports" / ("audit-organized.json" if args.organized else "audit-restored.json")
    write_json(path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["pass"]:
        raise RuntimeError("combined audit failed")
    return result


def execute_cycle(args) -> None:
    """Commit only listed human-reviewed operations, then audit and undo by default."""
    root, data, state = combined_paths(args.run_dir)
    report_path = root / "reports" / "execution.json"
    if report_path.exists():
        raise ValueError("execution already attempted on this copy")
    ids = sorted(set(args.proposal_ids))
    if not ids or any(value < 1 for value in ids):
        raise ValueError("provide reviewed positive proposal IDs")
    baseline = read_json(root / "reports" / "baseline.json")
    expected = sorted(({key: row[key] for key in ("path", "size", "sha256")}
                       for row in baseline), key=lambda row: row["path"])
    if current_manifest(data) != expected:
        raise ValueError("copy differs from extraction baseline before execution")
    meta = read_json(root / "run.json")
    if any(digest(Path(item["source"])) != item["source_sha256"]
           for item in meta["archives"]):
        raise ValueError("a source ZIP changed")
    selected_path, pipeline_report = _pipeline_report(
        root, getattr(args, "pipeline_report", "pipeline.json"))
    sid = pipeline_report["session_id"]
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType
    from donedatahoarder.executor import execute, _make_quiet_console
    from donedatahoarder.core.undo_log import undo_operations, get_undo_log_path

    journal = get_undo_log_path(sid)
    if not within(journal, state / "datahoarder") or journal.exists():
        raise ValueError("undo journal is unsafe or already exists")

    with Session(engine) as db:
        selected = db.query(Proposal, File).join(File).filter(Proposal.id.in_(ids)).all()
        if len(selected) != len(ids):
            raise ValueError("some proposal IDs do not exist")
        allowed = {ProposalType.RENAME, ProposalType.MOVE, ProposalType.MARK_DUPLICATE,
                   ProposalType.RENAME_FOLDER}
        for proposal, file in selected:
            if (file.session_id != sid or proposal.proposal_type not in allowed
                    or proposal.status not in (ProposalStatus.PENDING, ProposalStatus.APPROVED)
                    or not proposal.current_value or not within(Path(proposal.current_value), data)
                    or not proposal.proposed_value or not within(Path(proposal.proposed_value), data)):
                raise ValueError(f"proposal {proposal.id} is not safe for this copy")
        db_before = {f.id: f.path for f in db.query(File).filter(File.session_id == sid)}
        for proposal, _ in selected:
            proposal.status = ProposalStatus.APPROVED
            proposal.review_kind = "individual"
        db.commit()
    report = {"run_dir": str(root), "session_id": sid, "proposal_ids": ids,
              "pipeline_report": selected_path.name,
              "retain_organized": args.retain_organized}
    write_json(report_path, report)
    report["dry_run"] = execute(dry_run=True, min_confidence=1.1,
                                proposal_ids=ids, session_id=sid,
                                _console=_make_quiet_console())
    write_json(report_path, report)
    if report["dry_run"]["failed"] or report["dry_run"]["applied"] != len(ids):
        raise RuntimeError("dry run failed; no commit attempted")
    report["commit"] = execute(dry_run=False, min_confidence=1.1,
                               proposal_ids=ids, session_id=sid,
                               _console=_make_quiet_console())
    write_json(report_path, report)
    if report["commit"]["failed"] or report["commit"]["applied"] != len(ids):
        raise RuntimeError("commit failed; inspect journal and execution report")
    if not args.retain_organized:
        undone = undo_operations(session_id=sid, force=True, console=_make_quiet_console())
        report["undo"] = {key: value for key, value in undone.items() if key != "entries"}
        with Session(engine) as db:
            db_after = {f.id: f.path for f in db.query(File).filter(File.session_id == sid)}
        report["db_paths_restored"] = db_after == db_before
        report["files_restored"] = current_manifest(data) == expected
        report["directories_restored"] = directory_manifest(data) == read_json(
            root / "reports" / "directories.json")
    else:
        report["organized_manifest"] = current_manifest(data)
    report["source_zip_unchanged"] = {
        item["namespace"]: digest(Path(item["source"])) == item["source_sha256"]
        for item in meta["archives"]
    }
    report["audit_status"] = "pending"
    write_json(report_path, report)
    try:
        verified = audit(argparse.Namespace(run_dir=args.run_dir,
                                            pipeline_report=selected_path.name,
                                            organized=args.retain_organized))
    except Exception:
        report["audit_status"] = "failed"
        report["pass"] = False
        write_json(report_path, report)
        raise
    report["audit_status"] = "passed"
    report["audit"] = {key: verified[key] for key in (
        "content_match", "db_paths_on_disk", "foreign_keys_clean",
        "source_zip_unchanged", "directories_match", "pass",
    )}
    report["pass"] = (verified["pass"] and all(report["source_zip_unchanged"].values())
                      and (args.retain_organized or (report["undo"]["failed"] == 0
                       and report["db_paths_restored"] and report["files_restored"]
                       and report["directories_restored"])))
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["pass"]:
        raise RuntimeError("execution verification failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("prepare", help="extract six archives into one new isolated run")
    preparation.add_argument(
        "--corpus-zip", metavar="ZIP_BASENAME",
        help="select the additional top-level test ZIP when discovery is ambiguous",
    )
    run = sub.add_parser("pipeline", help="run one session across all six namespaces")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--model", default="gemma4:26b")
    run.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--mode", choices=("full", "representative", "metadata_only"), default="full")
    run.add_argument("--sequence-sample-stride", type=int, default=10)
    run.add_argument("--cache", action=argparse.BooleanOptionalAction, default=True)
    continuation = sub.add_parser(
        "continue-analysis", help="one guarded continuation of a failed full analysis")
    continuation.add_argument("--run-dir", required=True)
    continuation.add_argument("--original-commit", required=True)
    continuation.add_argument("--recovery-commit", required=True)
    continuation.add_argument("--original-source-report",
                              default="astra-source-fingerprint-start.json")
    continuation.add_argument("--reviewed-skipped-ids", type=int, nargs="*", default=[])
    continuation.add_argument("--model", default="gemma4:26b")
    continuation.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    check = sub.add_parser("audit", help="check source ZIPs, files, paths and database")
    check.add_argument("--run-dir", required=True)
    check.add_argument("--pipeline-report", default="pipeline.json",
                       help="completed report basename (pipeline.json or continuation-1.json)")
    check.add_argument("--organized", action="store_true", help="compare content multiset after final organization")
    execution = sub.add_parser("execute", help="dry-run, commit and normally undo reviewed IDs")
    execution.add_argument("--run-dir", required=True)
    execution.add_argument("--pipeline-report", default="pipeline.json",
                           help="completed report basename (pipeline.json or continuation-1.json)")
    execution.add_argument("--proposal-ids", type=int, nargs="+", required=True)
    execution.add_argument("--retain-organized", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "pipeline":
        pipeline(args)
    elif args.command == "continue-analysis":
        continue_analysis(args)
    elif args.command == "execute":
        execute_cycle(args)
    else:
        audit(args)


if __name__ == "__main__":
    main()

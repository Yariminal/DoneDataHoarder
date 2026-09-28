"""One isolated six-ZIP validation collection under D:\\Test.

Commands are explicit. `prepare` safely extracts into archive namespaces;
`pipeline` uses one database session; `audit` checks files, ZIPs and DB.
No command deletes an existing run or source archive.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
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
                    mode: str, analysis_result: dict | None) -> dict:
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
    if mode != "metadata_only":
        expected = {"fresh": "analyzed", "cached": "cached", "sampled": "sampled",
                    "skipped": "skipped", "errors": "errors"}
        if analysis_result is None or any(
            counts[name] != analysis_result.get(source, 0)
            for name, source in expected.items()
        ):
            raise RuntimeError("persisted analysis coverage differs from run counters")
    return counts


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
        isolate(root, state)
        engine = db_session(state)
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from donedatahoarder.db.models import File
        session_id = read_json(pipeline_path)["session_id"]
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
    pipeline_report = read_json(root / "reports" / "pipeline.json")
    if "final" not in pipeline_report:
        raise ValueError("pipeline incomplete")
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
    check = sub.add_parser("audit", help="check source ZIPs, files, paths and database")
    check.add_argument("--run-dir", required=True)
    check.add_argument("--organized", action="store_true", help="compare content multiset after final organization")
    execution = sub.add_parser("execute", help="dry-run, commit and normally undo reviewed IDs")
    execution.add_argument("--run-dir", required=True)
    execution.add_argument("--proposal-ids", type=int, nargs="+", required=True)
    execution.add_argument("--retain-organized", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "pipeline":
        pipeline(args)
    elif args.command == "execute":
        execute_cycle(args)
    else:
        audit(args)


if __name__ == "__main__":
    main()

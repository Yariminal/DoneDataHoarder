"""Repeatable, isolated validation against disposable copies of D:\\Test ZIPs.

Inventory is read-only. Prepare, pipeline, and execute are separate explicit
phases; no phase removes a source archive or an existing validation run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
import threading
from urllib.parse import urlparse
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


CORPUS_DIR = Path("D:/Test")
RUN_PREFIX = "DDH-validation-"
MAX_ENTRIES = 10_000
MAX_UNCOMPRESSED = 4 * 1024**3
MAX_ENTRY = 2 * 1024**3
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)
CHUNK = 1024 * 1024


def write_json(path: Path, value: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def run_paths(value: str) -> tuple[Path, Path, Path]:
    root = Path(value).resolve(strict=True)
    corpus_root = CORPUS_DIR.resolve(strict=True)
    if root.parent != corpus_root or not root.name.startswith(RUN_PREFIX):
        raise ValueError("run-dir must be a direct DDH-validation-* child of D:\\Test")
    meta = read_json(root / "run.json")
    if meta.get("schema") != 1 or meta.get("run_dir") != str(root):
        raise ValueError("invalid run.json")
    data = root / "data"
    state = root / "state"
    if (not data.is_dir() or not state.is_dir() or data.is_symlink() or state.is_symlink()
            or not within(data, root) or not within(state, root)):
        raise ValueError("run data/state directory missing or redirected")
    return root, data, state


def archive_path(name: str) -> Path:
    if (not name or name in (".", "..") or Path(name).name != name
            or any(char in name for char in ("/", "\\", ":"))
            or name.rstrip(" .") != name or not name.lower().endswith(".zip")):
        raise ValueError("corpus must be a plain ZIP basename within D:\\Test")
    path = CORPUS_DIR / name
    if not path.is_file() or path.is_symlink() or path.parent.resolve() != CORPUS_DIR.resolve():
        raise ValueError("source ZIP missing or redirected")
    return path


def safe_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise ValueError("archive exceeds entry or uncompressed-size cap")
    seen: set[str] = set()
    files: set[str] = set()
    result: list[tuple[zipfile.ZipInfo, Path]] = []
    for info in infos:
        raw = info.filename.replace("\\", "/")
        parts = PurePosixPath(raw).parts
        if (not raw or raw.startswith("/") or raw.startswith("//") or not parts
                or any(p in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', p)
                       or p.rstrip(" .") != p
                       or WINDOWS_RESERVED.match(p) for p in parts)):
            raise ValueError(f"unsafe ZIP path: {info.filename!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, 0o040000, 0o100000):
            raise ValueError(f"non-file ZIP entry: {info.filename!r}")
        if info.flag_bits & 1 or info.file_size > MAX_ENTRY:
            raise ValueError(f"encrypted or oversized entry: {info.filename!r}")
        rel = Path(*parts)
        key = str(rel).casefold()
        if key in seen:
            raise ValueError(f"case-insensitive duplicate ZIP entry: {info.filename!r}")
        if any(str(Path(*parts[:n])).casefold() in files for n in range(1, len(parts))):
            raise ValueError(f"file used as parent in ZIP: {info.filename!r}")
        seen.add(key)
        if not info.is_dir():
            files.add(key)
        result.append((info, rel))
    for _, rel in result:
        if any(str(other).casefold().startswith(str(rel).casefold() + os.sep)
               for other in files if str(rel).casefold() in files):
            raise ValueError(f"file has child entry: {rel}")
    return result


def inventory(args: argparse.Namespace) -> None:
    output = []
    for name in sorted(p.name for p in CORPUS_DIR.iterdir()
                       if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".zip"):
        source = archive_path(name)
        with zipfile.ZipFile(source) as zf:
            infos = zf.infolist()
            issue = None
            try:
                safe_members(zf)
            except ValueError as exc:
                issue = str(exc)
            output.append({
                "archive": name, "compressed_bytes": source.stat().st_size,
                "entries": len(infos), "files": sum(not x.is_dir() for x in infos),
                "uncompressed_bytes": sum(x.file_size for x in infos),
                "max_entry_bytes": max((x.file_size for x in infos), default=0),
                "preflight_error": issue,
            })
    print(json.dumps(output, ensure_ascii=True, indent=2))


def prepare(args: argparse.Namespace) -> None:
    source = archive_path(args.corpus)
    source_hash_before = digest(source)
    with zipfile.ZipFile(source) as zf:
        members = safe_members(zf)  # preflight before creating a run
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = CORPUS_DIR / f"{RUN_PREFIX}{stamp}-{uuid.uuid4().hex[:8]}"
        root.mkdir(exist_ok=False)
        data = root / "data"
        data.mkdir()
        (root / "state").mkdir()
        (root / "reports").mkdir()
        files = []
        total = 0
        for info, rel in members:
            target = data / rel
            if not within(target, data):
                raise ValueError(f"ZIP path escaped data directory: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            size = 0
            with zf.open(info) as src, target.open("xb") as dst:
                for block in iter(lambda: src.read(CHUNK), b""):
                    size += len(block)
                    total += len(block)
                    if size > info.file_size or total > MAX_UNCOMPRESSED:
                        raise ValueError(f"ZIP entry exceeded declared size: {info.filename!r}")
                    dst.write(block)
                    h.update(block)
            if size != info.file_size:
                raise ValueError(f"ZIP entry size mismatch: {info.filename!r}")
            modified = datetime(*info.date_time).timestamp()
            os.utime(target, (modified, modified))
            files.append({"path": rel.as_posix(), "size": size, "sha256": h.hexdigest(),
                          "zip_modified": info.date_time})
    source_hash_after = digest(source)
    if source_hash_after != source_hash_before:
        raise RuntimeError("source ZIP changed during extraction")
    metadata = {
        "schema": 1, "run_dir": str(root.resolve()), "corpus": source.name,
        "source_archive": str(source.resolve()), "source_size": source.stat().st_size,
        "source_sha256": source_hash_before,
        "source_sha256_after": source_hash_after,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_files": len(files), "baseline_bytes": total,
    }
    write_json(root / "run.json", metadata)
    write_json(root / "reports" / "baseline.json", files)
    print(json.dumps(metadata, ensure_ascii=True, indent=2))


def isolate(root: Path, state: Path) -> None:
    user = state / "home"
    roaming = state / "roaming"
    app_data = state / "datahoarder"
    for path in (user, roaming, app_data, state / "logs"):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["USERPROFILE"] = str(user)
    os.environ["APPDATA"] = str(roaming)
    os.environ["DDH_DATA_DIR"] = str(app_data)
    os.environ["DDH_DB"] = str(state / "corpus.sqlite")
    os.environ["DDH_BACKEND"] = "ollama"
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(state / "cache" / "huggingface")
    if not within(Path.home(), state):
        raise RuntimeError("config home isolation failed")
    from donedatahoarder.core.undo_log import get_undo_log_path
    if not within(get_undo_log_path(), app_data):
        raise RuntimeError("undo journal isolation failed")
    from donedatahoarder.logging import setup_logging
    setup_logging(log_file=state / "logs" / "donedatahoarder.log")


def db_session(state: Path):
    from donedatahoarder.db.session import init_db
    return init_db(state / "corpus.sqlite")


def require_local_provider(model: str) -> None:
    """Fail before an AI stage if context was lost or a cloud client was selected."""
    from donedatahoarder.ai.ollama_client import OllamaClient
    from donedatahoarder.ai.provider import get_client

    client = get_client(failover=False)
    if not isinstance(client, OllamaClient) or client.text_model != model or client.vision_model != model:
        raise RuntimeError("validation requires the configured local Ollama model")


def status_report(engine, session_id: str) -> dict:
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal
    with Session(engine) as db:
        files = db.query(File).filter_by(session_id=session_id).order_by(File.id).all()
        proposals = db.query(Proposal).join(File).filter(File.session_id == session_id).all()
        statuses = Counter(f.status.value for f in files)
        error_items = [
            {"path": f.path, "status": f.status.value, "error": f.error_message,
             "description": f.ai_description, "confidence": f.ai_confidence}
            for f in files if f.status.value == "error"
            or (f.ai_description or "").lower().startswith("ai inference failed")
        ]
        return {
            "files": len(files), "status_counts": dict(statuses),
            "analysis_outcomes": dict(Counter(f.analysis_outcome or "unrecorded" for f in files)),
            "analysis_reasons": dict(Counter(f.analysis_reason for f in files if f.analysis_reason)),
            "evidence_sources": dict(Counter(f.analysis_evidence_source or "unrecorded" for f in files)),
            "model_tags": dict(Counter(f.analysis_model_tag for f in files if f.analysis_model_tag)),
            "model_digests": sorted({f.analysis_model_digest for f in files if f.analysis_model_digest}),
            "proposal_types": dict(Counter(p.proposal_type.value for p in proposals)),
            "proposal_statuses": dict(Counter(p.status.value for p in proposals)),
            "provider_errors": error_items,
        }


def _process_rss_bytes() -> int | None:
    """Current process working set; native allocations are included on Windows."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ok = psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                       ctypes.byref(counters), counters.cb)
        return int(counters.WorkingSetSize) if ok else None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * page_size
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def run_step(report: dict, path: Path, name: str, fn):
    start = time.perf_counter()
    stop = threading.Event()
    samples = [_process_rss_bytes()]

    def sample_memory():
        while not stop.wait(0.1):
            samples.append(_process_rss_bytes())

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    try:
        result = fn()
        report["steps"][name] = {"seconds": round(time.perf_counter() - start, 3), "result": result}
        write_json(path, report)
        return result
    except Exception as exc:
        report["steps"][name] = {"seconds": round(time.perf_counter() - start, 3),
                                  "error": f"{type(exc).__name__}: {exc}"}
        write_json(path, report)
        raise
    finally:
        stop.set()
        sampler.join(timeout=1)
        samples.append(_process_rss_bytes())
        measured = [value for value in samples if value is not None]
        report["steps"][name]["peak_process_rss_bytes"] = max(measured) if measured else None
        report["steps"][name]["rss_samples"] = len(measured)
        write_json(path, report)


def stage_warnings(log_path: Path, offset: int) -> list[str]:
    """Return warning/error log lines emitted during one pipeline stage."""
    if not log_path.exists():
        return []
    with log_path.open("rb") as stream:
        stream.seek(offset)
        tail = stream.read().decode("utf-8", errors="replace")
    return [line for line in tail.splitlines()
            if "| WARNING" in line or "| ERROR" in line]


def run_ai_step(report: dict, path: Path, name: str, fn, log_path: Path) -> None:
    """Record swallowed LLM failures as stage failures for corpus validation."""
    offset = log_path.stat().st_size if log_path.exists() else 0
    run_step(report, path, name, fn)
    warnings = stage_warnings(log_path, offset)
    report["steps"][name]["warnings"] = warnings
    report["steps"][name]["warning_count"] = len(warnings)
    write_json(path, report)
    failure_prefixes = ("Relate LLM call failed", "Relate LLM for",
                        "Relate get_client failed", "Relate cross-script LLM call failed",
                        "Organizer LLM call failed")
    if any(any(prefix in line for prefix in failure_prefixes) for line in warnings):
        report["steps"][name]["error"] = "LLM fallback/failure logged; inspect warnings"
        write_json(path, report)
        raise RuntimeError(f"{name} logged LLM fallback/failure")


def completed_report(root: Path) -> tuple[dict, dict | None]:
    """Resolve an original successful pipeline or an explicit downstream retry."""
    original = read_json(root / "reports" / "pipeline.json")
    downstream_paths = sorted((root / "reports").glob("retry-downstream-[0-9]*.json"),
                              key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    analysis_paths = sorted((root / "reports").glob("retry-analysis-[0-9]*.json"),
                            key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    if downstream_paths and analysis_paths:
        raise ValueError("mixed retry types for one run")
    retry_paths = downstream_paths or analysis_paths
    retry_path = retry_paths[-1] if retry_paths else root / "reports" / "retry-downstream.json"
    retry = read_json(retry_path) if retry_path.exists() else None
    if retry is not None:
        if retry.get("session_id") != original.get("session_id") or retry.get("status") != "complete":
            raise ValueError("downstream retry is incomplete or mismatched")
    elif "final" not in original:
        raise ValueError("pipeline is incomplete; review/execute cannot proceed")
    return original, retry


def pipeline(args: argparse.Namespace) -> None:
    root, data, state = run_paths(args.run_dir)
    report_path = root / "reports" / "pipeline.json"
    if report_path.exists():
        raise ValueError("pipeline already started for this run; prepare a fresh copy")
    if args.workers < 1 or args.workers > 4:
        raise ValueError("workers must be 1..4")
    if not args.full_ai and args.ai_limit < 1:
        raise ValueError("ai-limit must be positive")
    endpoint = urlparse(args.ollama_host)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama host must be local HTTP loopback")
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, SessionStatus, UserSession
    session_id = str(uuid.uuid4())
    with Session(engine) as db:
        db.add(UserSession(id=session_id, name=f"validation-{root.name}",
                           root_path=str(data), backend="ollama", model=args.model,
                           workers=args.workers, status=SessionStatus.ACTIVE))
        db.commit()
    report = {
        "run_dir": str(root), "corpus": read_json(root / "run.json")["corpus"],
        "session_id": session_id, "backend": "ollama", "model": args.model,
        "workers": args.workers, "ai_scope": "full" if args.full_ai else "bounded_smoke",
        "requested_ai_limit": None if args.full_ai else args.ai_limit,
        "optional_media": {
            "faster_whisper_installed": importlib.util.find_spec("faster_whisper") is not None,
            "ffmpeg_python_installed": importlib.util.find_spec("ffmpeg") is not None,
            "ffmpeg_binary": shutil.which("ffmpeg"),
            "ffprobe_binary": shutil.which("ffprobe"),
            "huggingface_offline": True,
        },
        "steps": {},
    }
    write_json(report_path, report)
    from donedatahoarder.core.scanner import scan
    from donedatahoarder.core.enricher import enrich
    from donedatahoarder.core.dedup import (
        find_exact_duplicates, find_perceptual_duplicates,
        find_text_near_duplicates, find_semantic_duplicates, generate_dedup_proposals,
    )
    run_step(report, report_path, "scan", lambda: scan(data, session_id=session_id, workers=args.workers))
    run_step(report, report_path, "enrich", lambda: enrich(session_id=session_id, workers=args.workers))
    run_step(report, report_path, "dedup_exact", lambda: find_exact_duplicates(session_id=session_id))
    run_step(report, report_path, "dedup_perceptual", lambda: find_perceptual_duplicates(session_id=session_id))
    run_step(report, report_path, "dedup_text", lambda: find_text_near_duplicates(session_id=session_id))
    with Session(engine) as db:
        eligible = {f.id: f.path for f in db.query(File).filter_by(session_id=session_id, status=FileStatus.ENRICHED)}
    report["ai_eligible"] = len(eligible)
    write_json(report_path, report)
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    if args.model not in local.list_models():
        raise RuntimeError(f"local Ollama model {args.model!r} is unavailable at {args.ollama_host}")
    from donedatahoarder.ai.provider import init_ai
    init_ai(backend="ollama", ollama_host=args.ollama_host,
            text_model=args.model, vision_model=args.model)
    require_local_provider(args.model)
    from donedatahoarder.analyzers.pipeline import analyze
    ai_workers = args.workers  # analyzer now bounds submissions before the limit
    run_step(report, report_path, "analyze", lambda: analyze(
        workers=ai_workers, limit=None if args.full_ai else args.ai_limit,
        min_size_kb=0, session_id=session_id))
    with Session(engine) as db:
        after = {f.id: f for f in db.query(File).filter(File.id.in_(eligible))}
        attempted = [f for fid, f in after.items() if f.status != FileStatus.ENRICHED]
        provider_failures = [f for f in attempted if f.status == FileStatus.ERROR
                             or (f.ai_description or "").lower().startswith("ai inference failed")]
        unverified = [
            f for f in attempted if f.status == FileStatus.ANALYZED and
            (f.analysis_outcome != "content_verified" or
             (f.ai_description or "").startswith("[UNVERIFIED"))
        ]
        zero_confidence = [f for f in attempted if f.ai_confidence == 0]
        report["ai_coverage"] = {
            "eligible": len(eligible), "attempted": len(attempted),
            "succeeded": sum(f.status == FileStatus.ANALYZED and f not in provider_failures
                             for f in attempted),
            "failed": len(provider_failures),
            "skipped": sum(f.status == FileStatus.SKIPPED for f in attempted),
            "unprocessed": sum(f.status == FileStatus.ENRICHED for f in after.values()),
            "unverified_content": len(unverified),
            "zero_confidence": len(zero_confidence),
            "attempted_paths": sorted(f.path for f in attempted),
            "failed_paths": sorted(f.path for f in provider_failures),
            "unverified_paths": sorted(f.path for f in unverified),
            "zero_confidence_paths": sorted(f.path for f in zero_confidence),
            "selection": "application ENRICHED query order; actual paths recorded",
            "outcomes": dict(Counter(f.analysis_outcome or "unrecorded" for f in attempted)),
            "reasons": dict(Counter(f.analysis_reason for f in attempted if f.analysis_reason)),
            "evidence_sources": dict(Counter(f.analysis_evidence_source or "unrecorded" for f in attempted)),
            "model_tags": dict(Counter(f.analysis_model_tag for f in attempted if f.analysis_model_tag)),
            "model_digests": sorted({f.analysis_model_digest for f in attempted if f.analysis_model_digest}),
        }
        evidence = [{
            "file_id": f.id, "path": Path(f.path).relative_to(data).as_posix(),
            "status": f.status.value, "outcome": f.analysis_outcome,
            "reason": f.analysis_reason, "evidence_source": f.analysis_evidence_source,
            "content_chars": f.analysis_content_chars,
            "model_tag": f.analysis_model_tag, "model_digest": f.analysis_model_digest,
            "prompt_version": f.analysis_prompt_version,
            "extractor_version": f.analysis_extractor_version,
            "model_self_reported_confidence": f.ai_confidence,
        } for f in after.values()]
        write_json(root / "reports" / "analysis-evidence.json", evidence)
    write_json(report_path, report)
    if report["ai_coverage"]["failed"]:
        report["steps"]["analyze"]["error"] = (
            f"{report['ai_coverage']['failed']} local AI provider failure(s) persisted"
        )
        write_json(report_path, report)
        raise RuntimeError("AI analysis persisted provider failures; downstream stages blocked")
    run_step(report, report_path, "dedup_semantic", lambda: find_semantic_duplicates(session_id=session_id))
    run_step(report, report_path, "dedup_proposals", lambda: generate_dedup_proposals(session_id=session_id))
    if args.full_ai or args.relate:
        require_local_provider(args.model)
        from donedatahoarder.core.relate import relate
        run_ai_step(report, report_path, "relate", lambda: relate(session_id=session_id,
                    scope="per_directory", model=args.model),
                    state / "logs" / "donedatahoarder.log")
    else:
        report["steps"]["relate"] = {"skipped": "bounded smoke; pass --relate to include"}
    from donedatahoarder.proposals.namer import generate_proposals
    run_step(report, report_path, "propose", lambda: generate_proposals(session_id=session_id))
    if args.full_ai or args.organize:
        require_local_provider(args.model)
        from donedatahoarder.proposals.organizer import generate_reorg_proposals
        run_ai_step(report, report_path, "organize",
                    lambda: generate_reorg_proposals(session_id=session_id),
                    state / "logs" / "donedatahoarder.log")
    else:
        report["steps"]["organize"] = {"skipped": "bounded smoke; pass --organize to include"}
    report["final"] = status_report(engine, session_id)
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


def retry_downstream(args: argparse.Namespace) -> None:
    """Resume only stages after a preserved, interrupted relate attempt."""
    root, data, state = run_paths(args.run_dir)
    original = read_json(root / "reports" / "pipeline.json")
    incident_path = root / "reports" / "incident.json"
    retry_paths = sorted((root / "reports").glob("retry-downstream-[0-9]*.json"),
                         key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    if not incident_path.exists():
        raise ValueError("no recorded failed-relate incident")
    for previous_path in retry_paths:
        previous = read_json(previous_path)
        if previous.get("status") != "failed" or "propose" in previous.get("steps", {}):
            raise ValueError("previous retry did not fail in relation stage")
    attempt = len(retry_paths) + 1
    retry_path = root / "reports" / f"retry-downstream-{attempt}.json"
    if retry_path.exists():
        raise ValueError("retry report already exists")
    incident = read_json(incident_path)
    steps = original.get("steps", {})
    if (incident.get("failed_stage") != "relate" or incident.get("session_id") != original.get("session_id")
            or "final" in original or "relate" in steps or "propose" in steps
            or not all(k in steps and "error" not in steps[k]
                       for k in ("analyze", "dedup_semantic", "dedup_proposals"))):
        raise ValueError("original pipeline is not a verified failed-relate attempt")
    if args.model != original.get("model") or not within(data, root):
        raise ValueError("retry model or data path differs from original")
    if args.ollama_host not in ("http://localhost:11434", "http://127.0.0.1:11434"):
        raise ValueError("retry Ollama host must be local loopback")
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, Proposal, ProposalType
    with Session(engine) as db:
        if db.query(File).filter_by(session_id=original["session_id"]).count() != original["ai_eligible"]:
            raise ValueError("original session file count changed")
        existing = db.query(Proposal.proposal_type).join(File).filter(
            File.session_id == original["session_id"]).all()
        if any(kind != ProposalType.MARK_DUPLICATE for (kind,) in existing):
            raise ValueError("non-duplicate proposals already exist; downstream retry unsafe")
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    if args.model not in local.list_models():
        raise RuntimeError(f"local Ollama model {args.model!r} is unavailable")
    from donedatahoarder.ai.provider import init_ai
    init_ai(backend="ollama", ollama_host=args.ollama_host,
            text_model=args.model, vision_model=args.model)
    require_local_provider(args.model)
    report = {"run_dir": str(root), "session_id": original["session_id"],
              "status": "running", "model": args.model,
              "attempt": attempt, "report_path": str(retry_path),
              "original_pipeline_report": str(root / "reports" / "pipeline.json"),
              "original_incident_report": str(incident_path),
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "existing_duplicate_proposals": len(existing), "steps": {}}
    write_json(retry_path, report)
    try:
        from donedatahoarder.core.relate import relate
        run_ai_step(report, retry_path, "relate",
                    lambda: relate(session_id=original["session_id"],
                                   scope="per_directory", model=args.model),
                    state / "logs" / "donedatahoarder.log")
        from donedatahoarder.proposals.namer import generate_proposals
        run_step(report, retry_path, "propose",
                 lambda: generate_proposals(session_id=original["session_id"]))
        if args.organize:
            require_local_provider(args.model)
            from donedatahoarder.proposals.organizer import generate_reorg_proposals
            run_ai_step(report, retry_path, "organize",
                        lambda: generate_reorg_proposals(session_id=original["session_id"]),
                        state / "logs" / "donedatahoarder.log")
        else:
            report["steps"]["organize"] = {"skipped": "not requested for retry"}
        report["final"] = status_report(engine, original["session_id"])
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(retry_path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


def retry_analysis(args: argparse.Namespace) -> None:
    """Continue one interrupted full-AI run, retrying only its saved AI error."""
    root, data, state = run_paths(args.run_dir)
    original = read_json(root / "reports" / "pipeline.json")
    incident = read_json(root / "reports" / "incident.json")
    snapshot = read_json(root / "reports" / "interrupted-analysis-snapshot.json")
    prior = sorted((root / "reports").glob("retry-analysis-[0-9]*.json"),
                   key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    if prior or incident.get("failed_stage") != "analyze" or incident.get("session_id") != original.get("session_id"):
        raise ValueError("analysis retry requires one recorded interrupted analysis and no prior retry")
    if (original.get("ai_scope") != "full" or args.model != original.get("model")
            or "analyze" in original.get("steps", {}) or "final" in original):
        raise ValueError("original pipeline is not an interrupted full-AI analysis")
    if not all(k in original.get("steps", {}) for k in
               ("scan", "enrich", "dedup_exact", "dedup_perceptual", "dedup_text")):
        raise ValueError("original pre-analysis stages are incomplete")
    if snapshot.get("initial_ai_eligible") != original.get("ai_eligible"):
        raise ValueError("snapshot eligible count differs from original")
    baseline = read_json(root / "reports" / "baseline.json")
    expected = sorted(({k: row[k] for k in ("path", "size", "sha256")} for row in baseline),
                      key=lambda row: row["path"])
    if current_manifest(data) != expected:
        raise ValueError("copy differs from extraction baseline before analysis retry")
    endpoint = urlparse(args.ollama_host)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("retry Ollama host must be local HTTP loopback")
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus
    with Session(engine) as db:
        files = db.query(File).filter_by(session_id=original["session_id"]).order_by(File.id).all()
        statuses = Counter(f.status.name for f in files)
        legacy = [f for f in files if f.status == FileStatus.ANALYZED
                  and (f.ai_description or "").lower().startswith("ai inference failed")]
        if (len(files) != original["ai_eligible"] or statuses != snapshot["status_counts"]
                or [f.id for f in legacy] != snapshot["legacy_inference_failure_ids"]
                or len(legacy) != 1 or any(f.status == FileStatus.ERROR for f in files)):
            raise ValueError("isolated DB changed since interrupted-analysis snapshot")
        retry_ids = [f.id for f in files if f.status == FileStatus.ENRICHED] + [f.id for f in legacy]
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    if args.model not in local.list_models():
        raise RuntimeError(f"local Ollama model {args.model!r} is unavailable")
    from donedatahoarder.ai.provider import init_ai
    init_ai(backend="ollama", ollama_host=args.ollama_host,
            text_model=args.model, vision_model=args.model)
    require_local_provider(args.model)
    path = root / "reports" / "retry-analysis-1.json"
    report = {"run_dir": str(root), "session_id": original["session_id"],
              "status": "running", "attempt": 1, "report_path": str(path),
              "model": args.model, "workers": 1,
              "original_pipeline_report": str(root / "reports" / "pipeline.json"),
              "original_incident_report": str(root / "reports" / "incident.json"),
              "pre_retry_snapshot": str(root / "reports" / "interrupted-analysis-snapshot.json"),
              "retained_success_ids": sorted(set(snapshot["analyzed_ids"]) - set(snapshot["legacy_inference_failure_ids"])),
              "migrated_legacy_ids": [f.id for f in legacy],
              "retry_selected_ids": sorted(retry_ids),
              "started_utc": datetime.now(timezone.utc).isoformat(), "steps": {}}
    write_json(path, report)
    try:
        with Session(engine) as db:
            for fid in report["migrated_legacy_ids"]:
                file = db.get(File, fid)
                file.status = FileStatus.ERROR
                file.error_message = file.ai_description
            db.commit()
        from donedatahoarder.analyzers.pipeline import analyze
        run_step(report, path, "analyze_retry",
                 lambda: analyze(workers=1, limit=None, min_size_kb=0,
                                 session_id=original["session_id"], retry_errors=True))
        with Session(engine) as db:
            after = db.query(File).filter_by(session_id=original["session_id"]).order_by(File.id).all()
            provider_failures = [f for f in after if f.status == FileStatus.ERROR
                                 or (f.ai_description or "").lower().startswith("ai inference failed")]
            report["ai_coverage"] = {
                "eligible": len(after), "attempted_unique": sum(f.status != FileStatus.ENRICHED for f in after),
                "attempt_events": len(snapshot["analyzed_ids"]) + len(retry_ids),
                "retained_prior_successes": len(report["retained_success_ids"]),
                "retried_legacy_failures": len(report["migrated_legacy_ids"]),
                "succeeded": sum(f.status == FileStatus.ANALYZED for f in after),
                "failed": len(provider_failures),
                "skipped": sum(f.status == FileStatus.SKIPPED for f in after),
                "unprocessed": sum(f.status == FileStatus.ENRICHED for f in after),
                "failed_paths": sorted(f.path for f in provider_failures),
                "unverified_content": sum(
                    f.status == FileStatus.ANALYZED and
                    (f.analysis_outcome != "content_verified" or
                     (f.ai_description or "").startswith("[UNVERIFIED"))
                    for f in after
                ),
                "zero_confidence": sum(f.ai_confidence == 0 for f in after),
            }
        write_json(path, report)
        coverage = report["ai_coverage"]
        if (coverage["eligible"] != original["ai_eligible"] or coverage["failed"]
                or coverage["unprocessed"] or coverage["attempted_unique"] != coverage["eligible"]
                or coverage["succeeded"] + coverage["skipped"] != coverage["eligible"]):
            raise RuntimeError("analysis retry did not cover every initially eligible file successfully")
        from donedatahoarder.core.dedup import find_semantic_duplicates, generate_dedup_proposals
        run_step(report, path, "dedup_semantic", lambda: find_semantic_duplicates(session_id=original["session_id"]))
        run_step(report, path, "dedup_proposals", lambda: generate_dedup_proposals(session_id=original["session_id"]))
        require_local_provider(args.model)
        from donedatahoarder.core.relate import relate
        run_ai_step(report, path, "relate",
                    lambda: relate(session_id=original["session_id"], scope="per_directory", model=args.model),
                    state / "logs" / "donedatahoarder.log")
        from donedatahoarder.proposals.namer import generate_proposals
        run_step(report, path, "propose", lambda: generate_proposals(session_id=original["session_id"]))
        require_local_provider(args.model)
        from donedatahoarder.proposals.organizer import generate_reorg_proposals
        run_ai_step(report, path, "organize",
                    lambda: generate_reorg_proposals(session_id=original["session_id"]),
                    state / "logs" / "donedatahoarder.log")
        report["final"] = status_report(engine, original["session_id"])
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


def refresh_proposals(args: argparse.Namespace) -> None:
    """Regenerate downstream proposals after a fix, retaining AI evidence."""
    root, data, state = run_paths(args.run_dir)
    original = read_json(root / "reports" / "pipeline.json")
    path = root / "reports" / "proposal-refresh-1.json"
    if path.exists() or not (root / "reports" / "initial-proposals.sqlite").exists():
        raise ValueError("refresh requires preserved initial snapshot and no prior refresh")
    if "final" not in original or original.get("ai_scope") != "full":
        raise ValueError("initial full pipeline is incomplete")
    meta = read_json(root / "run.json")
    baseline = read_json(root / "reports" / "baseline.json")
    expected = sorted(
        ({key: row[key] for key in ("path", "size", "sha256")} for row in baseline),
        key=lambda row: row["path"],
    )
    if current_manifest(data) != expected or digest(archive_path(meta["corpus"])) != meta["source_sha256"]:
        raise ValueError("copy or source ZIP changed before proposal refresh")
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType
    sid = original["session_id"]
    with Session(engine) as db:
        files = db.query(File).filter_by(session_id=sid).all()
        proposals = db.query(Proposal).join(File).filter(File.session_id == sid).all()
        if (len(files) != original["ai_eligible"]
                or any(f.status not in (FileStatus.PROPOSED, FileStatus.ANALYZED,
                                        FileStatus.SKIPPED) for f in files)
                or any(p.status != ProposalStatus.PENDING for p in proposals)):
            raise ValueError("isolated DB has reviewed/applied or incomplete state")
        before = status_report(engine, sid)
        removed = [p for p in proposals if p.proposal_type != ProposalType.MARK_DUPLICATE]
        removed_ids = [p.id for p in removed]
        for proposal in removed:
            db.delete(proposal)
        reset_ids = []
        for file in files:
            if file.status == FileStatus.PROPOSED and file.analysis_outcome != "skipped":
                file.status = FileStatus.ANALYZED
                reset_ids.append(file.id)
        db.commit()
    from donedatahoarder.ai.ollama_client import OllamaClient
    local = OllamaClient(host=args.ollama_host, text_model=args.model, vision_model=args.model)
    if args.model not in local.list_models():
        raise RuntimeError(f"local Ollama model {args.model!r} unavailable")
    from donedatahoarder.ai.provider import init_ai
    init_ai(backend="ollama", ollama_host=args.ollama_host,
            text_model=args.model, vision_model=args.model)
    require_local_provider(args.model)
    report = {
        "run_dir": str(root), "session_id": sid, "status": "running",
        "original_pipeline_report": str(root / "reports" / "initial-pipeline.json"),
        "original_review_report": str(root / "reports" / "initial-review.json"),
        "original_db_snapshot": str(root / "reports" / "initial-proposals.sqlite"),
        "model": args.model, "ai_analysis_reused": True,
        "removed_pending_nonduplicate_proposal_ids": removed_ids,
        "reset_analyzed_file_ids": reset_ids,
        "before": before, "steps": {},
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(path, report)
    try:
        from donedatahoarder.proposals.namer import generate_proposals
        run_step(report, path, "propose", lambda: generate_proposals(session_id=sid))
        from donedatahoarder.proposals.organizer import generate_reorg_proposals
        run_ai_step(report, path, "organize",
                    lambda: generate_reorg_proposals(session_id=sid),
                    state / "logs" / "donedatahoarder.log")
        report["final"] = status_report(engine, sid)
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(path, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


def review(args: argparse.Namespace) -> None:
    root, data, state = run_paths(args.run_dir)
    report, retry = completed_report(root)
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal
    with Session(engine) as db:
        rows = db.query(Proposal, File).join(File).filter(File.session_id == report["session_id"]).order_by(Proposal.id)
        proposals = [{"id": p.id, "type": p.proposal_type.value,
                      "status": p.status.value, "confidence": p.confidence,
                      "file_id": f.id, "file_path": f.path,
                      "current": p.current_value, "proposed": p.proposed_value,
                      "reasoning": p.reasoning,
                      "current_within_copy": not p.current_value or within(Path(p.current_value), data),
                      "proposed_within_copy": not p.proposed_value or within(Path(p.proposed_value), data)}
                     for p, f in rows]
        unverified_paths = sorted(
            f.path for f in db.query(File).filter_by(session_id=report["session_id"])
            if f.analysis_outcome in ("context_only", "metadata_only") or
            (f.analysis_outcome is None and f.status in (FileStatus.ANALYZED, FileStatus.PROPOSED)
             and (f.ai_description or "").startswith("[UNVERIFIED"))
        )
    refresh_path = root / "reports" / "proposal-refresh-1.json"
    refresh = read_json(refresh_path) if refresh_path.exists() else None
    if refresh is not None and (refresh.get("status") != "complete"
                                or refresh.get("session_id") != report["session_id"]):
        raise ValueError("proposal refresh incomplete or mismatched")
    coverage = dict((retry or report).get("ai_coverage", report.get("ai_coverage")) or {})
    coverage["unverified_content"] = len(unverified_paths)
    coverage["unverified_paths"] = unverified_paths
    coverage["unverified_count_method"] = "persisted analyzed outcomes plus legacy prefix"
    result = {"run_dir": str(root), "session_id": report["session_id"],
              "pipeline": (refresh or retry or report)["final"],
              "ai_coverage": coverage,
              "downstream_retry": retry.get("report_path") if retry else None,
              "proposal_refresh": str(refresh_path) if refresh else None,
              "proposals": proposals}
    write_json(root / "reports" / "review.json", result)
    print(json.dumps(result, ensure_ascii=True, indent=2))


def current_manifest(data: Path) -> list[dict]:
    files = []
    for path in data.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink in copy: {path}")
        if path.is_file():
            files.append({"path": path.relative_to(data).as_posix(),
                          "size": path.stat().st_size, "sha256": digest(path)})
    return sorted(files, key=lambda x: x["path"])


def directory_manifest(data: Path) -> list[str]:
    return sorted(path.relative_to(data).as_posix() for path in data.rglob("*") if path.is_dir())


def validate_same_file_operations(proposals: list) -> None:
    """Allow one rename followed by one move only when their paths agree."""
    if len(proposals) <= 1:
        return
    by_type = {proposal.proposal_type.value: proposal for proposal in proposals}
    if (len(proposals) != 2 or set(by_type) != {"rename", "move"}
            or by_type["rename"].id >= by_type["move"].id
            or by_type["rename"].current_value != by_type["move"].current_value
            or Path(by_type["rename"].proposed_value).name
            != Path(by_type["move"].proposed_value).name):
        raise ValueError("same-file selection must be an ordered rename then matching move")


def execute_cycle(args: argparse.Namespace) -> None:
    root, data, state = run_paths(args.run_dir)
    report, _ = completed_report(root)
    if (root / "reports" / "execution.json").exists():
        raise ValueError("execution already attempted for this copy")
    ids = sorted(set(args.proposal_ids or []))
    if (not ids and not args.no_safe_ops) or (ids and args.no_safe_ops) or any(i < 1 for i in ids):
        raise ValueError("provide positive proposal IDs or explicit --no-safe-ops")
    source_meta = read_json(root / "run.json")
    source = archive_path(source_meta["corpus"])
    if digest(source) != source_meta["source_sha256"]:
        raise ValueError("source ZIP changed before execution review")
    baseline = read_json(root / "reports" / "baseline.json")
    baseline_content = sorted(
        ({k: row[k] for k in ("path", "size", "sha256")} for row in baseline),
        key=lambda x: x["path"],
    )
    if current_manifest(data) != baseline_content:
        raise ValueError("copy differs from extraction baseline before execution")
    directories_before = directory_manifest(data)
    isolate(root, state)
    engine = db_session(state)
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType
    allowed = {ProposalType.RENAME, ProposalType.MOVE, ProposalType.MARK_DUPLICATE}
    session_id = report["session_id"]
    if args.no_safe_ops:
        with Session(engine) as db:
            db_paths = {f.id: f.path for f in db.query(File).filter_by(session_id=session_id)}
        result = {
            "run_dir": str(root), "session_id": session_id,
            "proposal_ids": [], "safe_operations": 0,
            "mutation_exercised": False,
            "reason": "No safe filesystem proposal was selected after review",
            "content_and_paths_restored": current_manifest(data) == baseline_content,
            "directory_topology_restored": directory_manifest(data) == directories_before,
            "db_paths_restored": all(within(Path(path), data) for path in db_paths.values()),
            "source_zip_unchanged": digest(source) == source_meta["source_sha256"],
        }
        result["pass"] = all(result[key] for key in (
            "content_and_paths_restored", "directory_topology_restored",
            "db_paths_restored", "source_zip_unchanged",
        ))
        write_json(root / "reports" / "execution.json", result)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    with Session(engine) as db:
        selected = db.query(Proposal, File).join(File).filter(Proposal.id.in_(ids)).all()
        if len(selected) != len(ids):
            raise ValueError("some proposal IDs do not exist")
        file_operations: dict[int, list] = {}
        for prop, file in selected:
            if file.session_id != session_id or prop.proposal_type not in allowed:
                raise ValueError(f"proposal {prop.id} is outside this copy or unsupported")
            if prop.status not in (ProposalStatus.PENDING, ProposalStatus.APPROVED):
                raise ValueError(f"proposal {prop.id} is not pending/approved")
            file_operations.setdefault(file.id, []).append(prop)
            if not prop.current_value or not within(Path(prop.current_value), data):
                raise ValueError(f"proposal {prop.id} source is outside copy")
            if not prop.proposed_value or not within(Path(prop.proposed_value), data):
                raise ValueError(f"proposal {prop.id} destination/keeper is outside copy")
            if prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE):
                if Path(prop.proposed_value).exists() or not Path(prop.current_value).is_file():
                    raise ValueError(f"proposal {prop.id} has missing source or occupied destination")
        for operations in file_operations.values():
            validate_same_file_operations(operations)
    from donedatahoarder.core.undo_log import get_undo_log_path, undo_operations
    journal = get_undo_log_path(session_id)
    if not within(journal, state / "datahoarder") or journal.exists():
        raise ValueError("undo journal location is unsafe or already used")
    from donedatahoarder.executor import execute, _make_quiet_console
    result = {"run_dir": str(root), "session_id": session_id, "proposal_ids": ids,
              "before_files": len(baseline), "journal": str(journal)}
    path = root / "reports" / "execution.json"
    write_json(path, result)
    with Session(engine) as db:
        for prop in db.query(Proposal).filter(Proposal.id.in_(ids)):
            prop.status = ProposalStatus.APPROVED
            prop.review_kind = "individual"
        db.commit()
    with Session(engine) as db:
        db_before = {f.id: f.path for f in db.query(File).filter_by(session_id=session_id)}
    dry = execute(dry_run=True, min_confidence=1.1, proposal_ids=ids,
                  session_id=session_id, _console=_make_quiet_console())
    result["dry_run"] = dry
    write_json(path, result)
    if dry["failed"] or dry["applied"] != len(ids):
        raise RuntimeError("selected proposals failed dry run; no commit attempted")
    committed = execute(dry_run=False, min_confidence=1.1, proposal_ids=ids,
                        session_id=session_id, _console=_make_quiet_console())
    result["commit"] = committed
    result["after_commit_manifest"] = current_manifest(data)
    write_json(path, result)
    undone = undo_operations(session_id=session_id, force=True, console=_make_quiet_console())
    result["undo"] = {k: v for k, v in undone.items() if k != "entries"}
    after_undo = current_manifest(data)
    result["after_undo_manifest"] = after_undo
    result["content_and_paths_restored"] = after_undo == baseline_content
    directories_after = directory_manifest(data)
    result["directory_topology_restored"] = directories_after == directories_before
    result["added_directories_after_undo"] = sorted(set(directories_after) - set(directories_before))
    result["missing_directories_after_undo"] = sorted(set(directories_before) - set(directories_after))
    with Session(engine) as db:
        db_after = {f.id: f.path for f in db.query(File).filter_by(session_id=session_id)}
    result["db_paths_restored"] = db_after == db_before
    result["source_zip_unchanged"] = digest(source) == source_meta["source_sha256"]
    result["db_path_mismatches"] = [
        {"file_id": fid, "before": old, "after": db_after.get(fid)}
        for fid, old in db_before.items() if db_after.get(fid) != old
    ]
    result["pass"] = (committed["failed"] == 0 and committed["applied"] == len(ids)
                      and undone["failed"] == 0 and result["content_and_paths_restored"]
                      and result["directory_topology_restored"]
                      and result["db_paths_restored"] and result["source_zip_unchanged"])
    write_json(path, result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if not result["pass"]:
        raise RuntimeError("commit/undo verification failed; inspect execution.json")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    sub.add_parser("inventory", help="read ZIP metadata only")
    prep = sub.add_parser("prepare", help="safe-extract one ZIP to a new run")
    prep.add_argument("--corpus", required=True,
                      help="plain ZIP basename directly inside D:\\Test")
    pipe = sub.add_parser("pipeline", help="scan/enrich/dedup and local AI on a prepared copy")
    pipe.add_argument("--run-dir", required=True)
    pipe.add_argument("--model", default="gemma4:26b")
    pipe.add_argument("--ollama-host", default="http://localhost:11434")
    pipe.add_argument("--workers", type=int, default=1)
    scope = pipe.add_mutually_exclusive_group()
    scope.add_argument("--ai-limit", type=int, default=5, help="bounded AI smoke (default: 5 files)")
    scope.add_argument("--full-ai", action="store_true", help="analyze all eligible files")
    pipe.add_argument("--organize", action="store_true", help="include organizer in a bounded smoke")
    pipe.add_argument("--relate", action="store_true", help="include relation grouping in a bounded smoke")
    retry = sub.add_parser("retry-downstream", help="resume failed relate/propose/organize on the same isolated DB")
    retry.add_argument("--run-dir", required=True)
    retry.add_argument("--model", default="gemma4:26b")
    retry.add_argument("--ollama-host", default="http://localhost:11434")
    retry.add_argument("--organize", action="store_true")
    analysis_retry = sub.add_parser("retry-analysis", help="continue interrupted full AI with explicit inference-error retry")
    analysis_retry.add_argument("--run-dir", required=True)
    analysis_retry.add_argument("--model", default="gemma4:26b")
    analysis_retry.add_argument("--ollama-host", default="http://localhost:11434")
    rev = sub.add_parser("review", help="write review.json; no changes")
    rev.add_argument("--run-dir", required=True)
    refresh = sub.add_parser("refresh-proposals", help="regenerate proposals after fix, reusing analysis")
    refresh.add_argument("--run-dir", required=True)
    refresh.add_argument("--model", default="gemma4:26b")
    refresh.add_argument("--ollama-host", default="http://localhost:11434")
    execution = sub.add_parser("execute", help="dry run, commit selected proposals, undo, verify")
    execution.add_argument("--run-dir", required=True)
    execution.add_argument("--proposal-ids", type=int, nargs="*", default=[])
    execution.add_argument("--no-safe-ops", action="store_true",
                           help="record that review found zero safe filesystem operations")
    args = parser.parse_args()
    try:
        {"inventory": inventory, "prepare": prepare, "pipeline": pipeline,
         "retry-downstream": retry_downstream,
         "retry-analysis": retry_analysis,
         "refresh-proposals": refresh_proposals,
         "review": review, "execute": execute_cycle}[args.phase](args)
    except Exception as exc:
        print(f"validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

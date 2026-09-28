"""
Pipeline endpoints — execute, per-step triggers (scan / enrich / dedup /
relate / analyze / propose / organize), background-job control, and the
before/after organize tree builders.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import StreamingResponse
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    BackgroundJob,
    DuplicateGroup,
    DuplicateMember,
    File,
    FileStatus,
    Proposal,
    ProposalStatus,
    ProposalType,
    RunPlan,
    UserSession,
)
from donedatahoarder.db.session import get_engine

from .deps import _mark_session_unsaved, _require_session_id, _resolve_model, review_execute_lock, review_operation
from .schemas import ExecuteRequest, PipelineRequest, ResumeRunPlanRequest, RunPlanRequest

router = APIRouter()


def _require_worker_exit(job_manager) -> None:
    # force_cancel() clears the active slot before a blocked worker exits.
    if job_manager.get_active() is None and job_manager.has_live_workers():
        raise HTTPException(409, "Wait for the cancelled pipeline worker to exit")


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def _execute_preview(session_id: str) -> dict:
    from donedatahoarder.executor import plan_execution, select_executable_proposals

    if not session_id or not session_id.strip():
        raise HTTPException(400, "No active session. Create or load a session first.")
    engine = get_engine()
    with Session(engine) as db:
        user_session = db.get(UserSession, session_id)
        if user_session is None:
            raise HTTPException(404, "Session not found")
        proposals = select_executable_proposals(db, session_id=session_id)
        planned = plan_execution(db, proposals, Path(user_session.root_path)) if proposals else []
        proposal_by_id = {proposal.id: proposal for proposal in proposals}
        items = []
        for step in planned:
            proposal = proposal_by_id[step.proposal_id]
            file = db.get(File, proposal.file_id)
            item = {
                "id": proposal.id,
                "type": proposal.proposal_type.value,
                "status": proposal.status.value,
                "source": step.source or (file.path if file else ""),
                "destination": step.destination or "",
                "keeper": step.keeper or "",
                "error": step.error,
                "confidence": proposal.confidence,
            }
            if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                groups = (
                    db.query(DuplicateGroup)
                    .join(DuplicateMember, DuplicateMember.group_id == DuplicateGroup.id)
                    .filter(DuplicateMember.file_id == proposal.file_id,
                            DuplicateGroup.session_id == session_id)
                    .all()
                )
                item["keeper_groups"] = sorted(
                    (group.id, group.keep_file_id) for group in groups
                )
            items.append(item)
    by_type: dict[str, int] = {}
    for item in items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    # Bind the user's confirmation to the precise proposal values displayed.
    fingerprint = json.dumps([session_id, user_session.root_path, items], sort_keys=True, separators=(",", ":"))
    token = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return {"session_id": session_id, "total": len(items),
            "errors": sum(bool(item["error"]) for item in items),
            "by_type": by_type, "items": items, "token": token}


@router.get("/execute/preview")
def preview_execution(session_id: str):
    """Show exactly which reviewed proposals the default commit would select."""
    with review_operation("preview execution"):
        return _execute_preview(session_id)


@router.post("/execute")
def execute_proposals(body: ExecuteRequest):
    # A preview remains valid only while review decisions and proposal values
    # cannot change. Keep this lock through the filesystem commit and DB writes.
    with review_operation("commit reviewed proposals"):
        return _execute_proposals_locked(body)


def _execute_proposals_locked(body: ExecuteRequest):
    from donedatahoarder.executor import execute as do_execute, _make_quiet_console

    sid = body.session_id
    if not sid:
        raise HTTPException(400, "No active session. Create or load a session first.")

    # Dry-run is routed through the background-job system so it survives
    # disconnects (browser refresh / laptop sleep) during long unattended
    # runs. The destructive --commit path stays synchronous to preserve
    # the user-visible "Apply changes? y/N" confirmation flow.
    if body.dry_run:
        from donedatahoarder.core.jobs import job_manager
        try:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_execute_dry(
                session_id=sid,
                min_confidence=body.min_confidence,
            )
            return {"job_id": job_id, "status": "started"}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    preview = _execute_preview(sid)
    if not body.preview_token:
        raise HTTPException(400, "Preview the reviewed changes before committing")
    if body.preview_token != preview["token"]:
        raise HTTPException(409, "Reviewed changes have changed. Preview again before committing")
    if not preview["items"]:
        raise HTTPException(400, "No approved or edited proposals to commit")
    if preview["errors"]:
        raise HTTPException(409, "Preview has failing actions. Resolve or reject them before committing")
    from donedatahoarder.core.jobs import job_manager
    if job_manager.get_active() is not None or job_manager.has_live_workers():
        raise HTTPException(409, "Wait for the pipeline worker to exit before committing")
    counts = do_execute(
        dry_run=body.dry_run,
        min_confidence=body.min_confidence,
        session_id=sid,
        proposal_ids=[item["id"] for item in preview["items"]],
        _console=_make_quiet_console(),
    )
    _mark_session_unsaved(sid, step="execute")

    # Record completed folders after a real (non-dry) execute
    if not body.dry_run:
        try:
            from donedatahoarder.db.models import CompletedFolder as CF
            engine = get_engine()
            with Session(engine) as db:
                us = db.get(UserSession, sid)
                root_path = us.root_path if us else ""
                # Collect unique parent dirs of APPLIED files
                applied_parents = set(
                    str(Path(f.path).parent)
                    for f in db.query(File).filter(
                        File.session_id == sid,
                        File.status == FileStatus.APPLIED,
                    ).all()
                )
                # Also include the root itself
                if root_path:
                    applied_parents.add(root_path)
                # Upsert: avoid duplicate entries
                existing = {
                    r.folder_path
                    for r in db.query(CF.folder_path).filter(
                        CF.session_id == sid
                    ).all()
                }
                for folder_path in sorted(applied_parents):
                    if folder_path not in existing:
                        db.add(CF(
                            folder_path=folder_path,
                            session_id=sid,
                            root_path=root_path,
                        ))
                db.commit()
        except Exception:
            pass  # Non-fatal

    return counts


# ---------------------------------------------------------------------------
# Pipeline triggers (run steps on demand)
# ---------------------------------------------------------------------------

@router.post("/pipeline/scan")
def trigger_scan(body: PipelineRequest):
    with review_operation("scan session"):
        return _trigger_scan_locked(body)


def _trigger_scan_locked(body: PipelineRequest):
    from donedatahoarder.core.scanner import scan as do_scan
    import io
    import contextlib

    try:
        sid = _require_session_id(body.session_id)

        if not body.root_path or not body.root_path.strip():
            raise HTTPException(400, "Root path is required")

        root = Path(body.root_path)
        if not root.exists():
            raise HTTPException(400, f"Path does not exist: {root}")

        if not root.is_dir():
            raise HTTPException(400, f"Path is not a directory: {root}")

        # Update session root_path
        engine = get_engine()
        with Session(engine) as session:
            user_session = session.get(UserSession, sid)
            if user_session:
                user_session.root_path = str(root.resolve())
                session.commit()

        # Suppress stdout to prevent Rich progress bar Unicode errors in web context
        extra_skip = set(body.skip_dirs) if body.skip_dirs else None
        with contextlib.redirect_stdout(io.StringIO()):
            counts = do_scan(root, session_id=sid, extra_skip_dirs=extra_skip)

        _mark_session_unsaved(sid, step="scan")
        return counts
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Scan failed: {str(exc)}")


@router.post("/pipeline/enrich")
def trigger_enrich(body: PipelineRequest = PipelineRequest()):
    """Start an enrich background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_enrich(session_id=sid)
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Enrich failed: {str(exc)}")


@router.post("/pipeline/dedup")
def trigger_dedup(body: PipelineRequest = PipelineRequest()):
    """Start a dedup background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_dedup(session_id=sid)
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Dedup failed: {str(exc)}")


@router.post("/pipeline/relate")
def trigger_relate(body: PipelineRequest = PipelineRequest()):
    """
    Start a Relate background job. Returns job_id immediately.

    LLM groups conceptually-related files (e.g. a .dwg with its .bak backup
    and PDF exports), falling back to numeric-prefix regex clustering if the
    LLM is unavailable. Writes RelationGroups / RelationMembers. Idempotent —
    re-running wipes and rebuilds groups.

    Relate is filename-only reasoning (no vision), so it uses the session's
    `propose_model` (reasoning model) rather than `analyze_model` (vision).
    """
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        # Relate is a reasoning task — use propose_model (fallback chain in
        # _resolve_model covers empty / legacy cases).
        model = _resolve_model(body.model, sid, step="propose")

        # Pull the session's relate_scope preference
        scope = "per_directory"
        with Session(get_engine()) as db:
            us = db.get(UserSession, sid)
            if us and getattr(us, "relate_scope", None):
                scope = us.relate_scope

        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_relate(
                session_id=sid,
                backend=body.backend,
                model=model,
                scope=scope,
            )
        return {"job_id": job_id, "status": "started", "model": model, "scope": scope}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Relate failed: {str(exc)}")


@router.get("/sessions/{session_id}/relations")
def list_relations(session_id: str):
    """List RelationGroups (with members) for a session — for UI display."""
    from donedatahoarder.db.models import RelationGroup
    engine = get_engine()
    with Session(engine) as session:
        groups = (
            session.query(RelationGroup)
            .filter(RelationGroup.session_id == session_id)
            .order_by(RelationGroup.dir_path, RelationGroup.label)
            .all()
        )
        # Prefetch filenames for every file referenced by any member
        all_file_ids = {m.file_id for g in groups for m in g.members}
        filename_by_id: dict[int, str] = {}
        ids = sorted(all_file_ids)
        for start in range(0, len(ids), 500):
            for fid, fname in session.query(File.id, File.filename).filter(
                File.id.in_(ids[start:start + 500])
            ):
                filename_by_id[fid] = fname

        out = []
        for g in groups:
            out.append({
                "id": g.id,
                "label": g.label,
                "reason": g.reason,
                "confidence": g.confidence,
                "scope": g.scope,
                "dir_path": g.dir_path,
                "members": [
                    {
                        "file_id": m.file_id,
                        "filename": filename_by_id.get(m.file_id, "?"),
                        "role": m.role.value,
                    }
                    for m in g.members
                ],
            })
        return {"session_id": session_id, "groups": out}


@router.get("/pipeline/dedup/coverage")
def latest_dedup_coverage(session_id: str = Query(...)):
    """Durable similarity-search coverage from the latest dedup job."""
    sid = _require_session_id(session_id)
    with Session(get_engine()) as db:
        job = (db.query(BackgroundJob)
               .filter(BackgroundJob.session_id == sid,
                       BackgroundJob.job_type == "dedup",
                       BackgroundJob.state == "completed")
               .order_by(BackgroundJob.started_at.desc(), BackgroundJob.id.desc())
               .first())
        if job is None:
            return {"session_id": sid, "job_state": None, "stages": {}}
        try:
            progress = json.loads(job.progress_json or "{}")
        except (ValueError, TypeError):
            progress = {}
        stages = {
            name: progress.get(name, {})
            for name in ("exact", "perceptual", "semantic", "text_near")
        }
        return {"session_id": sid, "job_state": job.state,
                "job_id": job.id, "stages": stages}


@router.post("/pipeline/analyze")
def trigger_analyze(body: PipelineRequest):
    """Start an analyze background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="analyze")
        # Persist the resolved model back to the session so later steps (Propose, Organize) can use it
        if model and body.model:
            with Session(get_engine()) as db:
                us = db.get(UserSession, sid)
                if us and not us.model:
                    us.model = model
                    db.commit()
        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_analyze(
                session_id=sid,
                backend=body.backend,
                model=model,
                workers=body.workers,
                retry_errors=body.retry_errors,
                sequence_sample_stride=body.sequence_sample_stride,
                use_cache=body.use_cache,
            )
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Analyze failed: {str(exc)}")


# ---------------------------------------------------------------------------
# Job control endpoints (pause, resume, stream, active)
# ---------------------------------------------------------------------------

def _owned_run_plan(plan_id: str, session_id: str) -> dict:
    from donedatahoarder.core.jobs import job_manager
    plan = job_manager.get_run_plan(plan_id)
    if not plan or plan["session_id"] != _require_session_id(session_id):
        raise HTTPException(404, "Run plan not found in this session")
    return plan


@router.post("/pipeline/runs")
def create_run_plan(body: RunPlanRequest):
    """Store the plan before launch; the server owns every phase checkpoint."""
    from donedatahoarder.core.jobs import job_manager
    sid = _require_session_id(body.session_id)
    with review_operation("create run plan"):
        with Session(get_engine()) as db:
            owner = db.get(UserSession, sid)
            if owner is None:
                raise HTTPException(404, "Session not found")
            chosen_root = body.root_path or owner.root_path
            if not chosen_root or not chosen_root.strip():
                raise HTTPException(400, "Choose a folder for this session before starting")
            root = Path(chosen_root).resolve()
            if not root.is_dir():
                raise HTTPException(400, "Run folder does not exist")
            if body.root_path and owner.root_path and root != Path(owner.root_path).resolve():
                raise HTTPException(409, "Run folder differs from the saved session folder")
            if not owner.root_path:
                owner.root_path = str(root)
                db.commit()
            options = {
                "root_path": str(root), "skip_dirs": body.skip_dirs,
                "backend": body.backend or owner.backend or "ollama",
                "analyze_model": _resolve_model(body.analyze_model or body.model, sid, "analyze"),
                "propose_model": _resolve_model(body.propose_model or body.model, sid, "propose"),
                "workers": max(1, body.workers),
                "relate_scope": body.relate_scope or owner.relate_scope or "per_directory",
                "sequence_sample_stride": body.sequence_sample_stride,
                "use_cache": body.use_cache,
            }
        try:
            plan_id = job_manager.create_run_plan(sid, body.steps, options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    # Worker acquisition occurs after the review lock is released.
    try:
        job_id = job_manager.advance_run_plan(plan_id)
    except RuntimeError as exc:
        raise HTTPException(409, f"Run plan saved; start it from its ready state: {exc}") from exc
    return {"plan": job_manager.get_run_plan(plan_id), "job_id": job_id}


@router.get("/pipeline/runs/latest")
def latest_run_plan(session_id: str):
    from donedatahoarder.core.jobs import job_manager
    sid = _require_session_id(session_id)
    job_manager.reconcile_startup()
    with Session(get_engine()) as db:
        row = (db.query(RunPlan).filter(RunPlan.session_id == sid)
               .order_by(RunPlan.created_at.desc()).first())
        if row is None:
            return {"plan": None}
        plan_id = row.id
        last_job = (db.query(BackgroundJob).filter(BackgroundJob.run_plan_id == plan_id)
                    .order_by(BackgroundJob.started_at.desc()).first())
        failure = {"phase": last_job.job_type, "error": last_job.error} if last_job and last_job.error else None
    return {"plan": job_manager.get_run_plan(plan_id), "last_failure": failure}


@router.get("/pipeline/runs/{plan_id}")
def run_plan_status(plan_id: str, session_id: str):
    return {"plan": _owned_run_plan(plan_id, session_id)}


@router.post("/pipeline/runs/{plan_id}/resume")
def resume_run_plan(plan_id: str, body: ResumeRunPlanRequest):
    from donedatahoarder.core.jobs import job_manager
    _owned_run_plan(plan_id, body.session_id)
    try:
        job_id = job_manager.resume_run_plan(plan_id, retry_errors=body.retry_errors)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"plan": job_manager.get_run_plan(plan_id), "job_id": job_id}


@router.post("/pipeline/runs/{plan_id}/advance")
def advance_run_plan(plan_id: str, body: ResumeRunPlanRequest):
    from donedatahoarder.core.jobs import job_manager
    plan = _owned_run_plan(plan_id, body.session_id)
    if plan["state"] != "ready":
        raise HTTPException(409, "Only a ready run can be started")
    try:
        job_id = job_manager.advance_run_plan(plan_id)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"plan": job_manager.get_run_plan(plan_id), "job_id": job_id}


@router.post("/pipeline/runs/{plan_id}/cancel")
def cancel_run_plan(plan_id: str, body: ResumeRunPlanRequest):
    from donedatahoarder.core.jobs import job_manager
    _owned_run_plan(plan_id, body.session_id)
    job_manager.cancel_run_plan(plan_id)
    return {"plan": job_manager.get_run_plan(plan_id)}


@router.get("/pipeline/analyze/errors")
def analysis_errors(session_id: str):
    """Show retryable provider errors separately from unsupported content."""
    sid = _require_session_id(session_id)
    with Session(get_engine()) as db:
        if db.get(UserSession, sid) is None:
            raise HTTPException(404, "Session not found")
        failures = (db.query(File).filter(File.session_id == sid, File.status == FileStatus.ERROR)
                    .all())
        reasons: dict[str, int] = {}
        for file in failures:
            reason = file.analysis_reason or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
        retryable = sum(count for reason, count in reasons.items() if reason.startswith("provider_"))
    return {"total": len(failures), "retryable": retryable, "reasons": reasons}


@router.get("/pipeline/preflight")
def collection_preflight(session_id: str, mode: str = "full",
                         sequence_sample_stride: int = 0,
                         skip_dirs: list[str] = Query(default=[])):
    """Read-only estimate for the currently selected collection root."""
    from donedatahoarder.core.preflight import estimate_collection

    sid = _require_session_id(session_id)
    if mode not in {"full", "representative", "metadata_only"}:
        raise HTTPException(400, "Unknown preflight mode")
    if not 0 <= sequence_sample_stride <= 1000:
        raise HTTPException(400, "Sequence sample stride must be 0–1000")
    if mode == "representative" and sequence_sample_stride < 2:
        raise HTTPException(400, "Representative mode requires a stride of at least 2")
    with Session(get_engine()) as db:
        owner = db.get(UserSession, sid)
        if owner is None or not owner.root_path:
            raise HTTPException(404, "Choose a session folder first")
        root = Path(owner.root_path).resolve()
    if not root.is_dir():
        raise HTTPException(404, "Session folder is unavailable")
    try:
        return estimate_collection(root, mode=mode,
                                   sequence_sample_stride=sequence_sample_stride,
                                   extra_skip_dirs=set(skip_dirs))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, f"Cannot read selected folder: {exc}") from exc
    except OSError as exc:
        raise HTTPException(503, f"Cannot estimate selected folder: {exc}") from exc

@router.get("/pipeline/jobs/active")
def get_active_job():
    """Return the currently active background job, if any."""
    from donedatahoarder.core.jobs import job_manager
    job = job_manager.get_active()
    if job:
        return job.to_dict()
    return {"job_id": None}


@router.get("/pipeline/jobs/{job_id}")
def get_job_status(job_id: str):
    """Return status of a specific job."""
    from donedatahoarder.core.jobs import job_manager
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job.to_dict()


@router.get("/pipeline/jobs/{job_id}/stream")
def stream_job_progress(job_id: str):
    """SSE stream of job progress. Reconnectable after page refresh."""
    from donedatahoarder.core.jobs import job_manager

    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    def generate():
        try:
            for progress in job_manager.subscribe(job_id):
                yield f"data: {json.dumps(progress)}\n\n"
        except KeyError:
            yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/pipeline/jobs/{job_id}/pause")
def pause_job(job_id: str):
    """Pause a running job."""
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.pause(job_id)
        return {"status": "paused", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/jobs/{job_id}/resume")
def resume_job(job_id: str):
    """Resume a paused job."""
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.resume(job_id)
        return {"status": "resumed", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Request cancellation; the worker lease persists until it actually exits."""
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.force_cancel(job_id)
        current = job_manager.get_job(job_id)
        return {"status": current.state.value if current else "cancelling", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/propose")
def trigger_propose(body: PipelineRequest = PipelineRequest()):
    """Start a propose background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="propose")
        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_propose(
                session_id=sid,
                backend=body.backend,
                model=model,
            )
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Propose failed: {str(exc)}")


def _fs_walk_skeleton(root_path: str, max_depth: int = 3) -> dict:
    """
    Walk the actual filesystem under root_path and produce a skeletal tree
    that includes *every* folder and (up to N) file names — regardless of
    whether those files were analyzed / exist in the DB.

    This is what makes empty folders (GIF/, images/, _files/ companions) and
    un-analyzed / errored / pending root files (the 452 MB PDF, HTMLs) show
    up in the UI. Without it, the tree only ever reflects ANALYZED files
    and the user has no idea what's actually on disk.

    Depth is capped so we don't blow up on deep hierarchies; the UI tree
    only ever shows a few levels anyway.
    """
    root = Path(root_path)
    tree: dict = {}
    if not root.exists():
        return tree

    SAMPLE_CAP = 12  # per-folder cap on listed filenames

    def _walk(fs_path: Path, node: dict, depth: int) -> None:
        try:
            entries = list(fs_path.iterdir())
        except (OSError, PermissionError):
            return

        files: list[dict] = []
        total_size = 0
        for entry in entries:
            try:
                if entry.is_file():
                    size = 0
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass
                    total_size += size
                    files.append({"name": entry.name, "size": size})
                elif entry.is_dir() and depth < max_depth:
                    # Recurse into subdirs
                    sub: dict = {}
                    node[entry.name] = sub
                    _walk(entry, sub, depth + 1)
            except OSError:
                continue

        # Record file count (own directory, not including subfolders) and sample.
        # Sort files by size descending so the biggest show first — the 452 MB
        # PDF jumps to the top of the list where the user can't miss it.
        files.sort(key=lambda f: -f["size"])
        node["_files"] = len(files)
        node["_size"] = total_size
        if files:
            node["_sample_files"] = files[:SAMPLE_CAP]
            if len(files) > SAMPLE_CAP:
                node["_sample_truncated"] = len(files) - SAMPLE_CAP

    _walk(root, tree, 0)
    return tree


def _folder_summaries_to_tree(summaries, root_path: str) -> dict:
    """
    Build a nested dict tree for the frontend visualization.

    Starts from an actual filesystem walk so that *every* folder on disk is
    represented (even empty / un-analyzed ones like GIF/ or images/), then
    overlays analyzed-file metadata from the FolderSummary records. Without
    the fs walk, folders that had no analyzed files would silently disappear
    from the Current Structure panel, misleading the user about what's on disk.

    The overlay increments counters rather than overwriting them — that way
    the fs file counts include un-analyzed files too (so the 452 MB PDF at
    root shows up in the root's total even if it was never analyzed).
    """
    root = Path(root_path)

    # Phase 1: skeleton from actual filesystem — guarantees every folder and
    # file on disk is represented, regardless of analysis status.
    tree = _fs_walk_skeleton(str(root))

    # Phase 2: no overlay needed — the fs walk already populated _files,
    # _size, and _sample_files. FolderSummary metadata (tags, descriptions)
    # isn't used by the frontend tree renderer, so there's nothing to merge.
    # We keep `summaries` as a parameter for API compatibility and future use
    # (e.g. overlaying analysis-derived labels).
    _ = summaries

    return tree


def _build_proposed_tree(session_id: str, before_summaries, root_path: str) -> dict:
    """
    Build an 'after' tree reflecting pending MOVE and RENAME_FOLDER proposals.

    Starts from the filesystem-backed before-tree, then simulates the effect
    of applying every pending folder rename + file move. Unlike the previous
    implementation this also:

    - Decrements the source folder's counts (not just increments the target)
    - Moves the actual filename between source._sample_files and target._sample_files
      so the UI shows the file in its destination, not both places
    """
    tree = _folder_summaries_to_tree(before_summaries, root_path)
    root = Path(root_path)

    def _node_at(parts: tuple[str, ...]) -> dict | None:
        """Traverse the tree by path parts. Returns None if any segment missing."""
        node: dict = tree
        for part in parts:
            if part not in node or not isinstance(node[part], dict):
                return None
            node = node[part]
        return node

    def _ensure_node(parts: tuple[str, ...]) -> dict:
        """Like _node_at but creates missing segments as empty dicts."""
        node: dict = tree
        for part in parts:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {"_files": 0, "_size": 0}
            node = node[part]
        return node

    # Fetch each Proposal alongside its File's size_bytes so we can use the
    # DB as the authoritative source for move sizes. Relying on _sample_files
    # alone was buggy: when the moved file was past SAMPLE_CAP (12 entries
    # per folder), the destination node would end up with "1 files, 0 B"
    # because the fallback entry used size=0. DB size_bytes is always right.
    with Session(get_engine()) as db:
        rows = (
            db.query(Proposal, File.size_bytes)
            .join(File, Proposal.file_id == File.id)
            .filter(
                File.session_id == session_id,
                Proposal.status == ProposalStatus.PENDING,
                Proposal.proposal_type.in_([ProposalType.MOVE, ProposalType.RENAME_FOLDER]),
            )
            .all()
        )

    # Pass 1: apply folder renames (just a key rename in the parent node).
    # Done first so subsequent MOVE lookups resolve via post-rename paths.
    for p, _size in rows:
        if p.proposal_type == ProposalType.RENAME_FOLDER and p.current_value and p.proposed_value:
            try:
                old_rel = Path(p.current_value).relative_to(root)
                new_rel = Path(p.proposed_value).relative_to(root)
            except ValueError:
                continue
            old_parts = old_rel.parts
            new_name = new_rel.parts[-1] if new_rel.parts else None
            if old_parts and new_name:
                parent_node = _node_at(old_parts[:-1]) if len(old_parts) > 1 else tree
                if parent_node is None:
                    continue
                old_name = old_parts[-1]
                if old_name in parent_node:
                    parent_node[new_name] = parent_node.pop(old_name)

    # Pass 2: apply file MOVE proposals. Each MOVE updates both source and
    # target: decrement source counts / remove from source._sample_files,
    # increment target counts / add to target._sample_files. Size comes from
    # the DB (File.size_bytes) so the bookkeeping works even for files past
    # the per-folder sample cap.
    for p, db_size in rows:
        if p.proposal_type != ProposalType.MOVE or not p.proposed_value or not p.current_value:
            continue
        try:
            src_path = Path(p.current_value)
            dst_path = Path(p.proposed_value)
            src_parent_rel = src_path.parent.relative_to(root).parts
            dst_parent_rel = dst_path.parent.relative_to(root).parts
        except ValueError:
            continue

        file_size = int(db_size or 0)
        src_node = _node_at(src_parent_rel)
        dst_node = _ensure_node(dst_parent_rel)

        # Pop the entry from source._sample_files if present (so the UI
        # doesn't show the same file in two places). Absence is fine — the
        # file just wasn't among the 12 sampled names for that folder.
        src_name = src_path.name
        if src_node is not None:
            src_samples = src_node.get("_sample_files") or []
            for i, entry in enumerate(src_samples):
                if entry.get("name") == src_name:
                    src_samples.pop(i)
                    break
            # Always decrement source counts — the file is leaving regardless
            # of whether it was in the sample list.
            src_node["_files"] = max(0, src_node.get("_files", 1) - 1)
            src_node["_size"] = max(0, src_node.get("_size", 0) - file_size)

        # Place it into the destination with the authoritative DB size.
        dst_samples = dst_node.setdefault("_sample_files", [])
        dst_samples.append({"name": dst_path.name, "size": file_size})
        dst_node["_files"] = dst_node.get("_files", 0) + 1
        dst_node["_size"] = dst_node.get("_size", 0) + file_size

    return tree


@router.post("/pipeline/organize")
def trigger_organize(body: PipelineRequest = PipelineRequest()):
    """
    Start an organize background job. Returns job_id immediately.

    Uses LLM to suggest folder reorganization based on file analysis. The
    before/after folder trees are computed on completion and available via
    the GET /pipeline/organize/trees/{session_id} endpoint after the job
    finishes. (For backward UI compat the trees can also be re-fetched.)
    """
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="propose")
        with review_execute_lock:
            _require_worker_exit(job_manager)
            job_id = job_manager.start_organize(
                session_id=sid,
                backend=body.backend,
                model=model,
            )
        return {"job_id": job_id, "status": "started", "model": model}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Organize failed: {str(exc)}")


@router.get("/pipeline/organize/trees/{session_id}")
def get_organize_trees(session_id: str):
    """
    Build the before/after folder trees for a session that has organize
    proposals. Computed on demand after an organize background job finishes
    so the frontend can render the visual diff.
    """
    from donedatahoarder.proposals.organizer import build_folder_tree

    try:
        with Session(get_engine()) as db:
            us = db.get(UserSession, session_id)
            if not us:
                raise HTTPException(404, "Session not found")
            root_path = us.root_path or ""

        before_summaries = build_folder_tree(session_id, root_path)
        before_tree = _folder_summaries_to_tree(before_summaries, root_path)
        after_tree = _build_proposed_tree(session_id, before_summaries, root_path)
        return {"before_tree": before_tree, "after_tree": after_tree}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Tree build failed: {str(exc)}")


@router.get("/pipeline/organize/coverage")
def get_organize_coverage(session_id: str):
    """Partition indexed files into preserved, proposed, ordered and review."""
    from collections import defaultdict
    from donedatahoarder.core.dependency_protection import cached_protection_index
    from donedatahoarder.proposals.organizer.core import (
        _inside_project, _loose_source, _project_roots,
    )
    from donedatahoarder.proposals.organizer.tree import _file_category

    sid = _require_session_id(session_id)
    with Session(get_engine()) as db:
        owner = db.get(UserSession, sid)
        if owner is None or not owner.root_path:
            raise HTTPException(404, "Session folder unavailable")
        root = Path(owner.root_path).resolve()
        if not root.is_dir():
            raise HTTPException(404, "Session folder unavailable")
        protection = cached_protection_index(root)
        query = db.query(File).filter(File.session_id == sid)
        project_roots = _project_roots(root, query.yield_per(1000), protection)
        categories: dict[Path, set[str]] = defaultdict(set)
        for file_rec in query.yield_per(1000):
            categories[Path(file_rec.path).parent].add(
                _file_category(file_rec.mime_type, file_rec.extension)
            )
        move_ids = {
            file_id for file_id, destination in
            db.query(Proposal.file_id, Proposal.proposed_value)
            .join(File, File.id == Proposal.file_id)
            .filter(File.session_id == sid, Proposal.proposal_type == ProposalType.MOVE,
                    Proposal.status.in_([ProposalStatus.PENDING, ProposalStatus.MODIFIED,
                                         ProposalStatus.APPROVED, ProposalStatus.APPLIED]))
            if destination and Path(destination).is_relative_to(root / "Independent_Files")
        }
        counts = {"project_preserved": 0, "independent_proposed": 0,
                  "already_ordered": 0, "needs_review": 0, "total_indexed": 0}
        for file_rec in query.yield_per(1000):
            source = Path(file_rec.path)
            counts["total_indexed"] += 1
            if _inside_project(source, project_roots):
                counts["project_preserved"] += 1
            elif file_rec.id in move_ids:
                counts["independent_proposed"] += 1
            elif source.is_relative_to(root / "Independent_Files") or (
                source.parent != root and not _loose_source(
                    file_rec, root, categories, project_roots
                )
            ):
                counts["already_ordered"] += 1
            else:
                counts["needs_review"] += 1
    return {**counts, "note": (
        "Existing-folder retention is structural, not a verification of subject or date. "
        "Proposed moves require review, preview and commit; preserved projects remain in place."
    )}

"""
Pipeline endpoints — execute, per-step triggers (scan / enrich / dedup /
relate / analyze / propose / organize), background-job control, and the
before/after organize tree builders.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File,
    FileStatus,
    Proposal,
    ProposalStatus,
    ProposalType,
    UserSession,
)
from donedatahoarder.db.session import get_engine

from .deps import _mark_session_unsaved, _require_session_id, _resolve_model
from .schemas import ExecuteRequest, PipelineRequest

router = APIRouter()


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

@router.post("/execute")
def execute_proposals(body: ExecuteRequest):
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
            job_id = job_manager.start_execute_dry(
                session_id=sid,
                min_confidence=body.min_confidence,
            )
            return {"job_id": job_id, "status": "started"}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    counts = do_execute(
        dry_run=body.dry_run,
        min_confidence=body.min_confidence,
        session_id=sid,
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
        if all_file_ids:
            for fid, fname in session.query(File.id, File.filename).filter(
                File.id.in_(all_file_ids)
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
        job_id = job_manager.start_analyze(
            session_id=sid,
            backend=body.backend,
            model=model,
            workers=body.workers,
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
    """Cancel a running or paused job.

    Sets the cooperative cancel flag first, then immediately force-finishes
    the job so the UI reflects the cancellation and new jobs can start.
    The worker thread (if stuck on an Ollama call) will eventually exit
    on its own since it's a daemon thread.
    """
    from donedatahoarder.core.jobs import job_manager
    try:
        job_manager.force_cancel(job_id)
        return {"status": "cancelled", "job_id": job_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/pipeline/propose")
def trigger_propose(body: PipelineRequest = PipelineRequest()):
    """Start a propose background job. Returns job_id immediately."""
    from donedatahoarder.core.jobs import job_manager

    try:
        sid = _require_session_id(body.session_id)
        model = _resolve_model(body.model, sid, step="propose")
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

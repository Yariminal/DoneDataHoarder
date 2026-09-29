"""Real, session-scoped application services for the terminal interface.

All methods are synchronous and may perform disk/DB work: the UI calls them in
workers. Pipeline work belongs to JobManager, while apply/undo deliberately stay
inside one writer lease from confirmation validation through the last mutation.
No web server, HTTP routes, terminal rendering, or AI fallback is required here.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from donedatahoarder.core import job_store
from donedatahoarder.core.dependency_protection import cached_protection_index
from donedatahoarder.core.jobs import job_manager
from donedatahoarder.core.process_lock import operation_lock
from donedatahoarder.core.photo_quality import compare_photos, photo_evidence
from donedatahoarder.core.review import (
    ReviewError, change_keeper, duplicate_evidence, execution_preview, fingerprint,
    indexed_md5_match, owned_proposal, proposal_review_token, protected_reason,
    protection_index, require_session, stored_sha256_match, validated_edit,
    within_root,
)
from donedatahoarder.db.models import (
    BackgroundJob, DuplicateGroup, DuplicateMember, DupeType, File,
    Proposal, ProposalStatus, ProposalType, RelationGroup, RunPlan, SessionStatus,
    UserSession,
)
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.timeutils import utcnow

PIPELINE_STEPS = ("scan", "enrich", "analyze", "dedup", "relate", "propose",
                  "organize", "execute_dry")
METADATA_STEPS = ("scan", "enrich", "dedup", "execute_dry")


def _iso(value):
    return value.isoformat() if value else None


def _file_dict(file: File) -> dict:
    return {
        "id": file.id, "session_id": file.session_id, "path": file.path,
        "filename": file.filename, "mime_type": file.mime_type,
        "size_bytes": file.size_bytes, "extension": file.extension,
        "status": file.status.value, "date_modified": _iso(file.date_modified),
        "date_exif": _iso(file.date_exif), "ai_description": file.ai_description,
        "photo_metadata": photo_evidence(file),
        "text": file.ai_transcript or "", "tags": file.tags_list(),
        "analysis_outcome": file.analysis_outcome,
        "analysis_reason": file.analysis_reason,
        "analysis_evidence_source": file.analysis_evidence_source,
        "analysis_model_tag": file.analysis_model_tag,
        "error_message": file.error_message,
    }


def _session_dict(owner: UserSession) -> dict:
    return {
        "id": owner.id, "name": owner.name, "root_path": owner.root_path,
        "backend": owner.backend, "model": owner.model,
        "analyze_model": owner.analyze_model or owner.model,
        "propose_model": owner.propose_model or owner.model,
        "workers": owner.workers, "status": owner.status.value,
        "created_at": _iso(owner.created_at), "updated_at": _iso(owner.updated_at),
        "stats": owner.stats,
    }


class WorkspaceService:
    """One collection in the process's initialized SQLite database.

    Opening creates only a session record, never scans or changes collection
    files. Pass ``session_id`` to reopen existing work; its saved settings win.
    ``db_path`` is primarily useful to CLI callers and isolated tests.
    """

    def __init__(self, root: Path | None = None, session_id: str | None = None, *,
                 model: str = "gemma3:12b", workers: int = 1,
                 backend: str = "ollama", ollama_host: str = "http://localhost:11434",
                 db_path: Path | None = None):
        if backend != "ollama":
            raise ReviewError("The terminal interface uses the explicitly selected Ollama backend", 400)
        if not model.strip() or workers < 1:
            raise ReviewError("Choose a model and at least one worker", 400)
        if db_path is not None:
            db_path = Path(db_path).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            init_db(db_path)
        self.engine = get_engine()
        self.ollama_host = ollama_host
        self.database_path = Path(self.engine.url.database).resolve()
        if session_id:
            with Session(self.engine) as db:
                owner = require_session(db, session_id)
                if owner.backend != "ollama":
                    raise ReviewError("This session uses a cloud backend; open it in the existing CLI/web interface", 400)
                if root is not None and Path(root).expanduser().resolve() != Path(owner.root_path).resolve():
                    raise ReviewError("Folder differs from the saved session folder")
                self.session_id = owner.id
        else:
            if root is None:
                raise ReviewError("Choose a collection folder or an existing session", 400)
            chosen = self._validate_root(root)
            with operation_lock("create terminal session"), Session(self.engine) as db:
                owner = UserSession(
                    root_path=str(chosen), name=chosen.name or str(chosen),
                    backend="ollama", model=model, analyze_model=model,
                    propose_model=model, workers=workers, status=SessionStatus.NEW,
                )
                db.add(owner)
                db.commit()
                self.session_id = owner.id
        job_manager.reconcile_startup()

    @staticmethod
    def _validate_root(root: Path) -> Path:
        from donedatahoarder.core.scanner import _is_link_or_reparse

        root = Path(root).expanduser()
        if not root.is_dir() or _is_link_or_reparse(root):
            raise ReviewError("Collection folder is missing, unreadable, or a symlink/junction", 400)
        return root.resolve()

    def _check_database(self):
        if Path(get_engine().url.database).resolve() != self.database_path:
            raise RuntimeError("The active database changed; reopen this workspace")

    def _idle(self):
        self._check_database()
        if job_manager.get_active() is not None or job_manager.has_live_workers():
            raise ReviewError("Wait for the pipeline worker to exit before reviewing or changing files")

    def list_sessions(self, limit: int = 100) -> list[dict]:
        self._check_database()
        with Session(self.engine) as db:
            return [_session_dict(owner) for owner in db.query(UserSession)
                    .order_by(UserSession.updated_at.desc()).limit(max(1, min(limit, 1000)))]

    def get_file(self, file_id: int) -> dict:
        self._check_database()
        with Session(self.engine) as db:
            file = db.get(File, file_id)
            if file is None or file.session_id != self.session_id:
                raise ReviewError("File not found in this session", 404)
            return _file_dict(file)

    def snapshot(self, *, limit: int = 500, offset: int = 0,
                 filesystem_available: bool = True) -> dict:
        """Bounded rows plus complete counts; no inferred progress or telemetry."""
        self._check_database()
        limit, offset = max(1, min(limit, 2000)), max(0, offset)
        active = job_manager.get_active()
        own_active = active if active and active.session_id == self.session_id else None
        with Session(self.engine) as db:
            owner = require_session(db, self.session_id)
            files = db.query(File).filter(File.session_id == self.session_id)
            proposals = db.query(Proposal).join(File).filter(File.session_id == self.session_id)
            file_count, proposal_count = files.count(), proposals.count()
            index = (cached_protection_index(Path(owner.root_path))
                     if filesystem_available and Path(owner.root_path).is_dir() else None)
            file_rows = files.order_by(File.path, File.id).offset(offset).limit(limit).all()
            proposal_rows = (proposals.options(selectinload(Proposal.file)).order_by(Proposal.id)
                             .offset(offset).limit(limit).all())
            visible_proposals = []
            for proposal in proposal_rows:
                file = proposal.file
                reason = (protected_reason(proposal, file, index) if filesystem_available
                          else "Collection storage is unavailable")
                visible_proposals.append({
                    "id": proposal.id, "file_id": file.id, "filename": file.filename,
                    "file_path": file.path, "mime_type": file.mime_type,
                    "proposal_type": proposal.proposal_type.value,
                    "current_path": proposal.current_value, "proposed_path": proposal.proposed_value,
                    "status": proposal.status.value, "reasoning": proposal.reasoning,
                    "confidence": proposal.confidence, "review_kind": proposal.review_kind,
                    "protected": bool(reason), "protection_reason": reason,
                    "analysis_outcome": file.analysis_outcome,
                    "analysis_evidence_source": file.analysis_evidence_source,
                    "duplicate_evidence": duplicate_evidence(db, proposal, file) if filesystem_available else None,
                    "review_token": proposal_review_token(db, proposal, file) if filesystem_available else None,
                })
            relation_query = db.query(RelationGroup).filter(RelationGroup.session_id == self.session_id)
            collections = []
            for group in (relation_query.options(selectinload(RelationGroup.members))
                          .order_by(RelationGroup.id).offset(offset).limit(limit)):
                members = []
                for member in group.members:
                    file = db.get(File, member.file_id)
                    if file and file.session_id == self.session_id:
                        members.append({**_file_dict(file), "role": member.role.value})
                collections.append({"id": group.id, "label": group.label,
                                    "reason": group.reason, "confidence": group.confidence,
                                    "scope": group.scope, "members": members})
            duplicate_query = db.query(DuplicateGroup).filter(DuplicateGroup.session_id == self.session_id)
            # Comparisons follow the displayed rows, not an unrelated first
            # page of groups. One group per visible file guarantees a useful
            # pair while bounding group rows to at most twice the page size.
            visible_file_ids = {file.id for file in file_rows}
            visible_file_ids.update(proposal.file_id for proposal in proposal_rows)
            visible_group_ids = {
                group_id for _, group_id in (
                    db.query(DuplicateMember.file_id, func.min(DuplicateMember.group_id))
                    .join(DuplicateGroup, DuplicateMember.group_id == DuplicateGroup.id)
                    .filter(DuplicateGroup.session_id == self.session_id,
                            DuplicateMember.file_id.in_(visible_file_ids))
                    .group_by(DuplicateMember.file_id)
                )
            }
            duplicates = []
            for group in (duplicate_query.filter(DuplicateGroup.id.in_(visible_group_ids))
                          .options(selectinload(DuplicateGroup.members)).order_by(DuplicateGroup.id)):
                keeper = db.get(File, group.keep_file_id) if group.keep_file_id else None
                if keeper and keeper.session_id != self.session_id:
                    keeper = None
                members = []
                for member in group.members:
                    file = db.get(File, member.file_id)
                    if file and file.session_id == self.session_id:
                        members.append({**_file_dict(file),
                                        "similarity_score": member.similarity_score,
                                        "distance_to_keeper": member.distance_to_keeper,
                                        "exact_bytes": stored_sha256_match(file, keeper),
                                        "photo_quality": compare_photos(file, keeper) if keeper else None,
                                        "matching_indexed_md5": indexed_md5_match(file, keeper)})
                duplicates.append({"id": group.id, "type": group.dupe_type.value,
                                   "keep_file_id": keeper.id if keeper else None,
                                   "keeper_path": keeper.path if keeper else None,
                                   "members": members})
            plan = (db.query(RunPlan).filter(RunPlan.session_id == self.session_id)
                    .order_by(RunPlan.created_at.desc()).first())
            job = (db.query(BackgroundJob).filter(BackgroundJob.session_id == self.session_id)
                   .order_by(BackgroundJob.started_at.desc()).first())
            counts = {
                "files": file_count, "proposals": proposal_count,
                "collections": relation_query.count(), "duplicates": duplicate_query.count(),
                "file_statuses": {status.value: count for status, count in files.with_entities(File.status, func.count(File.id)).group_by(File.status)},
                "proposal_statuses": {status.value: count for status, count in proposals.with_entities(Proposal.status, func.count(Proposal.id)).group_by(Proposal.status)},
            }
            result = {
                "session": _session_dict(owner), "files": [_file_dict(file) for file in file_rows],
                "proposals": visible_proposals, "collections": collections, "duplicates": duplicates,
                "plan": job_store.plan_dict(plan) if plan else None,
                "job": job_store.job_dict(job) if job else None,
                "active_job": own_active.to_dict() if own_active else None,
                "other_session_busy": bool(active and own_active is None),
                "has_live_workers": job_manager.has_live_workers(),
                "owned_live_workers": job_manager.has_live_workers(self.session_id),
                "counts": counts,
                "page": {"limit": limit, "offset": offset,
                         "files_shown": len(file_rows), "proposals_shown": len(proposal_rows),
                         "collections_shown": len(collections), "duplicates_shown": len(duplicates),
                         "files_truncated": file_count > offset + len(file_rows),
                         "proposals_truncated": proposal_count > offset + len(proposal_rows),
                         "collections_truncated": counts["collections"] > offset + len(collections),
                         "duplicates_truncated": counts["duplicates"] > len(duplicates)},
            }
        result["history"] = self.history()
        return result

    def preflight(self, *, metadata_only: bool = False) -> dict:
        from donedatahoarder.core.preflight import estimate_collection

        with Session(self.engine) as db:
            root = require_session(db, self.session_id).root_path
        return estimate_collection(Path(root), mode="metadata_only" if metadata_only else "full")

    def update_settings(self, *, model: str | None = None,
                        workers: int | None = None) -> dict:
        """Change settings for the next run; an unfinished plan stays frozen."""
        if model is None and workers is None:
            raise ReviewError("Choose a model or worker count to update", 400)
        if model is not None and (not isinstance(model, str) or not model.strip()
                                  or len(model) > 200 or any(ord(char) < 32 for char in model)):
            raise ReviewError("Choose a non-empty model name of at most 200 characters", 400)
        if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                    or not 1 <= workers <= 32):
            raise ReviewError("Choose between 1 and 32 workers", 400)
        with operation_lock("update terminal session settings"), Session(self.engine) as db:
            self._idle()
            owner = require_session(db, self.session_id)
            latest = (db.query(RunPlan).filter(RunPlan.session_id == self.session_id)
                      .order_by(RunPlan.created_at.desc()).first())
            if latest is not None and latest.state != "completed":
                raise ReviewError("This session has an unfinished run plan. Resume it with its saved settings first")
            if model is not None:
                owner.model = owner.analyze_model = owner.propose_model = model.strip()
            if workers is not None:
                owner.workers = workers
            owner.updated_at = utcnow()
            owner.is_unsaved = True
            db.commit()
            return _session_dict(owner)

    def start_pipeline(self, *, metadata_only: bool = False) -> dict:
        self._check_database()
        with operation_lock("create terminal run plan"):
            self._idle()
            with Session(self.engine) as db:
                owner = require_session(db, self.session_id)
                root = self._validate_root(Path(owner.root_path))
                steps = METADATA_STEPS if metadata_only else PIPELINE_STEPS
                options = {
                    "root_path": str(root), "backend": "ollama", "ollama_host": self.ollama_host,
                    "analyze_model": owner.analyze_model or owner.model,
                    "propose_model": owner.propose_model or owner.model,
                    "workers": owner.workers, "relate_scope": owner.relate_scope,
                    "use_cache": True, "sequence_sample_stride": 0,
                    "mode": "metadata_only" if metadata_only else "full",
                    "skipped_steps": [step for step in PIPELINE_STEPS if step not in steps],
                }
            plan_id = job_manager.create_run_plan(self.session_id, list(steps), options)
        job_id = job_manager.advance_run_plan(plan_id)
        return {"plan": job_manager.get_run_plan(plan_id), "job_id": job_id}

    def _latest_plan(self) -> dict:
        self._check_database()
        job_manager.reconcile_startup()
        with Session(self.engine) as db:
            require_session(db, self.session_id)
            plan = (db.query(RunPlan).filter(RunPlan.session_id == self.session_id)
                    .order_by(RunPlan.created_at.desc()).first())
            if plan is None:
                raise ReviewError("No run plan exists for this session. Start a pipeline first", 400)
            return job_store.plan_dict(plan)

    def resume_pipeline(self, *, retry_errors: bool = False) -> dict:
        plan = self._latest_plan()
        if plan["options"].get("backend", "ollama") != "ollama":
            raise ReviewError("This saved plan uses a cloud backend; resume it in the existing CLI/web interface", 400)
        if plan["active_job_id"]:
            job = job_manager.get_job(plan["active_job_id"])
            if job is None or job.session_id != self.session_id:
                raise ReviewError("Active job no longer belongs to this session")
            try:
                job_manager.resume(job.job_id)
            except KeyError as exc:
                raise ReviewError("Resume this job in the process that owns it; remote cancellation is available") from exc
            job_id = job.job_id
        elif plan["state"] == "ready":
            job_id = job_manager.advance_run_plan(plan["plan_id"])
        else:
            job_id = job_manager.resume_run_plan(plan["plan_id"], retry_errors=retry_errors)
        return {"plan": job_manager.get_run_plan(plan["plan_id"]), "job_id": job_id}

    def pause_pipeline(self) -> None:
        plan = self._latest_plan()
        if not plan["active_job_id"]:
            raise ReviewError("There is no running job to pause")
        job = job_manager.get_job(plan["active_job_id"])
        if job is None or job.session_id != self.session_id:
            raise ReviewError("Active job no longer belongs to this session")
        # Scan's callback currently supports cancellation, not pausing.
        if job.job_type == "scan":
            raise ReviewError("Scanning cannot pause; cancel it and resume the saved plan")
        try:
            job_manager.pause(job.job_id)
        except KeyError as exc:
            raise ReviewError("Pause this job in its owning process") from exc

    def cancel_pipeline(self) -> None:
        plan = self._latest_plan()
        job_manager.cancel_run_plan(plan["plan_id"])

    def _reviewable(self, db: Session, proposal_id: int, review_token: str | None = None):
        proposal, file, owner = owned_proposal(db, proposal_id, self.session_id)
        if proposal.status == ProposalStatus.APPLIED:
            raise ReviewError("Applied proposals cannot be reviewed again")
        if review_token and review_token != proposal_review_token(db, proposal, file):
            raise ReviewError("Proposal or comparison changed; review it again")
        return proposal, file, owner

    def approve(self, proposal_id: int, review_token: str | None = None) -> dict:
        with operation_lock("approve terminal proposal"), Session(self.engine) as db:
            self._idle()
            proposal, file, owner = self._reviewable(db, proposal_id, review_token)
            reason = protected_reason(proposal, file, protection_index(owner))
            if reason:
                raise ReviewError(f"Protected resource: {reason}")
            if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                evidence = duplicate_evidence(db, proposal, file)
                if (not evidence or not evidence["keeper_id"]
                        or file.id not in evidence["member_ids"]
                        or evidence["keeper_id"] not in evidence["member_ids"]
                        or proposal.current_value != file.path
                        or proposal.proposed_value != evidence["keeper_path"]):
                    raise ReviewError("Duplicate comparison changed; review this pair again")
                if evidence["type"] != "exact" and not review_token:
                    raise ReviewError("Inspect the candidate and keeper before individually approving a near match")
            proposal.status = ProposalStatus.APPROVED
            proposal.review_kind = "individual"
            db.commit()
        return {"id": proposal_id, "status": "approved"}

    def reject(self, proposal_id: int) -> dict:
        with operation_lock("reject terminal proposal"), Session(self.engine) as db:
            self._idle()
            proposal, _, _ = self._reviewable(db, proposal_id)
            proposal.status = ProposalStatus.REJECTED
            db.commit()
        return {"id": proposal_id, "status": "rejected"}

    def set_keeper(self, group_id: int, file_id: int,
                   expected_keeper_id: int | None = None) -> dict:
        with operation_lock("change terminal duplicate keeper"), Session(self.engine) as db:
            self._idle()
            result = change_keeper(db, self.session_id, group_id, file_id,
                                   expected_keeper_id=expected_keeper_id)
            db.commit()
        return result

    def edit(self, proposal_id: int, value: str) -> dict:
        with operation_lock("edit terminal proposal"), Session(self.engine) as db:
            self._idle()
            proposal, file, owner = self._reviewable(db, proposal_id)
            reason = protected_reason(proposal, file, protection_index(owner))
            if reason:
                raise ReviewError(f"Protected resource: {reason}")
            if not owner.root_path:
                raise ReviewError("Proposal has no session folder", 400)
            proposed = validated_edit(proposal, file, Path(owner.root_path), value)
            proposal.proposed_value = proposed
            proposal.status = ProposalStatus.MODIFIED
            proposal.review_kind = "individual"
            db.commit()
        return {"id": proposal_id, "status": "modified", "proposed_value": proposed}

    def approve_clear(self, min_confidence: float = 0.9) -> dict:
        """Bulk approval preserves rejection and skips nonexact/unknown evidence."""
        from donedatahoarder.executor import _validate_operation_paths

        counts = {"approved": 0, "skipped_protected": 0, "skipped_near_duplicate": 0,
                  "skipped_unverified_rename": 0, "skipped_invalid": 0}
        with operation_lock("bulk terminal approval"), Session(self.engine) as db:
            self._idle()
            owner = require_session(db, self.session_id)
            index = protection_index(owner)
            query = (db.query(Proposal).join(File).filter(
                File.session_id == self.session_id, Proposal.status == ProposalStatus.PENDING,
                Proposal.confidence >= min_confidence))
            for proposal in query:
                file = db.get(File, proposal.file_id)
                if protected_reason(proposal, file, index):
                    counts["skipped_protected"] += 1
                    continue
                if proposal.proposal_type == ProposalType.RENAME and not (
                    file.analysis_outcome == "content_verified"
                    and file.analysis_evidence_source in {"text", "vision"}
                ):
                    counts["skipped_unverified_rename"] += 1
                    continue
                if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                    group = db.get(DuplicateGroup, proposal.duplicate_group_id) if proposal.duplicate_group_id else None
                    if not group or group.session_id != self.session_id or group.dupe_type != DupeType.EXACT:
                        counts["skipped_near_duplicate"] += 1
                        continue
                try:
                    _validate_operation_paths(proposal, Path(owner.root_path), file, index)
                except (ValueError, OSError):
                    counts["skipped_invalid"] += 1
                    continue
                proposal.status = ProposalStatus.APPROVED
                proposal.review_kind = "bulk"
                counts["approved"] += 1
            db.commit()
        return counts

    def preview(self) -> dict:
        with operation_lock("preview terminal execution"):
            self._idle()
            return execution_preview(self.session_id)

    def apply(self, token: str, *, confirmed: bool = False) -> dict:
        if not confirmed:
            raise ReviewError("Confirm the displayed preview before applying files", 400)
        from donedatahoarder.executor import execute, _make_quiet_console

        with operation_lock("commit terminal review"):
            self._idle()
            preview = execution_preview(self.session_id)
            if not token or token != preview["token"]:
                raise ReviewError("Reviewed changes have changed. Preview again before committing")
            if not preview["items"]:
                raise ReviewError("No approved or edited proposals to commit", 400)
            if preview["errors"]:
                raise ReviewError("Preview has failing actions. Resolve or reject them before committing")
            counts = execute(dry_run=False, session_id=self.session_id,
                             proposal_ids=[item["id"] for item in preview["items"]],
                             _console=_make_quiet_console())
            with Session(self.engine) as db:
                owner = require_session(db, self.session_id)
                owner.is_unsaved = True
                owner.updated_at = utcnow()
                if not counts.get("failed"):
                    stats = owner.stats
                    stats["completed_steps"] = list(dict.fromkeys([*stats.get("completed_steps", []), "execute"]))
                    owner.stats = stats
                db.commit()
            return counts

    def history(self, limit: int = 100) -> list[dict]:
        from donedatahoarder.core.undo_log import _entry_id, get_last_session_entries, parse_undo_log

        self._check_database()
        outstanding = get_last_session_entries(self.session_id)
        pending_ids = {_entry_id(entry) for entry in outstanding}
        events = parse_undo_log(self.session_id)
        result = []
        for event in events:
            if event.get("session_id") != self.session_id or "original_path" not in event:
                continue
            normalized = dict(event)
            normalized.setdefault("phase", "complete")
            identity = _entry_id(normalized)
            result.append({"id": identity, "operation": event["operation"],
                           "source": event["original_path"], "destination": event["new_path"],
                           "timestamp": event.get("timestamp"),
                           "state": "outstanding" if identity in pending_ids else "undone",
                           "undoable": identity in pending_ids})
        return list(reversed(result[-max(1, min(limit, 1000)):]))

    def undo_preview(self) -> dict:
        from donedatahoarder.core.undo_log import get_last_session_entries

        with operation_lock("preview terminal recovery"):
            self._idle()
            with Session(self.engine) as db:
                owner = require_session(db, self.session_id)
                root = Path(owner.root_path)
            entries = get_last_session_entries(self.session_id)
            items = [{"id": entry.get("operation_id"), "type": entry["operation"],
                      "source": entry["new_path"], "destination": entry["original_path"],
                      "timestamp": entry.get("timestamp"), "phase": entry.get("phase"),
                      "error": None if within_root(Path(entry["original_path"]), root)
                      and within_root(Path(entry["new_path"]), root) else "Recovery path is outside the collection"}
                     for entry in reversed(entries)]
            return {"session_id": self.session_id, "items": items, "total": len(items),
                    "errors": sum(bool(item["error"]) for item in items),
                    "token": fingerprint([str(self.database_path), self.session_id, entries])}

    def undo(self, token: str, *, confirmed: bool = False) -> dict:
        if not confirmed:
            raise ReviewError("Confirm the displayed recovery plan before undoing files", 400)
        from donedatahoarder.core.undo_log import undo_operations
        from donedatahoarder.executor import _make_quiet_console

        with operation_lock("recover terminal changes"):
            self._idle()
            preview = self.undo_preview()
            if not token or token != preview["token"]:
                raise ReviewError("Recovery journal changed. Preview it again")
            if preview["errors"]:
                raise ReviewError("Recovery preview contains paths outside this collection")
            return undo_operations(session_id=self.session_id, force=True,
                                   console=_make_quiet_console())


TuiService = WorkspaceService

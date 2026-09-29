"""
Proposal review endpoints (list / approve / reject / edit / bulk ops).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from donedatahoarder.core.dependency_protection import ProtectionIndex, cached_protection_index
from donedatahoarder.core.dedup import sequence_comparison_metadata
from donedatahoarder.core.photo_quality import compare_photos, photo_evidence
from donedatahoarder.core.review import proposal_review_token
from donedatahoarder.db.models import (
    DupeType, DuplicateGroup, File, Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import get_engine

from .deps import review_operation
from .duplicate_evidence import indexed_md5_match, stored_sha256_match
from .schemas import BulkApproveRequest, BulkRejectRequest, EditProposalRequest, ReviewProposalRequest

router = APIRouter()


def _require_session(session: Session, session_id: str) -> UserSession:
    if not session_id or not session_id.strip():
        raise HTTPException(400, "An active session is required")
    user_session = session.get(UserSession, session_id)
    if user_session is None:
        raise HTTPException(404, "Session not found")
    return user_session


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def _validated_edit(proposal: Proposal, file: File, root: Path, value: str) -> str:
    from donedatahoarder.core.review import ReviewError, validated_edit

    try:
        return validated_edit(proposal, file, root, value)
    except ReviewError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def _owned_proposal(session: Session, proposal_id: int, session_id: str) -> tuple[Proposal, File, UserSession]:
    user_session = _require_session(session, session_id)
    proposal = session.get(Proposal, proposal_id)
    file = session.get(File, proposal.file_id) if proposal else None
    if file is None or file.session_id != session_id:
        raise HTTPException(404, "Proposal not found in this session")
    return proposal, file, user_session


def _protection_index(user_session: UserSession) -> ProtectionIndex | None:
    root = Path(user_session.root_path) if user_session.root_path else None
    return ProtectionIndex(root) if root and root.is_dir() else None


def _protected_reason(proposal: Proposal, file: File, index: ProtectionIndex | None) -> str | None:
    if index is None or proposal.proposal_type not in {
        ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER,
        ProposalType.MARK_DUPLICATE,
    }:
        return None
    decision = index.assess(Path(proposal.current_value or file.path))
    return decision.reason if decision.protected else None


@router.get("/proposals")
def list_proposals(
    status: Optional[str] = None,
    proposal_type: Optional[str] = None,
    min_confidence: float = 0.0,
    search: Optional[str] = None,
    sort: str = "confidence",
    order: str = "desc",
    page: int = 1,
    per_page: int = 50,
    session_id: Optional[str] = None,
):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(Proposal).join(File)
        if session_id:
            query = query.filter(File.session_id == session_id)

        if status:
            try:
                query = query.filter(Proposal.status == ProposalStatus(status))
            except ValueError:
                pass
        else:
            # Default: show pending
            query = query.filter(Proposal.status == ProposalStatus.PENDING)

        if proposal_type:
            try:
                query = query.filter(Proposal.proposal_type == ProposalType(proposal_type))
            except ValueError:
                pass
        if min_confidence > 0:
            query = query.filter(Proposal.confidence >= min_confidence)
        if search:
            term = f"%{search}%"
            query = query.filter(
                File.filename.ilike(term)
                | Proposal.proposed_value.ilike(term)
                | Proposal.reasoning.ilike(term)
            )

        # Sort
        if sort == "confidence":
            sort_col = Proposal.confidence.desc() if order == "desc" else Proposal.confidence
        elif sort == "filename":
            sort_col = File.filename.desc() if order == "desc" else File.filename
        else:
            sort_col = Proposal.id.desc() if order == "desc" else Proposal.id
        query = query.order_by(sort_col)

        total = query.count()
        proposals = query.offset((page - 1) * per_page).limit(per_page).all()

        items = []
        protection_by_session: dict[str, ProtectionIndex | None] = {}
        for p in proposals:
            f = session.get(File, p.file_id)
            if f and f.session_id not in protection_by_session:
                owner = session.get(UserSession, f.session_id)
                root = Path(owner.root_path) if owner and owner.root_path else None
                protection_by_session[f.session_id] = cached_protection_index(root) if root and root.is_dir() else None
            protection = protection_by_session.get(f.session_id) if f else None
            target = Path(p.current_value or f.path) if f else None
            decision = protection.assess(target) if protection and target else None
            current_name = Path(p.current_value).name if p.current_value else (f.filename if f else "")
            proposed_name = Path(p.proposed_value).name if p.proposed_value and p.proposal_type == ProposalType.RENAME else p.proposed_value
            name_date_source = None
            if f and p.proposal_type == ProposalType.RENAME and p.proposed_value:
                from donedatahoarder.proposals.namer.naming import (
                    _is_meaningful_date, _source_filename_date,
                )
                proposed_date = _source_filename_date(Path(p.proposed_value).stem)
                if proposed_date:
                    original_date = _source_filename_date(Path(f.path).stem)
                    if original_date == proposed_date:
                        name_date_source = "Original filename date identifier; event date unverified"
                    elif (_is_meaningful_date(f)
                          and f.date_exif.strftime("%Y-%m-%d") == proposed_date):
                        name_date_source = "Stored photo EXIF metadata; capture date unverified"
                    elif (f.date_modified
                          and f.date_modified.strftime("%Y-%m-%d") == proposed_date):
                        name_date_source = "Matches filesystem modified date; event date unverified"
                    else:
                        name_date_source = "Unverified date in proposed name"
            duplicate_evidence = None
            group_id = getattr(p, "duplicate_group_id", None)
            if f and p.proposal_type == ProposalType.MARK_DUPLICATE and group_id:
                group = session.get(DuplicateGroup, group_id)
                if group and group.session_id == f.session_id:
                    keeper = session.get(File, group.keep_file_id) if group.keep_file_id else None
                    if keeper and keeper.session_id != f.session_id:
                        keeper = None
                    membership = next((member for member in group.members if member.file_id == f.id), None)
                    duplicate_evidence = {
                        "group_id": group.id,
                        "type": group.dupe_type.value,
                        "keeper_id": keeper.id if keeper else None,
                        "keeper_path": keeper.path if keeper else None,
                        "keeper_mime_type": keeper.mime_type if keeper else None,
                        "keeper_size_bytes": keeper.size_bytes if keeper else None,
                        "keeper_description": keeper.ai_description if keeper else None,
                        "keeper_analysis_outcome": getattr(keeper, "analysis_outcome", None) if keeper else None,
                        "exact_bytes": stored_sha256_match(f, keeper),
                        "matching_indexed_md5": indexed_md5_match(f, keeper),
                        "similarity_score": membership.similarity_score if membership else None,
                        "distance_to_keeper": getattr(membership, "distance_to_keeper", None) if membership else None,
                        "perceptual_bits": len(f.hash_perceptual) * 4 if f.hash_perceptual else None,
                        "sequence_comparison": sequence_comparison_metadata(f, keeper),
                        "photo_quality": compare_photos(f, keeper) if keeper else None,
                    }
            items.append({
                "id": p.id,
                "file_id": p.file_id,
                "filename": f.filename if f else "",
                "file_path": f.path if f else "",
                "proposal_type": p.proposal_type.value,
                "current_value": current_name,
                "proposed_value": proposed_name,
                "current_path": p.current_value,
                "proposed_path": p.proposed_value,
                "reasoning": p.reasoning,
                "confidence": p.confidence,
                "status": p.status.value,
                "mime_type": f.mime_type if f else None,
                "ai_description": f.ai_description if f else None,
                "analysis_outcome": getattr(f, "analysis_outcome", None) if f else None,
                "analysis_reason": getattr(f, "analysis_reason", None) if f else None,
                "analysis_evidence_source": getattr(f, "analysis_evidence_source", None) if f else None,
                "analysis_model_tag": getattr(f, "analysis_model_tag", None) if f else None,
                "name_date_source": name_date_source,
                "duplicate_evidence": duplicate_evidence,
                "photo_metadata": photo_evidence(f) if f else None,
                "review_token": proposal_review_token(session, p, f) if f else None,
                "review_kind": getattr(p, "review_kind", None),
                "protected": bool(decision and decision.protected),
                "protection_reason": decision.reason if decision else None,
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, body: ReviewProposalRequest):
    with review_operation("approve proposal"):
        engine = get_engine()
        with Session(engine) as session:
            p, file, user_session = _owned_proposal(session, proposal_id, body.session_id)
            if (body.expected_review_token is not None
                    and body.expected_review_token != proposal_review_token(session, p, file)):
                raise HTTPException(409, "Proposal or photo evidence changed; review this pair again")
            if p.status == ProposalStatus.APPLIED:
                raise HTTPException(409, "Applied proposals cannot be reviewed again")
            protected = _protected_reason(p, file, _protection_index(user_session))
            if protected:
                raise HTTPException(409, f"Protected resource: {protected}")
            if p.proposal_type == ProposalType.MARK_DUPLICATE:
                group = session.get(DuplicateGroup, p.duplicate_group_id) if p.duplicate_group_id else None
                if group is None or group.session_id != file.session_id:
                    raise HTTPException(409, "Duplicate evidence group changed")
                if group.dupe_type != DupeType.EXACT:
                    keeper = session.get(File, group.keep_file_id) if group.keep_file_id else None
                    if (compare_photos(file, keeper) is not None
                            and not body.expected_review_token):
                        raise HTTPException(409, "Inspect current photo evidence before approving this pair")
                    if (keeper is None or keeper.session_id != file.session_id
                            or body.expected_duplicate_group_id != group.id
                            or body.expected_duplicate_type != group.dupe_type.value
                            or body.expected_keeper_id != keeper.id
                            or body.expected_candidate_path != file.path
                            or body.expected_keeper_path != keeper.path
                            or p.current_value != file.path or p.proposed_value != keeper.path):
                        raise HTTPException(409, "Duplicate comparison changed; review this pair again")
            p.status = ProposalStatus.APPROVED
            p.review_kind = "individual"
            session.commit()
    return {"status": "approved", "id": proposal_id}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, body: ReviewProposalRequest):
    with review_operation("reject proposal"):
        engine = get_engine()
        with Session(engine) as session:
            p, _, _ = _owned_proposal(session, proposal_id, body.session_id)
            if p.status == ProposalStatus.APPLIED:
                raise HTTPException(409, "Applied proposals cannot be reviewed again")
            p.status = ProposalStatus.REJECTED
            session.commit()
    return {"status": "rejected", "id": proposal_id}


@router.post("/proposals/{proposal_id}/edit")
def edit_proposal(proposal_id: int, body: EditProposalRequest):
    with review_operation("edit proposal"):
        engine = get_engine()
        with Session(engine) as session:
            p, file, user_session = _owned_proposal(session, proposal_id, body.session_id)
            if p.status == ProposalStatus.APPLIED:
                raise HTTPException(409, "Applied proposals cannot be edited")
            if not user_session.root_path:
                raise HTTPException(400, "Proposal has no session folder")
            protected = _protected_reason(p, file, _protection_index(user_session))
            if protected:
                raise HTTPException(409, f"Protected resource: {protected}")
            p.proposed_value = _validated_edit(p, file, Path(user_session.root_path), body.proposed_value)

            p.status = ProposalStatus.MODIFIED
            p.review_kind = "individual"
            proposed_value = p.proposed_value
            session.commit()
    return {"status": "modified", "id": proposal_id, "proposed_value": proposed_value}


@router.post("/proposals/bulk-approve")
def bulk_approve(body: BulkApproveRequest):
    with review_operation("bulk approve"):
        engine = get_engine()
        with Session(engine) as session:
            user_session = _require_session(session, body.session_id)
            index = _protection_index(user_session)
            file_ids = session.query(File.id).filter(File.session_id == body.session_id)
            query = session.query(Proposal).filter(
                Proposal.file_id.in_(file_ids),
                Proposal.status == ProposalStatus.PENDING,
                Proposal.confidence >= body.min_confidence,
            )
            if body.proposal_type:
                try:
                    query = query.filter(Proposal.proposal_type == ProposalType(body.proposal_type))
                except ValueError:
                    pass
            count = 0
            skipped_protected = 0
            skipped_near_duplicate = 0
            skipped_unverified_rename = 0
            for proposal in query.all():
                file = session.get(File, proposal.file_id)
                if file is None:
                    continue
                reason = _protected_reason(proposal, file, index)
                if reason:
                    skipped_protected += 1
                    continue
                if proposal.proposal_type == ProposalType.RENAME and not (
                    file.analysis_outcome == "content_verified"
                    and file.analysis_evidence_source in {"text", "vision"}
                ):
                    # Legacy NULLs are unknown, not migrated proof of content.
                    # A user may still inspect and approve one rename explicitly.
                    skipped_unverified_rename += 1
                    continue
                if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                    group = session.get(DuplicateGroup, proposal.duplicate_group_id) if proposal.duplicate_group_id else None
                    if group is None or group.dupe_type.value != "exact":
                        skipped_near_duplicate += 1
                        continue
                proposal.status = ProposalStatus.APPROVED
                proposal.review_kind = "bulk"
                count += 1
            session.commit()
    return {"approved": count, "skipped_protected": skipped_protected,
            "skipped_near_duplicate": skipped_near_duplicate,
            "skipped_unverified_rename": skipped_unverified_rename}


@router.post("/proposals/bulk-reject")
def bulk_reject(body: BulkRejectRequest):
    with review_operation("bulk reject"):
        engine = get_engine()
        with Session(engine) as session:
            _require_session(session, body.session_id)
            file_ids = session.query(File.id).filter(File.session_id == body.session_id)
            count = (
                session.query(Proposal)
                .filter(Proposal.file_id.in_(file_ids), Proposal.status == ProposalStatus.PENDING)
                .update({"status": ProposalStatus.REJECTED})
            )
            session.commit()
    return {"rejected": count}

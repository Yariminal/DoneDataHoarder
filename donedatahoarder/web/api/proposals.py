"""
Proposal review endpoints (list / approve / reject / edit / bulk ops).
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.db.models import (
    DuplicateGroup, File, Proposal, ProposalStatus, ProposalType, UserSession,
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
    if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
        raise HTTPException(400, "Choose a duplicate keeper in the duplicate review instead")
    value = value.strip()
    if not value or "\x00" in value:
        raise HTTPException(400, "A non-empty destination is required")
    if proposal.proposal_type == ProposalType.RENAME:
        # A rename changes only the leaf name, never the parent directory.
        if value in (".", "..") or Path(value).name != value or PureWindowsPath(value).name != value or ":" in value:
            raise HTTPException(400, "Enter a filename without directories or drive letters")
        source = Path(proposal.current_value or file.path)
        if not _within_root(source, root):
            raise HTTPException(400, "Source is outside the session folder")
        return str(source.parent / value)
    if proposal.proposal_type in (ProposalType.MOVE, ProposalType.RENAME_FOLDER):
        destination = Path(value)
        source = Path(proposal.current_value or file.path)
        if not destination.is_absolute() or not _within_root(destination, root):
            raise HTTPException(400, "Destination must be inside the session folder")
        if not _within_root(source, root):
            raise HTTPException(400, "Source is outside the session folder")
        return str(destination.resolve(strict=False))
    return value


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
                protection_by_session[f.session_id] = ProtectionIndex(root) if root and root.is_dir() else None
            protection = protection_by_session.get(f.session_id) if f else None
            target = Path(p.current_value or f.path) if f else None
            decision = protection.assess(target) if protection and target else None
            current_name = Path(p.current_value).name if p.current_value else (f.filename if f else "")
            proposed_name = Path(p.proposed_value).name if p.proposed_value and p.proposal_type == ProposalType.RENAME else p.proposed_value
            duplicate_evidence = None
            group_id = getattr(p, "duplicate_group_id", None)
            if f and p.proposal_type == ProposalType.MARK_DUPLICATE and group_id:
                group = session.get(DuplicateGroup, group_id)
                if group and group.session_id == f.session_id:
                    keeper = session.get(File, group.keep_file_id) if group.keep_file_id else None
                    membership = next((member for member in group.members if member.file_id == f.id), None)
                    duplicate_evidence = {
                        "group_id": group.id,
                        "type": group.dupe_type.value,
                        "keeper_id": keeper.id if keeper else None,
                        "keeper_path": keeper.path if keeper else None,
                        "keeper_description": keeper.ai_description if keeper else None,
                        "keeper_analysis_outcome": getattr(keeper, "analysis_outcome", None) if keeper else None,
                        "exact_bytes": stored_sha256_match(f, keeper),
                        "matching_indexed_md5": indexed_md5_match(f, keeper),
                        "similarity_score": membership.similarity_score if membership else None,
                        "distance_to_keeper": getattr(membership, "distance_to_keeper", None) if membership else None,
                        "perceptual_bits": len(f.hash_perceptual) * 4 if f.hash_perceptual else None,
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
                "duplicate_evidence": duplicate_evidence,
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
            if p.status == ProposalStatus.APPLIED:
                raise HTTPException(409, "Applied proposals cannot be reviewed again")
            protected = _protected_reason(p, file, _protection_index(user_session))
            if protected:
                raise HTTPException(409, f"Protected resource: {protected}")
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

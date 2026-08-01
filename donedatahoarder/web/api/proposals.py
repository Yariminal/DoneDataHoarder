"""
Proposal review endpoints (list / approve / reject / edit / bulk ops).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType
from donedatahoarder.db.session import get_engine

from .schemas import BulkApproveRequest, EditProposalRequest

router = APIRouter()


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
        for p in proposals:
            f = session.get(File, p.file_id)
            current_name = Path(p.current_value).name if p.current_value else (f.filename if f else "")
            proposed_name = Path(p.proposed_value).name if p.proposed_value and p.proposal_type == ProposalType.RENAME else p.proposed_value
            items.append({
                "id": p.id,
                "file_id": p.file_id,
                "filename": f.filename if f else "",
                "proposal_type": p.proposal_type.value,
                "current_value": current_name,
                "proposed_value": proposed_name,
                "reasoning": p.reasoning,
                "confidence": p.confidence,
                "status": p.status.value,
                "mime_type": f.mime_type if f else None,
            })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")
        p.status = ProposalStatus.APPROVED
        session.commit()
    return {"status": "approved", "id": proposal_id}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")
        p.status = ProposalStatus.REJECTED
        session.commit()
    return {"status": "rejected", "id": proposal_id}


@router.post("/proposals/{proposal_id}/edit")
def edit_proposal(proposal_id: int, body: EditProposalRequest):
    engine = get_engine()
    with Session(engine) as session:
        p = session.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, "Proposal not found")

        if p.proposal_type == ProposalType.RENAME and p.current_value:
            # Replace just the filename, keep the directory
            old_dir = str(Path(p.current_value).parent)
            p.proposed_value = str(Path(old_dir) / body.proposed_value)
        else:
            p.proposed_value = body.proposed_value

        p.status = ProposalStatus.MODIFIED
        session.commit()
    return {"status": "modified", "id": proposal_id, "proposed_value": p.proposed_value}


@router.post("/proposals/bulk-approve")
def bulk_approve(body: BulkApproveRequest):
    engine = get_engine()
    with Session(engine) as session:
        query = session.query(Proposal).filter(
            Proposal.status == ProposalStatus.PENDING,
            Proposal.confidence >= body.min_confidence,
        )
        if body.proposal_type:
            try:
                query = query.filter(Proposal.proposal_type == ProposalType(body.proposal_type))
            except ValueError:
                pass
        count = query.update({"status": ProposalStatus.APPROVED})
        session.commit()
    return {"approved": count}


@router.post("/proposals/bulk-reject")
def bulk_reject():
    engine = get_engine()
    with Session(engine) as session:
        count = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .update({"status": ProposalStatus.REJECTED})
        )
        session.commit()
    return {"rejected": count}

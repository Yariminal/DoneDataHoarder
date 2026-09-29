"""Transport-independent review validation and execution previews.

Both the browser and terminal use these rules. Callers hold ``operation_lock``
while validating a mutation or binding an execution preview to its commit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath

from sqlalchemy.orm import Session

from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, File, Proposal, ProposalType, UserSession,
)
from donedatahoarder.db.session import get_engine


class ReviewError(ValueError):
    """A review or recovery decision is invalid or needs to be refreshed."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def require_session(db: Session, session_id: str) -> UserSession:
    if not session_id or not session_id.strip():
        raise ReviewError("An active session is required", 400)
    owner = db.get(UserSession, session_id)
    if owner is None:
        raise ReviewError("Session not found", 404)
    return owner


def owned_proposal(db: Session, proposal_id: int, session_id: str):
    owner = require_session(db, session_id)
    proposal = db.get(Proposal, proposal_id)
    file = db.get(File, proposal.file_id) if proposal else None
    if file is None or file.session_id != session_id:
        raise ReviewError("Proposal not found in this session", 404)
    return proposal, file, owner


def within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def protection_index(owner: UserSession) -> ProtectionIndex | None:
    root = Path(owner.root_path) if owner.root_path else None
    return ProtectionIndex(root) if root and root.is_dir() else None


def protected_reason(proposal: Proposal, file: File,
                     index: ProtectionIndex | None) -> str | None:
    if index is None or proposal.proposal_type not in {
        ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER,
        ProposalType.MARK_DUPLICATE,
    }:
        return None
    decision = index.assess(Path(proposal.current_value or file.path))
    return decision.reason if decision.protected else None


def validated_edit(proposal: Proposal, file: File, root: Path, value: str) -> str:
    """Validate the same filename/destination rules for every presentation."""
    if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
        raise ReviewError("Choose a duplicate keeper in the duplicate review instead", 400)
    value = value.strip()
    if not value or "\x00" in value:
        raise ReviewError("A non-empty destination is required", 400)
    if proposal.proposal_type == ProposalType.RENAME:
        if (value in (".", "..") or Path(value).name != value
                or PureWindowsPath(value).name != value or ":" in value):
            raise ReviewError("Enter a filename without directories or drive letters", 400)
        source = Path(proposal.current_value or file.path)
        if not within_root(source, root):
            raise ReviewError("Source is outside the session folder", 400)
        return str(source.parent / value)
    if proposal.proposal_type in (ProposalType.MOVE, ProposalType.RENAME_FOLDER):
        destination = Path(value)
        source = Path(proposal.current_value or file.path)
        if not destination.is_absolute() or not within_root(destination, root):
            raise ReviewError("Destination must be inside the session folder", 400)
        if not within_root(source, root):
            raise ReviewError("Source is outside the session folder", 400)
        return str(destination.resolve(strict=False))
    return value


def stored_sha256_match(candidate: File, keeper: File | None) -> bool | None:
    if keeper is None or not candidate.hash_sha256 or not keeper.hash_sha256:
        return None
    return candidate.hash_sha256 == keeper.hash_sha256


def indexed_md5_match(candidate: File, keeper: File | None) -> bool | None:
    if keeper is None or not candidate.hash_md5 or not keeper.hash_md5:
        return None
    return candidate.hash_md5 == keeper.hash_md5


def duplicate_evidence(db: Session, proposal: Proposal, file: File) -> dict | None:
    if proposal.proposal_type != ProposalType.MARK_DUPLICATE:
        return None
    group = db.get(DuplicateGroup, proposal.duplicate_group_id) if proposal.duplicate_group_id else None
    if group is None or group.session_id != file.session_id:
        return None
    keeper = db.get(File, group.keep_file_id) if group.keep_file_id else None
    if keeper and keeper.session_id != file.session_id:
        keeper = None
    membership = next((member for member in group.members if member.file_id == file.id), None)
    from donedatahoarder.core.dedup import sequence_comparison_metadata

    return {
        "group_id": group.id, "type": group.dupe_type.value,
        "candidate_id": file.id, "candidate_path": file.path,
        "keeper_id": keeper.id if keeper else None,
        "keeper_path": keeper.path if keeper else None,
        "keeper_mime_type": keeper.mime_type if keeper else None,
        "keeper_size_bytes": keeper.size_bytes if keeper else None,
        "keeper_description": keeper.ai_description if keeper else None,
        "exact_bytes": stored_sha256_match(file, keeper),
        "matching_indexed_md5": indexed_md5_match(file, keeper),
        "similarity_score": membership.similarity_score if membership else None,
        "distance_to_keeper": membership.distance_to_keeper if membership else None,
        "sequence_comparison": sequence_comparison_metadata(file, keeper),
        "member_ids": sorted(member.file_id for member in group.members),
    }


def proposal_review_token(db: Session, proposal: Proposal, file: File) -> str:
    """Bind a displayed decision to its values, owner and duplicate evidence."""
    return fingerprint([
        file.session_id, proposal.id, proposal.proposal_type.value,
        proposal.status.value, proposal.current_value, proposal.proposed_value,
        file.path, file.hash_sha256, file.hash_md5, file.size_bytes,
        duplicate_evidence(db, proposal, file),
    ])


def execution_preview(session_id: str) -> dict:
    """Return the exact reviewed selection, projected paths, errors and token.

    The caller holds the writer lease; commits repeat this operation under that
    same lease and compare tokens before executing any filesystem mutation.
    """
    from donedatahoarder.executor import plan_execution, select_executable_proposals

    with Session(get_engine()) as db:
        owner = require_session(db, session_id)
        proposals = select_executable_proposals(db, session_id=session_id)
        planned = plan_execution(db, proposals, Path(owner.root_path)) if proposals else []
        by_id = {proposal.id: proposal for proposal in proposals}
        items = []
        for step in planned:
            proposal = by_id[step.proposal_id]
            file = db.get(File, proposal.file_id)
            item = {
                "id": proposal.id, "type": proposal.proposal_type.value,
                "status": proposal.status.value,
                "source": step.source or (file.path if file else ""),
                "destination": step.destination or "", "keeper": step.keeper or "",
                "error": step.error, "confidence": proposal.confidence,
            }
            if proposal.proposal_type == ProposalType.MARK_DUPLICATE:
                groups = (db.query(DuplicateGroup)
                          .join(DuplicateMember, DuplicateMember.group_id == DuplicateGroup.id)
                          .filter(DuplicateMember.file_id == proposal.file_id,
                                  DuplicateGroup.session_id == session_id).all())
                item["keeper_groups"] = sorted((group.id, group.keep_file_id) for group in groups)
            items.append(item)
        root_path = owner.root_path
    by_type: dict[str, int] = {}
    for item in items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
    return {
        "session_id": session_id, "total": len(items),
        "errors": sum(bool(item["error"]) for item in items),
        "by_type": by_type, "items": items,
        "token": fingerprint([session_id, root_path, items]),
    }

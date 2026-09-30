"""
Executor — applies approved Proposals to the filesystem.

ALWAYS run with dry_run=True first to preview changes.
All applied changes are logged to the database and to ~/.datahoarder/undo.log.
"""
import hashlib
import os
import sys
import shutil
from donedatahoarder.timeutils import utcnow
from pathlib import Path
from typing import Optional

import io
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    DuplicateGroup, File, FileStatus,
    Proposal, ProposalStatus, ProposalType,
)
from donedatahoarder.db.session import get_engine
from donedatahoarder.core.undo_log import (
    log_operation, complete_operation, complete_database_operation,
    get_last_session_entries, undo_operations, list_undo_sessions,
)
from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.core.process_lock import operation_lock

console = Console()


def _make_quiet_console() -> Console:
    """Create a Console that writes to a buffer (safe for web/non-terminal use)."""
    return Console(file=io.StringIO(), force_terminal=False)


# ---------------------------------------------------------------------------
# Metadata writing
# ---------------------------------------------------------------------------

def _write_exif_comment(path: Path, comment: str) -> bool:
    """Embed a comment/description into image EXIF without re-encoding pixels."""
    try:
        import piexif

        try:
            exif_dict = piexif.load(str(path))
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

        exif_dict["0th"][piexif.ImageIFD.ImageDescription] = comment.encode("utf-8", errors="replace")
        new_exif = piexif.dump(exif_dict)
        # piexif.insert rewrites only the EXIF segment in place — unlike
        # Image.save(), it never re-encodes the JPEG data, so repeated tag
        # writes cause no generation loss.
        piexif.insert(new_exif, str(path))
        return True
    except Exception:
        return False


def _write_pdf_metadata(path: Path, tags: list[str], description: str) -> bool:
    """Write subject/keywords into PDF metadata."""
    try:
        import pdfplumber  # noqa - just checking availability
        # PDF metadata writing requires pikepdf or pypdf
        try:
            import pikepdf
            with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
                with pdf.open_metadata() as meta:
                    if description:
                        meta["dc:description"] = description
                    if tags:
                        meta["dc:subject"] = tags
            return True
        except ImportError:
            pass
    except ImportError:
        pass
    return False


def _write_windows_properties(path: Path, tags: list[str], description: str) -> bool:
    """
    Write tags/description into Windows file properties using NTFS Extended Attributes.

    On Windows, this writes to standard Summary Information stream:
    - Title: first 3 tags joined (or full description if no tags)
    - Keywords: all tags semicolon-separated
    - Comments: full AI description

    Requires pywin32 (win32com.client) for Shell.Application approach,
    or uses PowerShell as fallback for broader Windows support.
    Only works on Windows (sys.platform == "win32").
    """
    if sys.platform != "win32":
        return False

    try:
        # Attempt 1: Try pywin32 (most reliable if installed)
        try:
            import win32com.client

            shell = win32com.client.Dispatch("Shell.Application")
            folder = shell.NameSpace(str(path.parent.resolve()))
            file_item = folder.ParseName(path.name)

            if file_item:
                # Build metadata
                title = " | ".join(tags[:3]) if tags else (description[:50] if description else "")
                keywords = "; ".join(tags) if tags else ""

                # Set properties via shell interface (indices are OS-specific)
                # Common property indices: 9=Subject, 21=Keywords, 27=Comments
                # Try to set via available property setters
                try:
                    # Note: Direct property assignment doesn't work; we use SetProperty if available
                    if hasattr(file_item, "SetProperty"):
                        file_item.SetProperty(9, title)  # Subject
                        file_item.SetProperty(21, keywords)  # Keywords
                        file_item.SetProperty(27, description)  # Comments
                        return True
                except Exception:
                    pass

                return True  # Optimistically return True even if specific properties failed
        except ImportError:
            pass

        # No PowerShell fallback: Set-ItemProperty cannot write shell
        # properties (System.Subject etc.) on regular files, and building a
        # command string from filenames / AI-generated text is a command
        # injection risk. Tags are always preserved in the database.
        return False
    except Exception:
        # Silently fail — Windows property writing is opportunistic
        return False


# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------

def _replace_path_prefix(value: str, old_prefix: str, new_prefix: str) -> str:
    """
    Replace *old_prefix* at the start of *value* only when it ends on a path
    boundary. Returns *value* unchanged otherwise — so `C:\\x\\ab` never
    matches inside `C:\\x\\abc\\file.txt`.
    """
    if value == old_prefix:
        return new_prefix
    for sep in ("\\", "/"):
        if value.startswith(old_prefix + sep):
            return new_prefix + value[len(old_prefix):]
    return value


def _proposal_snapshots(proposals: list[Proposal] | None) -> list[dict]:
    return [
        {"id": p.id, "current_value": p.current_value,
         "proposed_value": p.proposed_value, "status": p.status.value,
         "applied_at": p.applied_at.isoformat() if p.applied_at else None}
        for p in (proposals or [])
    ]


def _expected_after(
    applied: Proposal,
    related_changes: list[tuple[Proposal, str | None, str | None]] | None = None,
) -> list[dict]:
    """Expected review state after this operation, before later operations."""
    expected = [{"id": applied.id, "current_value": applied.current_value,
                 "proposed_value": applied.proposed_value,
                 "status": ProposalStatus.APPLIED.value}]
    for other, current, proposed in related_changes or []:
        expected.append({"id": other.id, "current_value": current,
                         "proposed_value": proposed, "status": other.status.value})
    return expected


def _missing_parent_directories(parent: Path) -> list[str]:
    """Return only directory paths this operation is about to create."""
    missing = []
    cursor = parent
    while not cursor.exists():
        missing.append(str(cursor))
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return list(reversed(missing))


def _affected_by_folder_rename(proposals: list[Proposal] | None,
                               source: Path, destination: Path) -> list[Proposal]:
    old, new = str(source), str(destination)
    return [p for p in (proposals or []) if
            _replace_path_prefix(p.current_value or "", old, new) != (p.current_value or "")
            or _replace_path_prefix(p.proposed_value or "", old, new) != (p.proposed_value or "")]


def _file_cascade_changes(
    proposals: list[Proposal], applied: Proposal, old_path: str, new_path: str,
) -> list[tuple[Proposal, str | None, str | None]]:
    """Plan path updates for proposals depending on a renamed or moved file."""
    changes = []
    for other in proposals:
        if other is applied or other.status in (ProposalStatus.APPLIED, ProposalStatus.REJECTED):
            continue
        current, proposed = other.current_value, other.proposed_value
        if other.file_id == applied.file_id and current == old_path:
            if other.proposal_type in (ProposalType.RENAME, ProposalType.MOVE,
                                       ProposalType.MARK_DUPLICATE):
                current = new_path
            if other.proposal_type == ProposalType.RENAME and proposed:
                # Preserve the pending filename choice in the file's new folder.
                proposed = str(Path(new_path).parent / Path(proposed).name)
            elif (other.proposal_type == ProposalType.MOVE and proposed
                  and applied.proposal_type == ProposalType.RENAME
                  and Path(proposed).name == Path(old_path).name):
                proposed = str(Path(proposed).with_name(Path(new_path).name))
        if other.proposal_type == ProposalType.MARK_DUPLICATE and proposed == old_path:
            # The changed file is the keeper named by another file's proposal.
            proposed = new_path
        if (current, proposed) != (other.current_value, other.proposed_value):
            changes.append((other, current, proposed))
    return changes


def _apply_file_cascade(changes: list[tuple[Proposal, str | None, str | None]]) -> None:
    for proposal, current, proposed in changes:
        proposal.current_value = current
        proposal.proposed_value = proposed

def _apply_rename(
    proposal: Proposal, dry_run: bool, session_id: str | None = None,
    previous_file_status: FileStatus | None = None,
    related_proposals: list[Proposal] | None = None,
    related_changes: list[tuple[Proposal, str | None, str | None]] | None = None,
) -> tuple[bool, str]:
    """Rename a file on disk."""
    src = Path(proposal.current_value)
    dst = Path(proposal.proposed_value)

    if not src.exists():
        return False, f"Source not found: {src}"
    if not src.is_file():
        return False, f"Source is no longer a regular file: {src}"
    if dst.exists() and dst != src:
        return False, f"Destination already exists: {dst}"

    if not dry_run:
        try:
            journal = log_operation(
                operation="RENAME", original_path=str(src), new_path=str(dst),
                session_id=session_id,
                extra={"proposal_id": proposal.id, "file_id": proposal.file_id,
                       "proposal_status": proposal.status.value,
                       "file_status": previous_file_status.value if previous_file_status else None,
                       "proposal_snapshots": _proposal_snapshots(related_proposals),
                       "proposal_expected": _expected_after(proposal, related_changes)},
            )
            src.rename(dst)
            complete_operation(journal)
        except OSError as exc:
            return False, str(exc)

    return True, f"{'[DRY RUN] ' if dry_run else ''}Renamed: {src.name} -> {dst.name}"


def _apply_tags(proposal: Proposal, file_rec: File, dry_run: bool,
                session_id: str | None = None) -> tuple[bool, str]:
    """Record reviewed tags in the database without changing file bytes."""
    path = Path(file_rec.path)
    if not path.is_file():
        return False, f"Source is no longer a regular file: {path}"

    if dry_run:
        return True, f"[DRY RUN] Would record tags for: {path.name}"

    # Embedding metadata changes file bytes and cannot be reversed by a move
    # journal. Keep tags in the database until a byte-preserving backup path
    # is available for this operation.
    journal = log_operation(
        operation="TAGS", original_path=str(path), new_path=str(path),
        session_id=session_id,
        extra={"proposal_id": proposal.id, "file_id": file_rec.id,
               "proposal_status": proposal.status.value,
               "file_status": file_rec.status.value,
               "proposal_expected": _expected_after(proposal)},
    )
    complete_operation(journal)
    return True, f"Tags noted for {path.name}"


def _apply_move(
    proposal: Proposal, dry_run: bool, session_id: str | None = None,
    previous_file_status: FileStatus | None = None,
    related_proposals: list[Proposal] | None = None,
    related_changes: list[tuple[Proposal, str | None, str | None]] | None = None,
) -> tuple[bool, str]:
    """Move a file to a new directory on disk."""
    src = Path(proposal.current_value)
    dst = Path(proposal.proposed_value)

    # After folder-rename cascade, src and dst can become identical — skip silently
    if src.resolve() == dst.resolve():
        return True, f"No-op (already in place): {src.name}"

    if not src.exists():
        return False, f"Source not found: {src}"
    if not src.is_file():
        return False, f"Source is no longer a regular file: {src}"
    if dst.exists():
        return False, f"Destination already exists: {dst}"

    if dry_run:
        return True, f"[DRY RUN] Would move: {src} -> {dst}"

    try:
        created_dirs = _missing_parent_directories(dst.parent)
        journal = log_operation(
            operation="MOVE", original_path=str(src), new_path=str(dst),
            session_id=session_id,
            extra={"proposal_id": proposal.id, "file_id": proposal.file_id,
                   "proposal_status": proposal.status.value,
                   "file_status": previous_file_status.value if previous_file_status else None,
                   "proposal_snapshots": _proposal_snapshots(related_proposals),
                   "proposal_expected": _expected_after(proposal, related_changes),
                   "created_dirs": created_dirs},
        )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        complete_operation(journal)
    except OSError as exc:
        return False, str(exc)

    return True, f"Moved: {src.name} -> {dst.parent.name}/{dst.name}"


def _apply_rename_folder(
    proposal: Proposal, dry_run: bool, db_session: Session, session_id: str | None = None,
    previous_file_status: FileStatus | None = None,
    related_proposals: list[Proposal] | None = None,
    selected_proposal_ids: set[int] | None = None,
) -> tuple[bool, str]:
    """Rename a folder on disk and update all File paths in the DB."""
    src = Path(proposal.current_value)
    dst = Path(proposal.proposed_value)

    if not src.exists():
        return False, f"Source folder not found: {src}"
    if not src.is_dir():
        return False, f"Not a directory: {src}"
    if dst.exists() and dst != src:
        return False, f"Destination already exists: {dst}"

    if dry_run:
        return True, f"[DRY RUN] Would rename folder: {src.name} -> {dst.name}"

    try:
        affected = _affected_by_folder_rename(related_proposals, src, dst)
        expected = _expected_after(proposal)
        for other in affected:
            if other.id == proposal.id:
                continue
            current = _replace_path_prefix(other.current_value or "", str(src), str(dst))
            proposed = _replace_path_prefix(other.proposed_value or "", str(src), str(dst))
            status = other.status
            if (other.id in (selected_proposal_ids or set())
                    and other.proposal_type == ProposalType.MOVE and current == proposed):
                status = ProposalStatus.APPLIED
            expected.append({"id": other.id, "current_value": current or other.current_value,
                             "proposed_value": proposed or other.proposed_value,
                             "status": status.value})
        journal = log_operation(
            operation="RENAME_FOLDER", original_path=str(src), new_path=str(dst),
            session_id=session_id,
            extra={"proposal_id": proposal.id, "file_id": proposal.file_id,
                   "proposal_status": proposal.status.value,
                   "file_status": previous_file_status.value if previous_file_status else None,
                   "proposal_snapshots": _proposal_snapshots(affected),
                   "proposal_expected": expected},
        )
        src.rename(dst)
        complete_operation(journal)
    except OSError as exc:
        return False, f"Rename failed: {exc}"

    # Update all File records whose paths start with the old folder path.
    # The LIKE is only a prefilter (its wildcards can over-match); the real
    # path-boundary check happens in Python so a rename of `...\ab` never
    # rewrites files under a sibling `...\abc`.
    src_str = str(src)
    dst_str = str(dst)
    candidates = db_session.query(File).filter(File.path.like(f"{src_str}%"))
    if session_id:
        candidates = candidates.filter(File.session_id == session_id)
    candidates = candidates.all()
    updated = 0
    for f in candidates:
        new_path = _replace_path_prefix(f.path, src_str, dst_str)
        if new_path != f.path:
            f.path = new_path
            f.filename = Path(new_path).name
            updated += 1

    return True, f"Renamed folder: {src.name} -> {dst.name} ({updated} files updated)"


def _delete_duplicate(
    file_id: int, dry_run: bool, db_session: Session, session_id: str | None = None,
    proposal: Proposal | None = None,
) -> tuple[bool, str]:
    """Move a duplicate to a consolidated .ddh_trash folder instead of hard-deleting."""
    f = db_session.get(File, file_id)
    if not f:
        return False, "File not found in DB"
    path = Path(f.path)
    if not path.exists():
        return False, f"File not on disk: {path}"
    if not path.is_file():
        return False, f"Source is no longer a regular file: {path}"
    if proposal:
        if proposal.current_value != f.path:
            return False, "Duplicate proposal source is stale"
        keeper_path = Path(proposal.proposed_value or "")
        keeper = db_session.query(File).filter(
            File.session_id == f.session_id, File.path == str(keeper_path)
        ).first()
        if not keeper or keeper.id == f.id or not keeper_path.is_file():
            return False, "Duplicate keeper is missing or stale"
        if not f.hash_md5:
            return False, "Duplicate source has no recorded content hash"
        md5 = hashlib.md5()
        with path.open("rb") as source_stream:
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                md5.update(chunk)
        if md5.hexdigest() != f.hash_md5:
            return False, "Duplicate source changed since analysis"
        from donedatahoarder.db.models import DuplicateMember, DupeType
        from sqlalchemy.orm import aliased
        victim_member = aliased(DuplicateMember)
        keeper_member = aliased(DuplicateMember)
        pair_groups = (
            db_session.query(DuplicateGroup)
            .join(victim_member, victim_member.group_id == DuplicateGroup.id)
            .join(keeper_member, keeper_member.group_id == DuplicateGroup.id)
            .filter(victim_member.file_id == f.id,
                    keeper_member.file_id == keeper.id)
            .all()
        )
        if proposal.duplicate_group_id is not None:
            group = db_session.get(DuplicateGroup, proposal.duplicate_group_id)
            if (group is None or group not in pair_groups
                    or group.session_id != f.session_id
                    or group.keep_file_id != keeper.id):
                return False, "Duplicate evidence or keeper changed since review"
            evidence_type = group.dupe_type
        else:
            # A legacy/group-free proposal has no exact evidence provenance.
            # It can only be applied after a fresh individual decision.
            evidence_type = None
        # A file can appear in more than one duplicate group. A proposal is
        # stale if any group containing this victim/keeper pair now selects a
        # different keeper. Group-free proposals from the filename postpass
        # still use the reviewed near-match path below.
        if any(group.keep_file_id != keeper.id for group in pair_groups):
            return False, "Duplicate keeper changed since proposal review"
        if evidence_type != DupeType.EXACT and proposal.review_kind != "individual":
            return False, "Near-duplicate disposal requires individual review"
        if evidence_type != DupeType.EXACT and not _keeper_matches_index(keeper, keeper_path):
            return False, "Duplicate keeper changed since analysis"
        if evidence_type == DupeType.EXACT:
            from donedatahoarder.core.undo_log import _compute_sha256
            source_hash = _compute_sha256(path)
            keeper_hash = _compute_sha256(keeper_path)
            if not source_hash or not keeper_hash or source_hash != keeper_hash:
                return False, "Exact duplicate no longer matches its keeper"

    # Use a root-level trash folder if we can determine the session root,
    # otherwise fall back to the file's parent directory.
    trash_dir: Path
    if session_id:
        from donedatahoarder.db.models import UserSession
        us = db_session.get(UserSession, session_id)
        if us and us.root_path:
            trash_dir = Path(us.root_path) / ".ddh_trash"
        else:
            trash_dir = path.parent / ".ddh_trash"
    else:
        trash_dir = path.parent / ".ddh_trash"

    dst = trash_dir / path.name

    if dry_run:
        return True, f"[DRY RUN] Would trash: {path}"

    created_dirs = _missing_parent_directories(trash_dir)
    # Handle collision in trash
    if dst.exists():
        stem, suffix = dst.stem, dst.suffix
        i = 1
        while dst.exists():
            dst = trash_dir / f"{stem}_{i}{suffix}"
            i += 1
    journal = log_operation(
        operation="TRASH",
        original_path=str(path),
        new_path=str(dst),
        session_id=session_id,
        extra={"file_id": file_id, "file_status": f.status.value,
               "proposal_id": proposal.id if proposal else None,
               "proposal_status": proposal.status.value if proposal else None,
               "proposal_expected": _expected_after(proposal) if proposal else [],
               "created_dirs": created_dirs},
    )
    trash_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dst))
    complete_operation(journal)

    f.path = str(dst)
    f.status = FileStatus.APPLIED
    return True, f"Trashed: {path.name} -> {dst}"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def preview(
    min_confidence: float = 0.0,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    session_id: str | None = None,
) -> None:
    """Print a rich table of pending proposals."""
    engine = get_engine()
    with Session(engine) as session:
        query = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .join(File)
        )
        if session_id:
            query = query.filter(File.session_id == session_id)
        total = query.count()
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)
        proposals = query.all()
        if not proposals:
            console.print("[yellow]No pending proposals.[/yellow]")
            return

        table = Table(
            title=f"Pending Proposals (showing {len(proposals)} of {total})",
            show_lines=True,
            expand=True,
        )
        table.add_column("ID", style="dim", width=6)
        table.add_column("Type", style="cyan", width=12)
        table.add_column("Confidence", width=10)
        table.add_column("Current", style="red", overflow="fold")
        table.add_column("Proposed", style="green", overflow="fold")

        for p in proposals:
            if p.confidence and p.confidence < min_confidence:
                continue
            conf_str = f"{p.confidence:.0%}" if p.confidence else "?"
            curr = Path(p.current_value).name if p.current_value else ""
            prop = Path(p.proposed_value).name if p.proposed_value else str(p.proposed_value)
            table.add_row(str(p.id), p.proposal_type.value, conf_str, curr, prop)

        console.print(table)


def select_executable_proposals(
    db_session: Session, *, session_id: str | None = None,
    min_confidence: float = 0.7, include_pending: bool = False,
    proposal_ids: Optional[list[int]] = None,
    proposal_types: Optional[list[ProposalType]] = None,
) -> list[Proposal]:
    """Single selection rule shared by preview and commit."""
    query = db_session.query(Proposal).filter(
        Proposal.status.in_([ProposalStatus.APPROVED, ProposalStatus.MODIFIED])
    )
    if include_pending:
        query = db_session.query(Proposal).filter(
            (Proposal.status.in_([ProposalStatus.APPROVED, ProposalStatus.MODIFIED]))
            | ((Proposal.status == ProposalStatus.PENDING)
               & Proposal.confidence.isnot(None)
               & (Proposal.confidence >= min_confidence))
        )
    if session_id:
        query = query.join(File).filter(File.session_id == session_id)
    if proposal_ids is not None:
        query = query.filter(Proposal.id.in_(proposal_ids))
    if proposal_types is not None:
        query = query.filter(Proposal.proposal_type.in_(proposal_types))
    return query.all()


@dataclass(frozen=True)
class ExecutionStep:
    proposal_id: int
    proposal_type: ProposalType
    source: str | None
    destination: str | None
    error: str | None = None
    keeper: str | None = None


def _sort_proposals(proposals: list[Proposal]) -> list[Proposal]:
    def key(p: Proposal):
        if p.proposal_type == ProposalType.RENAME_FOLDER:
            return (0, -(p.current_value or "").count("/")
                    - (p.current_value or "").count("\\"), p.id)
        if p.proposal_type == ProposalType.RENAME:
            return (1, 0, p.id)
        if p.proposal_type == ProposalType.MOVE:
            return (2, 0, p.id)
        return (3, 0, p.id)
    return sorted(proposals, key=key)


def _duplicate_evidence_error(db_session: Session, proposal: Proposal) -> str | None:
    from donedatahoarder.db.models import DuplicateMember, DupeType
    if proposal.duplicate_group_id is None:
        return (None if proposal.review_kind == "individual"
                else "Near-duplicate disposal requires individual review")
    group = db_session.get(DuplicateGroup, proposal.duplicate_group_id)
    if group is None:
        return "Duplicate evidence group is missing"
    members = {m.file_id for m in db_session.query(DuplicateMember).filter_by(group_id=group.id)}
    keeper = db_session.get(File, group.keep_file_id) if group.keep_file_id else None
    victim = db_session.get(File, proposal.file_id)
    if (keeper is None or victim is None or group.session_id != victim.session_id
            or keeper.session_id != victim.session_id or proposal.file_id not in members
            or keeper.id not in members or proposal.proposed_value != keeper.path):
        return "Duplicate evidence or keeper changed since review"
    if group.dupe_type != DupeType.EXACT and proposal.review_kind != "individual":
        return "Near-duplicate disposal requires individual review"
    return None


def _keeper_matches_index(keeper: File, path: Path) -> bool:
    """A reviewed near-match survivor must still have its indexed bytes."""
    if not path.is_file():
        return False
    if keeper.hash_sha256:
        from donedatahoarder.core.undo_log import _compute_sha256
        return _compute_sha256(path) == keeper.hash_sha256
    if not keeper.hash_md5:
        return False
    digest = hashlib.md5()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == keeper.hash_md5


def plan_execution(
    db_session: Session, selected_proposals: list[Proposal], root: Path,
    *, protection: ProtectionIndex | None = None,
) -> list[ExecutionStep]:
    """Simulate ordered paths and collisions without changing DB or disk.

    This is the same ordered preflight consumed by commit and review preview.
    It projects selected cascades onto later operations, including keeper paths.
    """
    root = root.resolve()
    if protection is None:
        protection = ProtectionIndex(root)
    # Selected rows must all belong to one collection. The caller supplies
    # the root; session scope comes from the selected file records.
    ids = {p.file_id for p in selected_proposals}
    files = {f.id: f for f in db_session.query(File).filter(File.id.in_(ids)).all()}
    session_ids = {f.session_id for f in files.values()}
    if len(session_ids) > 1:
        raise ValueError("A plan cannot span multiple sessions")
    session_id = next(iter(session_ids), None)
    all_props = (
        db_session.query(Proposal).join(File).filter(File.session_id == session_id).all()
        if session_id else selected_proposals
    )
    values = {p.id: [p.current_value, p.proposed_value] for p in all_props}
    prop_by_id = {p.id: p for p in all_props}
    indexed_paths = {
        f.id: f.path for f in db_session.query(File).filter(File.session_id == session_id).all()
    } if session_id else {}
    def path_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    created: dict[str, str] = {}
    vacated: set[str] = set()
    validated_source_ids: set[int] = set()
    steps: list[ExecutionStep] = []

    def occupied(path: str) -> bool:
        key = path_key(path)
        return key in created or (Path(path).exists() and key not in vacated)

    for prop in _sort_proposals(selected_proposals):
        kind = prop.proposal_type
        source, destination = values[prop.id]
        if kind == ProposalType.MARK_DUPLICATE:
            source = indexed_paths.get(prop.file_id, source)
            trash = root / ".ddh_trash" / Path(source or "").name
            candidate = trash
            suffix = 1
            while occupied(str(candidate)):
                candidate = trash.with_name(f"{trash.stem}_{suffix}{trash.suffix}")
                suffix += 1
            trash_destination = str(candidate)
        else:
            trash_destination = None
        step_destination = trash_destination if kind == ProposalType.MARK_DUPLICATE else destination
        error = None
        if kind in (ProposalType.RENAME, ProposalType.MOVE,
                    ProposalType.RENAME_FOLDER, ProposalType.MARK_DUPLICATE):
            if not source or not step_destination:
                error = "Missing source or destination"
            elif (not Path(source).resolve().is_relative_to(root)
                  or not Path(step_destination).resolve().is_relative_to(root)):
                error = "Proposal path escapes the session root"
            elif protection.assess(Path(source)).protected:
                error = f"Protected dependency: {protection.assess(Path(source)).reason}"
            elif (kind in (ProposalType.RENAME, ProposalType.MOVE,
                           ProposalType.MARK_DUPLICATE)
                  and values[prop.id][0] != indexed_paths.get(prop.file_id)):
                error = "Proposal source no longer matches the indexed file"
            elif kind == ProposalType.MOVE and Path(source).name != Path(destination).name:
                error = "MOVE must preserve the current filename; approve RENAME separately"
            elif not occupied(source):
                error = f"Source not found: {source}"
            elif source != step_destination and occupied(step_destination):
                error = f"Destination already exists: {step_destination}"
            elif kind == ProposalType.RENAME and Path(source).parent != Path(destination).parent:
                error = "Rename must stay in the same directory"
            elif kind == ProposalType.RENAME_FOLDER and Path(source).is_file():
                error = "Folder source is a file"
            elif kind != ProposalType.RENAME_FOLDER and Path(source).is_dir():
                error = "File source is a directory"
            if error is None and kind != ProposalType.RENAME_FOLDER:
                file_rec = files.get(prop.file_id)
                if file_rec and file_rec.id not in validated_source_ids:
                    initial = Path(file_rec.path)
                    if file_rec.hash_sha256:
                        from donedatahoarder.core.undo_log import _compute_sha256
                        if _compute_sha256(initial) != file_rec.hash_sha256:
                            error = "Proposal source changed since analysis"
                    elif file_rec.size_bytes is not None and initial.is_file():
                        if initial.stat().st_size != file_rec.size_bytes:
                            error = "Proposal source size changed since analysis"
                    validated_source_ids.add(file_rec.id)
            if error is None and kind == ProposalType.MARK_DUPLICATE:
                error = _duplicate_evidence_error(db_session, prop)
            if error is None and kind == ProposalType.MARK_DUPLICATE:
                projected_keeper = destination
                keeper_id = next(
                    (fid for fid, path in indexed_paths.items()
                     if path_key(path) == path_key(projected_keeper)), None,
                )
                keeper_file = db_session.get(File, keeper_id) if keeper_id else None
                victim_file = db_session.get(File, prop.file_id)
                evidence_group = (
                    db_session.get(DuplicateGroup, prop.duplicate_group_id)
                    if prop.duplicate_group_id is not None else None
                )
                exact_evidence = bool(evidence_group and evidence_group.dupe_type.value == "exact")
                if (keeper_file is None or keeper_id == prop.file_id
                        or not occupied(projected_keeper)
                        or not Path(keeper_file.path).is_file()):
                    error = "Duplicate keeper is missing or stale"
                elif not exact_evidence and not _keeper_matches_index(
                    keeper_file, Path(keeper_file.path)
                ):
                    error = "Duplicate keeper changed since analysis"
                elif victim_file is None or not victim_file.hash_md5:
                    error = "Duplicate source has no recorded content hash"
                else:
                    md5 = hashlib.md5()
                    try:
                        with Path(victim_file.path).open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                md5.update(chunk)
                    except OSError:
                        error = "Duplicate source is missing"
                    if error is None and md5.hexdigest() != victim_file.hash_md5:
                        error = "Duplicate source changed since analysis"
                    elif error is None and prop.duplicate_group_id is not None:
                        from donedatahoarder.db.models import DupeType
                        group = db_session.get(DuplicateGroup, prop.duplicate_group_id)
                        if group and group.dupe_type == DupeType.EXACT:
                            from donedatahoarder.core.undo_log import _compute_sha256
                            source_hash = _compute_sha256(Path(victim_file.path))
                            keeper_hash = _compute_sha256(Path(keeper_file.path))
                            if not source_hash or not keeper_hash or source_hash != keeper_hash:
                                error = "Exact duplicate no longer matches its keeper"
        steps.append(ExecutionStep(
            prop.id, kind, source, step_destination, error,
            destination if kind == ProposalType.MARK_DUPLICATE else None,
        ))
        if error or kind not in (ProposalType.RENAME, ProposalType.MOVE,
                                  ProposalType.RENAME_FOLDER, ProposalType.MARK_DUPLICATE):
            continue
        if source == step_destination:
            continue
        created.pop(path_key(source), None)
        vacated.add(path_key(source))
        created[path_key(step_destination)] = step_destination
        if kind == ProposalType.RENAME_FOLDER:
            # A directory transition carries its descendants and any pending
            # proposal values; preexisting destination checks use virtual state.
            for file_id, current in list(indexed_paths.items()):
                rewritten = _replace_path_prefix(current, source, destination)
                indexed_paths[file_id] = rewritten
                if rewritten != current:
                    created.pop(path_key(current), None)
                    vacated.add(path_key(current))
                    created[path_key(rewritten)] = rewritten
            for other_id, pair in values.items():
                if other_id == prop.id:
                    continue
                pair[0] = _replace_path_prefix(pair[0] or "", source, destination) or pair[0]
                pair[1] = _replace_path_prefix(pair[1] or "", source, destination) or pair[1]
            for path in list(created.values()):
                rewritten = _replace_path_prefix(path, source, destination)
                if rewritten != path:
                    created.pop(path_key(path), None)
                    created[path_key(rewritten)] = rewritten
        elif kind in (ProposalType.RENAME, ProposalType.MOVE):
            indexed_paths[prop.file_id] = destination
            for other_id, pair in values.items():
                other = prop_by_id[other_id]
                if other_id == prop.id or other.status in (ProposalStatus.APPLIED, ProposalStatus.REJECTED):
                    continue
                if other.file_id == prop.file_id and pair[0] == source:
                    pair[0] = destination
                    if other.proposal_type == ProposalType.RENAME and pair[1]:
                        pair[1] = str(Path(destination).parent / Path(pair[1]).name)
                    elif (other.proposal_type == ProposalType.MOVE and kind == ProposalType.RENAME
                          and pair[1] and Path(pair[1]).name == Path(source).name):
                        pair[1] = str(Path(pair[1]).with_name(Path(destination).name))
                if other.proposal_type == ProposalType.MARK_DUPLICATE and pair[1] == source:
                    pair[1] = destination
        else:
            indexed_paths[prop.file_id] = step_destination
    return steps


def _validate_operation_paths(
    prop: Proposal, root: Path | None, file_rec: File | None,
    protection: ProtectionIndex | None = None,
) -> None:
    """Reject stale sources and paths outside the selected session root."""
    if prop.proposal_type not in (
        ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER,
        ProposalType.MARK_DUPLICATE,
    ):
        return
    if root is None:
        raise ValueError("A session root is required for filesystem changes")
    source = Path(file_rec.path if prop.proposal_type == ProposalType.MARK_DUPLICATE else prop.current_value)
    if prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE,
                              ProposalType.MARK_DUPLICATE) and not source.is_file():
        raise ValueError("Proposal source is no longer a regular file")
    if (file_rec is not None and prop.proposal_type in
            (ProposalType.RENAME, ProposalType.MOVE, ProposalType.MARK_DUPLICATE)):
        if file_rec.hash_sha256:
            from donedatahoarder.core.undo_log import _compute_sha256
            if _compute_sha256(source) != file_rec.hash_sha256:
                raise ValueError("Proposal source changed since analysis")
        elif file_rec.size_bytes is not None and source.stat().st_size != file_rec.size_bytes:
            raise ValueError("Proposal source size changed since analysis")
    if prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE):
        if file_rec is None or Path(file_rec.path) != source:
            raise ValueError("Proposal source no longer matches the indexed file")
    root = root.resolve()
    paths = [source]
    if prop.proposal_type != ProposalType.MARK_DUPLICATE:
        paths.append(Path(prop.proposed_value))
    elif prop.proposed_value:
        paths.append(Path(prop.proposed_value))
    if any(not p.resolve().is_relative_to(root) for p in paths):
        raise ValueError("Proposal path escapes the session root")
    if protection is not None:
        decision = protection.assess(source)
        if decision.protected:
            raise ValueError(f"Protected dependency: {decision.reason}")
    if prop.proposal_type == ProposalType.RENAME and source.parent.resolve() != paths[1].parent.resolve():
        raise ValueError("Rename must stay in the same directory")


def _execute_unlocked(
    dry_run: bool = True,
    min_confidence: float = 0.7,
    proposal_ids: Optional[list[int]] = None,
    proposal_types: Optional[list[ProposalType]] = None,
    session_id: str | None = None,
    include_pending: bool = False,
    _console: Console | None = None,
) -> dict:
    """
    Apply reviewed proposals. Pending proposals require include_pending=True.

    IMPORTANT: Defaults to dry-run mode (dry_run=True).
    You must explicitly pass dry_run=False or use --commit in CLI to apply changes.

    Filesystem changes are journaled before mutation with SHA256 hashes.
    Use datahoarder undo --session <id> to reverse operations.

    Args:
        dry_run:          If True (default), preview only. Set to False to apply changes.
        min_confidence:   Threshold for pending proposals when opted in.
        proposal_ids:     If set, only apply these specific proposal IDs.
        proposal_types:   If set, only apply these proposal types.
        session_id:       Session identifier for transaction log grouping.
        include_pending:  Explicitly include high-confidence pending proposals.
        _console:         Optional Console override (use _make_quiet_console() for web).

    Returns:
        Summary dict with 'applied', 'failed', 'skipped' counts.
    """
    con = _console or console
    if not dry_run and not session_id:
        raise ValueError("A session_id is required to commit filesystem changes")
    engine = get_engine()
    counts = {"applied": 0, "failed": 0, "skipped": 0}
    old_journal_ids = (
        {entry.get("operation_id") for entry in get_last_session_entries(session_id)}
        if not dry_run else set()
    )

    class _DatabaseCommitFailure(RuntimeError):
        pass

    def mark_committed_journal() -> None:
        for entry in get_last_session_entries(session_id):
            operation_id = entry.get("operation_id")
            if (operation_id not in old_journal_ids
                    and entry.get("phase") == "complete"
                    and not entry.get("_db_complete")):
                complete_database_operation(entry)
                old_journal_ids.add(operation_id)

    if dry_run:
        con.print("[bold yellow]DRY RUN -- no files will be changed[/bold yellow]\n")

    with Session(engine) as session:
        # An edited or approved proposal is an explicit yes.
        # A still-pending one needs a real confidence score.
        proposals = select_executable_proposals(
            session, session_id=session_id, min_confidence=min_confidence,
            include_pending=include_pending, proposal_ids=proposal_ids,
            proposal_types=proposal_types,
        )
        all_session_proposals = (
            session.query(Proposal).join(File).filter(File.session_id == session_id).all()
            if session_id else proposals
        )
        from donedatahoarder.db.models import UserSession
        user_session = session.get(UserSession, session_id) if session_id else None
        root = Path(user_session.root_path) if user_session and user_session.root_path else None
        protection = ProtectionIndex(root) if root is not None else None
        con.print(f"[bold]Processing {len(proposals)} proposals...[/bold]")

        proposals = _sort_proposals(proposals)
        steps = (
            plan_execution(session, proposals, root, protection=protection)
            if root is not None else []
        )
        planned = {step.proposal_id: step for step in steps}

        if dry_run and root is not None:
            for step in steps:
                if step.error:
                    counts["failed"] += 1
                    con.print(f"  [red]Proposal {step.proposal_id}: {step.error}[/red]")
                elif step.source and step.source == step.destination:
                    counts["skipped"] += 1
                else:
                    counts["applied"] += 1
                    con.print(f"  [green]{step.proposal_type.value}: {step.source or ''} -> {step.destination or ''}[/green]")
            return counts

        for prop in proposals:
            if prop.status == ProposalStatus.APPLIED:
                continue  # A folder rename may have resolved a later move.
            file_rec = session.get(File, prop.file_id)

            try:
                step = planned.get(prop.id)
                if step and step.error:
                    raise ValueError(step.error)
                if step and prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE,
                                                    ProposalType.RENAME_FOLDER):
                    if (prop.current_value, prop.proposed_value) != (step.source, step.destination):
                        raise ValueError("Operation paths changed after preflight")
                _validate_operation_paths(prop, root, file_rec, protection)
                if prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE,
                                          ProposalType.RENAME_FOLDER):
                    if Path(prop.current_value).resolve() == Path(prop.proposed_value).resolve():
                        counts["skipped"] += 1
                        con.print(f"  [dim]No change for proposal {prop.id}[/dim]")
                        continue
                if prop.proposal_type == ProposalType.RENAME:
                    dependent_changes = _file_cascade_changes(
                        all_session_proposals, prop, prop.current_value, prop.proposed_value)
                    ok, msg = _apply_rename(prop, dry_run, session_id,
                                            file_rec.status if file_rec else None,
                                            [change[0] for change in dependent_changes],
                                            dependent_changes)
                    if ok and not dry_run:
                        # Update File.path in DB to new location
                        if file_rec:
                            file_rec.path = prop.proposed_value
                            file_rec.filename = Path(prop.proposed_value).name
                        _apply_file_cascade(dependent_changes)

                elif prop.proposal_type == ProposalType.ADD_TAGS:
                    ok, msg = _apply_tags(prop, file_rec, dry_run, session_id)

                elif prop.proposal_type == ProposalType.RENAME_FOLDER:
                    ok, msg = _apply_rename_folder(prop, dry_run, session, session_id,
                                                   file_rec.status if file_rec else None,
                                                   all_session_proposals,
                                                   {selected.id for selected in proposals})
                    # After a successful folder rename, update paths in all
                    # remaining proposals that reference the old folder path
                    if ok and not dry_run:
                        old_prefix = prop.current_value
                        new_prefix = prop.proposed_value
                        for other in all_session_proposals:
                            if other is prop or other.status == ProposalStatus.APPLIED:
                                continue
                            if other.current_value:
                                other.current_value = _replace_path_prefix(other.current_value, old_prefix, new_prefix)
                            if other.proposed_value:
                                other.proposed_value = _replace_path_prefix(other.proposed_value, old_prefix, new_prefix)
                        # Pre-mark any MOVE proposals that became no-ops after the rename
                        # (source == destination means the file is already where it should be)
                        for other in proposals:
                            if (
                                other is not prop
                                and other.proposal_type == ProposalType.MOVE
                                and other.status not in (ProposalStatus.APPLIED, ProposalStatus.REJECTED)
                                and other.current_value
                                and other.current_value == other.proposed_value
                            ):
                                other.status = ProposalStatus.APPLIED
                                other.applied_at = utcnow()
                                counts["applied"] += 1
                                con.print(f"  [dim]Skipped no-op move (folder renamed in-place): {Path(other.current_value).name}[/dim]")

                elif prop.proposal_type == ProposalType.MOVE:
                    dependent_changes = _file_cascade_changes(
                        all_session_proposals, prop, prop.current_value, prop.proposed_value)
                    ok, msg = _apply_move(prop, dry_run, session_id,
                                          file_rec.status if file_rec else None,
                                          [change[0] for change in dependent_changes],
                                          dependent_changes)
                    if ok and not dry_run:
                        if file_rec:
                            file_rec.path = prop.proposed_value
                            file_rec.filename = Path(prop.proposed_value).name
                        _apply_file_cascade(dependent_changes)

                elif prop.proposal_type == ProposalType.MARK_DUPLICATE:
                    ok, msg = _delete_duplicate(prop.file_id, dry_run, session, session_id, prop)

                else:
                    ok, msg = True, f"Skipped unsupported type: {prop.proposal_type}"
                    counts["skipped"] += 1
                    continue

                if ok:
                    counts["applied"] += 1
                    if not dry_run:
                        prop.status = ProposalStatus.APPLIED
                        prop.applied_at = utcnow()
                        if file_rec and prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER):
                            file_rec.status = FileStatus.APPLIED
                        # Each filesystem transition gets its own durable DB
                        # boundary. A later commit failure cannot leave a
                        # whole rename→move batch with only pre-batch rows.
                        try:
                            session.commit()
                        except Exception as exc:
                            raise _DatabaseCommitFailure(str(exc)) from exc
                        try:
                            mark_committed_journal()
                        except Exception as exc:
                            raise _DatabaseCommitFailure(
                                f"Database committed but journal completion failed: {exc}"
                            ) from exc
                    color = "green"
                else:
                    counts["failed"] += 1
                    if not dry_run:
                        prop.user_notes = msg[:500]
                    color = "red"

                con.print(f"  [{color}]{msg}[/{color}]")

            except _DatabaseCommitFailure:
                raise
            except Exception as exc:
                counts["failed"] += 1
                con.print(f"  [red]ERROR on proposal {prop.id}: {exc}[/red]")

        if not dry_run:
            session.commit()

    con.print(
        f"\n[bold]Done:[/bold] {counts['applied']} applied, "
        f"{counts['failed']} failed, {counts['skipped']} skipped"
    )
    return counts


def execute(
    dry_run: bool = True,
    min_confidence: float = 0.7,
    proposal_ids: Optional[list[int]] = None,
    proposal_types: Optional[list[ProposalType]] = None,
    session_id: str | None = None,
    include_pending: bool = False,
    _console: Console | None = None,
) -> dict:
    """Apply proposals under the database-wide writer lease on commit."""
    if dry_run:
        return _execute_unlocked(
            dry_run, min_confidence, proposal_ids, proposal_types,
            session_id, include_pending, _console,
        )
    with operation_lock("execute"):
        return _execute_unlocked(
            dry_run, min_confidence, proposal_ids, proposal_types,
            session_id, include_pending, _console,
        )


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper (DRY-RUN ONLY)
# ---------------------------------------------------------------------------
# Note: execute --commit deliberately stays synchronous so the user-visible
# "Apply changes? y/N" confirmation flow is preserved. Only execute --dry-run
# (used by the unattended runner) becomes a background job, since the
# dry-run is the step that benefits from disconnect resilience.

def execute_with_progress(
    min_confidence: float = 0.7,
    proposal_ids: Optional[list[int]] = None,
    proposal_types: Optional[list[ProposalType]] = None,
    session_id: str | None = None,
    pause_event=None,
    cancel_check=None,
):
    """
    Background-job-friendly wrapper around execute(dry_run=True).

    Runs execute() in a worker thread and emits periodic heartbeats while
    the consumer waits for the final summary. dry_run is hard-coded to True
    here — the destructive commit path stays synchronous and is reached
    through the existing /execute endpoint when dry_run=False.

    Pause is honored before launching the worker; once the worker is running
    it cannot be interrupted (dry-run is read-only and typically completes in
    seconds to minutes, so this is acceptable).

    Yields:
      {"phase": "starting"}
      {"phase": "running", "heartbeat": True}                every ~2s
      {"cancelled": True}                                     on cancel (before start)
      {"done": True, "applied": ..., "failed": ..., ...}     terminal
    """
    import contextlib
    import io
    import queue
    import threading

    if pause_event is not None:
        pause_event.wait()
    if cancel_check and cancel_check():
        yield {"cancelled": True}
        return

    yield {"phase": "starting"}

    result_queue: "queue.Queue" = queue.Queue(maxsize=4)
    sentinel_done = object()
    sentinel_error = object()
    final_result: dict = {}
    error_holder: list = [None]

    def _runner():
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                summary = execute(
                    dry_run=True,
                    min_confidence=min_confidence,
                    proposal_ids=proposal_ids,
                    proposal_types=proposal_types,
                    session_id=session_id,
                    _console=_make_quiet_console(),
                )
            final_result.update(summary)
            result_queue.put(sentinel_done)
        except Exception as exc:
            error_holder[0] = exc
            result_queue.put(sentinel_error)

    worker = threading.Thread(target=_runner, daemon=True, name="execute-dry-worker")
    from donedatahoarder.core.jobs import job_manager
    job_manager.start_tracked_worker(worker)

    while True:
        try:
            msg = result_queue.get(timeout=2.0)
        except queue.Empty:
            if cancel_check and cancel_check():
                # Worker can't be cleanly interrupted mid-run; let it finish
                # in the background. Dry-run is read-only so this is safe.
                yield {"cancelled": True, "phase": "running"}
                return
            yield {"phase": "running", "heartbeat": True}
            continue

        if msg is sentinel_done:
            break
        if msg is sentinel_error:
            raise error_holder[0] if error_holder[0] else RuntimeError("execute failed")

    worker.join(timeout=5.0)
    yield {"done": True, **final_result}


def _cleanup_junk_files(
    root: Path, session_id: str | None, con: Console
) -> int:
    """
    Move known junk files (system metadata, OS thumbnail caches, plot logs)
    to .ddh_trash so they no longer clutter the organized output.

    The scanner correctly excludes these files from the index — but it never
    physically deletes them from disk. After a successful execute, the root
    tree often still contains `.DS_Store`, `Thumbs.db`, `monochrome henkin.ctb`,
    `plot.log`, etc. — files the user did not author and cannot meaningfully
    review. This pass sweeps them into .ddh_trash where the user can verify
    and permanently delete on their own schedule.

    Filename rules mirror `donedatahoarder.core.scanner.SKIP_FILENAMES`,
    `SKIP_FILENAME_PREFIXES`, and `SKIP_EXTENSIONS` so the on-disk cleanup
    can never trash a file the scanner would've kept.

    Returns the number of junk files moved to trash.
    """
    if not root.exists() or not root.is_dir():
        return 0

    # Mirror the scanner's filter lists so this cleanup never trashes a file
    # the scanner would have indexed. Importing rather than redefining keeps
    # the two in lockstep — add a junk type to the scanner and the cleanup
    # picks it up automatically. Note: JUNK_FILE_EXTENSIONS (not
    # SKIP_EXTENSIONS) — skipping a file from indexing is a much weaker
    # statement than physically moving it off disk.
    from donedatahoarder.core.scanner import (
        SKIP_FILENAMES, SKIP_FILENAME_PREFIXES, JUNK_FILE_EXTENSIONS, SKIP_DIRS,
    )

    trash_dir = root / ".ddh_trash"
    moved = 0

    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        # Don't descend into trash or other skip dirs
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
            and d != ".ddh_trash"
            and not d.startswith(".")
        ]
        for name in filenames:
            is_junk = (
                name in SKIP_FILENAMES
                or name.startswith(SKIP_FILENAME_PREFIXES)
                or Path(name).suffix.lower() in JUNK_FILE_EXTENSIONS
            )
            if not is_junk:
                continue
            src = Path(dirpath) / name
            try:
                trash_dir.mkdir(parents=True, exist_ok=True)
                dst = trash_dir / name
                # Resolve collision in trash with a counter suffix.
                if dst.exists():
                    stem, suffix = dst.stem, dst.suffix
                    i = 1
                    while dst.exists():
                        dst = trash_dir / f"{stem}_{i}{suffix}"
                        i += 1
                shutil.move(str(src), str(dst))
                # Log so the operation can be undone if the user changes
                # their mind. No file_id since these were never indexed.
                log_operation(
                    operation="JUNK_TRASH",
                    original_path=str(src),
                    new_path=str(dst),
                    session_id=session_id,
                    extra={"reason": "scanner-skipped junk file"},
                )
                moved += 1
            except OSError:
                # File in use, permission denied, etc. — best effort only.
                continue

    if moved:
        con.print(f"[dim]Cleaned up {moved} junk file(s) to .ddh_trash[/dim]")
    return moved


def _cleanup_empty_dirs_recursive(root: Path) -> None:
    """
    Walk the entire root tree and remove any empty directories (bottom-up).
    This catches pre-existing empty dirs that were never touched by MOVE proposals.
    Never removes the root itself.
    """
    if not root.exists() or not root.is_dir():
        return
    # Walk bottom-up so children are removed before parents
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


def _cleanup_empty_dirs(moved_dirs: set[Path]) -> None:
    """
    Remove empty directories left behind after MOVE proposals were applied.

    Expands each source dir to its full ancestor chain (up to 6 levels), then
    sorts deepest-first so children are always removed before parents. This
    handles orphan subtrees like Year_2019_Summary/Stone_Sales_Data that were
    left stranded at the wrong depth after a rename+move cascade.

    Never removes a directory that still contains files or subdirectories.
    """
    if not moved_dirs:
        return

    # Expand each moved dir to include its ancestors so whole orphan trees get cleaned
    to_check: set[Path] = set()
    for d in moved_dirs:
        current = d
        for _ in range(6):
            to_check.add(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

    # Deepest first — removes children before parents
    for dir_path in sorted(to_check, key=lambda p: len(p.parts), reverse=True):
        try:
            if dir_path.exists() and dir_path.is_dir():
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
        except OSError:
            pass  # in use or protected — skip silently



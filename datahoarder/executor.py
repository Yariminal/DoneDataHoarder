"""
Executor — applies approved Proposals to the filesystem.

ALWAYS run with dry_run=True first to preview changes.
All applied changes are logged to the database.
"""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import io

from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from datahoarder.db.models import (
    DuplicateGroup, File, FileStatus,
    Proposal, ProposalStatus, ProposalType,
)
from datahoarder.db.session import get_engine

console = Console()


def _make_quiet_console() -> Console:
    """Create a Console that writes to a buffer (safe for web/non-terminal use)."""
    return Console(file=io.StringIO(), force_terminal=False)


# ---------------------------------------------------------------------------
# Metadata writing
# ---------------------------------------------------------------------------

def _write_exif_comment(path: Path, comment: str) -> bool:
    """Embed a comment/description into image EXIF using Pillow."""
    try:
        from PIL import Image
        import piexif

        with Image.open(path) as img:
            exif_bytes = img.info.get("exif", b"")
            if exif_bytes:
                exif_dict = piexif.load(exif_bytes)
            else:
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

            exif_dict["0th"][piexif.ImageIFD.ImageDescription] = comment.encode("utf-8", errors="replace")
            new_exif = piexif.dump(exif_dict)
            img.save(path, exif=new_exif)
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


# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------

def _apply_rename(proposal: Proposal, dry_run: bool) -> tuple[bool, str]:
    """Rename a file on disk."""
    src = Path(proposal.current_value)
    dst = Path(proposal.proposed_value)

    if not src.exists():
        return False, f"Source not found: {src}"
    if dst.exists() and dst != src:
        return False, f"Destination already exists: {dst}"

    if not dry_run:
        try:
            src.rename(dst)
        except OSError as exc:
            return False, str(exc)

    return True, f"{'[DRY RUN] ' if dry_run else ''}Renamed: {src.name} -> {dst.name}"


def _apply_tags(proposal: Proposal, file_rec: File, dry_run: bool) -> tuple[bool, str]:
    """Write tags/description into file metadata."""
    path = Path(file_rec.path)
    tags = []
    try:
        tags = json.loads(proposal.proposed_value or "[]")
    except (json.JSONDecodeError, TypeError):
        pass

    description = file_rec.ai_description or ""
    mime = file_rec.mime_type or ""
    ext = path.suffix.lower()

    if dry_run:
        return True, f"[DRY RUN] Would write tags to: {path.name}"

    written = False
    if ext in (".jpg", ".jpeg") or mime == "image/jpeg":
        written = _write_exif_comment(path, description)
    elif ext == ".pdf" or mime == "application/pdf":
        written = _write_pdf_metadata(path, tags, description)
    # For other types we just record in DB (no-op on disk)

    return True, f"Tags written to {path.name}" if written else f"Tags noted for {path.name}"


def _apply_move(proposal: Proposal, dry_run: bool) -> tuple[bool, str]:
    """Move a file to a new directory on disk."""
    src = Path(proposal.current_value)
    dst = Path(proposal.proposed_value)

    # After folder-rename cascade, src and dst can become identical — skip silently
    if src.resolve() == dst.resolve():
        return True, f"No-op (already in place): {src.name}"

    if not src.exists():
        return False, f"Source not found: {src}"
    if dst.exists():
        return False, f"Destination already exists: {dst}"

    if dry_run:
        return True, f"[DRY RUN] Would move: {src} -> {dst}"

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return False, str(exc)

    return True, f"Moved: {src.name} -> {dst.parent.name}/{dst.name}"


def _apply_rename_folder(proposal: Proposal, dry_run: bool, db_session: Session) -> tuple[bool, str]:
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
        src.rename(dst)
    except OSError as exc:
        return False, f"Rename failed: {exc}"

    # Update all File records whose paths start with the old folder path
    src_str = str(src)
    dst_str = str(dst)
    files_in_folder = (
        db_session.query(File)
        .filter(File.path.like(f"{src_str}%"))
        .all()
    )
    for f in files_in_folder:
        f.path = f.path.replace(src_str, dst_str, 1)
        f.filename = Path(f.path).name

    return True, f"Renamed folder: {src.name} -> {dst.name} ({len(files_in_folder)} files updated)"


def _delete_duplicate(file_id: int, dry_run: bool) -> tuple[bool, str]:
    """Move a duplicate to a .trash folder instead of hard-deleting."""
    engine = get_engine()
    with Session(engine) as session:
        f = session.get(File, file_id)
        if not f:
            return False, "File not found in DB"
        path = Path(f.path)
        if not path.exists():
            return False, f"File not on disk: {path}"

        trash_dir = path.parent / ".datahoarder_trash"
        dst = trash_dir / path.name

        if dry_run:
            return True, f"[DRY RUN] Would trash: {path}"

        trash_dir.mkdir(exist_ok=True)
        # Handle collision in trash
        if dst.exists():
            stem, suffix = dst.stem, dst.suffix
            i = 1
            while dst.exists():
                dst = trash_dir / f"{stem}_{i}{suffix}"
                i += 1
        shutil.move(str(path), str(dst))

        f.path = str(dst)
        f.status = FileStatus.APPLIED
        session.commit()

    return True, f"Trashed: {path.name} -> {dst}"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def preview(min_confidence: float = 0.0) -> None:
    """Print a rich table of pending proposals."""
    engine = get_engine()
    with Session(engine) as session:
        proposals = (
            session.query(Proposal)
            .filter(Proposal.status == ProposalStatus.PENDING)
            .join(File)
            .all()
        )
        if not proposals:
            console.print("[yellow]No pending proposals.[/yellow]")
            return

        table = Table(
            title=f"Pending Proposals ({len(proposals)})",
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


def execute(
    dry_run: bool = True,
    min_confidence: float = 0.7,
    proposal_ids: Optional[list[int]] = None,
    proposal_types: Optional[list[ProposalType]] = None,
    session_id: str | None = None,
    _console: Console | None = None,
) -> dict:
    """
    Apply approved (or all pending) proposals.

    Args:
        dry_run:          If True, print what would happen but change nothing.
        min_confidence:   Only apply proposals above this confidence level.
        proposal_ids:     If set, only apply these specific proposal IDs.
        proposal_types:   If set, only apply these proposal types.
        _console:         Optional Console override (use _make_quiet_console() for web).

    Returns:
        Summary dict.
    """
    con = _console or console
    engine = get_engine()
    counts = {"applied": 0, "failed": 0, "skipped": 0}
    move_source_dirs: set[Path] = set()

    if dry_run:
        con.print("[bold yellow]DRY RUN -- no files will be changed[/bold yellow]\n")

    with Session(engine) as session:
        query = session.query(Proposal).filter(
            Proposal.status.in_([ProposalStatus.PENDING, ProposalStatus.APPROVED])
        )
        if session_id:
            query = query.join(File).filter(File.session_id == session_id)
        if proposal_ids:
            query = query.filter(Proposal.id.in_(proposal_ids))
        if proposal_types:
            query = query.filter(Proposal.proposal_type.in_(proposal_types))
        if min_confidence > 0:
            query = query.filter(
                (Proposal.confidence >= min_confidence) | (Proposal.confidence.is_(None))
            )

        proposals = query.all()
        con.print(f"[bold]Processing {len(proposals)} proposals...[/bold]")

        # Sort proposals: folder renames go FIRST (deepest paths first so
        # children are renamed before their parents), then file operations.
        def _sort_key(p):
            if p.proposal_type == ProposalType.RENAME_FOLDER:
                # Deepest paths first (most separators = most nested)
                depth = (p.current_value or "").count("\\") + (p.current_value or "").count("/")
                return (0, -depth)  # group 0, deepest first
            elif p.proposal_type == ProposalType.RENAME:
                return (1, 0)
            elif p.proposal_type == ProposalType.MOVE:
                return (2, 0)
            else:
                return (3, 0)

        proposals.sort(key=_sort_key)

        for prop in proposals:
            file_rec = session.get(File, prop.file_id)

            try:
                if prop.proposal_type == ProposalType.RENAME:
                    ok, msg = _apply_rename(prop, dry_run)
                    if ok and not dry_run:
                        # Update File.path in DB to new location
                        if file_rec:
                            old_path = file_rec.path
                            file_rec.path = prop.proposed_value
                            file_rec.filename = Path(prop.proposed_value).name
                            # Cascade: update MOVE proposals that reference
                            # this file's old path (since renames run before moves)
                            for other in proposals:
                                if other is prop or other.status == ProposalStatus.APPLIED:
                                    continue
                                if other.proposal_type == ProposalType.MOVE and other.file_id == prop.file_id:
                                    if other.current_value == old_path:
                                        # Update source path to new renamed path
                                        other.current_value = prop.proposed_value
                                        # Update destination to use new filename
                                        old_dst = Path(other.proposed_value)
                                        new_filename = Path(prop.proposed_value).name
                                        other.proposed_value = str(old_dst.parent / new_filename)

                elif prop.proposal_type == ProposalType.ADD_TAGS:
                    ok, msg = _apply_tags(prop, file_rec, dry_run)

                elif prop.proposal_type == ProposalType.RENAME_FOLDER:
                    ok, msg = _apply_rename_folder(prop, dry_run, session)
                    # After a successful folder rename, update paths in all
                    # remaining proposals that reference the old folder path
                    if ok and not dry_run:
                        old_prefix = prop.current_value
                        new_prefix = prop.proposed_value
                        for other in proposals:
                            if other is prop or other.status == ProposalStatus.APPLIED:
                                continue
                            if other.current_value and old_prefix in other.current_value:
                                other.current_value = other.current_value.replace(old_prefix, new_prefix, 1)
                            if other.proposed_value and old_prefix in other.proposed_value:
                                other.proposed_value = other.proposed_value.replace(old_prefix, new_prefix, 1)
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
                                other.applied_at = datetime.utcnow()
                                counts["applied"] += 1
                                con.print(f"  [dim]Skipped no-op move (folder renamed in-place): {Path(other.current_value).name}[/dim]")

                elif prop.proposal_type == ProposalType.MOVE:
                    ok, msg = _apply_move(prop, dry_run)
                    if ok and not dry_run:
                        if file_rec:
                            file_rec.path = prop.proposed_value
                            file_rec.filename = Path(prop.proposed_value).name

                elif prop.proposal_type == ProposalType.MARK_DUPLICATE:
                    ok, msg = _delete_duplicate(prop.file_id, dry_run)

                else:
                    ok, msg = True, f"Skipped unsupported type: {prop.proposal_type}"
                    counts["skipped"] += 1
                    continue

                if ok:
                    counts["applied"] += 1
                    if not dry_run:
                        prop.status = ProposalStatus.APPLIED
                        prop.applied_at = datetime.utcnow()
                        if file_rec and prop.proposal_type in (ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER):
                            file_rec.status = FileStatus.APPLIED
                    color = "green"
                else:
                    counts["failed"] += 1
                    if not dry_run:
                        prop.status = ProposalStatus.REJECTED
                    color = "red"

                con.print(f"  [{color}]{msg}[/{color}]")

            except Exception as exc:
                counts["failed"] += 1
                con.print(f"  [red]ERROR on proposal {prop.id}: {exc}[/red]")

        # Capture source dirs for cleanup BEFORE session closes (objects detach on close)
        move_source_dirs: set[Path] = set()
        if not dry_run:
            for prop in proposals:
                if (
                    prop.proposal_type == ProposalType.MOVE
                    and prop.status == ProposalStatus.APPLIED
                    and prop.current_value
                ):
                    move_source_dirs.add(Path(prop.current_value).parent)
            session.commit()

    # After applying all moves, clean up any now-empty source directories
    if not dry_run and move_source_dirs:
        _cleanup_empty_dirs(move_source_dirs)

    # Also sweep the session root for any pre-existing empty directories
    if not dry_run and session_id:
        from datahoarder.db.models import UserSession
        from sqlalchemy.orm import Session as _OrmSession
        try:
            with _OrmSession(engine) as _s:
                us = _s.get(UserSession, session_id)
                if us and us.root_path:
                    _cleanup_empty_dirs_recursive(Path(us.root_path))
        except Exception:
            pass

    con.print(
        f"\n[bold]Done:[/bold] {counts['applied']} applied, "
        f"{counts['failed']} failed, {counts['skipped']} skipped"
    )
    return counts


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



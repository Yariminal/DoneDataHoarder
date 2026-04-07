"""
Executor — applies approved Proposals to the filesystem.

ALWAYS run with dry_run=True first to preview changes.
All applied changes are logged to the database.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from datahoarder.db.models import (
    DuplicateGroup, DuplicateMember, File, FileStatus,
    Proposal, ProposalStatus, ProposalType,
)
from datahoarder.db.session import get_engine

console = Console()


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

    return True, f"{'[DRY RUN] ' if dry_run else ''}Renamed: {src.name} → {dst.name}"


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

    return True, f"Trashed: {path.name} → {dst}"


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
) -> dict:
    """
    Apply approved (or all pending) proposals.

    Args:
        dry_run:          If True, print what would happen but change nothing.
        min_confidence:   Only apply proposals above this confidence level.
        proposal_ids:     If set, only apply these specific proposal IDs.
        proposal_types:   If set, only apply these proposal types.

    Returns:
        Summary dict.
    """
    engine = get_engine()
    counts = {"applied": 0, "failed": 0, "skipped": 0}

    if dry_run:
        console.print("[bold yellow]DRY RUN — no files will be changed[/bold yellow]\n")

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
        console.print(f"[bold]Processing {len(proposals)} proposals…[/bold]")

        for prop in proposals:
            file_rec = session.get(File, prop.file_id)

            try:
                if prop.proposal_type == ProposalType.RENAME:
                    ok, msg = _apply_rename(prop, dry_run)
                    if ok and not dry_run:
                        # Update File.path in DB to new location
                        if file_rec:
                            file_rec.path = prop.proposed_value
                            file_rec.filename = Path(prop.proposed_value).name

                elif prop.proposal_type == ProposalType.ADD_TAGS:
                    ok, msg = _apply_tags(prop, file_rec, dry_run)

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
                        if file_rec and prop.proposal_type == ProposalType.RENAME:
                            file_rec.status = FileStatus.APPLIED
                    color = "green"
                else:
                    counts["failed"] += 1
                    prop.status = ProposalStatus.REJECTED
                    color = "red"

                console.print(f"  [{color}]{msg}[/{color}]")

            except Exception as exc:
                counts["failed"] += 1
                console.print(f"  [red]ERROR on proposal {prop.id}: {exc}[/red]")

        if not dry_run:
            session.commit()

    console.print(
        f"\n[bold]Done:[/bold] {counts['applied']} applied, "
        f"{counts['failed']} failed, {counts['skipped']} skipped"
    )
    return counts


def approve_all(min_confidence: float = 0.8) -> int:
    """Bulk-approve all PENDING proposals above confidence threshold."""
    engine = get_engine()
    with Session(engine) as session:
        updated = (
            session.query(Proposal)
            .filter(
                Proposal.status == ProposalStatus.PENDING,
                Proposal.confidence >= min_confidence,
            )
            .update({"status": ProposalStatus.APPROVED})
        )
        session.commit()
    return updated


def mark_duplicate_for_deletion(group_id: int, keep_file_id: Optional[int] = None) -> None:
    """
    Create MARK_DUPLICATE proposals for all non-keeper files in a duplicate group.
    """
    engine = get_engine()
    with Session(engine) as session:
        group = session.get(DuplicateGroup, group_id)
        if not group:
            raise ValueError(f"Duplicate group {group_id} not found")

        keep_id = keep_file_id or group.keep_file_id
        if keep_id:
            group.keep_file_id = keep_id

        for member in group.members:
            if member.file_id == keep_id:
                continue
            existing = (
                session.query(Proposal)
                .filter_by(file_id=member.file_id, proposal_type=ProposalType.MARK_DUPLICATE)
                .first()
            )
            if not existing:
                f = session.get(File, member.file_id)
                session.add(Proposal(
                    file_id=member.file_id,
                    proposal_type=ProposalType.MARK_DUPLICATE,
                    current_value=f.path if f else None,
                    proposed_value=".datahoarder_trash/",
                    reasoning=f"Duplicate of file ID {keep_id} (group {group_id})",
                    confidence=member.similarity_score or 1.0,
                    status=ProposalStatus.PENDING,
                ))
        session.commit()

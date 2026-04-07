"""
Proposal generator — builds rename / move / tag proposals for analyzed files.

For every ANALYZED file it creates one or more Proposal records:
- RENAME:  new filename based on date + AI description
- MOVE:    suggested destination folder (future)
- ADD_TAGS: metadata tags to embed

Naming conventions:
  Photos/Videos:   YYYY-MM-DD_HH-MM-SS_<description>.<ext>
                   YYYY-MM-DD_<description>.<ext>  (if no time)
  Documents:       YYYY-MM_<description>.<ext>
                   <description>.<ext>  (if no date)
  Other:           <description>.<ext>
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from datahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType
from datahoarder.db.session import get_engine

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".mp4", ".mov", ".avi", ".mkv",
    ".wmv", ".m4v", ".3gp", ".mp3", ".m4a", ".flac", ".wav",
}
DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt", ".xlsx", ".xls",
    ".pptx", ".ppt", ".txt", ".md", ".rtf",
}


# ---------------------------------------------------------------------------
# Name building
# ---------------------------------------------------------------------------

def _safe(text: str) -> str:
    """Sanitise a string for use in a filename."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)      # keep word chars, spaces, hyphens
    text = re.sub(r"[\s_]+", "_", text)       # normalise whitespace/underscores
    text = re.sub(r"-+", "-", text)
    text = text.strip("_-")
    return text[:60]


def _date_prefix(dt: Optional[datetime], include_time: bool = False) -> str:
    if not dt:
        return ""
    if include_time and (dt.hour or dt.minute or dt.second):
        return dt.strftime("%Y-%m-%d_%H-%M-%S")
    return dt.strftime("%Y-%m-%d")


def _month_prefix(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m")


def build_new_name(file_rec: File) -> Optional[str]:
    """
    Construct a proposed new filename (with extension) for a file.
    Prefers AI tags over description for more specific, meaningful names.
    Returns None if we can't improve on the original name.
    """
    path = Path(file_rec.path)
    ext = path.suffix.lower()
    desc = file_rec.ai_description or ""
    tags_str = file_rec.ai_tags or ""

    if not desc and not tags_str:
        return None

    stem_from_desc = None

    # Try to use tags first (more specific and meaningful)
    if tags_str:
        try:
            tags = json.loads(tags_str)
            # Filter out generic/vague tags that don't add meaningful info
            generic_tags = {"artwork", "photo", "image", "picture", "file", "document", "text"}
            specific_tags = [t.lower().replace(" ", "_") for t in tags if t.lower() not in generic_tags]

            if specific_tags:
                # Use first 2-3 most relevant tags for more descriptive names
                stem_from_desc = "_".join(specific_tags[:3])
                stem_from_desc = _safe(stem_from_desc)
        except (json.JSONDecodeError, TypeError):
            # If tags fail to parse, fall through to description
            pass

    # Fallback: use description if tags didn't work or were empty
    if not stem_from_desc and desc:
        words = re.sub(r"[^a-zA-Z0-9\s]", " ", desc).split()
        stem_from_desc = "_".join(w.lower() for w in words[:6] if len(w) > 2)
        stem_from_desc = _safe(stem_from_desc)

    if not stem_from_desc:
        return None

    dt = file_rec.date_best or file_rec.date_exif or file_rec.date_modified

    if ext in MEDIA_EXTENSIONS:
        date_part = _date_prefix(dt, include_time=True)
        stem = f"{date_part}_{stem_from_desc}" if date_part else stem_from_desc
    elif ext in DOC_EXTENSIONS:
        date_part = _month_prefix(dt)
        stem = f"{date_part}_{stem_from_desc}" if date_part else stem_from_desc
    else:
        stem = stem_from_desc

    # Clean up double underscores
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem + ext


def _resolve_collision(proposed_path: Path, original_path: Path) -> Path:
    """If proposed_path already exists (and isn't the same file), add a counter suffix."""
    if not proposed_path.exists() or proposed_path == original_path:
        return proposed_path
    stem = proposed_path.stem
    ext = proposed_path.suffix
    parent = proposed_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------

def generate_proposals(limit: Optional[int] = None, session_id: str | None = None) -> dict:
    """
    Create Proposal records for all ANALYZED files.

    Returns:
        Summary dict with proposal counts.
    """
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn,
    )

    engine = get_engine()
    counts = {"rename": 0, "tags": 0, "skipped": 0}

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.ANALYZED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if limit:
            query = query.limit(limit)
        total = query.count()

    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn, TaskProgressColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Generating proposals…", total=total)

        offset = 0
        while True:
            with Session(engine) as session:
                p_q = session.query(File).filter(File.status == FileStatus.ANALYZED)
                if session_id:
                    p_q = p_q.filter(File.session_id == session_id)
                batch = p_q.limit(100).offset(offset).all()
                if not batch:
                    break

                for file_rec in batch:
                    path = Path(file_rec.path)
                    made_proposal = False

                    # --- RENAME proposal ---
                    new_name = build_new_name(file_rec)
                    if new_name and new_name != path.name:
                        proposed_path = _resolve_collision(
                            path.parent / new_name, path
                        )
                        existing = (
                            session.query(Proposal)
                            .filter_by(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.RENAME,
                            )
                            .first()
                        )
                        if not existing:
                            session.add(Proposal(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.RENAME,
                                current_value=str(path),
                                proposed_value=str(proposed_path),
                                reasoning=(
                                    f"Renamed based on AI description: "
                                    f"{(file_rec.ai_description or '')[:120]}"
                                ),
                                confidence=file_rec.ai_confidence or 0.5,
                                status=ProposalStatus.PENDING,
                            ))
                            counts["rename"] += 1
                            made_proposal = True

                    # --- ADD_TAGS proposal ---
                    if file_rec.ai_tags:
                        existing_tag = (
                            session.query(Proposal)
                            .filter_by(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.ADD_TAGS,
                            )
                            .first()
                        )
                        if not existing_tag:
                            session.add(Proposal(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.ADD_TAGS,
                                current_value=None,
                                proposed_value=file_rec.ai_tags,
                                reasoning="Tags generated by AI analysis",
                                confidence=file_rec.ai_confidence or 0.5,
                                status=ProposalStatus.PENDING,
                            ))
                            counts["tags"] += 1
                            made_proposal = True

                    # Update file status
                    file_rec.status = FileStatus.PROPOSED
                    if not made_proposal:
                        counts["skipped"] += 1

                    progress.advance(task)

                session.commit()
                offset += 100

    return counts

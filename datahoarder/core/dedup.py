"""
Duplicate detector — finds exact and near-duplicate files.

Stage 1 — Exact:     group by MD5 hash (same bytes)
Stage 2 — Perceptual: group images by pHash distance ≤ threshold
Stage 3 — Content:   semantic similarity using AI descriptions and tags

Results are written to DuplicateGroup / DuplicateMember tables.
The "keep" file in each group defaults to the one with the earliest
best-date (i.e. original) and longest path (i.e. most specific location).
"""
import json
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from datahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
)
from datahoarder.db.session import get_engine

try:
    import imagehash
    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False

PHASH_THRESHOLD = 8  # max bit-distance to consider "near-duplicate" (lowered from 10 for better sensitivity)
AI_SIMILARITY_THRESHOLD = 0.55  # min similarity score for AI-based duplicates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_keeper(files: list[File]) -> int:
    """
    Pick the file to keep from a duplicate group.

    Strategy:
    1. Prefer the file with the earliest known date (most likely original).
    2. Break ties by preferring the longer path (more descriptive location).
    3. Break further ties by largest file size (higher quality).
    """
    def sort_key(f: File):
        date = f.date_best or f.date_modified or f.date_created or datetime(9999, 1, 1)
        return (date, -len(f.path), -(f.size_bytes or 0))

    return min(files, key=sort_key).id


def _upsert_group(
    session: Session,
    dupe_type: DupeType,
    group_hash: str,
    file_ids: list[int],
    similarity: float = 1.0,
    session_id: str | None = None,
) -> None:
    """Insert or update a DuplicateGroup and its members."""
    group = (
        session.query(DuplicateGroup)
        .filter_by(dupe_type=dupe_type, group_hash=group_hash)
        .first()
    )
    if group is None:
        kwargs = dict(dupe_type=dupe_type, group_hash=group_hash)
        if session_id:
            kwargs["session_id"] = session_id
        group = DuplicateGroup(**kwargs)
        session.add(group)
        session.flush()

    existing_member_ids = {m.file_id for m in group.members}
    for fid in file_ids:
        if fid not in existing_member_ids:
            session.add(DuplicateMember(
                group_id=group.id,
                file_id=fid,
                similarity_score=similarity,
            ))

    # Set keeper if not already set
    if group.keep_file_id is None:
        files = session.query(File).filter(File.id.in_(file_ids)).all()
        group.keep_file_id = _pick_keeper(files)


# ---------------------------------------------------------------------------
# Stage 1 — Exact duplicates (MD5)
# ---------------------------------------------------------------------------

def find_exact_duplicates(session_id: str | None = None) -> dict:
    """Group files by MD5 and record exact duplicate groups."""
    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        # Only consider enriched+ files with a hash
        q = (
            session.query(File.id, File.hash_md5)
            .filter(File.hash_md5.isnot(None))
            .filter(File.status.in_([FileStatus.ENRICHED, FileStatus.ANALYZED, FileStatus.PROPOSED]))
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    # Build hash → [id, ...] map
    hash_map: dict[str, list[int]] = defaultdict(list)
    for file_id, md5 in rows:
        hash_map[md5].append(file_id)

    dupes = {h: ids for h, ids in hash_map.items() if len(ids) > 1}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Finding exact duplicates…", total=len(dupes))

        with Session(engine) as session:
            for group_hash, file_ids in dupes.items():
                _upsert_group(session, DupeType.EXACT, group_hash, file_ids, session_id=session_id)
                counts["groups"] += 1
                counts["duplicates"] += len(file_ids) - 1
                progress.advance(task)
            session.commit()

    return counts


# ---------------------------------------------------------------------------
# Stage 2 — Perceptual duplicates (pHash)
# ---------------------------------------------------------------------------

def find_perceptual_duplicates(threshold: int = PHASH_THRESHOLD, session_id: str | None = None) -> dict:
    """Find near-duplicate images using perceptual hashing."""
    if not _HAS_IMAGEHASH:
        return {"error": "imagehash not installed"}

    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        q = (
            session.query(File.id, File.hash_perceptual)
            .filter(File.hash_perceptual.isnot(None))
            .filter(File.mime_type.like("image/%"))
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    if not rows:
        return counts

    # Build list of (id, pHash) pairs
    hashes = [(fid, imagehash.hex_to_hash(phash)) for fid, phash in rows]

    # O(n²) comparison — acceptable for up to ~50k images; can be improved with BK-tree
    visited: set[int] = set()
    groups: list[list[int]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Comparing image hashes…", total=len(hashes))

        for i, (id_a, hash_a) in enumerate(hashes):
            progress.advance(task)
            if id_a in visited:
                continue
            group = [id_a]
            for id_b, hash_b in hashes[i + 1:]:
                if id_b in visited:
                    continue
                if abs(hash_a - hash_b) <= threshold:
                    group.append(id_b)
            if len(group) > 1:
                for gid in group:
                    visited.add(gid)
                groups.append(group)

    with Session(engine) as session:
        for group in groups:
            # Use string of sorted IDs as group key
            group_hash = "-".join(str(x) for x in sorted(group))
            _upsert_group(
                session,
                DupeType.PERCEPTUAL,
                group_hash,
                group,
                similarity=0.95,
                session_id=session_id,
            )
            counts["groups"] += 1
            counts["duplicates"] += len(group) - 1
        session.commit()

    return counts


# ---------------------------------------------------------------------------
# Stage 3 — AI-based semantic duplicates (descriptions + tags)
# ---------------------------------------------------------------------------

def _string_similarity(s1: str, s2: str) -> float:
    """Calculate string similarity ratio (0.0 to 1.0)."""
    if not s1 or not s2:
        return 0.0
    return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()


def _tags_overlap(tags1: list[str], tags2: list[str]) -> float:
    """Calculate tag overlap as Jaccard similarity."""
    if not tags1 or not tags2:
        return 0.0
    set1, set2 = set(tags1), set(tags2)
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


def find_semantic_duplicates(session_id: str | None = None) -> dict:
    """Find semantically similar files using AI descriptions and tags."""
    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        # Only consider analyzed files with descriptions or tags
        q = (
            session.query(File.id, File.ai_description, File.ai_tags)
            .filter(File.status.in_([FileStatus.ANALYZED, FileStatus.PROPOSED]))
            .filter((File.ai_description.isnot(None)) | (File.ai_tags.isnot(None)))
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    if len(rows) < 2:
        return counts

    # O(n²) comparison of AI descriptions and tags
    visited: set[int] = set()
    groups: list[list[int]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Comparing AI descriptions…", total=len(rows))

        for i, (id_a, desc_a, tags_a) in enumerate(rows):
            progress.advance(task)
            if id_a in visited:
                continue

            group = [id_a]
            tags_a_list = []
            try:
                if tags_a:
                    tags_a_list = json.loads(tags_a)
            except (json.JSONDecodeError, TypeError):
                pass

            for id_b, desc_b, tags_b in rows[i + 1:]:
                if id_b in visited:
                    continue

                # Calculate similarity across description and tags
                desc_sim = _string_similarity(desc_a or "", desc_b or "")

                tags_b_list = []
                try:
                    if tags_b:
                        tags_b_list = json.loads(tags_b)
                except (json.JSONDecodeError, TypeError):
                    pass

                tags_sim = _tags_overlap(tags_a_list, tags_b_list)

                # Weighted average: 40% description, 60% tags (tags are more consistent)
                combined_sim = 0.4 * desc_sim + 0.6 * tags_sim

                if combined_sim >= AI_SIMILARITY_THRESHOLD:
                    group.append(id_b)

            if len(group) > 1:
                for gid in group:
                    visited.add(gid)
                groups.append(group)

    with Session(engine) as session:
        for group in groups:
            # Use string of sorted IDs as group key
            group_hash = "-".join(str(x) for x in sorted(group))
            _upsert_group(
                session,
                DupeType.PERCEPTUAL,  # Use PERCEPTUAL type for semantic duplicates too
                group_hash,
                group,
                similarity=0.85,
                session_id=session_id,
            )
            counts["groups"] += 1
            counts["duplicates"] += len(group) - 1
        session.commit()

    return counts


# ---------------------------------------------------------------------------
# Summary query
# ---------------------------------------------------------------------------

def duplicate_summary() -> list[dict]:
    """Return a summary of all duplicate groups for display."""
    engine = get_engine()
    results = []

    with Session(engine) as session:
        groups = session.query(DuplicateGroup).all()
        for group in groups:
            member_files = [
                session.get(File, m.file_id) for m in group.members
            ]
            member_files = [f for f in member_files if f]
            total_wasted = sum(
                f.size_bytes or 0
                for f in member_files
                if f.id != group.keep_file_id
            )
            results.append(
                {
                    "group_id": group.id,
                    "type": group.dupe_type,
                    "count": len(member_files),
                    "keep_id": group.keep_file_id,
                    "wasted_bytes": total_wasted,
                    "files": [f.path for f in member_files],
                }
            )

    return results

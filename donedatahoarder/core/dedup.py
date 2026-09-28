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
from functools import wraps
from typing import Callable

from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
    Proposal, ProposalStatus, ProposalType,
)
from donedatahoarder.db.session import get_engine

from donedatahoarder.config import load_phash_config
from donedatahoarder.phash import hash_distance
from donedatahoarder.core.process_lock import operation_lock

try:
    import imagehash
    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False

# Threshold loaded from user config (defaults to 8)
AI_SIMILARITY_THRESHOLD = 0.55  # min similarity score for AI-based duplicates
AI_SIMILARITY_THRESHOLD = 0.55  # min similarity score for AI-based duplicates
TEXT_NEAR_THRESHOLD = 0.90  # min SequenceMatcher ratio for near-identical text files
TEXT_DEDUP_SIZE_CAP = 200_000  # skip text files larger than ~200 KB to keep O(n^2) bounded
TEXT_EXTENSIONS = {
    ".txt", ".md", ".html", ".htm", ".xml", ".json", ".yaml", ".yml",
    ".csv", ".tsv", ".log", ".srt", ".vtt", ".rst", ".ini", ".cfg",
    ".py", ".js", ".ts", ".css", ".scss",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _writer_locked(function):
    """Protect direct CLI/API calls as well as background dedup stages."""
    @wraps(function)
    def locked(*args, **kwargs):
        with operation_lock(f"dedup:{function.__name__}"):
            return function(*args, **kwargs)
    return locked

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
    compare_to_keeper: Callable[[int, int], tuple[float, float | None]] | None = None,
) -> None:
    """Insert a candidate group with direct evidence against its keeper."""
    group = (
        session.query(DuplicateGroup)
        .filter_by(dupe_type=dupe_type, group_hash=group_hash)
        .filter(
            DuplicateGroup.session_id == session_id
            if session_id
            else DuplicateGroup.session_id.is_(None)
        )
        .first()
    )
    if group is None:
        kwargs = dict(dupe_type=dupe_type, group_hash=group_hash)
        if session_id:
            kwargs["session_id"] = session_id
        group = DuplicateGroup(**kwargs)
        session.add(group)
        session.flush()

    # A chosen keeper must be known before scores are attached. The score of
    # a transitive edge is never evidence about a different keeper.
    if group.keep_file_id is None:
        files = session.query(File).filter(File.id.in_(file_ids)).all()
        group.keep_file_id = _pick_keeper(files)

    existing_members = {m.file_id: m for m in group.members}
    for fid in file_ids:
        score, distance = (
            compare_to_keeper(group.keep_file_id, fid)
            if compare_to_keeper else (similarity, None)
        )
        member = existing_members.get(fid)
        if member is None:
            member = DuplicateMember(
                group_id=group.id,
                file_id=fid,
            )
            session.add(member)
        member.similarity_score = score
        member.distance_to_keeper = distance


def _score_member_to_keeper(group: DuplicateGroup, keeper: File, member: File) -> tuple[float, float | None]:
    """Recompute direct evidence when the keeper changes during review."""
    if keeper.id == member.id:
        return 1.0, 0.0 if group.dupe_type == DupeType.PERCEPTUAL else None
    if group.dupe_type == DupeType.EXACT:
        return (1.0 if keeper.hash_md5 and keeper.hash_md5 == member.hash_md5 else 0.0), None
    if group.dupe_type == DupeType.PERCEPTUAL:
        distance = hash_distance(keeper.hash_perceptual, member.hash_perceptual)
        if distance is None:
            return 0.0, None
        bits = max(len(keeper.hash_perceptual or "") * 4, 1)
        return max(0.0, 1.0 - distance / bits), float(distance)
    if group.dupe_type == DupeType.SEMANTIC:
        score = (0.4 * _string_similarity(keeper.ai_description or "", member.ai_description or "")
                 + 0.6 * _tags_overlap(_parse_tags(keeper.ai_tags), _parse_tags(member.ai_tags)))
        return score, None
    if group.dupe_type == DupeType.CONTENT:
        try:
            if max(keeper.size_bytes or 0, member.size_bytes or 0) > TEXT_DEDUP_SIZE_CAP:
                return 0.0, None
            from pathlib import Path
            left = Path(keeper.path).read_text(encoding="utf-8", errors="replace")
            right = Path(member.path).read_text(encoding="utf-8", errors="replace")
            return SequenceMatcher(None, left, right).ratio(), None
        except (OSError, UnicodeError):
            return 0.0, None
    return 0.0, None


# ---------------------------------------------------------------------------
# Stage 1 — Exact duplicates (MD5)
# ---------------------------------------------------------------------------

@_writer_locked
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
# Union-find — one component for a chain of near-duplicates
# ---------------------------------------------------------------------------

class _UnionFind:
    """Disjoint set. A~B and B~C become one component even when A is far from C."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}
        self._rank: dict[int, int] = {}

    def add(self, item: int) -> None:
        if item not in self._parent:
            self._parent[item] = item
            self._rank[item] = 0

    def find(self, item: int) -> int:
        parent = self._parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        rank = self._rank
        parent = self._parent
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    def components(self) -> dict[int, list[int]]:
        groups: dict[int, list[int]] = defaultdict(list)
        for item in self._parent:
            groups[self.find(item)].append(item)
        return groups


def _perceptual_bitstring(hex_hash: str) -> str | None:
    """MSB-first bits imagehash.hex_to_hash flattens, or None if unusable.

    hex_to_hash zero-extends to side*side when the integer is shorter, and
    keeps extra high bits when the format width (a minimum) does not clip.
    side < 2 is the case imagehash cannot compare.
    """
    if not isinstance(hex_hash, str) or not hex_hash:
        return None
    try:
        value = int(hex_hash, 16)
    except ValueError:
        return None
    side = int((len(hex_hash) * 4) ** 0.5)
    if side < 2:
        return None
    min_width = side * side
    bits = format(value, "b")
    if len(bits) < min_width:
        bits = bits.zfill(min_width)
    return bits


def _perceptual_candidate_pairs(hexes: list[str], threshold: int) -> list[tuple[str, str]]:
    """Pairs that share a hash band.

    threshold + 1 bands is the pigeonhole cut: Hamming distance <= threshold
    cannot spoil every band, so a true near-pair shares at least one.
    """
    n_bands = threshold + 1
    if n_bands < 1 or len(hexes) < 2:
        return []

    bits_of: dict[str, str] = {}
    by_len: dict[int, list[str]] = defaultdict(list)
    for hex_hash in hexes:
        bits = _perceptual_bitstring(hex_hash)
        if bits is None:
            continue
        bits_of[hex_hash] = bits
        by_len[len(bits)].append(hex_hash)

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []

    def _add_pair(left: str, right: str) -> None:
        if left == right:
            return
        key = (left, right) if left <= right else (right, left)
        if key in seen:
            return
        seen.add(key)
        pairs.append(key)

    for bit_len, group in by_len.items():
        if len(group) < 2:
            continue
        # Fewer bits than bands: a diff can land in every band and the
        # filter would drop pairs whose distance is still <= threshold.
        if n_bands > bit_len:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    _add_pair(group[i], group[j])
            continue

        buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
        base, extra = divmod(bit_len, n_bands)
        for hex_hash in group:
            bits = bits_of[hex_hash]
            pos = 0
            for band_index in range(n_bands):
                width = base + (1 if band_index < extra else 0)
                buckets[(band_index, bits[pos:pos + width])].append(hex_hash)
                pos += width
        for members in buckets.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    _add_pair(members[i], members[j])
    return pairs


def _parse_tags(raw: str | None) -> list[str]:
    """JSON tag list, or [] when the column is empty or not a list of strings."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [tag for tag in parsed if isinstance(tag, str)]


# ---------------------------------------------------------------------------
# Stage 2 — Perceptual duplicates (pHash)
# ---------------------------------------------------------------------------

@_writer_locked
def find_perceptual_duplicates(threshold: int | None = None, session_id: str | None = None) -> dict:
    """
    Find near-duplicate images and videos using perceptual hashing.

    Uses the configured threshold (or the provided override). Includes both
    image/* and video/* MIME types now that the enricher extracts frame
    thumbnails for videos.

    Candidates come from hash bands. Distance is hash_distance. Union-find
    puts a chain in one group when each hop is within threshold.
    """
    if not _HAS_IMAGEHASH:
        return {"error": "imagehash not installed"}

    cfg = load_phash_config()
    threshold = threshold if threshold is not None else cfg.get("threshold", 8)

    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        q = (
            session.query(File.id, File.hash_perceptual)
            .filter(File.hash_perceptual.isnot(None))
            .filter(
                (File.mime_type.like("image/%")) | (File.mime_type.like("video/%"))
            )
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    if len(rows) < 2:
        return counts

    hash_to_ids: dict[str, list[int]] = defaultdict(list)
    for file_id, phash in rows:
        if not phash:
            continue
        hash_to_ids[phash].append(file_id)

    if not hash_to_ids:
        return counts

    uf = _UnionFind()
    for phash, ids in hash_to_ids.items():
        for file_id in ids:
            uf.add(file_id)
        if len(ids) < 2:
            continue
        dist = hash_distance(phash, phash)
        if dist is not None and dist <= threshold:
            anchor = ids[0]
            for file_id in ids[1:]:
                uf.union(anchor, file_id)

    candidates = _perceptual_candidate_pairs(list(hash_to_ids), threshold)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(
            "Comparing perceptual hashes…",
            total=max(len(candidates), 1),
        )
        if not candidates:
            progress.advance(task)
        for hash_a, hash_b in candidates:
            dist = hash_distance(hash_a, hash_b)
            if dist is not None and dist <= threshold:
                uf.union(hash_to_ids[hash_a][0], hash_to_ids[hash_b][0])
            progress.advance(task)

    hash_by_id = {fid: phash for fid, phash in rows if phash}
    with Session(engine) as session:
        # Components only find candidates. Split every transitive chain into
        # keeper-centred groups so no member is presented as a near duplicate
        # of a keeper beyond the actual threshold.
        for component in uf.components().values():
            remaining = set(component)
            while len(remaining) > 1:
                files = session.query(File).filter(File.id.in_(remaining)).all()
                keep_id = _pick_keeper(files)
                keep_hash = hash_by_id[keep_id]
                group = sorted(
                    fid for fid in remaining
                    if (distance := hash_distance(keep_hash, hash_by_id[fid])) is not None
                    and distance <= threshold
                )
                remaining.difference_update(group)
                if len(group) < 2:
                    continue

                def compare(keeper: int, member: int) -> tuple[float, float]:
                    dist = hash_distance(hash_by_id[keeper], hash_by_id[member])
                    bits = max(len(hash_by_id[keeper]) * 4, 1)
                    return (max(0.0, 1.0 - dist / bits), float(dist))

                _upsert_group(
                    session, DupeType.PERCEPTUAL,
                    "-".join(str(x) for x in group), group,
                    session_id=session_id, compare_to_keeper=compare,
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


@_writer_locked
def find_semantic_duplicates(session_id: str | None = None) -> dict:
    """Find semantically similar files using AI descriptions and tags.

    A pair is scored only when it shares a tag and the same mime group.
    Union-find keeps a chain in one group. Each group is stored as
    DupeType.SEMANTIC with the mean similarity of the links that joined it.
    """
    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        # Only consider analyzed files with descriptions or tags
        q = (
            session.query(File.id, File.ai_description, File.ai_tags, File.mime_type)
            .filter(File.status.in_([FileStatus.ANALYZED, FileStatus.PROPOSED]))
            .filter((File.ai_description.isnot(None)) | (File.ai_tags.isnot(None)))
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    if len(rows) < 2:
        return counts

    # file_id, description, tags, mime group (part before '/')
    records: list[tuple[int, str, list[str], str]] = []
    for file_id, desc, tags_raw, mime in rows:
        records.append((
            file_id,
            desc or "",
            _parse_tags(tags_raw),
            (mime or "").split("/")[0],
        ))

    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, (_file_id, _desc, tags, mime_group) in enumerate(records):
        for tag in dict.fromkeys(tags):
            buckets[(mime_group, tag)].append(index)

    uf = _UnionFind()
    for file_id, _desc, _tags, _mime_group in records:
        uf.add(file_id)

    seen_pairs: set[tuple[int, int]] = set()
    links: list[tuple[int, int, float]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(
            "Comparing AI descriptions…",
            total=max(len(buckets), 1),
        )
        if not buckets:
            progress.advance(task)
        for indexes in buckets.values():
            progress.advance(task)
            if len(indexes) < 2:
                continue
            for left in range(len(indexes)):
                ia = indexes[left]
                id_a, desc_a, tags_a, _mime_a = records[ia]
                for right in range(left + 1, len(indexes)):
                    ib = indexes[right]
                    pair = (ia, ib) if ia < ib else (ib, ia)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    id_b, desc_b, tags_b, _mime_b = records[ib]
                    # 40% description, 60% tags (tags are more consistent)
                    combined = (
                        0.4 * _string_similarity(desc_a, desc_b)
                        + 0.6 * _tags_overlap(tags_a, tags_b)
                    )
                    if combined >= AI_SIMILARITY_THRESHOLD:
                        uf.union(id_a, id_b)
                        links.append((id_a, id_b, combined))

    records_by_id = {row[0]: row for row in records}

    def direct_semantic(keeper: int, member: int) -> tuple[float, None]:
        _id_a, desc_a, tags_a, _mime_a = records_by_id[keeper]
        _id_b, desc_b, tags_b, _mime_b = records_by_id[member]
        return (0.4 * _string_similarity(desc_a, desc_b)
                + 0.6 * _tags_overlap(tags_a, tags_b), None)

    grouped: list[list[int]] = []
    for _root, members in uf.components().items():
        if len(members) < 2:
            continue
        grouped.append(sorted(members))

    with Session(engine) as session:
        for group in grouped:
            group_hash = "-".join(str(x) for x in group)
            _upsert_group(
                session,
                DupeType.SEMANTIC,
                group_hash,
                group,
                session_id=session_id,
                compare_to_keeper=direct_semantic,
            )
            counts["groups"] += 1
            counts["duplicates"] += len(group) - 1
        session.commit()

    return counts


# ---------------------------------------------------------------------------
# Stage 4 — Near-identical text files (byte-level fuzzy match on small text)
# ---------------------------------------------------------------------------

@_writer_locked
def find_text_near_duplicates(
    session_id: str | None = None,
    threshold: float = TEXT_NEAR_THRESHOLD,
    size_cap: int = TEXT_DEDUP_SIZE_CAP,
) -> dict:
    """
    Catch near-identical text files (e.g. two HTML files differing by only a
    few hundred bytes — a comment, an updated link, a timestamp).

    Why this exists: such files have different MD5s (so the exact stage misses),
    no perceptual hash (so the perceptual stage skips them), and often weak/
    similar AI tag output (so the semantic stage's 0.55 threshold may miss
    them too). This stage compares raw text content directly.

    Strategy:
    - Restrict to text-like files (mime text/* or known text extension)
    - Skip files larger than size_cap bytes (keeps the pairwise scan bounded)
    - Group only within the same extension (don't compare .py vs .html)
    - Pre-filter pairs by length: skip if lengths differ by more than 10%
    - Use SequenceMatcher ratio; group if >= threshold
    """
    from pathlib import Path as _P

    engine = get_engine()
    counts = {"groups": 0, "duplicates": 0}

    with Session(engine) as session:
        q = (
            session.query(File.id, File.path, File.mime_type, File.extension, File.size_bytes)
            .filter(File.status.in_([
                FileStatus.ENRICHED, FileStatus.ANALYZED, FileStatus.PROPOSED,
            ]))
        )
        if session_id:
            q = q.filter(File.session_id == session_id)
        rows = q.all()

    # Filter to text-like, small enough files
    candidates = []
    for fid, fpath, mime, ext, size in rows:
        if size is None or size > size_cap or size == 0:
            continue
        ext_lc = (ext or "").lower()
        if not ext_lc.startswith("."):
            ext_lc = "." + ext_lc if ext_lc else ""
        is_text_mime = bool(mime and mime.startswith("text/"))
        is_text_ext = ext_lc in TEXT_EXTENSIONS
        if not (is_text_mime or is_text_ext):
            continue
        candidates.append((fid, fpath, ext_lc, size))

    if len(candidates) < 2:
        return counts

    # Read content (best-effort) — skip files that can't be decoded
    contents: dict[int, tuple[str, str, int]] = {}  # id -> (text, ext, size)
    for fid, fpath, ext_lc, size in candidates:
        try:
            text = _P(fpath).read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        contents[fid] = (text, ext_lc, size)

    # Bucket by extension to avoid cross-ext comparisons (.py vs .html etc.)
    by_ext: dict[str, list[int]] = defaultdict(list)
    for fid, (_text, ext_lc, _size) in contents.items():
        by_ext[ext_lc].append(fid)

    visited: set[int] = set()
    groups: list[tuple[list[int], float]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Comparing text content…", total=len(contents))

        for ext_lc, ids in by_ext.items():
            for i, id_a in enumerate(ids):
                progress.advance(task)
                if id_a in visited:
                    continue
                text_a, _, size_a = contents[id_a]
                group = [id_a]
                sims: list[float] = []
                for id_b in ids[i + 1:]:
                    if id_b in visited:
                        continue
                    text_b, _, size_b = contents[id_b]
                    # Cheap length pre-filter: if sizes differ by >10%, skip
                    if size_a == 0 or size_b == 0:
                        continue
                    if abs(size_a - size_b) / max(size_a, size_b) > 0.10:
                        continue
                    ratio = SequenceMatcher(None, text_a, text_b).quick_ratio()
                    if ratio < threshold:
                        # quick_ratio is an upper bound; if even that fails, skip
                        continue
                    real_ratio = SequenceMatcher(None, text_a, text_b).ratio()
                    if real_ratio >= threshold:
                        group.append(id_b)
                        sims.append(real_ratio)
                if len(group) > 1:
                    for gid in group:
                        visited.add(gid)
                    avg_sim = sum(sims) / len(sims) if sims else threshold
                    groups.append((group, round(avg_sim, 3)))

    def direct_content(keeper: int, member: int) -> tuple[float, None]:
        return (SequenceMatcher(None, contents[keeper][0], contents[member][0]).ratio(), None)

    with Session(engine) as session:
        for group, _avg_sim in groups:
            group_hash = "-".join(str(x) for x in sorted(group))
            _upsert_group(
                session,
                DupeType.CONTENT,
                group_hash,
                group,
                session_id=session_id,
                compare_to_keeper=direct_content,
            )
            counts["groups"] += 1
            counts["duplicates"] += len(group) - 1
        session.commit()

    return counts


# ---------------------------------------------------------------------------
# Stage 5 — Convert duplicate groups into actionable MARK_DUPLICATE proposals
# ---------------------------------------------------------------------------

@_writer_locked
def generate_dedup_proposals(session_id: str | None = None) -> dict:
    """
    Walk every DuplicateGroup for the session and emit a MARK_DUPLICATE proposal
    for each member that is NOT the group's keeper.

    This is what makes the four detection stages actionable. Without it, the
    DB ends up with DuplicateGroup / DuplicateMember rows that the executor
    already knows how to handle (it moves the file to .ddh_trash) but
    no Proposal row ever surfaces those groups to the UI / approval flow, so
    no duplicate is ever cleaned up.

    A MARK_DUPLICATE proposal stores:
      - file_id        : the duplicate to evict
      - current_value  : its current path (for the UI "from" column)
      - proposed_value : the keeper's path (for the UI "merge into" column)
      - reasoning      : human-readable justification including dupe type
      - confidence     : 1.0 for EXACT, similarity_score for the rest

    Keeps one proposal per victim, preferring exact evidence, and never treats
    a near-match score as permission to discard a file.
    """
    engine = get_engine()
    counts = {"groups": 0, "created": 0, "skipped": 0, "no_keeper": 0}

    type_label = {
        DupeType.EXACT:      "exact byte-for-byte duplicate",
        DupeType.PERCEPTUAL: "perceptual similarity candidate",
        DupeType.SEMANTIC:   "semantic near-duplicate (similar AI tags/description)",
        DupeType.CONTENT:    "near-identical text content",
    }

    with Session(engine) as session:
        q = session.query(DuplicateGroup)
        if session_id:
            q = q.filter(DuplicateGroup.session_id == session_id)
        groups = q.all()
        priority = {DupeType.EXACT: 0, DupeType.PERCEPTUAL: 1,
                    DupeType.CONTENT: 2, DupeType.SEMANTIC: 3}
        groups.sort(key=lambda group: (priority[group.dupe_type], group.id))

        if not groups:
            return counts

        # Pre-load every existing MARK_DUPLICATE proposal so we don't double up
        # when the dedup endpoint is re-run after a partial application.
        existing_marked: set[int] = {
            file_id
            for (file_id,) in session.query(Proposal.file_id).filter(
                Proposal.proposal_type == ProposalType.MARK_DUPLICATE,
            )
        }

        for group in groups:
            counts["groups"] += 1

            # Make sure we have a keeper. Older rows or aborted runs may have
            # left keep_file_id NULL — re-elect deterministically rather than
            # skipping the group (which would silently lose a duplicate
            # detection).
            keep_id = group.keep_file_id
            if keep_id is None:
                member_files = [
                    session.get(File, m.file_id) for m in group.members
                ]
                member_files = [f for f in member_files if f is not None]
                if len(member_files) < 2:
                    counts["no_keeper"] += 1
                    continue
                keep_id = _pick_keeper(member_files)
                group.keep_file_id = keep_id

            keep_file = session.get(File, keep_id)
            keep_path = keep_file.path if keep_file else "(unknown)"
            label = type_label.get(group.dupe_type, str(group.dupe_type))

            for m in group.members:
                if m.file_id == keep_id:
                    continue
                if m.file_id in existing_marked:
                    counts["skipped"] += 1
                    continue
                victim = session.get(File, m.file_id)
                if victim is None:
                    counts["skipped"] += 1
                    continue

                similarity = m.similarity_score or 0.0
                # Candidate similarity is evidence, not a calibrated
                # probability. Only exact matches receive auto-approval
                # confidence; all other classes require individual review.
                confidence = 1.0 if group.dupe_type == DupeType.EXACT else None

                reasoning = (
                    f"{label} (direct keeper similarity={similarity:.3f}). "
                    f"Candidate for individual review; keeper: {keep_path}"
                )

                session.add(Proposal(
                    file_id=victim.id,
                    proposal_type=ProposalType.MARK_DUPLICATE,
                    current_value=victim.path,
                    proposed_value=keep_path,
                    reasoning=reasoning,
                    confidence=confidence,
                    status=ProposalStatus.PENDING,
                    duplicate_group_id=group.id,
                ))
                existing_marked.add(victim.id)
                counts["created"] += 1

        session.commit()

    return counts


def refresh_group_proposals(session: Session, group_id: int) -> dict[str, int]:
    """Refresh direct evidence and reset decisions after a keeper change.

    The caller owns the transaction. APPLIED history must first be undone;
    otherwise changing the keeper would rewrite the reason for past disposal.
    """
    group = session.get(DuplicateGroup, group_id)
    if group is None or group.keep_file_id is None:
        raise ValueError("Duplicate group has no selected keeper")
    members = {m.file_id: m for m in group.members}
    if group.keep_file_id not in members:
        raise ValueError("Selected keeper is not a group member")
    keeper = session.get(File, group.keep_file_id)
    if keeper is None or (group.session_id and keeper.session_id != group.session_id):
        raise ValueError("Selected keeper is not in the duplicate session")
    proposals = session.query(Proposal).filter(
        Proposal.duplicate_group_id == group.id,
        Proposal.proposal_type == ProposalType.MARK_DUPLICATE,
    ).all()
    if any(p.status == ProposalStatus.APPLIED for p in proposals):
        raise ValueError("Undo applied duplicate proposals before changing keeper")
    by_file = {p.file_id: p for p in proposals}
    changed = created = 0
    for member_id, member in members.items():
        victim = session.get(File, member_id)
        if victim is None:
            continue
        score, distance = _score_member_to_keeper(group, keeper, victim)
        member.similarity_score = score
        member.distance_to_keeper = distance
        proposal = by_file.get(member_id)
        if member_id == keeper.id:
            if proposal is not None:
                # This file is now preserved. Retire its stale decision.
                proposal.status = ProposalStatus.REJECTED
                proposal.review_kind = None
                proposal.user_notes = "Keeper changed; this file is now kept"
                changed += 1
            continue
        if proposal is None:
            # Do not silently replace a decision from another evidence group.
            existing = session.query(Proposal).filter(
                Proposal.file_id == member_id,
                Proposal.proposal_type == ProposalType.MARK_DUPLICATE,
            ).first()
            if existing is not None:
                continue
            proposal = Proposal(
                file_id=member_id, proposal_type=ProposalType.MARK_DUPLICATE,
                duplicate_group_id=group.id, current_value=victim.path,
                status=ProposalStatus.PENDING,
            )
            session.add(proposal)
            created += 1
        proposal.current_value = victim.path
        proposal.proposed_value = keeper.path
        proposal.confidence = (
            1.0 if group.dupe_type == DupeType.EXACT and score == 1.0 else None
        )
        proposal.reasoning = (
            f"{group.dupe_type.value} candidate; direct keeper similarity={score:.3f}. "
            f"Keeper: {keeper.path}"
        )
        proposal.status = ProposalStatus.PENDING
        proposal.review_kind = None
        proposal.applied_at = None
        proposal.user_notes = "Keeper changed; review this candidate again"
        changed += 1
    return {"changed": changed, "created": created}


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper
# ---------------------------------------------------------------------------

def dedup_with_progress(
    session_id: str | None = None,
    pause_event=None,
    cancel_check=None,
):
    """
    Like running the dedup endpoint sequentially, but yields progress dicts
    suitable for the JobManager's SSE streaming. Survives browser disconnect.

    Phases (cancel-checkable between each):
      1. exact         — hash-based exact duplicates
      2. perceptual    — pHash near-duplicate images/videos
      3. semantic      — AI-description / tag similarity
      4. text_near     — byte-level fuzzy text match
      5. proposals     — emit MARK_DUPLICATE proposals

    Yields:
      {"phase": str, "current": N, "total": 5, **counts}  per phase
      {"cancelled": True, ...}                             if cancel_check fires
      {"done": True, "exact": ..., "perceptual": ..., ...} terminal
    """
    import contextlib
    import io

    PHASES = ["exact", "perceptual", "semantic", "text_near", "proposals"]
    TOTAL = len(PHASES)
    results: dict[str, dict] = {}

    def _check_pause_cancel() -> bool:
        """Return True if cancellation requested. Block on pause."""
        if pause_event is not None:
            pause_event.wait()
        return bool(cancel_check and cancel_check())

    # Initial yield so subscribers see the job has started
    yield {"phase": "starting", "current": 0, "total": TOTAL}

    # Suppress Rich Progress output (each find_* uses its own progress bar)
    with operation_lock("dedup"), contextlib.redirect_stdout(io.StringIO()):
        for idx, phase in enumerate(PHASES, start=1):
            if _check_pause_cancel():
                yield {"cancelled": True, "phase": phase, "current": idx - 1, "total": TOTAL, **results}
                return

            # Announce phase start
            yield {"phase": phase, "current": idx - 1, "total": TOTAL, "status": "running", **results}

            try:
                if phase == "exact":
                    results["exact"] = find_exact_duplicates(session_id=session_id)
                elif phase == "perceptual":
                    results["perceptual"] = find_perceptual_duplicates(session_id=session_id)
                elif phase == "semantic":
                    results["semantic"] = find_semantic_duplicates(session_id=session_id)
                elif phase == "text_near":
                    results["text_near"] = find_text_near_duplicates(session_id=session_id)
                elif phase == "proposals":
                    proposal_counts = generate_dedup_proposals(session_id=session_id)
                    # generate_dedup_proposals returns int OR dict depending on
                    # version; normalise to dict for the UI
                    if isinstance(proposal_counts, int):
                        results["proposals"] = {"created": proposal_counts}
                    else:
                        results["proposals"] = proposal_counts
            except Exception as exc:
                # Surface phase failure but keep going to next phase
                results[phase] = {"error": str(exc)[:200]}

            # Phase done — emit completion progress
            yield {"phase": phase, "current": idx, "total": TOTAL, "status": "complete", **results}

    yield {"done": True, "current": TOTAL, "total": TOTAL, **results}


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

"""
Proposal generator — builds rename / move / tag proposals for analyzed files.

For every ANALYZED file it creates one or more Proposal records:
- RENAME:  new filename based on date + AI description
- MOVE:    suggested destination folder (future)
- ADD_TAGS: metadata tags to embed

Naming conventions:
  Photos/Videos:   YYYY-MM-DD_HH-MM-SS_<description>.<ext>
                   YYYY-MM-DD_<description>.<ext>  (if no time component)
  Documents:       YYYY-MM-DD_<description>.<ext>
                   <description>.<ext>  (if no meaningful date)
  Other:           <description>.<ext>
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from datahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession
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


def _is_meaningful_date(file_rec) -> bool:
    """
    Check if the file has a meaningful date (EXIF, or filesystem date that
    isn't just the scan/extract timestamp).
    """
    # EXIF date is always meaningful
    if file_rec.date_exif:
        return True
    # If date_modified is within 48h of date_created, it was likely
    # mass-copied/extracted — the date is noise, not signal.
    if file_rec.date_modified and file_rec.date_created:
        delta = abs((file_rec.date_modified - file_rec.date_created).total_seconds())
        if delta < 60:  # modified and created within 1 minute = freshly extracted
            return False
    # If we have a modified date that's at least different from created, it's meaningful
    if file_rec.date_modified:
        return True
    return False


def build_new_name(file_rec: File) -> Optional[str]:
    """
    Construct a proposed new filename (with extension) for a file.
    Prefers AI tags over description for more specific, meaningful names.
    Preserves sequential/numbered patterns (e.g., "class_1", "class_2") while enhancing with description.
    Returns None if we can't improve on the original name.
    """
    from datahoarder.proposals.sequence_detector import detect_sequences

    path = Path(file_rec.path)
    ext = path.suffix.lower()
    desc = file_rec.ai_description or ""
    tags_str = file_rec.ai_tags or ""

    if not desc and not tags_str:
        return None

    # Guard: skip if AI description is actually an error message
    _error_keywords = {
        "could not open", "cannot identify", "cannot open", "error",
        "failed to", "not found", "no such file", "unsupported",
        "unable to", "traceback", "exception",
    }
    desc_lower = desc.lower()
    if any(kw in desc_lower for kw in _error_keywords):
        return None

    stem_from_desc = None

    # Try AI's suggested_name first — it's the most specific and preserves proper nouns
    # (e.g. "liberman_house_final_submission", "greece_partnership_agreement")
    if file_rec.ai_suggested_name:
        stem_from_desc = _safe(file_rec.ai_suggested_name)

    # Fallback: try tags (more specific than a free-text description)
    if not stem_from_desc and tags_str:
        try:
            tags = json.loads(tags_str)
            # Filter out generic/vague tags that don't add meaningful info
            generic_tags = {
                "artwork", "photo", "image", "picture", "file", "document", "text",
                "other", "place", "object", "scene", "unknown", "misc",
            }
            # Also filter compound tags like "photo_place", "photo_object"
            generic_prefixes = ("photo_", "image_", "picture_")

            def _is_generic(tag: str) -> bool:
                t = tag.lower().replace(" ", "_")
                if t in generic_tags:
                    return True
                if t.startswith(generic_prefixes):
                    return True
                return False

            specific_tags = [t.lower().replace(" ", "_") for t in tags if not _is_generic(t)]

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

    # Only use date prefix if the date is meaningful (not just a copy/extract timestamp).
    # Prefer real hardware sources (EXIF > filesystem) over AI-inferred date_best,
    # because date_best may contain LLM-guessed dates that can be wrong.
    has_good_date = _is_meaningful_date(file_rec)
    dt = (file_rec.date_exif or file_rec.date_modified) if has_good_date else None

    if ext in MEDIA_EXTENSIONS:
        # Photos/videos: full timestamp if available, otherwise date only
        date_part = _date_prefix(dt, include_time=True)
        stem = f"{date_part}_{stem_from_desc}" if date_part else stem_from_desc
    elif ext in DOC_EXTENSIONS:
        # Documents: date only (no time component), consistent with media format
        date_part = _date_prefix(dt, include_time=False)
        stem = f"{date_part}_{stem_from_desc}" if date_part else stem_from_desc
    else:
        stem = stem_from_desc

    # Clean up double underscores
    stem = re.sub(r"_+", "_", stem).strip("_")

    # --- SEQUENCE PATTERN PRESERVATION ---
    # Check if this file is part of a numbered sequence
    sequence_info = detect_sequences(path.parent, path.name)
    if sequence_info:
        # Extract the original number from the current filename
        original_match = re.search(
            rf"{re.escape(sequence_info.base_name)}{re.escape(sequence_info.separator)}(\d+)",
            path.stem
        )
        if original_match:
            original_number_str = original_match.group(1)
            original_number = int(original_number_str)

            # Reconstruct filename preserving the sequence pattern
            # Format: base_name{sep}{number}_{description}.ext
            formatted_number = sequence_info.format_number(original_number)
            stem = f"{sequence_info.base_name}{sequence_info.separator}{formatted_number}_{stem_from_desc}"
            # Clean up double underscores that might have resulted
            stem = re.sub(r"_+", "_", stem).strip("_")

    return stem + ext


# ---------------------------------------------------------------------------
# Translation (language normalization)
# ---------------------------------------------------------------------------

_translation_cache: dict[tuple[str, str], str] = {}  # (filename, target_lang) → translated


def translate_filename(filename: str, target_language: str) -> str:
    """
    Translate a filename to the target language using LLM.

    Args:
        filename: The filename to translate (including extension)
        target_language: "english", "hebrew", or "leave_as_is"

    Returns:
        Translated filename, or original if target_language is "leave_as_is"
    """
    if target_language == "leave_as_is":
        return filename

    # Check cache first
    cache_key = (filename, target_language)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    try:
        from datahoarder.ai.router import get_client

        # Separate filename from extension
        stem, ext = Path(filename).stem, Path(filename).suffix

        # Build the translation prompt
        lang_name = "English" if target_language == "english" else "Hebrew"
        prompt = (
            f"Translate this filename to {lang_name}, preserving the file extension. "
            f"Return ONLY the translated filename with the extension, nothing else.\n\n"
            f"Original: {filename}"
        )

        # Call LLM for translation
        client = get_client()
        translated_filename = client.generate(prompt).strip()

        # Ensure extension is preserved
        if not translated_filename.endswith(ext):
            translated_stem = Path(translated_filename).stem
            translated_filename = translated_stem + ext

        # Cache the result
        _translation_cache[cache_key] = translated_filename
        return translated_filename

    except Exception:
        # On any error, return original filename
        return filename


def _resolve_collision(
    proposed_path: Path,
    original_path: Path,
    reserved_names: set[Path] | None = None,
) -> Path:
    """
    Resolve filename collisions by adding a counter suffix.

    Checks against:
    1. Files already on disk
    2. Previously proposed names in this batch (reserved_names)

    Args:
        proposed_path: The desired target path
        original_path: The current file path (allow renaming to self)
        reserved_names: Set of paths already proposed in this batch

    Returns:
        A non-conflicting path (with _1, _2, etc. suffix if needed)
    """
    reserved = reserved_names or set()

    # If no conflict, return as-is
    if (not proposed_path.exists() and proposed_path not in reserved) or proposed_path == original_path:
        return proposed_path

    # Add counter suffix to resolve conflicts
    stem = proposed_path.stem
    ext = proposed_path.suffix
    parent = proposed_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{ext}"
        if not candidate.exists() and candidate not in reserved:
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Spelling normalisation across a session
# ---------------------------------------------------------------------------

def _normalize_spelling_in_proposals(session_id: str | None) -> int:
    """
    After all RENAME proposals are generated, look for spelling variants of the
    same word across the session (e.g. solar_decathlon vs solar_dekathlon) and
    rewrite less-frequent variants to a canonical form.

    Algorithm:
    - Tokenise every proposed filename (stem only) into alphabetic runs >= 5 chars
    - Bucket tokens by their first 2 characters (cheap prefix gate so unrelated
      short edits never cluster). 2 chars instead of 3 so that single-letter
      substitutions at index 2 — e.g. de(c)athlon vs de(k)athlon, re(c)ieve vs
      re(c)eive — still land in the same bucket and get compared.
    - Inside each bucket, run a fast SequenceMatcher.quick_ratio() pre-filter
      to skip obviously dissimilar pairs, then full ratio() >= 0.85 clusters.
    - Pick the most-frequent token in each cluster as canonical
    - Substitute non-canonical occurrences in proposed_value, preserving everything
      else (date prefix, sequence number, extension)

    Returns the number of proposals rewritten.
    """
    from collections import Counter, defaultdict
    from difflib import SequenceMatcher

    if not session_id:
        return 0

    engine = get_engine()
    rewritten = 0

    with Session(engine) as session:
        proposals = (
            session.query(Proposal)
            .join(File, Proposal.file_id == File.id)
            .filter(
                File.session_id == session_id,
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.status == ProposalStatus.PENDING,
            )
            .all()
        )

        if len(proposals) < 2:
            return 0

        token_re = re.compile(r"[a-zA-Z]{5,}")

        # Count token frequencies across all proposed names
        token_counter: Counter[str] = Counter()
        for p in proposals:
            stem = Path(p.proposed_value or "").stem.lower()
            token_counter.update(token_re.findall(stem))

        if not token_counter:
            return 0

        # Bucket by first 2 chars; within each bucket, find similarity clusters.
        # 2 chars (not 3) so that typos at index 2 — decathlon/dekathlon,
        # recieve/receive — still collide into the same bucket.
        buckets: dict[str, list[str]] = defaultdict(list)
        for tok in token_counter:
            buckets[tok[:2]].append(tok)

        canonical: dict[str, str] = {}
        for prefix, toks in buckets.items():
            if len(toks) < 2:
                continue
            # Sort by descending frequency so the most-popular spelling tends to
            # become the cluster anchor and therefore the canonical form.
            toks_sorted = sorted(toks, key=lambda t: -token_counter[t])
            visited: set[str] = set()
            for i, t1 in enumerate(toks_sorted):
                if t1 in visited:
                    continue
                cluster = [t1]
                for t2 in toks_sorted[i + 1:]:
                    if t2 in visited or t1 == t2:
                        continue
                    # quick_ratio is an upper bound on ratio() — if it's already
                    # below the threshold, the full ratio cannot exceed 0.85,
                    # so we save the expensive call.
                    sm = SequenceMatcher(None, t1, t2)
                    if sm.quick_ratio() < 0.85:
                        continue
                    if sm.ratio() >= 0.85:
                        cluster.append(t2)
                if len(cluster) > 1:
                    # The first member (highest freq) is canonical.
                    canon = cluster[0]
                    for tok in cluster:
                        canonical[tok] = canon
                        visited.add(tok)

        # Drop self-mappings (token already canonical)
        canonical = {k: v for k, v in canonical.items() if k != v}
        if not canonical:
            return 0

        def _replace(match: re.Match) -> str:
            word = match.group(0)
            canon = canonical.get(word.lower())
            return canon if canon else word

        for p in proposals:
            old = p.proposed_value or ""
            path = Path(old)
            new_stem = token_re.sub(_replace, path.stem)
            if new_stem != path.stem:
                # Path.with_stem preserves the extension; use the same parent
                p.proposed_value = str(path.with_stem(new_stem))
                rewritten += 1

        if rewritten:
            session.commit()

    return rewritten


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------

def generate_proposals(limit: Optional[int] = None, session_id: str | None = None) -> dict:
    """
    Create Proposal records for all ANALYZED files.
    Optionally translates filenames based on session's preferred_language setting.

    Returns:
        Summary dict with proposal counts.
    """
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn,
    )

    engine = get_engine()
    counts = {"rename": 0, "tags": 0, "skipped": 0}

    # Get the session's language preference
    preferred_language = "leave_as_is"
    if session_id:
        with Session(engine) as session:
            user_sess = session.get(UserSession, session_id)
            if user_sess:
                preferred_language = user_sess.preferred_language

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.ANALYZED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        if limit:
            query = query.limit(limit)
        total = query.count()


    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Generating proposals…", total=total)

        reserved_names: set[Path] = set()  # Track proposed names to prevent collisions within batch

        while True:
            with Session(engine) as session:
                p_q = session.query(File).filter(File.status == FileStatus.ANALYZED)
                if session_id:
                    p_q = p_q.filter(File.session_id == session_id)
                # Always query from offset 0: processed files change status
                # from ANALYZED to PROPOSED and no longer match the filter.
                batch = p_q.limit(100).all()
                if not batch:
                    break

                # Load existing proposals for this whole batch in one query
                # instead of firing two SELECTs per file (N+1 pattern).
                batch_ids = {f.id for f in batch}
                existing_proposals: set[tuple[int, ProposalType]] = {
                    (p.file_id, p.proposal_type)
                    for p in session.query(Proposal.file_id, Proposal.proposal_type)
                    .filter(Proposal.file_id.in_(batch_ids))
                }

                for file_rec in batch:
                    path = Path(file_rec.path)
                    made_proposal = False

                    # --- RENAME proposal ---
                    new_name = build_new_name(file_rec)
                    if new_name and new_name != path.name:
                        # Apply language translation if preferred
                        if preferred_language != "leave_as_is":
                            new_name = translate_filename(new_name, preferred_language)

                        proposed_path = _resolve_collision(
                            path.parent / new_name, path, reserved_names=reserved_names
                        )
                        # Add to reserved names so other files in this batch won't collide
                        reserved_names.add(proposed_path)
                        if (file_rec.id, ProposalType.RENAME) not in existing_proposals:
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
                        if (file_rec.id, ProposalType.ADD_TAGS) not in existing_proposals:
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

    # Post-pass: collapse spelling variants across all proposed names so that
    # "solar_decathlon" and "solar_dekathlon" don't both appear in the same
    # session output. Runs once after every batch is committed.
    try:
        spelling_fixed = _normalize_spelling_in_proposals(session_id)
        if spelling_fixed:
            counts["spelling_normalized"] = spelling_fixed
    except Exception:
        # Normalisation is best-effort — never break the pipeline if it fails.
        pass

    return counts

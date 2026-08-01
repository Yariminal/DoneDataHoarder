"""
Name-building heuristics for rename proposals.

Stem usefulness checks, filename hygiene, context-echo stripping, date
prefixes, collision resolution, and the main `build_new_name` entry point.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from donedatahoarder.config import get_compiled_useless_patterns, get_hygiene_config
from donedatahoarder.db.models import File, UserSession
from donedatahoarder.db.session import get_engine

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".mp4", ".mov", ".avi", ".mkv",
    ".wmv", ".m4v", ".3gp", ".mp3", ".m4a", ".flac", ".wav",
}
DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt", ".xlsx", ".xls",
    ".pptx", ".ppt", ".txt", ".md", ".rtf",
}

# Lazy-loaded compiled patterns from ~/.datahoarder/naming_rules.json
_useless_stem_patterns_cache: list[re.Pattern] | None = None


def _get_useless_patterns() -> list[re.Pattern]:
    global _useless_stem_patterns_cache
    if _useless_stem_patterns_cache is None:
        _useless_stem_patterns_cache = get_compiled_useless_patterns()
    return _useless_stem_patterns_cache


def _is_useless_stem(stem: str) -> bool:
    """
    True if the file's current stem carries effectively zero information about
    its content — pure digits, single chars, generic placeholders like
    'untitled', camera-default IDs like IMG_1234 / DSC0001 / P1010234.
    """
    if not stem:
        return True
    s = stem.strip().lower()
    if not s:
        return True
    return any(p.match(s) for p in _get_useless_patterns())


# Stem tokens that convey only content type with no actual identity
# Used to detect and disambiguate generic AI-generated names like
# "architectural_floor_plan_drawing.pdf" that don't distinguish files
# in the same directory.
_GENERIC_STEM_TOKENS: frozenset[str] = frozenset({
    "drawing", "document", "floor_plan", "plan", "scan", "sheet",
    "architectural_drawing", "architectural_floor_plan",
    "architectural_floor_plan_drawing", "architectural_floor_plan_layout",
    "floor_plan_drawing", "floor_plan_layout", "floor_plan_layout_drawing",
    "site_plan", "section_drawing", "structural_plan_drawing",
    "building_floor_plans", "elevation", "layout",
})


def _content_type_prefix(extension: str | None) -> str:
    """
    Return a generic content-type prefix for a file extension.

    Used as a last-resort name when the file's parent folder is non-Latin
    (Hebrew/Arabic/CJK) and gets stripped to empty by _safe(). Prevents files
    like '1.jpg' inside 'הנקין-שביט/' from staying as '1.jpg' just because
    the parent name has no Latin characters.
    """
    if not extension:
        return "file"
    ext = extension.lower().lstrip(".")
    image_exts = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "raw"}
    video_exts = {"mp4", "mov", "avi", "mkv", "webm", "wmv", "flv", "m4v"}
    audio_exts = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "wma"}
    doc_exts = {"pdf", "doc", "docx", "txt", "rtf", "odt", "tex"}
    sheet_exts = {"xls", "xlsx", "csv", "ods", "tsv"}
    slide_exts = {"ppt", "pptx", "odp", "key"}
    cad_exts = {"dwg", "dxf", "3dm", "3ds", "skp", "step", "stp", "iges", "igs"}
    archive_exts = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}
    if ext in image_exts:
        return "image"
    if ext in video_exts:
        return "video"
    if ext in audio_exts:
        return "audio"
    if ext in doc_exts:
        return "document"
    if ext in sheet_exts:
        return "spreadsheet"
    if ext in slide_exts:
        return "presentation"
    if ext in cad_exts:
        return "drawing"
    if ext in archive_exts:
        return "archive"
    return "file"


def _folder_context_fallback(file_rec: File) -> Optional[str]:
    """
    Last-resort name when the stem is useless AND the AI gave us nothing.

    Combines the parent folder name + original stem digits so the file at
    least gains contextual location info: '1.pdf' inside 'Event_Menus/'
    becomes 'event_menus_1.pdf'. Better than leaving '1.pdf' to languish.

    When the parent folder name strips to empty (e.g. Hebrew/Arabic/CJK
    folders like 'הנקין-שביט/'), falls back to a content-type prefix derived
    from the extension ('1.jpg' -> 'image_1.jpg'). Without this fallback,
    files in non-Latin parent folders end up with the same useless stem as
    before, and `_generate_fallback_for_useless_stems` skips them because
    the proposed name equals the original.

    Returns the new stem (no extension) or None if even this can't be built.
    """
    path = Path(file_rec.path)
    parent_name = _safe(path.parent.name) if path.parent.name else ""
    original_stem = _safe(path.stem)
    if not parent_name and not original_stem:
        return None
    if parent_name and original_stem:
        # Avoid double-prefixing if the original stem already contains the
        # parent name (rare for useless stems, but cheap to guard against).
        if original_stem.startswith(parent_name):
            return original_stem
        return f"{parent_name}_{original_stem}"
    if parent_name:
        return parent_name
    # Parent name was empty (likely non-Latin and stripped). Use the file's
    # content type as a synthetic prefix so the result differs from the
    # original stem and the rescue pass actually emits a proposal.
    type_prefix = _content_type_prefix(file_rec.extension or path.suffix)
    return f"{type_prefix}_{original_stem}"


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


# Hygiene regexes loaded from ~/.datahoarder/naming_rules.json
_hygiene_config_cache: dict[str, str] | None = None


def _get_hygiene_config() -> dict[str, str]:
    global _hygiene_config_cache
    if _hygiene_config_cache is None:
        _hygiene_config_cache = get_hygiene_config()
    return _hygiene_config_cache


def _hygienic_stem(stem: str) -> str:
    """
    Minimum-hygiene cleanup for a filename stem: fix whitespace, collapse
    duplicate separators, replace illegal filesystem characters, and strip
    noisy punctuation — WITHOUT lowercasing or truncating (that's _safe's job
    for AI-derived stems). Preserves the user's original capitalization and
    any proper nouns that happen to be in the filename.

    Examples:
      "My Report (Final Draft).pdf"         -> "My_Report_Final_Draft"
      "file&with#bad!chars"                 -> "file_with_bad_chars"
      "too    many   spaces"                -> "too_many_spaces"
      "normal_file_name"                    -> "normal_file_name"  (no change)
    """
    cfg = _get_hygiene_config()
    illegal_re = re.compile(cfg.get("illegal_chars_regex", r'[<>:"|?*\\/\x00-\x1f]'))
    noisy_re = re.compile(cfg.get("noisy_chars_regex", r'[()\[\]{}#&%!;,@$=+`~^]'))

    s = stem.strip()
    # Illegal chars first — must be rewritten, never just stripped.
    s = illegal_re.sub("_", s)
    # Noisy but legal chars — rewrite to underscore for visual consistency.
    s = noisy_re.sub("_", s)
    # Whitespace runs -> single underscore.
    s = re.sub(r"\s+", "_", s)
    # Collapse duplicate underscores (may have been introduced above).
    s = re.sub(r"_+", "_", s)
    # Collapse duplicate hyphens.
    s = re.sub(r"-+", "-", s)
    # Strip leading/trailing separators and dots (leading dots make files hidden
    # on *nix; trailing dots are stripped by Windows anyway).
    s = s.strip("._- ")
    return s


def _needs_hygiene(stem: str) -> bool:
    """
    True if `stem` has cosmetic issues worth fixing even when we have no AI
    signal: whitespace, illegal/noisy chars, duplicate separators. Used as the
    last-chance rename trigger so files like "Some File Name (1).pdf" get a
    proposal even when analysis produced nothing useful.
    """
    if not stem:
        return False
    hygienic = _hygienic_stem(stem)
    return bool(hygienic) and hygienic != stem


# Tokens we never strip even if they appear in folder/root context — they're
# either too generic to be a real "echo" or carry meaningful semantics on their
# own. Without this guard, "_strip_context_echo" would happily turn
# "annual_report" into nothing if either word appeared in the folder name.
_ECHO_STOPWORDS: set[str] = {
    "the", "and", "for", "with", "from", "into", "onto", "this", "that",
    "report", "list", "notes", "draft", "final", "copy", "version",
}


def _build_echo_blocklist(file_rec: File, root_path: str | None) -> set[str]:
    """
    Tokens to strip from a proposed filename because they're already implied
    by the file's location: parent folder name and session root folder name.

    Mirrors the analyzers/base.py _clean_tags() echo logic so that what gets
    blocked from tags also gets blocked from rename proposals.

    Tokens shorter than 4 chars are kept (too risky — would strip year-like
    fragments and short proper-noun abbreviations like "ibm", "nyc").
    """
    block: set[str] = set()
    parent = Path(file_rec.path).parent.name
    if parent:
        for tok in re.split(r"[\s_\-\.]+", parent.lower()):
            if len(tok) >= 4 and tok not in _ECHO_STOPWORDS:
                block.add(tok)

    if root_path:
        root_name = Path(root_path).name
        if root_name:
            for tok in re.split(r"[\s_\-\.]+", root_name.lower()):
                if len(tok) >= 4 and tok not in _ECHO_STOPWORDS:
                    block.add(tok)

    return block


def _deduplicate_stem_words(stem: str) -> str:
    """
    Remove duplicate words from a stem, keeping the first occurrence.
    Prevents verbose AI descriptions like "architectural floor plan floor 310"
    from producing "architectural_floor_plan_floor_310".

    Preserves order and only drops exact duplicates (case-sensitive, since
    stems are already lowercased at this point).
    """
    if not stem:
        return stem
    words = stem.split("_")
    seen: set[str] = set()
    unique: list[str] = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            unique.append(w)
    return "_".join(unique)


def _strip_context_echo(stem: str, blocklist: set[str]) -> str:
    """
    Drop tokens from `stem` that appear in `blocklist` (parent folder name +
    root folder name). Returns the stripped stem, but only if at least one
    informative token survives — otherwise returns the original stem so we
    don't degrade the name into something worse than what we started with.

    Examples (with blocklist = {sponsors, solar, dekathlon, 2018}):
      sponsors_list_solar_dekathlon_2018  ->  list
      sponsors_list_2024                  ->  list_2024
      sponsors                            ->  sponsors          (kept: would otherwise be empty)
      menu_options_for_event              ->  menu_options_for_event  (no overlap)
    """
    if not stem or not blocklist:
        return stem
    tokens = stem.split("_")
    kept: list[str] = []
    for tok in tokens:
        if tok and tok.lower() not in blocklist:
            kept.append(tok)

    # Safety: if stripping leaves nothing, or only tokens that are too short
    # (< 3 chars) or pure digits, prefer the original stem.
    if not kept:
        return stem
    informative = [t for t in kept if len(t) >= 3 and not t.isdigit()]
    if not informative:
        return stem

    new_stem = "_".join(kept)
    # Final sanity: collapse double-underscores from the strip operation.
    new_stem = re.sub(r"_+", "_", new_stem).strip("_")
    return new_stem or stem


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


def build_new_name(file_rec: File, root_path: str | None = None) -> Optional[str]:
    """
    Construct a proposed new filename (with extension) for a file.
    Prefers AI tags over description for more specific, meaningful names.
    Preserves sequential/numbered patterns (e.g., "class_1", "class_2") while enhancing with description.
    Returns None if we can't improve on the original name.

    Args:
        file_rec: The file to propose a new name for.
        root_path: Optional session root path used to strip project-name echoes
            from the generated stem (e.g. drop "solar_dekathlon_2018" when the
            session root is named "SOLAR DEKATHLON 2018"). When None, the
            session is looked up from the DB by file_rec.session_id; pass
            explicitly when generating proposals in a tight loop to avoid
            re-querying for every file.
    """
    from donedatahoarder.proposals.sequence_detector import detect_sequences

    path = Path(file_rec.path)
    ext = path.suffix.lower()
    desc = file_rec.ai_description or ""
    tags_str = file_rec.ai_tags or ""

    # If the existing stem is useless (1.pdf, IMG_1234.jpg, untitled.docx),
    # we want to propose *something* even when AI gave us nothing — falling
    # back to the parent folder name + original digits as last-resort context.
    stem_is_useless = _is_useless_stem(path.stem)

    # Additional safety net: if the analyzer reported zero confidence, treat
    # the description as unreliable (likely a backend failure or "I can't
    # see anything" response). Never build a stem from a zero-confidence
    # description — that only produces garbage filenames.
    zero_confidence = file_rec.ai_confidence == 0.0

    if not desc and not tags_str:
        if stem_is_useless:
            fallback = _folder_context_fallback(file_rec)
            if fallback:
                return fallback + ext
        # Last-chance hygiene fallback: no AI signal AND the stem isn't
        # "useless", but it still has whitespace / illegal / noisy chars
        # worth fixing (e.g. "My Report (Final).pdf" -> "My_Report_Final.pdf").
        # Cheap, high-precision win — we only rewrite when the result is a
        # strict improvement.
        if _needs_hygiene(path.stem):
            hygienic = _hygienic_stem(path.stem)
            if hygienic:
                return hygienic + ext
        return None

    # Guard: skip if AI description is actually an error message
    _error_keywords = {
        "could not open", "cannot identify", "cannot open", "error",
        "failed to", "failed", "not found", "no such file", "unsupported",
        "unable to", "traceback", "exception",
        "inference failed", "backend is unhealthy", "circuit breaker",
    }
    desc_lower = desc.lower()
    is_error_desc = (
        any(kw in desc_lower for kw in _error_keywords)
        or desc.startswith("AI inference failed:")
    )
    if is_error_desc or zero_confidence:
        # AI output was garbage — still emit a hygiene fix if the original
        # stem has cosmetic issues. Better than leaving "My File (1).pdf"
        # unchanged just because vision model hallucinated an error string.
        if _needs_hygiene(path.stem):
            hygienic = _hygienic_stem(path.stem)
            if hygienic:
                return hygienic + ext
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
                stem_from_desc = _deduplicate_stem_words(stem_from_desc)
                stem_from_desc = _safe(stem_from_desc)
        except (json.JSONDecodeError, TypeError):
            # If tags fail to parse, fall through to description
            pass

    # Fallback: use description if tags didn't work or were empty
    if not stem_from_desc and desc:
        words = re.sub(r"[^a-zA-Z0-9\s]", " ", desc).split()
        stem_from_desc = "_".join(w.lower() for w in words[:6] if len(w) > 2)
        stem_from_desc = _deduplicate_stem_words(stem_from_desc)
        stem_from_desc = _safe(stem_from_desc)

    if not stem_from_desc:
        # AI signal was present but produced nothing usable — still emit a
        # hygiene fix if the original stem has cosmetic issues.
        if _needs_hygiene(path.stem):
            hygienic = _hygienic_stem(path.stem)
            if hygienic:
                return hygienic + ext
        return None

    # Strip echoes of the parent folder name + session root folder name. Without
    # this, an AI suggestion like "sponsors_list_solar_dekathlon_2018" inside
    # /SOLAR DEKATHLON 2018/Sponsors/ keeps both the folder name and the project
    # name in the filename, even though they're already implied by the path.
    # Mirrors the echo filter applied to tags in analyzers/base.py _clean_tags.
    if root_path is None:
        # Look up session root once per file. Cheap because it's keyed by PK.
        try:
            engine = get_engine()
            with Session(engine) as _s:
                _us = _s.get(UserSession, file_rec.session_id)
                root_path = _us.root_path if _us else None
        except Exception:
            root_path = None
    echo_block = _build_echo_blocklist(file_rec, root_path)
    stem_from_desc = _strip_context_echo(stem_from_desc, echo_block)
    stem_from_desc = _deduplicate_stem_words(stem_from_desc)

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

    # Final deduplication pass — sequence reconstruction or date+stem combo can
    # introduce duplicates (e.g. "floor_plan_floor_310" when date+stem merge).
    stem = _deduplicate_stem_words(stem)

    return stem + ext


_ORIGINAL_PREFIX_RE = re.compile(r"^(?P<num>\d+(?:[._]\d+)*)[\s_.\-]")


def _extract_distinguishing_prefix(original_stem: str) -> str | None:
    """
    Pull a leading numeric prefix off an original filename stem, slugified
    for use inside a filename. e.g.
        '10.8-binoy -1'     -> '10_8'
        '18.9-binoy -1 (2)' -> '18_9'
        '3.9-elect-1.3'     -> '3_9'
        'plan_final'        -> None
    The leading block must be followed by a separator (space, dash, dot,
    underscore) to count as a "prefix" and not the whole stem.
    """
    m = _ORIGINAL_PREFIX_RE.match(original_stem)
    if not m:
        return None
    return m.group("num").replace(".", "_")


def _ensure_prefix(stem: str, original_stem: str) -> str:
    """
    If the original stem had a numeric prefix and the proposed stem lacks
    any form of it (dot-form or underscore-form), prepend the slugified
    prefix. Idempotent: safe to call on already-prefixed stems.

    Examples:
        ('floor_plan_drawing', '10.8-floor_plan')     -> '10_8_floor_plan_drawing'
        ('10_8_floor_plan', '10.8-something_else')   -> '10_8_floor_plan'  (unchanged)
        ('drawing', 'plan_final')                     -> 'drawing'  (unchanged, no orig prefix)
    """
    prefix = _extract_distinguishing_prefix(original_stem)
    if not prefix:
        return stem
    # Check if stem already has the prefix in any form
    dot_form = prefix.replace("_", ".")
    if (stem.startswith(f"{prefix}_") or
            stem.startswith(f"{dot_form}") or
            stem.startswith(f"{prefix}-")):
        return stem
    # Prepend the prefix
    return f"{prefix}_{stem}"


def _resolve_collision(
    proposed_path: Path,
    original_path: Path,
    reserved_names: set[Path] | None = None,
) -> Path:
    """
    Resolve filename collisions, preferring an informative discriminator
    over a blind counter suffix.

    Checks against:
    1. Files already on disk
    2. Previously proposed names in this batch (reserved_names)

    Strategy:
    - If the original filename had a leading numeric prefix (e.g. `18.9-` or
      `10.8-binoy`), prepend the slugified prefix to the proposed stem
      before falling back to `_1, _2, …`. This turns
          architectural_floor_plan_drawing.pdf  (collision)
      into
          18_9_architectural_floor_plan_drawing.pdf
      which is both distinct AND preserves the original's chronological
      identity — the prefix is usually a date like "18.9" (Sep 18).
    - If the prefixed candidate ALSO collides (two files share the same
      date prefix), fall through to `_N` suffixing on that prefixed base.
    - If the original has no prefix at all, fall back to plain `_N`.

    Args:
        proposed_path: The desired target path
        original_path: The current file path (allow renaming to self)
        reserved_names: Set of paths already proposed in this batch

    Returns:
        A non-conflicting path.
    """
    reserved = reserved_names or set()

    # If no conflict, return as-is
    if (not proposed_path.exists() and proposed_path not in reserved) or proposed_path == original_path:
        return proposed_path

    stem = proposed_path.stem
    ext = proposed_path.suffix
    parent = proposed_path.parent

    # Discriminator fallback before counters: use the original's numeric prefix
    prefix = _extract_distinguishing_prefix(original_path.stem)
    # Avoid double-dating: if the proposed stem already starts with an ISO
    # date (YYYY-MM-DD…), don't prepend another numeric/date prefix.
    stem_has_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}", stem))
    if prefix and not stem.startswith(f"{prefix}_") and not stem_has_date:
        candidate = parent / f"{prefix}_{stem}{ext}"
        if (not candidate.exists() and candidate not in reserved) or candidate == original_path:
            return candidate
        # Prefixed collision too → base future counters on the prefixed stem
        stem = f"{prefix}_{stem}"

    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{ext}"
        if (not candidate.exists() and candidate not in reserved) or candidate == original_path:
            return candidate
        counter += 1

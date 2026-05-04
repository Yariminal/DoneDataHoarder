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

from donedatahoarder.config import get_compiled_useless_patterns, get_hygiene_config
from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession
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
        from donedatahoarder.ai.router import get_client

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
# Post-pass: disambiguate generic stems in the same directory
# ---------------------------------------------------------------------------

def _disambiguate_generic_stems_in_dir(session_id: str | None) -> int:
    """
    Post-pass: find PENDING RENAME proposals in the same directory whose
    proposed stem is in _GENERIC_STEM_TOKENS (after stripping any leading
    digits). For every such proposal, force-prepend the distinguishing
    prefix derived from the ORIGINAL filename.

    Also catches the case where the LLM produced two different-but-equally-
    generic stems for sibling files by checking token-set overlap >= 0.8
    within the directory.

    Returns the number of proposals rewritten.
    """
    if not session_id:
        return 0

    from difflib import SequenceMatcher

    engine = get_engine()
    rewritten = 0

    with Session(engine) as session:
        # Group PENDING RENAME proposals by target parent directory
        proposals_by_dir: dict[str, list] = {}
        proposals_q = session.query(Proposal).filter(
            Proposal.file_id.in_(
                session.query(File.id).filter(File.session_id == session_id)
            ),
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.status == ProposalStatus.PENDING,
        )

        for prop in proposals_q.all():
            parent = str(Path(prop.proposed_value).parent)
            if parent not in proposals_by_dir:
                proposals_by_dir[parent] = []
            proposals_by_dir[parent].append(prop)

        # For each directory, check for generic stems
        for dir_path, dir_proposals in proposals_by_dir.items():
            if len(dir_proposals) < 2:
                continue

            # Get the original stems for these files
            file_ids = {p.file_id for p in dir_proposals}
            files_by_id = {
                f.id: f for f in session.query(File).filter(File.id.in_(file_ids))
            }

            # Check each proposal for generic stem
            for prop in dir_proposals:
                if not prop.proposed_value:
                    continue

                proposed_stem = Path(prop.proposed_value).stem
                # Strip leading digits to get the core tokens
                core_stem = re.sub(r"^\d+[_\.]", "", proposed_stem, count=1)

                # Check if core stem is in generic tokens or if multiple siblings
                # have very similar stems (80%+ token overlap)
                is_generic = core_stem in _GENERIC_STEM_TOKENS

                if not is_generic:
                    # Check for token-set overlap with other proposals in same dir
                    for other_prop in dir_proposals:
                        if other_prop.file_id == prop.file_id:
                            continue
                        other_core = re.sub(r"^\d+[_\.]", "",
                                           Path(other_prop.proposed_value).stem, count=1)
                        if other_core == core_stem:
                            is_generic = True
                            break
                        # Also check similarity
                        sm = SequenceMatcher(None, core_stem, other_core)
                        if sm.ratio() >= 0.8:
                            is_generic = True
                            break

                if is_generic:
                    # Prepend the original file's distinguishing prefix
                    orig_file = files_by_id.get(prop.file_id)
                    if orig_file:
                        prefix = _extract_distinguishing_prefix(Path(orig_file.path).stem)
                        if prefix:
                            new_stem = f"{prefix}_{core_stem}"
                            new_name = f"{new_stem}{Path(prop.proposed_value).suffix}"
                            proposed_path = _resolve_collision(
                                Path(dir_path) / new_name,
                                Path(orig_file.path),
                                reserved_names={Path(p.proposed_value) for p in dir_proposals}
                            )
                            prop.proposed_value = str(proposed_path)
                            rewritten += 1

        if rewritten:
            session.commit()

    return rewritten


# ---------------------------------------------------------------------------
# Post-pass: flag near-duplicate proposals
# ---------------------------------------------------------------------------

def _flag_near_duplicate_proposals(session_id: str | None) -> int:
    """
    Post-pass: for PENDING RENAME proposals in the same parent directory whose
    proposed stems are >= 0.92 similar AND share extension AND share size_bytes,
    reject the later-mtime file's RENAME and add a MARK_DUPLICATE proposal
    pointing at the earlier-mtime file.

    Returns the number of MARK_DUPLICATE proposals created.
    """
    if not session_id:
        return 0

    from difflib import SequenceMatcher

    engine = get_engine()
    marked = 0

    with Session(engine) as session:
        # Get all files in this session with their proposals
        files_q = session.query(File).filter(File.session_id == session_id)
        files = {f.id: f for f in files_q}

        # Group PENDING RENAME proposals by directory
        proposals_by_dir: dict[str, list[Proposal]] = {}
        for prop in session.query(Proposal).filter(
            Proposal.file_id.in_(files.keys()),
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.status == ProposalStatus.PENDING,
        ):
            parent = str(Path(prop.proposed_value).parent)
            if parent not in proposals_by_dir:
                proposals_by_dir[parent] = []
            proposals_by_dir[parent].append(prop)

        # For each directory, find near-duplicates
        for dir_path, dir_proposals in proposals_by_dir.items():
            # Compare all pairs
            for i, prop1 in enumerate(dir_proposals):
                file1 = files.get(prop1.file_id)
                if not file1 or not prop1.proposed_value:
                    continue

                for prop2 in dir_proposals[i + 1:]:
                    file2 = files.get(prop2.file_id)
                    if not file2 or not prop2.proposed_value:
                        continue

                    path1 = Path(prop1.proposed_value)
                    path2 = Path(prop2.proposed_value)

                    # Must have same extension and size
                    if path1.suffix.lower() != path2.suffix.lower():
                        continue
                    if file1.size_bytes != file2.size_bytes:
                        continue

                    # Check stem similarity
                    stem1 = path1.stem.lower()
                    stem2 = path2.stem.lower()
                    sm = SequenceMatcher(None, stem1, stem2)
                    if sm.ratio() < 0.92:
                        continue

                    # This is a near-duplicate pair
                    # Mark the later-mtime file as duplicate of the earlier one
                    if (file2.modified_at or datetime.min) > (file1.modified_at or datetime.min):
                        duplicate_file, canonical_file = file2, file1
                        dup_prop, canon_prop = prop2, prop1
                    else:
                        duplicate_file, canonical_file = file1, file2
                        dup_prop, canon_prop = prop1, prop2

                    # Remove the RENAME proposal from the duplicate file
                    session.delete(dup_prop)

                    # Add a MARK_DUPLICATE proposal pointing at the canonical file
                    existing_dup = session.query(Proposal).filter(
                        Proposal.file_id == duplicate_file.id,
                        Proposal.proposal_type == ProposalType.MARK_DUPLICATE,
                    ).first()
                    if not existing_dup:
                        session.add(Proposal(
                            file_id=duplicate_file.id,
                            proposal_type=ProposalType.MARK_DUPLICATE,
                            current_value=duplicate_file.path,
                            proposed_value=canonical_file.path,
                            reasoning=(
                                f"Near-duplicate of {Path(canonical_file.path).name} "
                                f"(stem similarity: {sm.ratio():.1%})"
                            ),
                            confidence=0.9,
                            status=ProposalStatus.PENDING,
                        ))
                        marked += 1

        if marked:
            session.commit()

    return marked


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------

def generate_proposals(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    session_id: str | None = None,
) -> dict:
    """
    Create Proposal records for all ANALYZED files.
    Optionally translates filenames based on session's preferred_language setting.

    Args:
        limit: Maximum number of files to process.
        offset: Skip first N files (useful for debugging partial runs).
        session_id: Restrict to files belonging to this session.

    Returns:
        Summary dict with proposal counts.
    """
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn,
    )

    engine = get_engine()
    counts = {"rename": 0, "tags": 0, "skipped": 0}

    # Get the session's language preference and root path (latter is passed to
    # build_new_name so it can strip project-name echoes from generated stems)
    preferred_language = "leave_as_is"
    session_root_path: str | None = None
    if session_id:
        with Session(engine) as session:
            user_sess = session.get(UserSession, session_id)
            if user_sess:
                preferred_language = user_sess.preferred_language
                session_root_path = user_sess.root_path

    with Session(engine) as session:
        query = session.query(File).filter(File.status == FileStatus.ANALYZED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        total = query.count()
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)


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
                    new_name = build_new_name(file_rec, root_path=session_root_path)
                    if new_name and new_name != path.name:
                        # Ensure prefix is preserved if original had one
                        new_stem = Path(new_name).stem
                        new_stem = _ensure_prefix(new_stem, path.stem)
                        new_name = f"{new_stem}{Path(new_name).suffix}"

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

    # Post-pass 1: propagate renames to same-stem siblings so that pairs like
    # 10.8.pdf / 10.8.dwg / 10.8.bak don't get split — the analyzable file's
    # AI-derived stem pulls its non-analyzable companions along. MUST run
    # before the useless-stem and hygiene fallbacks: those would otherwise
    # emit low-confidence fallback renames for .dwg / .bak files with
    # digit-like stems ('10.8', '12.9', ...) and the sibling pass would then
    # see them in existing_renames and skip propagation, stranding the
    # companions under folder-context names unrelated to their primary.
    try:
        sibling_propagated = _propagate_renames_to_siblings(session_id)
        if sibling_propagated:
            counts["sibling_renames"] = sibling_propagated
    except Exception:
        # Best-effort propagation — never break the pipeline if it errors.
        pass

    # Post-pass 1b: cross-stem propagation via RelationGroup. Rescues clusters
    # where the exact-stem pass couldn't bridge — e.g. 10.8.dwg + 10.8.bak
    # grouped with 10.8-binoy -1.pdf (stems differ). Runs AFTER stem-matching
    # so that easier cases ship with their tighter reasoning and higher
    # confidence, and BEFORE the useless-stem / hygiene fallbacks so that
    # group-rescued files never get a low-conf folder-context fallback name.
    try:
        group_propagated = _propagate_renames_via_relation_groups(session_id)
        if group_propagated:
            counts["group_renames"] = group_propagated
    except Exception:
        # Best-effort — never break the pipeline if it errors.
        pass

    # Post-pass 1c: disambiguate generic stems in the same directory. Catches
    # cases where the LLM emitted a generic name like "architectural_floor_plan"
    # that doesn't distinguish files in the directory, or two sibling files got
    # the same generic stem. Re-prepends the distinguishing prefix from the
    # original filename so each file becomes unique within its directory.
    try:
        disambiguated = _disambiguate_generic_stems_in_dir(session_id)
        if disambiguated:
            counts["disambiguated_renames"] = disambiguated
    except Exception:
        # Best-effort — never break the pipeline if it errors.
        pass

    # Post-pass 2: rescue files that still have a useless stem (1.pdf,
    # IMG_1234.jpg, untitled.docx, etc.) and didn't pick up a RENAME proposal
    # in the main loop or sibling pass. Generates a low-confidence folder-
    # context fallback so the user at least sees them in the review UI rather
    # than silently shipping with the original meaningless name.
    try:
        rescued = _generate_fallback_for_useless_stems(session_id)
        if rescued:
            counts["fallback_renames"] = rescued
    except Exception:
        # Best-effort rescue pass — never break the pipeline if it errors.
        pass

    # Post-pass 3: hygiene rescue for files with cosmetic issues (whitespace,
    # illegal chars, parentheses, etc.) that never got a RENAME proposal.
    # These are pure mechanical fixes independent of any AI signal, so they
    # rescue files stuck at ENRICHED / PENDING too. Distinct from pass 2
    # because the stems aren't "useless" — just ugly.
    try:
        hygiene_fixed = _generate_hygiene_fallback(session_id)
        if hygiene_fixed:
            counts["hygiene_renames"] = hygiene_fixed
    except Exception:
        pass

    # Post-pass 4: collapse spelling variants across all proposed names so that
    # "solar_decathlon" and "solar_dekathlon" don't both appear in the same
    # session output. Runs once after every batch is committed.
    try:
        spelling_fixed = _normalize_spelling_in_proposals(session_id)
        if spelling_fixed:
            counts["spelling_normalized"] = spelling_fixed
    except Exception:
        # Normalisation is best-effort — never break the pipeline if it fails.
        pass

    # Post-pass 5: flag near-duplicate RENAME proposals. For files in the same
    # directory with stems >= 0.92 similar, same extension, and same size,
    # replace the later-mtime file's RENAME with a MARK_DUPLICATE proposal.
    # This catches cases like "3.8 binoy -1.pdf" / "3.8-binoy -1.pdf" that
    # should be deduplicated rather than both renamed.
    try:
        marked = _flag_near_duplicate_proposals(session_id)
        if marked:
            counts["marked_duplicates"] = marked
    except Exception:
        # Best-effort — never break the pipeline if it errors.
        pass

    return counts


def _generate_hygiene_fallback(session_id: str | None) -> int:
    """
    Post-pass: for every file in the session with cosmetic issues in its stem
    (whitespace, illegal FS chars, noisy punctuation, duplicate separators)
    AND no RENAME proposal yet, emit a mechanical hygiene-only rename. No AI
    signal required — this is a pure string cleanup.

    Confidence 0.5 — higher than the useless-stem fallback (0.3) because the
    outcome is deterministic and never "guesses" semantic content, just fixes
    objectively bad characters. Still below typical AI-derived confidences
    (usually 0.7-0.9) so a later re-analysis that produces a real description
    can still override via the main-loop existence check if we ever change
    that behaviour.

    Returns the number of proposals created.
    """
    if not session_id:
        return 0

    engine = get_engine()
    created = 0

    with Session(engine) as session:
        files_q = session.query(File).filter(
            File.session_id == session_id,
            File.status.in_([
                FileStatus.PENDING,
                FileStatus.ENRICHED,
                FileStatus.ANALYZED,
                FileStatus.PROPOSED,
            ]),
        )
        files = files_q.all()
        if not files:
            return 0

        existing_renames: set[int] = {
            file_id
            for (file_id,) in session.query(Proposal.file_id).filter(
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.file_id.in_([f.id for f in files]),
            )
        }

        reserved_paths: set[Path] = set()
        for (proposed_value,) in session.query(Proposal.proposed_value).filter(
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.file_id.in_([f.id for f in files]),
        ):
            if proposed_value:
                reserved_paths.add(Path(proposed_value))

        for f in files:
            if f.id in existing_renames:
                continue
            path = Path(f.path)
            if not _needs_hygiene(path.stem):
                continue
            hygienic = _hygienic_stem(path.stem)
            if not hygienic or hygienic == path.stem:
                continue
            ext = path.suffix.lower()
            new_name = hygienic + ext
            if new_name == path.name:
                continue
            proposed_path = _resolve_collision(
                path.parent / new_name, path, reserved_names=reserved_paths
            )
            reserved_paths.add(proposed_path)
            session.add(Proposal(
                file_id=f.id,
                proposal_type=ProposalType.RENAME,
                current_value=str(path),
                proposed_value=str(proposed_path),
                reasoning=(
                    "Hygiene cleanup — removed whitespace / illegal / "
                    f"noisy characters from stem '{path.stem}'."
                ),
                confidence=0.5,
                status=ProposalStatus.PENDING,
            ))
            existing_renames.add(f.id)
            created += 1

        if created:
            session.commit()

    return created


# ---------------------------------------------------------------------------
# Fallback rename pass for files with useless stems that the main loop missed
# ---------------------------------------------------------------------------

def _generate_fallback_for_useless_stems(session_id: str | None) -> int:
    """
    For every file in the session whose current stem is useless (1.pdf,
    IMG_1234.jpg, untitled.docx, etc.) AND has no RENAME proposal yet,
    emit a low-confidence fallback rename built from the parent folder name +
    original digits. Better than letting the file ship with a meaningless
    placeholder stem.

    Confidence 0.6 — above the recommended 0.55 review threshold so the
    fallback rename auto-applies under typical settings, but below typical
    AI-derived confidences (0.7-0.9) so a re-analysis with a real description
    can override via the main loop's existence check. The fallback is
    deterministic and structurally grounded (parent folder + original stem,
    or content-type prefix when the parent name is non-Latin), so it's safe
    to apply without per-file human review.

    Returns the number of fallback proposals created.
    """
    if not session_id:
        return 0

    engine = get_engine()
    created = 0

    with Session(engine) as session:
        # Pull every non-applied file in the session — the fallback should
        # also help files stuck at ENRICHED (analyzer failed) and PENDING.
        files_q = session.query(File).filter(
            File.session_id == session_id,
            File.status.in_([
                FileStatus.PENDING,
                FileStatus.ENRICHED,
                FileStatus.ANALYZED,
                FileStatus.PROPOSED,
            ]),
        )
        files = files_q.all()
        if not files:
            return 0

        # Pre-load existing RENAME proposals so we don't duplicate.
        existing_renames: set[int] = {
            file_id
            for (file_id,) in session.query(Proposal.file_id).filter(
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.file_id.in_([f.id for f in files]),
            )
        }

        # Build the set of paths already reserved by RENAME proposals so we
        # don't generate a fallback that collides with another file's planned
        # new path.
        reserved_paths: set[Path] = set()
        for (proposed_value,) in session.query(Proposal.proposed_value).filter(
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.file_id.in_([f.id for f in files]),
        ):
            if proposed_value:
                reserved_paths.add(Path(proposed_value))

        for f in files:
            if f.id in existing_renames:
                continue
            path = Path(f.path)
            if not _is_useless_stem(path.stem):
                continue
            fallback_stem = _folder_context_fallback(f)
            if not fallback_stem:
                continue
            ext = path.suffix.lower()
            new_name = fallback_stem + ext
            if new_name == path.name:
                continue
            proposed_path = _resolve_collision(
                path.parent / new_name, path, reserved_names=reserved_paths
            )
            reserved_paths.add(proposed_path)
            session.add(Proposal(
                file_id=f.id,
                proposal_type=ProposalType.RENAME,
                current_value=str(path),
                proposed_value=str(proposed_path),
                reasoning=(
                    f"Fallback rename — original stem '{path.stem}' carries no "
                    "information; using parent folder name (or content-type "
                    "prefix when parent is non-Latin) as context. Re-run "
                    "analysis if you want a content-derived name."
                ),
                confidence=0.6,
                status=ProposalStatus.PENDING,
            ))
            existing_renames.add(f.id)
            created += 1

        if created:
            session.commit()

    return created


# ---------------------------------------------------------------------------
# Sibling propagation — keep companion files linked to the renamed primary
# ---------------------------------------------------------------------------

def _propagate_renames_to_siblings(session_id: str | None) -> int:
    """
    Post-pass: when a file gets a RENAME proposal, propagate the new stem to
    every sibling in the same directory that shares its current stem but has
    a different extension. This preserves the relationship between an
    analyzable file and its unsupported companions.

    Examples handled:
      midul.3dm (rename -> kitchen_layout.3dm) pulls
        midul.3dmbak -> kitchen_layout.3dmbak

      10.8.dwg    (rename -> south_facade_2017.dwg) pulls
        10.8.bak  -> south_facade_2017.bak

      Event_Menu_1.pdf (rename -> solar_decathlon_menu.pdf) pulls any
      Event_Menu_1.xmp sidecar under the same directory.

    Stem matching is case-insensitive and uses Path.stem, which already
    handles compound extensions correctly (Path('midul.3dmbak').stem ==
    'midul'). If a sibling already has a RENAME proposal, it is left
    untouched — the earlier pass (main loop, useless-stem, or hygiene)
    already decided its fate.

    Returns the number of sibling RENAME proposals created.
    """
    if not session_id:
        return 0

    engine = get_engine()
    created = 0

    with Session(engine) as session:
        # Pull every rename proposal in the session joined with its source file
        # so we know the original stem and directory. Both PENDING and APPLIED
        # primaries count: PENDING covers a single propose run that's about to
        # ship; APPLIED covers the case where the user already executed the
        # primary (e.g. ran propose -> apply on PDFs in batch 1, then propose
        # again later) and the .dwg/.bak siblings need to catch up. The
        # proposal.current_value still holds the ORIGINAL path even after
        # apply, so the sibling-stem match still works.
        rename_rows = (
            session.query(Proposal, File)
            .join(File, Proposal.file_id == File.id)
            .filter(
                File.session_id == session_id,
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.status.in_([
                    ProposalStatus.PENDING,
                    ProposalStatus.APPLIED,
                ]),
            )
            .all()
        )
        if not rename_rows:
            return 0

        # Group primary proposals by (parent_dir, lowercased original_stem) so
        # that when multiple primaries share the same stem (rare but possible —
        # e.g. collision-suffixed renames), we apply the first one deterministically.
        primary_new_stems: dict[tuple[str, str], str] = {}
        for proposal, file_rec in rename_rows:
            src_path = Path(proposal.current_value or file_rec.path)
            dst_path = Path(proposal.proposed_value or "")
            if not dst_path.name:
                continue
            key = (str(src_path.parent), src_path.stem.lower())
            primary_new_stems.setdefault(key, dst_path.stem)

        if not primary_new_stems:
            return 0

        # Pull every non-applied file in the session — sibling candidates may
        # be at any pre-apply status. SKIPPED is the critical one: the
        # analyzer auto-marks files with no analyzer (.dwg, .bak, .3dmbak,
        # .shx, .ctb, .zip, .rar, .log) as SKIPPED, NOT as PENDING. Without
        # SKIPPED in this filter, sibling propagation never sees the very
        # files it most needs to rescue.
        files = (
            session.query(File)
            .filter(
                File.session_id == session_id,
                File.status.in_([
                    FileStatus.PENDING,
                    FileStatus.ENRICHED,
                    FileStatus.ANALYZED,
                    FileStatus.PROPOSED,
                    FileStatus.SKIPPED,
                ]),
            )
            .all()
        )
        if not files:
            return 0

        file_ids = [f.id for f in files]

        # Files already carrying a RENAME proposal — don't overwrite them.
        existing_renames: set[int] = {
            file_id
            for (file_id,) in session.query(Proposal.file_id).filter(
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.file_id.in_(file_ids),
            )
        }

        # Paths already reserved by RENAME proposals, so we don't generate a
        # sibling rename that collides with the primary's new path.
        reserved_paths: set[Path] = set()
        for (proposed_value,) in session.query(Proposal.proposed_value).filter(
            Proposal.proposal_type == ProposalType.RENAME,
            Proposal.file_id.in_(file_ids),
        ):
            if proposed_value:
                reserved_paths.add(Path(proposed_value))

        for f in files:
            if f.id in existing_renames:
                continue
            path = Path(f.path)
            # Skip files whose stem is empty (e.g. ".hidden" on unix) — no
            # meaningful sibling relationship to propagate.
            if not path.stem:
                continue

            key = (str(path.parent), path.stem.lower())
            new_stem = primary_new_stems.get(key)
            if not new_stem:
                continue

            # Preserve the sibling's ORIGINAL extension — that's the whole
            # point of this pass. path.suffix keeps the leading dot and the
            # original casing, which is what users expect on disk.
            ext = path.suffix
            new_name = new_stem + ext
            if new_name == path.name:
                # Already matches the primary's new stem (shouldn't happen —
                # the existing_renames filter would've caught it — but guard
                # against pathological inputs).
                continue

            proposed_path = _resolve_collision(
                path.parent / new_name, path, reserved_names=reserved_paths
            )
            reserved_paths.add(proposed_path)

            session.add(Proposal(
                file_id=f.id,
                proposal_type=ProposalType.RENAME,
                current_value=str(path),
                proposed_value=str(proposed_path),
                reasoning=(
                    "Sibling rename — companion file shares stem "
                    f"'{path.stem}' with a renamed primary in the same "
                    "folder; propagating the new stem keeps the "
                    "relationship visible after reorganization."
                ),
                # Match the sibling to its primary's confidence floor. 0.6 is
                # a touch above hygiene (0.5) because the rename is
                # structurally grounded — the primary already justifies it —
                # but below AI-derived values so the UI still flags it for
                # review on low-confidence primaries.
                confidence=0.6,
                status=ProposalStatus.PENDING,
            ))
            existing_renames.add(f.id)
            created += 1

        if created:
            session.commit()

    return created


def _propagate_renames_via_relation_groups(session_id: str | None) -> int:
    """
    Post-pass: propagate renames across RelationGroup members so that an
    LLM-identified cluster doesn't get half its files renamed and the other
    half stranded.

    Why we need this on top of `_propagate_renames_to_siblings`:
        The stem-matching pass only rescues files that share an EXACT stem
        with a renamed primary. It handles `midul.3dm / midul.3dmbak` because
        both stems are 'midul'. It fails for clusters like
            10.8.dwg, 10.8.bak, 10.8-binoy -1.pdf, 10.8-binoy -1.1.pdf
        where the PDFs get renamed (via the analyzer) but the stems differ
        (`10.8` vs `10.8-binoy -1`) so the .dwg / .bak never get pulled along.

    Algorithm:
      1. Pull every RelationGroup for this session with confidence >= 0.5.
         Backstop groups (0.3) are too noisy — we don't want to propagate
         renames across every numeric-prefix cluster.
      2. For each group, find members that already have a RENAME proposal.
         These are the "primaries" that will anchor the group's new name.
      3. Pick the canonical new stem from the highest-confidence primary's
         proposed name (strip any trailing `_N` collision suffix so siblings
         get the clean base).
      4. For every un-renamed member in the group, emit a RENAME proposal
         preserving its extension, suffixed with `_<role>` when the role is
         not 'source' or 'sibling' (keeps the .dwg / .pdf pair distinct on
         disk: canonical.dwg + canonical_backup.bak + canonical_export.pdf).
         Collisions within the group fall back to `_1, _2, …`.

    Returns the number of RENAME proposals created.
    """
    if not session_id:
        return 0

    from donedatahoarder.db.models import RelationGroup, RelationRole
    engine = get_engine()
    created = 0
    # Lower the bar: LLM groups at 0.8 pass; backstop (0.3) does not.
    _MIN_CONF = 0.5

    with Session(engine) as session:
        groups = (
            session.query(RelationGroup)
            .filter(
                RelationGroup.session_id == session_id,
                RelationGroup.confidence >= _MIN_CONF,
            )
            .all()
        )
        if not groups:
            return 0

        # Gather every member file up-front in one query
        all_file_ids: set[int] = set()
        for g in groups:
            all_file_ids.update(m.file_id for m in g.members)
        if not all_file_ids:
            return 0

        file_by_id: dict[int, File] = {
            f.id: f
            for f in session.query(File)
            .filter(File.id.in_(all_file_ids))
            .all()
        }

        # Pull existing rename proposals for these files, prefer highest-conf
        existing_renames_by_file: dict[int, Proposal] = {}
        for p in (
            session.query(Proposal)
            .filter(
                Proposal.file_id.in_(all_file_ids),
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.status.in_([
                    ProposalStatus.PENDING,
                    ProposalStatus.APPLIED,
                ]),
            )
            .all()
        ):
            prev = existing_renames_by_file.get(p.file_id)
            if prev is None or (p.confidence or 0) > (prev.confidence or 0):
                existing_renames_by_file[p.file_id] = p

        # Running reservation set across all groups — prevents two groups
        # from proposing the same destination path.
        reserved_paths: set[Path] = set()
        for pv in (
            session.query(Proposal.proposed_value)
            .filter(
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.file_id.in_(all_file_ids),
            )
        ):
            if pv[0]:
                reserved_paths.add(Path(pv[0]))

        # Strip a trailing collision suffix (`_1`, `_2`, …) so propagation
        # uses the clean canonical stem. Only strips a SINGLE trailing
        # `_\d+` to avoid eating intentional version numbers like `_v2`.
        _COLLISION_RE = re.compile(r"^(?P<base>.+)_\d+$")

        def _clean_stem(stem: str) -> str:
            m = _COLLISION_RE.match(stem)
            return m.group("base") if m else stem

        for group in groups:
            # Members with renames → primaries; without → siblings to fill in
            primaries: list[tuple[Proposal, File, "RelationRole"]] = []
            orphans: list[tuple[File, "RelationRole"]] = []
            for mem in group.members:
                f = file_by_id.get(mem.file_id)
                if f is None:
                    continue
                role = mem.role
                p = existing_renames_by_file.get(mem.file_id)
                if p is not None:
                    primaries.append((p, f, role))
                else:
                    orphans.append((f, role))

            if not primaries or not orphans:
                continue

            # Pick the best primary as the canonical source of the new stem.
            # Prefer 'source' role > highest confidence > alphabetical.
            def _rank(pfr):
                p, f, role = pfr
                role_score = 0 if role == RelationRole.SOURCE else 1
                conf = -(p.confidence or 0.0)
                return (role_score, conf, f.filename)
            primaries.sort(key=_rank)
            best_prop, best_file, _best_role = primaries[0]
            canonical_stem = _clean_stem(Path(best_prop.proposed_value or "").stem)
            if not canonical_stem:
                continue

            for f, role in orphans:
                path = Path(f.path)
                ext = path.suffix
                # Role-based suffix so peer files in the same group still
                # disambiguate on disk. 'source' keeps the bare stem; all
                # others append their role name.
                if role in (RelationRole.SOURCE, RelationRole.SIBLING):
                    role_suffix = ""
                else:
                    role_suffix = f"_{role.value}"

                new_stem = canonical_stem + role_suffix
                new_name = new_stem + ext
                if new_name == path.name:
                    continue

                proposed_path = _resolve_collision(
                    path.parent / new_name, path, reserved_names=reserved_paths,
                )
                reserved_paths.add(proposed_path)

                session.add(Proposal(
                    file_id=f.id,
                    proposal_type=ProposalType.RENAME,
                    current_value=str(path),
                    proposed_value=str(proposed_path),
                    reasoning=(
                        f"RelationGroup propagation — file is in the '{group.label}' "
                        f"cluster (role={role.value}) alongside '{best_file.filename}'. "
                        "Inheriting the cluster's canonical name keeps the group "
                        "visible together after reorganization."
                    ),
                    # 0.55 sits between hygiene (0.5) and sibling-stem (0.6):
                    # group propagation is more speculative than exact-stem
                    # matching but more grounded than cosmetic hygiene.
                    confidence=0.55,
                    status=ProposalStatus.PENDING,
                ))
                # Mark the file as having a rename now, so later groups
                # that also contain it won't double-propose. Use a simple
                # sentinel proposal — only the file_id key matters.
                existing_renames_by_file[f.id] = Proposal(
                    file_id=f.id,
                    proposal_type=ProposalType.RENAME,
                    current_value=str(path),
                    proposed_value=str(proposed_path),
                    confidence=0.55,
                )
                created += 1

        if created:
            session.commit()

    return created

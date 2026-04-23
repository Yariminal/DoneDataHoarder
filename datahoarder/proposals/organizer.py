"""
Folder organizer — uses LLM to suggest folder-level reorganization.

Two-phase approach:
1. Build a compact folder summary tree from analyzed file metadata
2. Ask the LLM to propose MOVE operations for better organization
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from datahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType,
)
from datahoarder.db.session import get_engine

logger = logging.getLogger(__name__)


@dataclass
class FolderSummary:
    path: str
    file_count: int = 0
    total_size: int = 0
    mime_breakdown: dict[str, int] = field(default_factory=dict)
    top_tags: list[str] = field(default_factory=list)
    description_keywords: list[str] = field(default_factory=list)
    child_folders: list[str] = field(default_factory=list)
    sample_filenames: list[str] = field(default_factory=list)
    # Files that don't match the folder's dominant theme — surface these to the
    # LLM so individual misfits (e.g. a .max file inside a textures folder, or a
    # sewing-pattern HTML inside a solar-competition folder) can be moved out.
    outlier_files: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------
# mime_type alone is too unreliable for outlier detection — libmagic on
# Windows often returns vendor-specific labels like "application/CDFV2" for
# .max files, "application/postscript" for .ai files, and stdlib mimetypes
# returns None for plenty of common extensions (.max, .3ds, .fbx, .psd, .blend).
# A coarse extension-based category map gives every file a meaningful bucket
# so outlier detection can compare apples-to-apples.

_EXT_CATEGORY: dict[str, str] = {
    # Raster images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".bmp": "image", ".tiff": "image", ".tif": "image", ".webp": "image",
    ".heic": "image", ".heif": "image", ".ico": "image", ".jp2": "image",
    # Designed graphics (raster + vector design files)
    ".psd": "design", ".ai": "design", ".sketch": "design", ".fig": "design",
    ".xd": "design", ".afdesign": "design", ".afphoto": "design",
    ".cdr": "design", ".indd": "design", ".eps": "design",
    # 3D models / scenes
    ".max": "3d", ".3ds": "3d", ".3dm": "3d", ".obj": "3d", ".fbx": "3d",
    ".dae": "3d", ".blend": "3d", ".stl": "3d", ".ply": "3d", ".gltf": "3d",
    ".glb": "3d", ".usd": "3d", ".usdz": "3d", ".c4d": "3d", ".ma": "3d",
    ".mb": "3d", ".lwo": "3d", ".lws": "3d", ".skp": "3d",
    # CAD
    ".dwg": "cad", ".dxf": "cad", ".step": "cad", ".stp": "cad",
    ".iges": "cad", ".igs": "cad", ".sat": "cad",
    # Documents
    ".pdf": "document", ".docx": "document", ".doc": "document",
    ".odt": "document", ".rtf": "document", ".txt": "document",
    ".md": "document", ".rst": "document", ".tex": "document",
    ".xlsx": "document", ".xls": "document", ".ods": "document",
    ".pptx": "document", ".ppt": "document", ".odp": "document",
    ".csv": "document", ".tsv": "document",
    # Markup / web (kept distinct from "document" so HTML pages don't get
    # lumped with PDFs when both live in the same folder)
    ".html": "web", ".htm": "web", ".xml": "web", ".xhtml": "web",
    ".json": "web", ".yaml": "web", ".yml": "web", ".toml": "web",
    # Audio
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".flac": "audio",
    ".ogg": "audio", ".oga": "audio", ".aac": "audio", ".wma": "audio",
    ".opus": "audio", ".aiff": "audio", ".aif": "audio",
    # Video
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video",
    ".wmv": "video", ".m4v": "video", ".3gp": "video", ".webm": "video",
    ".flv": "video", ".mpg": "video", ".mpeg": "video",
    # Archives
    ".zip": "archive", ".rar": "archive", ".7z": "archive", ".tar": "archive",
    ".gz": "archive", ".bz2": "archive", ".xz": "archive", ".tgz": "archive",
    ".tbz": "archive", ".iso": "archive",
    # Code (kept separate from web markup; outlier detection cares about this)
    ".py": "code", ".js": "code", ".ts": "code", ".jsx": "code",
    ".tsx": "code", ".java": "code", ".c": "code", ".cpp": "code",
    ".h": "code", ".hpp": "code", ".cs": "code", ".go": "code",
    ".rs": "code", ".rb": "code", ".php": "code", ".swift": "code",
    ".kt": "code", ".scala": "code", ".sh": "code", ".bat": "code",
    ".ps1": "code", ".sql": "code",
    # Fonts
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
    ".eot": "font",
    # Email / contacts
    ".eml": "email", ".msg": "email", ".vcf": "contact", ".ics": "calendar",
}


def _file_category(mime_type: str | None, extension: str | None) -> str:
    """
    Return a coarse category for a file. Prefers a known extension category
    over mime_type, because libmagic and stdlib mimetypes both produce a lot
    of "application/octet-stream" / vendor-specific noise for non-web formats.

    Falls back to mime_type's first segment if no extension match, then "other".
    """
    ext = (extension or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if ext in _EXT_CATEGORY:
        return _EXT_CATEGORY[ext]

    # Mime fallback — but only trust the well-known top-level types
    mime = (mime_type or "").lower()
    if "/" in mime:
        top = mime.split("/", 1)[0]
        if top in {"image", "video", "audio", "text", "font", "model"}:
            return top
        if top == "application":
            # Some application/* mimes are still informative
            sub = mime.split("/", 1)[1]
            if "pdf" in sub:
                return "document"
            if "zip" in sub or "compressed" in sub or "tar" in sub:
                return "archive"
            if "photoshop" in sub:
                return "design"
            if "postscript" in sub:
                return "design"
            if "msword" in sub or "officedocument" in sub or "opendocument" in sub:
                return "document"
            # Otherwise: don't trust application/octet-stream and friends
    return "other"


# ---------------------------------------------------------------------------
# Mojibake detection — recover garbled Hebrew/Arabic/Cyrillic folder names
# ---------------------------------------------------------------------------
# Folders created on one system and copied to another with a different default
# codepage often end up with names like `êÇòÖö` — a Mac Roman reading of bytes
# that were originally cp862 (DOS Hebrew), or a cp1252 reading of cp1255 bytes.
# The LLM organizer can't see the underlying bytes, so a deterministic
# round-trip decoder is the only reliable way to recover these.

# "Basic letter" ranges — the alphabetic core of each script, excluding
# combining marks, ligatures, rare historical forms, and supplement blocks
# that usually only appear in mojibake noise. A recovery is judged meaningful
# only when most of its non-ASCII characters fall inside these ranges.
_BASIC_LETTER_RANGES = (
    (0x05D0, 0x05EA),  # Hebrew letters (aleph..tav, including finals)
    (0x0621, 0x064A),  # Arabic letters
    (0x0410, 0x044F),  # Cyrillic basic letters (А..я)
    (0x0391, 0x03C9),  # Greek letters
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
)

# Broader script ranges used only for the "already-good" guard — if a name
# contains ANY character from these, we assume the user typed it deliberately
# and leave it alone. Broader than _BASIC_LETTER_RANGES because we don't want
# to "fix" a clean name that happens to include a combining mark or ligature.
_SCRIPT_RANGES = (
    (0x0590, 0x05FF),  # Hebrew block
    (0x0600, 0x06FF),  # Arabic block
    (0x0400, 0x04FF),  # Cyrillic block
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x0370, 0x03FF),  # Greek & Coptic
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
)

# Encodings commonly used to misread cp1255 / cp862 / utf-8 Hebrew bytes.
_MOJIBAKE_WRONG_DECODINGS = ("cp1252", "latin1", "mac_roman", "utf-8")
_MOJIBAKE_RIGHT_DECODINGS = ("cp1255", "cp862", "cp1256", "cp1251", "utf-8")


def _is_basic_letter(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _BASIC_LETTER_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _is_recognised_script(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _case_alternation_ratio(text: str) -> float:
    """
    Fraction of adjacent cased-letter pairs whose case disagrees.

    Real words mostly have consistent case: 'Москва' = 1 upper + 5 lower,
    one transition out of 5 adjacent pairs = 0.2. Mojibake like 'кЗтЦц'
    alternates every character: 4 transitions out of 4 pairs = 1.0.

    Scripts without case (Hebrew, Arabic, CJK) produce 0 cased letters and
    this function returns 0.0 (no penalty), so Hebrew recoveries aren't
    punished. Only penalises cased-script recoveries.
    """
    cased = [c for c in text if c.isalpha() and (c.isupper() or c.islower())]
    if len(cased) < 2:
        return 0.0
    transitions = 0
    for a, b in zip(cased, cased[1:]):
        if a.isupper() != b.isupper():
            transitions += 1
    return transitions / (len(cased) - 1)


def _score_mojibake_recovery(text: str) -> float:
    """
    Score how "word-like" a candidate mojibake recovery is.

    Returns 0.0 when the recovery is rejected. Otherwise higher = better.

    Filters applied in order (all must pass):
    1. At least 2 basic-letter characters in a recognised script.
    2. At least 2 UNIQUE basic-letter characters — catches repeating-byte
       mojibake like 'ГЄГ‡ГІГ–Г¶' where every other char is the same 'Г'.
    3. Basic letters account for ≥50% of non-whitespace characters —
       catches single-accent false positives like 'MГјnchen' from a
       UTF-8 → cp1251 round-trip of real German text.
    4. Case-alternation ratio ≤ 0.5 for cased scripts — catches Cyrillic
       gibberish like 'кЗтЦц' (4-of-4 case transitions) while allowing
       real words ('Москва' has 1-of-5) through. Hebrew/Arabic/CJK
       recoveries aren't cased so this filter doesn't penalise them, which
       lets genuine Hebrew recoveries beat fake Cyrillic matches on ties.
    """
    if not text:
        return 0.0
    from collections import Counter

    letter_counts: Counter[str] = Counter()
    for c in text:
        if _is_basic_letter(c):
            letter_counts[c] += 1

    total_letters = sum(letter_counts.values())
    unique = len(letter_counts)
    if total_letters < 2 or unique < 2:
        return 0.0

    non_ws_total = sum(1 for c in text if not c.isspace())
    if non_ws_total == 0:
        return 0.0
    letter_ratio = total_letters / non_ws_total
    if letter_ratio < 0.5:
        return 0.0

    uniqueness = unique / total_letters
    if uniqueness < 0.5:
        return 0.0

    # Case-alternation filter — only penalises cased scripts.
    case_alt = _case_alternation_ratio(text)
    if case_alt > 0.5:
        return 0.0

    return total_letters * uniqueness


def _recover_mojibake(name: str) -> str | None:
    """
    Attempt to recover the original non-Latin folder name from mojibake.

    Returns the recovered string when a round-trip produces a result that
    passes _score_mojibake_recovery(). Returns None when the name looks fine
    as-is or no round-trip yields a meaningful recovery.

    Conservatively rejects:
    - Pure ASCII names (never mojibake)
    - Names that already contain non-Latin script characters (clean unicode)
    - Round-trips whose outputs fail the word-likeness score (real European
      names with diacritics produce low-quality "Cyrillic" noise that the
      scorer rejects)
    """
    if not name:
        return None

    # Rule 1: pure ASCII can't be mojibake.
    if all(ord(c) < 0x80 for c in name):
        return None

    # Rule 2: if the name already contains non-Latin script chars, the user
    # wrote it correctly — don't touch it. Handles clean Hebrew folder names
    # like 'example-org' which must NOT be "recovered".
    if any(_is_recognised_script(c) for c in name):
        return None

    best: str | None = None
    best_score = 0.0
    for wrong in _MOJIBAKE_WRONG_DECODINGS:
        try:
            raw = name.encode(wrong, errors="strict")
        except (UnicodeEncodeError, LookupError):
            continue
        for right in _MOJIBAKE_RIGHT_DECODINGS:
            if right == wrong:
                continue
            try:
                recovered = raw.decode(right, errors="strict")
            except (UnicodeDecodeError, LookupError):
                continue
            if recovered == name:
                continue
            # Reject results that still contain control chars or the
            # replacement codepoint — those are broken, not recovered.
            if any(ord(c) < 0x20 or c == "\ufffd" for c in recovered):
                continue
            score = _score_mojibake_recovery(recovered)
            if score > best_score:
                best = recovered
                best_score = score
    return best


def _folder_is_mojibake(name: str) -> bool:
    """True if _recover_mojibake() would yield a meaningful recovery."""
    return _recover_mojibake(name) is not None


def build_folder_tree(session_id: str, root_path: str | None = None) -> list[FolderSummary]:
    """
    Aggregate file-level metadata into per-folder summaries.

    Reads all files in the session that have been analyzed (or at least enriched)
    and groups them by parent directory.
    """
    engine = get_engine()
    folders: dict[str, FolderSummary] = {}
    # Also cache per-folder file records so we can run a second pass for
    # outlier detection without re-querying the DB.
    files_by_folder: dict[str, list] = defaultdict(list)

    with Session(engine) as db:
        # Include ALL non-skipped files — PENDING and ERROR too. A large PDF
        # that hit the analyzer's size guard, an HTML whose parser threw, or
        # anything that never got past scan still deserves to be surfaced in
        # the folder tree and flagged for moves. Previously these silently
        # dropped out of the tree and the organizer never got a chance to
        # propose anything for them (especially painful for root-level loose
        # files, which my recent root-outlier fix was supposed to rescue but
        # couldn't because they were never in the query result).
        query = db.query(File).filter(
            File.session_id == session_id,
            File.status.in_([
                FileStatus.PENDING,
                FileStatus.ENRICHED,
                FileStatus.ANALYZED,
                FileStatus.PROPOSED,
                FileStatus.APPLIED,
                FileStatus.ERROR,
            ]),
        )
        files = query.all()

        if not files:
            return []

        # Determine root path from session or first file
        if not root_path:
            from datahoarder.db.models import UserSession
            us = db.get(UserSession, session_id)
            root_path = us.root_path if us else ""

        # Aggregate by parent directory
        tag_counter: dict[str, Counter] = defaultdict(Counter)
        desc_words: dict[str, Counter] = defaultdict(Counter)
        child_map: dict[str, set[str]] = defaultdict(set)

        for f in files:
            parent = str(Path(f.path).parent)
            if parent not in folders:
                folders[parent] = FolderSummary(path=parent)
            fs = folders[parent]
            fs.file_count += 1
            fs.total_size += f.size_bytes or 0
            files_by_folder[parent].append(f)

            # Category breakdown — extension-aware so .max / .psd / .blend etc.
            # don't all collapse into "application" or "unknown" the way raw
            # mime_type would. The dict key is still called mime_breakdown for
            # backward-compat with downstream display code.
            cat = _file_category(f.mime_type, f.extension)
            fs.mime_breakdown[cat] = fs.mime_breakdown.get(cat, 0) + 1

            # Collect tags
            if f.ai_tags:
                try:
                    tags = json.loads(f.ai_tags)
                    if isinstance(tags, list):
                        for t in tags[:5]:
                            tag_counter[parent][str(t).lower()] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

            # Collect description keywords
            if f.ai_description:
                words = f.ai_description.lower().split()[:10]
                for w in words:
                    w = w.strip(".,;:!?\"'()[]")
                    if len(w) > 3:
                        desc_words[parent][w] += 1

            # Sample filenames (keep max 5)
            if len(fs.sample_filenames) < 5:
                fs.sample_filenames.append(f.filename or Path(f.path).name)

        # Build child folder relationships
        all_paths = sorted(folders.keys())
        for p in all_paths:
            parent_of_p = str(Path(p).parent)
            if parent_of_p in folders and parent_of_p != p:
                child_map[parent_of_p].add(Path(p).name)

        # Finalize summaries
        for path, fs in folders.items():
            fs.top_tags = [t for t, _ in tag_counter[path].most_common(8)]
            fs.description_keywords = [w for w, _ in desc_words[path].most_common(5)]
            fs.child_folders = sorted(child_map.get(path, set()))

        # Resolve the root path once for the root special-case below. Using
        # Path() comparison avoids tripping on trailing-slash / case differences
        # on Windows (e.g. "D:\Stuff" vs "D:/Stuff/").
        root_norm: Path | None = None
        if root_path:
            try:
                root_norm = Path(root_path).resolve()
            except (OSError, ValueError):
                root_norm = Path(root_path)

        # Second pass: per-folder outlier detection. A file is an outlier if its
        # mime group differs from the folder's dominant mime group (>=60%), or
        # if it has tags but shares none of them with the folder's top tags.
        # Only run outlier detection on folders with >=4 files (statistical signal).
        for path, fs in folders.items():
            folder_files = files_by_folder.get(path, [])

            # --- Root-folder special case --------------------------------
            # Files sitting directly at the archive root are categorically
            # "outliers": the system prompt mandates they be assigned to a
            # subfolder, but the LLM only acts on filenames it actually sees.
            # Without this branch, the root only appears as percentage
            # breakdowns ("30% document, 25% image, …") and the LLM never gets
            # individual filenames to MOVE. The normal outlier logic below
            # would also miss root files because (a) the dominant-category
            # gate rarely fires for genuinely-mixed roots, and (b) we still
            # want this even when the root has fewer than 4 files.
            try:
                path_norm = Path(path).resolve()
            except (OSError, ValueError):
                path_norm = Path(path)
            is_root = root_norm is not None and path_norm == root_norm
            if is_root and folder_files:
                root_outliers: list[dict] = []
                for f in folder_files:
                    cat = _file_category(f.mime_type, f.extension)
                    file_tags: list[str] = []
                    if f.ai_tags:
                        try:
                            parsed = json.loads(f.ai_tags)
                            if isinstance(parsed, list):
                                file_tags = [str(t).lower() for t in parsed]
                        except (json.JSONDecodeError, TypeError):
                            pass
                    root_outliers.append({
                        "filename": f.filename or Path(f.path).name,
                        "mime_group": cat,
                        "size": f.size_bytes or 0,
                        "tags": file_tags[:5],
                        "reason": "loose file at archive root — needs subfolder assignment",
                    })
                # Surface up to 20 (vs the per-folder cap of 5) since root
                # is exactly where the LLM most needs full visibility. Sort
                # so files with tags come first (more actionable for the
                # LLM), then by descending size so big misfits rise.
                root_outliers.sort(key=lambda o: (0 if o["tags"] else 1, -o["size"]))
                fs.outlier_files = root_outliers[:20]
                continue
            # -------------------------------------------------------------

            if len(folder_files) < 4:
                continue

            total = fs.file_count or 1
            dominant_cat = None
            if fs.mime_breakdown:
                top_cat, top_count = max(fs.mime_breakdown.items(), key=lambda kv: kv[1])
                if top_count / total >= 0.6:
                    dominant_cat = top_cat

            theme_tags = set(fs.top_tags)

            outliers: list[tuple[int, dict]] = []  # (priority, info) for sorting
            for f in folder_files:
                cat = _file_category(f.mime_type, f.extension)

                file_tags: list[str] = []
                if f.ai_tags:
                    try:
                        parsed = json.loads(f.ai_tags)
                        if isinstance(parsed, list):
                            file_tags = [str(t).lower() for t in parsed]
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Category outlier: file's category differs from folder's
                # dominant category. "other" is excluded because we genuinely
                # don't know — flagging would create false positives.
                is_cat_outlier = bool(
                    dominant_cat
                    and cat != dominant_cat
                    and cat != "other"
                )
                # Tag outlier: file has tags, folder has a theme, zero overlap
                is_tag_outlier = bool(
                    file_tags
                    and theme_tags
                    and not (set(file_tags) & theme_tags)
                )

                if not (is_cat_outlier or is_tag_outlier):
                    continue

                # Priority: category outliers are more reliable than tag outliers
                priority = 0 if is_cat_outlier else 1
                reason_bits = []
                if is_cat_outlier:
                    reason_bits.append(f"type={cat} (folder is {dominant_cat})")
                if is_tag_outlier:
                    reason_bits.append("tags unrelated to folder theme")

                outliers.append((priority, {
                    "filename": f.filename or Path(f.path).name,
                    "mime_group": cat,  # key kept for prompt-format compat
                    "size": f.size_bytes or 0,
                    "tags": file_tags[:5],
                    "reason": "; ".join(reason_bits),
                }))

            # Keep the 5 most obvious outliers per folder (mime mismatches first,
            # then by descending size so big misfits rise to the top).
            outliers.sort(key=lambda x: (x[0], -x[1]["size"]))
            fs.outlier_files = [info for _, info in outliers[:5]]

    # Sort by path for consistent ordering
    return sorted(folders.values(), key=lambda f: f.path)


def _format_tree_for_prompt(
    folder_summaries: list[FolderSummary],
    root_path: str,
) -> str:
    """Render folder summaries as a compact text representation for the LLM."""
    lines = []
    for fs in folder_summaries:
        # Make path relative to root
        try:
            rel = str(Path(fs.path).relative_to(root_path))
        except ValueError:
            rel = fs.path
        if rel == ".":
            rel = "(root)"

        # MIME breakdown as percentages
        total = fs.file_count or 1
        mime_parts = []
        for mime_type, count in sorted(fs.mime_breakdown.items(), key=lambda x: -x[1]):
            pct = int(count / total * 100)
            mime_parts.append(f"{pct}% {mime_type}")

        line = f"[{rel}]  {fs.file_count} files, {_human_size(fs.total_size)}"
        if mime_parts:
            line += f"  |  {', '.join(mime_parts)}"
        if fs.top_tags:
            line += f"  |  Tags: {', '.join(fs.top_tags[:5])}"
        if fs.description_keywords:
            line += f"  |  Keywords: {', '.join(fs.description_keywords[:3])}"
        if fs.child_folders:
            line += f"  |  Subfolders: {', '.join(fs.child_folders[:8])}"
        if fs.sample_filenames:
            line += f"  |  Examples: {', '.join(fs.sample_filenames[:3])}"
        lines.append(line)

        # Surface per-file outliers as indented sub-lines so the LLM can
        # propose moves for specific misfit files even when the folder's
        # overall theme is correct.
        for ol in fs.outlier_files:
            tag_str = f", tags=[{', '.join(ol['tags'])}]" if ol["tags"] else ""
            lines.append(
                f"    OUTLIER: {ol['filename']} ({_human_size(ol['size'])}, "
                f"{ol['mime_group']}{tag_str}) — {ol['reason']}"
            )

    return "\n".join(lines)


def _human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if n != int(n) else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


REORG_SYSTEM_PROMPT = """You are a file organization expert. Your goal is to create \
a folder structure that makes data discoverable and encourages people to interact \
with their files again rather than forgetting they exist.

You will receive a summary of a folder tree. Each entry shows:
path, file count, size, content types, semantic tags, keywords, and — when \
present — lines prefixed with "    OUTLIER:" listing individual files whose \
type or tags do not match the folder's theme.

Suggest reorganizations that improve discoverability and logical grouping. You may suggest:
- MOVE: Move all files matching a description from one folder to a new or existing folder
- MOVE_FILES: Move a specific list of named files out of one folder into another (use this \
  for the OUTLIER entries — move each misfit file to a folder that matches its content)
- MERGE: Combine two similar folders into one (move files from source to destination)
- RENAME_FOLDER: Rename a folder IN PLACE to a clearer, more descriptive name

Rules:
- Preserve the user's existing organizational intent where it already exists
- Group by semantic meaning, not just file type
- Prioritize highest-impact changes (large unsorted folders, mixed-content folders)
- Rename folders with cryptic, abbreviated, or meaningless names to something descriptive
- Correct obvious English spelling errors in folder names (e.g. "Sponsers" -> "Sponsors", \
"Recieved" -> "Received", "Seperated" -> "Separated"). Use rename_folder for these even \
when the folder's content is otherwise well-organized — misspelled names still hurt \
discoverability and look unprofessional.
- Folders whose names look like corrupted/mojibake text (random mixes of accented \
Latin characters like "êÇòÖö", "Ã©Ã¨Ã ", "Ð¿Ñ€Ð¸Ð²ÐµÑ‚", etc.) have lost their \
original encoding and are unreadable. Emit a rename_folder for these, using the \
content of the folder (file tags, keywords, sample filenames) to propose a \
descriptive name. Do NOT attempt to preserve the garbled original.
- Suggest at most 25 changes
- For move/merge: specify source folder, destination folder, which files (all or description)
- For move_files: list the exact filenames (as shown in the OUTLIER entries) that should \
  leave their current folder. One move_files proposal per (source, destination) pair.
- For rename_folder: "new_name" must be ONLY the new folder name (e.g. "Brand_Logos"), \
NOT a full path. The folder stays in its current parent directory, only its name changes.
- IMPORTANT: Do NOT both rename a folder AND move all its files out of it. \
If a folder has the right content but just a bad name, use rename_folder. \
If files need to move to a different location, use move — but then don't also rename the emptied source.
- IMPORTANT: Files sitting directly in the root folder (shown as "(root)") are \
unorganized and MUST be assigned to an appropriate subfolder. Always include move \
proposals for any files in "(root)".
- IMPORTANT: For every "    OUTLIER:" line you see, emit a move_files proposal to \
a folder whose theme matches the outlier's tags/type. Do not leave outliers in place.
- Create meaningful folder names based on content themes
- Folder names should use underscores instead of spaces, and be in English
- Use relative paths from the root

Respond with a JSON array of objects. For move/merge:
{
  "action": "move" or "merge",
  "source_folder": "relative/path/from/root",
  "destination_folder": "relative/path/to/target",
  "file_filter": "all" or a description like "images tagged beach",
  "reasoning": "why this improves organization",
  "confidence": 0.0 to 1.0
}

For move_files (targeted, named-file moves — preferred for outliers):
{
  "action": "move_files",
  "source_folder": "relative/path/from/root",
  "destination_folder": "relative/path/to/target",
  "filenames": ["scene_file.max", "another_misfit.obj"],
  "reasoning": "why these specific files do not belong here",
  "confidence": 0.0 to 1.0
}

For rename_folder:
{
  "action": "rename_folder",
  "source_folder": "relative/path/of/folder",
  "new_name": "New_Folder_Name",
  "reasoning": "why this name is better",
  "confidence": 0.0 to 1.0
}

IMPORTANT for rename_folder:
- "new_name" is JUST the folder name, not a path. Example: "Brand_Logos" NOT "root/Brand_Logos"
- The folder keeps its current parent. Only the last segment of the path changes.
- Do NOT use rename_folder to move folders to a different parent — use "move" for that."""


def generate_reorg_proposals(session_id: str) -> dict:
    """
    Analyze the folder tree and generate MOVE and RENAME_FOLDER proposals for reorganization.

    Returns summary dict with proposal counts.
    """
    from datahoarder.ai.router import get_client

    engine = get_engine()
    counts = {"move": 0, "rename_folder": 0, "skipped": 0, "errors": 0}

    # Get root path and language preference from session
    with Session(engine) as db:
        from datahoarder.db.models import UserSession
        us = db.get(UserSession, session_id)
        if not us:
            return {"error": "Session not found", **counts}
        root_path = us.root_path
        preferred_language = us.preferred_language or "leave_as_is"

    # Clear any previous PENDING organizer proposals for this session so that
    # re-running Organize always starts from a clean slate.  Applied/rejected
    # proposals are preserved — we only discard ones the user hasn't acted on yet.
    with Session(engine) as db:
        stale_ids = [
            p_id for (p_id,) in (
                db.query(Proposal.id)
                .join(File, Proposal.file_id == File.id)
                .filter(
                    File.session_id == session_id,
                    Proposal.status == ProposalStatus.PENDING,
                    Proposal.proposal_type.in_([
                        ProposalType.MOVE,
                        ProposalType.RENAME_FOLDER,
                    ]),
                )
            )
        ]
        if stale_ids:
            db.query(Proposal).filter(
                Proposal.id.in_(stale_ids)
            ).delete(synchronize_session=False)
        db.commit()

    # Phase 1: Build folder summary tree
    folder_summaries = build_folder_tree(session_id, root_path)
    if not folder_summaries:
        return {"message": "No analyzed files found", **counts}

    tree_text = _format_tree_for_prompt(folder_summaries, root_path)

    # Phase 2: Ask LLM for reorganization suggestions
    client = get_client()

    # Build language instruction for folder names
    lang_instruction = ""
    if preferred_language == "english":
        lang_instruction = (
            "IMPORTANT: All proposed folder names MUST be in English. "
            "Translate any non-English folder names to English. "
        )
    elif preferred_language == "hebrew":
        lang_instruction = (
            "IMPORTANT: All proposed folder names MUST be in Hebrew. "
            "Translate any non-Hebrew folder names to Hebrew. "
        )

    prompt = (
        f"Here is the folder tree summary for the collection at: {root_path}\n\n"
        f"{tree_text}\n\n"
        "Based on this structure, suggest folder reorganization to improve discoverability. "
        "Include folder renames for cryptic, abbreviated, or unclear folder names. "
        f"{lang_instruction}"
        "Respond with a JSON array of move/merge/rename_folder proposals."
    )

    try:
        result = client.generate_json(prompt, system=REORG_SYSTEM_PROMPT)
    except Exception as exc:
        # LLM unavailable — fall through to deterministic backstops only.
        # Don't return early; the backstops below can still improve structure.
        logger.warning("Organizer LLM call failed: %s — falling back to rule-based backstops.", exc)
        result = []

    # Parse the LLM response into Proposal records
    if isinstance(result, list):
        proposals = result
    elif isinstance(result, dict):
        # Try known keys, including raw_response fallback from generate_json
        proposals = result.get("proposals", result.get("suggestions", [])) or []
        if not proposals and "raw_response" in result:
            # generate_json fell back to raw text — try parsing it ourselves
            import json as _json
            raw = result["raw_response"]
            try:
                arr_start = raw.index("[")
                arr_end = raw.rindex("]")
                proposals = _json.loads(raw[arr_start : arr_end + 1])
            except (ValueError, _json.JSONDecodeError):
                pass
    else:
        proposals = []
    if not isinstance(proposals, list):
        return {"error": "LLM did not return a valid proposal list", "raw": result, **counts}

    with Session(engine) as db:
        for prop in proposals:
            if not isinstance(prop, dict):
                counts["skipped"] += 1
                continue

            action = prop.get("action", "move")
            reasoning = prop.get("reasoning", "AI-suggested reorganization")
            confidence = min(max(float(prop.get("confidence", 0.5)), 0.0), 1.0)

            # --- RENAME_FOLDER action ---
            if action == "rename_folder":
                src_folder = prop.get("source_folder", "")
                new_name = prop.get("new_name", "")
                if not src_folder or not new_name:
                    counts["skipped"] += 1
                    continue

                # Safety: LLM sometimes puts a full path in new_name — extract just the name
                import re
                new_name = Path(new_name).name
                # Strip characters that are problematic in folder names
                new_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", new_name)
                new_name = re.sub(r"\s+", "_", new_name.strip())
                new_name = re.sub(r"_+", "_", new_name)
                new_name = new_name.strip("._")
                if not new_name:
                    counts["skipped"] += 1
                    continue

                src_abs = str(Path(root_path) / src_folder)
                # Build the new folder path (rename in place — same parent, new name)
                src_path_obj = Path(root_path) / src_folder
                dst_abs = str(src_path_obj.parent / new_name)

                if src_abs == dst_abs:
                    counts["skipped"] += 1
                    continue

                # Find a representative file in this folder to anchor the proposal
                anchor_file = db.query(File).filter(
                    File.session_id == session_id,
                    File.path.like(f"{src_abs}%"),
                ).first()
                if not anchor_file:
                    counts["skipped"] += 1
                    continue

                # Check for existing RENAME_FOLDER proposal for same folder
                existing = db.query(Proposal).filter(
                    Proposal.proposal_type == ProposalType.RENAME_FOLDER,
                    Proposal.current_value == src_abs,
                ).first()
                if existing:
                    counts["skipped"] += 1
                    continue

                db.add(Proposal(
                    file_id=anchor_file.id,
                    proposal_type=ProposalType.RENAME_FOLDER,
                    current_value=src_abs,
                    proposed_value=dst_abs,
                    reasoning=reasoning,
                    confidence=confidence,
                    status=ProposalStatus.PENDING,
                ))
                counts["rename_folder"] += 1
                continue

            # --- MOVE_FILES action (named-file moves, typically for outliers) ---
            if action == "move_files":
                src_folder = prop.get("source_folder", "")
                dst_folder = prop.get("destination_folder", "")
                filenames = prop.get("filenames", [])
                if not src_folder or not dst_folder or not isinstance(filenames, list):
                    counts["skipped"] += 1
                    continue
                # Filter empty / non-string entries
                filenames = [str(n).strip() for n in filenames if isinstance(n, str) and n.strip()]
                if not filenames:
                    counts["skipped"] += 1
                    continue

                src_abs = str(Path(root_path) / src_folder)
                dst_abs = str(Path(root_path) / dst_folder)

                # Look up each named file in the source folder
                for fname in filenames:
                    file_rec = db.query(File).filter(
                        File.session_id == session_id,
                        File.path.like(f"{src_abs}%"),
                        File.filename == fname,
                        File.status.in_([
                            FileStatus.ANALYZED,
                            FileStatus.PROPOSED,
                        ]),
                    ).first()
                    if not file_rec:
                        counts["skipped"] += 1
                        continue

                    src_path = Path(file_rec.path)
                    dst_path = Path(dst_abs) / src_path.name
                    if str(src_path) == str(dst_path):
                        counts["skipped"] += 1
                        continue

                    existing = db.query(Proposal).filter_by(
                        file_id=file_rec.id,
                        proposal_type=ProposalType.MOVE,
                    ).first()
                    if existing:
                        counts["skipped"] += 1
                        continue

                    db.add(Proposal(
                        file_id=file_rec.id,
                        proposal_type=ProposalType.MOVE,
                        current_value=str(src_path),
                        proposed_value=str(dst_path),
                        reasoning=reasoning,
                        confidence=confidence,
                        status=ProposalStatus.PENDING,
                    ))
                    counts["move"] += 1
                continue

            # --- MOVE / MERGE actions ---
            src_folder = prop.get("source_folder", "")
            dst_folder = prop.get("destination_folder", "")
            file_filter = prop.get("file_filter", "all")

            if not src_folder or not dst_folder:
                counts["skipped"] += 1
                continue

            # Resolve to absolute paths
            src_abs = str(Path(root_path) / src_folder)
            dst_abs = str(Path(root_path) / dst_folder)

            # Find files in source folder
            query = db.query(File).filter(
                File.session_id == session_id,
                File.path.like(f"{src_abs}%"),
                File.status.in_([
                    FileStatus.ANALYZED,
                    FileStatus.PROPOSED,
                ]),
            )

            # If file_filter is not "all", try to match by tags/description
            files = query.all()
            if not files:
                counts["skipped"] += 1
                continue

            for file_rec in files:
                # Check if filter matches (basic keyword matching)
                if file_filter and file_filter != "all":
                    desc = (file_rec.ai_description or "").lower()
                    tags = (file_rec.ai_tags or "").lower()
                    filter_lower = file_filter.lower()
                    # Simple keyword check
                    filter_words = [w.strip() for w in filter_lower.replace(",", " ").split() if len(w.strip()) > 2]
                    if filter_words and not any(w in desc or w in tags for w in filter_words):
                        continue

                # Build destination path (preserve filename)
                src_path = Path(file_rec.path)
                # Compute the relative path within the source folder
                try:
                    rel_to_src = src_path.relative_to(src_abs)
                except ValueError:
                    rel_to_src = Path(src_path.name)
                dst_path = Path(dst_abs) / rel_to_src

                # Don't create proposal if source == destination
                if str(src_path) == str(dst_path):
                    continue

                # Check for existing MOVE proposal
                existing = db.query(Proposal).filter_by(
                    file_id=file_rec.id,
                    proposal_type=ProposalType.MOVE,
                ).first()
                if existing:
                    counts["skipped"] += 1
                    continue

                db.add(Proposal(
                    file_id=file_rec.id,
                    proposal_type=ProposalType.MOVE,
                    current_value=str(src_path),
                    proposed_value=str(dst_path),
                    reasoning=reasoning,
                    confidence=confidence,
                    status=ProposalStatus.PENDING,
                ))
                counts["move"] += 1

        db.commit()

    # Post-pass: emit per-cluster MOVE proposals for high-confidence
    # RelationGroups. For each LLM-reasoned group (confidence >= 0.5) we
    # create a new subfolder `<label>/` inside the group's common parent and
    # move all members into it. Backstop groups (confidence 0.3) are skipped
    # — numeric-prefix clusters are too noisy to auto-folder.
    try:
        cluster_moves = _emit_relation_group_moves(session_id, root_path)
        if cluster_moves:
            counts["cluster_moves"] = cluster_moves
            counts["move"] = counts.get("move", 0) + cluster_moves
    except Exception:
        # Best-effort — never break the pipeline if cluster moves fail.
        pass

    # Deterministic backstop: detect mojibake-encoded folder names and ensure
    # they always get a RENAME_FOLDER proposal, even if the LLM missed them.
    # The LLM only sees the garbled string in its prompt (it can't access the
    # underlying bytes), so it often preserves corrupted names verbatim. This
    # pass recovers the original via encoding round-trips (cp1252→cp1255,
    # mac_roman→cp862, utf-8→cp1251) and emits renames the LLM can't.
    try:
        mojibake_fixed = _backstop_mojibake_folders(session_id, root_path)
        if mojibake_fixed:
            counts["mojibake_renames"] = mojibake_fixed
            counts["rename_folder"] = counts.get("rename_folder", 0) + mojibake_fixed
    except Exception:
        # Best-effort — never break the pipeline if the backstop errors.
        pass

    # Deterministic backstop: generic/cryptic folder names and root loose files.
    # When the LLM returns no proposals (common on small, already-organized
    # datasets), this backstop still improves discoverability by:
    #   1. Renaming numbered/generic folders to content-based names
    #   2. Grouping loose root files by dominant content type
    try:
        generic_fixed = _backstop_generic_folders(session_id, root_path)
        if generic_fixed:
            counts["generic_renames"] = generic_fixed.get("renames", 0)
            counts["generic_moves"] = generic_fixed.get("moves", 0)
            counts["rename_folder"] = counts.get("rename_folder", 0) + generic_fixed.get("renames", 0)
            counts["move"] = counts.get("move", 0) + generic_fixed.get("moves", 0)
    except Exception:
        pass

    return counts


def _emit_relation_group_moves(session_id: str, root_path: str) -> int:
    """
    For each high-confidence RelationGroup, emit MOVE proposals that place
    every member into a `<label>/` subfolder under their common parent.

    Rules:
    - Only groups with confidence >= 0.5 (LLM-reasoned) participate; backstop
      groups at 0.3 are too noisy (they'd create a subfolder per date prefix).
    - Group members must share a common parent directory. Cross-directory
      groups are skipped — we don't want to pull files out of their enclosing
      project folder just because they're conceptually related.
    - The target folder name is the group's `label` (already slugified by the
      Relate step). Collisions with existing folders get `_2`, `_3`, …
      appended.
    - Files that already have a MOVE or RENAME_FOLDER proposal are skipped
      so we don't double-propose.
    - The destination filename is the CURRENT filename (or the one the Namer
      proposed, if there's a pending RENAME). This keeps Namer and Organizer
      proposals composable at execute time.

    Returns the number of MOVE proposals created.
    """
    from datahoarder.db.models import RelationGroup, RelationMember
    engine = get_engine()
    created = 0
    _MIN_CONF = 0.5

    with Session(engine) as db:
        groups = (
            db.query(RelationGroup)
            .filter(
                RelationGroup.session_id == session_id,
                RelationGroup.confidence >= _MIN_CONF,
            )
            .all()
        )
        if not groups:
            return 0

        # Gather every member file up-front
        all_file_ids: set[int] = set()
        for g in groups:
            all_file_ids.update(m.file_id for m in g.members)
        if not all_file_ids:
            return 0

        file_by_id: dict[int, File] = {
            f.id: f
            for f in db.query(File).filter(File.id.in_(all_file_ids)).all()
        }

        # Files that already have a MOVE proposal — leave them alone.
        existing_moves: set[int] = {
            fid
            for (fid,) in db.query(Proposal.file_id).filter(
                Proposal.file_id.in_(all_file_ids),
                Proposal.proposal_type == ProposalType.MOVE,
            )
        }

        # Pending RENAME proposals by file_id so the MOVE target uses the
        # renamed filename (preserves Namer's work when both apply at execute).
        rename_by_file: dict[int, str] = {}
        for p in (
            db.query(Proposal)
            .filter(
                Proposal.file_id.in_(all_file_ids),
                Proposal.proposal_type == ProposalType.RENAME,
                Proposal.status.in_([
                    ProposalStatus.PENDING,
                    ProposalStatus.APPLIED,
                ]),
            )
        ):
            if p.proposed_value:
                rename_by_file[p.file_id] = Path(p.proposed_value).name

        # Reserved dest paths across all groups — avoids two clusters in the
        # same parent from trying to move a file into colliding subfolders.
        reserved_dests: set[Path] = set()

        for group in groups:
            members = [
                file_by_id[m.file_id]
                for m in group.members
                if m.file_id in file_by_id
            ]
            if len(members) < 2:
                continue

            # Require a single common parent — skip cross-directory groups.
            parents = {str(Path(f.path).parent) for f in members}
            if len(parents) != 1:
                continue
            parent = Path(parents.pop())

            # Pick target subfolder name, avoiding collisions with existing
            # dirs in this parent. `label` was slugified at Relate time.
            base_name = group.label or "cluster"
            target_dir = parent / base_name
            n = 2
            while target_dir.exists() and not target_dir.is_dir():
                target_dir = parent / f"{base_name}_{n}"
                n += 1

            # At least one member must still be inside `parent` on disk —
            # if they've all been moved elsewhere by earlier proposals we
            # skip so we don't resurrect stale paths.
            for member in members:
                if member.id in existing_moves:
                    continue
                src_path = Path(member.path)
                if src_path.parent != parent:
                    # Group spans multiple dirs on disk (e.g. folder-rename
                    # already reshuffled some members) — leave alone.
                    continue
                # Destination filename: prefer pending RENAME's value if any,
                # otherwise the current basename.
                dst_filename = rename_by_file.get(member.id, src_path.name)
                dst_path = target_dir / dst_filename
                # Resolve dest-level collisions (two members mapped to same
                # filename after rename) by appending `_2`, `_3`, …
                if dst_path in reserved_dests:
                    stem, suffix = dst_path.stem, dst_path.suffix
                    k = 2
                    while True:
                        cand = target_dir / f"{stem}_{k}{suffix}"
                        if cand not in reserved_dests:
                            dst_path = cand
                            break
                        k += 1
                reserved_dests.add(dst_path)

                if str(src_path) == str(dst_path):
                    continue

                db.add(Proposal(
                    file_id=member.id,
                    proposal_type=ProposalType.MOVE,
                    current_value=str(src_path),
                    proposed_value=str(dst_path),
                    reasoning=(
                        f"Cluster move — member of RelationGroup '{group.label}' "
                        f"(confidence {group.confidence:.2f}): "
                        f"{(group.reason or '').strip()[:200]}"
                    ),
                    confidence=float(group.confidence or 0.5),
                    status=ProposalStatus.PENDING,
                ))
                existing_moves.add(member.id)
                created += 1

        if created:
            db.commit()

    return created


def _backstop_mojibake_folders(session_id: str, root_path: str) -> int:
    """
    Post-pass: walk every folder under root_path that holds session files,
    detect mojibake-encoded names, and emit RENAME_FOLDER proposals to the
    recovered original name. Skips folders that already have a pending
    RENAME_FOLDER proposal (the LLM got to them first).

    Returns the number of proposals created.
    """
    if not root_path or not session_id:
        return 0

    engine = get_engine()
    root = Path(root_path)
    created = 0

    with Session(engine) as db:
        # Pull every distinct folder path that contains at least one session
        # file. parent_dir stays stable across status transitions, so we
        # query File.path directly — cheaper than joining through proposals.
        file_paths = (
            db.query(File.path)
            .filter(File.session_id == session_id)
            .all()
        )
        if not file_paths:
            return 0

        # Build the set of folders whose basename is mojibake. A folder is
        # eligible if any ancestor segment between root and the leaf is
        # garbled — a single mojibake segment in the middle of an otherwise-
        # clean path still hurts discoverability, and renaming it in place
        # rescues every descendant.
        mojibake_folders: dict[str, str] = {}  # abs_path -> recovered basename
        seen: set[str] = set()
        for (path_str,) in file_paths:
            try:
                p = Path(path_str)
            except Exception:
                continue
            # Walk up from the file's parent to root, inspecting each segment.
            for ancestor in p.parents:
                ancestor_str = str(ancestor)
                if ancestor_str in seen:
                    break
                seen.add(ancestor_str)
                # Stop walking at the root — we don't touch the user's
                # chosen root directory.
                try:
                    if ancestor == root or root not in ancestor.parents:
                        break
                except Exception:
                    break
                recovered = _recover_mojibake(ancestor.name)
                if recovered:
                    mojibake_folders[ancestor_str] = recovered

        if not mojibake_folders:
            return 0

        # Existing RENAME_FOLDER proposals (any status) for these folders —
        # skip anything already handled by the LLM or previously applied.
        already: set[str] = set()
        existing_rows = db.query(Proposal.current_value).filter(
            Proposal.proposal_type == ProposalType.RENAME_FOLDER,
            Proposal.current_value.in_(list(mojibake_folders.keys())),
        ).all()
        for (cv,) in existing_rows:
            if cv:
                already.add(cv)

        import re
        for src_abs, recovered_name in mojibake_folders.items():
            if src_abs in already:
                continue

            # Sanitize — strip Windows-illegal chars; collapse whitespace but
            # preserve non-Latin letters (Hebrew, Arabic, Cyrillic, etc.).
            clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", recovered_name)
            clean = re.sub(r"\s+", "_", clean.strip())
            clean = re.sub(r"_+", "_", clean)
            clean = clean.strip("._")
            if not clean:
                continue

            src_path_obj = Path(src_abs)
            dst_abs = str(src_path_obj.parent / clean)
            if src_abs == dst_abs:
                continue

            # Need an anchor File record living under this folder.
            anchor_file = db.query(File).filter(
                File.session_id == session_id,
                File.path.like(f"{src_abs}%"),
            ).first()
            if not anchor_file:
                continue

            db.add(Proposal(
                file_id=anchor_file.id,
                proposal_type=ProposalType.RENAME_FOLDER,
                current_value=src_abs,
                proposed_value=dst_abs,
                reasoning=(
                    f"Mojibake recovery — folder name '{src_path_obj.name}' "
                    "appears to be an encoding artefact; decoded original "
                    f"name is '{recovered_name}'."
                ),
                # 0.75 — deterministic enough to auto-surface but below the
                # 0.8+ band that signals user-bypass-worthy confidence.
                confidence=0.75,
                status=ProposalStatus.PENDING,
            ))
            already.add(src_abs)
            created += 1

        if created:
            db.commit()

    return created


# Generic folder names that provide zero discoverability — if a folder has one
# of these names, we ALWAYS try to rename it based on content, even when the
# LLM returns nothing.
_GENERIC_FOLDER_PATTERNS = [
    re.compile(r"^\d+$"),                       # "1", "2", "01", "99"
    re.compile(r"^\d+[\s._-].*", re.I),       # "1 Milestone", "2_Final", "3.Photos"
    re.compile(r"^(temp|tmp|new[\s_-]?folder|untitled|folder[\s_-]?\d*|item[\s_-]?\d*)$", re.I),
    re.compile(r"^(section|part|chapter|stage|phase)[\s_-]?\d*$", re.I),
]


def _is_generic_folder_name(name: str) -> bool:
    """Return True if *name* is a non-descriptive, numbered, or templated folder name."""
    if not name or name in {".", "..", "(root)"}:
        return False
    for pat in _GENERIC_FOLDER_PATTERNS:
        if pat.match(name):
            return True
    return False


def _folder_content_label(mime_breakdown: dict[str, int]) -> str | None:
    """Return a descriptive label for a folder based on its dominant MIME categories."""
    if not mime_breakdown:
        return None
    # Ordered preference for common project folder names
    priority = {
        "image": "Images",
        "design": "Design_Files",
        "3d": "3D_Models",
        "cad": "CAD_Drawings",
        "document": "Documents",
        "video": "Videos",
        "audio": "Audio",
        "code": "Code",
        "web": "Web_Files",
        "archive": "Archives",
    }
    total = sum(mime_breakdown.values())
    for cat, label in priority.items():
        if mime_breakdown.get(cat, 0) / total >= 0.4:
            return label
    # Fallback: plurality wins if no strong majority
    top_cat, top_count = max(mime_breakdown.items(), key=lambda kv: kv[1])
    if top_count / total >= 0.35:
        return top_cat.replace("_", " ").title().replace(" ", "_")
    return "Mixed_Content"


def _backstop_generic_folders(session_id: str, root_path: str) -> dict[str, int]:
    """
    Deterministic backstop for generic folder names and loose root files.

    1. Any folder whose name matches _GENERIC_FOLDER_PATTERNS gets a
       RENAME_FOLDER proposal based on its dominant content type.
    2. Loose files sitting directly in root (not in any subfolder) with a
       clear MIME category get a MOVE proposal to root/<category>/.

    Returns {"renames": int, "moves": int}.
    """
    engine = get_engine()
    created_renames = 0
    created_moves = 0

    with Session(engine) as db:
        # Build folder summaries fresh (lightweight — no LLM call)
        folder_summaries = build_folder_tree(session_id, root_path)
        root_norm = Path(root_path).resolve() if root_path else None

        # --- Pass 1: rename generic folders ---
        for fs in folder_summaries:
            folder_name = Path(fs.path).name
            if not _is_generic_folder_name(folder_name):
                continue

            # Skip if a proposal already exists for this folder
            existing = db.query(Proposal).filter(
                Proposal.proposal_type == ProposalType.RENAME_FOLDER,
                Proposal.current_value == fs.path,
            ).first()
            if existing:
                continue

            label = _folder_content_label(fs.mime_breakdown)
            if not label:
                continue

            anchor = db.query(File).filter(
                File.session_id == session_id,
                File.path.like(f"{fs.path}%"),
            ).first()
            if not anchor:
                continue

            src_path_obj = Path(fs.path)
            dst_abs = str(src_path_obj.parent / label)
            if dst_abs == fs.path:
                continue

            db.add(Proposal(
                file_id=anchor.id,
                proposal_type=ProposalType.RENAME_FOLDER,
                current_value=fs.path,
                proposed_value=dst_abs,
                reasoning=(
                    f"Generic folder name '{folder_name}' has low discoverability. "
                    f"Renaming to '{label}' based on dominant content types: "
                    f"{', '.join(f'{k}({v})' for k, v in sorted(fs.mime_breakdown.items(), key=lambda x: -x[1])[:3])}."
                ),
                confidence=0.65,
                status=ProposalStatus.PENDING,
            ))
            created_renames += 1

        # --- Pass 2: group loose root files by type ---
        if root_norm:
            root_files = [
                f for f in folder_summaries
                if Path(f.path).resolve() == root_norm and f.file_count > 0
            ]
            for root_fs in root_files:
                if root_fs.file_count < 2:
                    continue  # Not enough files to justify a move

                # Categorize files
                files_by_cat: dict[str, list[File]] = defaultdict(list)
                for f_rec in db.query(File).filter(
                    File.session_id == session_id,
                    File.path.like(f"{root_fs.path}%"),
                ).all():
                    cat = _file_category(f_rec.mime_type, f_rec.extension)
                    if cat != "other":
                        files_by_cat[cat].append(f_rec)

                # For each category with ≥2 files, suggest a subfolder
                for cat, cat_files in files_by_cat.items():
                    if len(cat_files) < 2:
                        continue
                    label = _folder_content_label({cat: len(cat_files)})
                    if not label:
                        continue
                    dst_folder = str(root_norm / label)

                    for f_rec in cat_files:
                        # Skip if already has a MOVE proposal
                        has_move = db.query(Proposal).filter_by(
                            file_id=f_rec.id,
                            proposal_type=ProposalType.MOVE,
                        ).first()
                        if has_move:
                            continue

                        src_path = Path(f_rec.path)
                        dst_path = Path(dst_folder) / src_path.name
                        if str(src_path) == str(dst_path):
                            continue

                        db.add(Proposal(
                            file_id=f_rec.id,
                            proposal_type=ProposalType.MOVE,
                            current_value=str(src_path),
                            proposed_value=str(dst_path),
                            reasoning=(
                                f"Root-level {cat} file — grouping with other "
                                f"{cat} files into '{label}/' for better organization."
                            ),
                            confidence=0.55,
                            status=ProposalStatus.PENDING,
                        ))
                        created_moves += 1

        if created_renames or created_moves:
            db.commit()

    return {"renames": created_renames, "moves": created_moves}

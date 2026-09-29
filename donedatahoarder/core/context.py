"""
Context builder — assembles a rich text context string for a file.

This context is fed to the AI so it can make informed naming/tagging decisions
even when the filename itself is useless (e.g. "IMG_0042.jpg" or "FINAL2.docx").
"""
import re
import threading
from collections import OrderedDict
from pathlib import Path

from donedatahoarder.db.models import File


_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^a-zA-Z0-9]+")

# How many siblings to include in context (avoid context-window explosion)
MAX_SIBLINGS = 15
MAX_PARENT_DEPTH = 4  # how many folder levels to walk up

# Directory listings are stable for a run and expensive on big folders.
# Keyed by directory, shared by every worker in the process.
_DIR_FILE_CACHE: OrderedDict[Path, tuple[dict[str, tuple[str, ...]], tuple[str, ...]]] = OrderedDict()
_DIR_FILE_CACHE_LOCK = threading.Lock()
MAX_CACHED_DIRECTORIES = 128
MAX_CACHED_EXTENSIONS_PER_DIRECTORY = 128


def clear_context_cache() -> None:
    with _DIR_FILE_CACHE_LOCK:
        _DIR_FILE_CACHE.clear()


def _tokenise(name: str) -> list[str]:
    """Split a filename or folder name into readable tokens."""
    # Remove extension
    stem = Path(name).stem
    # Split CamelCase
    stem = _CAMEL_SPLIT.sub(" ", stem)
    # Split on non-word chars
    tokens = _NON_WORD.sub(" ", stem).split()
    return [t for t in tokens if len(t) > 1]


def _folder_chain(path: Path, max_depth: int = MAX_PARENT_DEPTH) -> list[str]:
    """Return folder names from the file up to max_depth levels."""
    parts = []
    current = path.parent
    for _ in range(max_depth):
        parts.append(current.name)
        if current.parent == current:
            break
        current = current.parent
    return parts  # nearest-first


def _cached_directory_filenames(directory: Path):
    """Bounded per-extension and fallback samples for sibling context."""
    with _DIR_FILE_CACHE_LOCK:
        cached = _DIR_FILE_CACHE.get(directory)
        if cached is not None:
            _DIR_FILE_CACHE.move_to_end(directory)
            return cached
    by_ext: dict[str, list[str]] = {}
    fallback: list[str] = []
    try:
        for p in directory.iterdir():
            if not p.is_file():
                continue
            name = p.name
            ext = p.suffix.lower()
            if ext in by_ext or len(by_ext) < MAX_CACHED_EXTENSIONS_PER_DIRECTORY:
                sample = by_ext.setdefault(ext, [])
                if len(sample) < MAX_SIBLINGS + 1:
                    sample.append(name)
            if len(fallback) < MAX_SIBLINGS + 1:
                fallback.append(name)
    except OSError:
        pass
    sampled = ({k: tuple(v) for k, v in by_ext.items()}, tuple(fallback))
    with _DIR_FILE_CACHE_LOCK:
        if directory not in _DIR_FILE_CACHE:
            _DIR_FILE_CACHE[directory] = sampled
            while len(_DIR_FILE_CACHE) > MAX_CACHED_DIRECTORIES:
                _DIR_FILE_CACHE.popitem(last=False)
        return _DIR_FILE_CACHE[directory]


def _sibling_summary(path: Path, max_siblings: int = MAX_SIBLINGS) -> str:
    """Summarise nearby files in the same directory."""
    by_ext, fallback = _cached_directory_filenames(path.parent)
    same_ext = [name for name in by_ext.get(path.suffix.lower(), ())
                if name != path.name]
    other = [name for name in fallback if name != path.name
             and Path(name).suffix.lower() != path.suffix.lower()]
    ordered = same_ext[:max_siblings // 2] + other[:max_siblings // 2]

    if not ordered:
        return ""
    return ", ".join(ordered[:max_siblings])


def build_context(file_rec: File, db_siblings: list[File] | None = None) -> str:
    """
    Build a plain-text context string describing a file's surroundings.

    Args:
        file_rec:    The File ORM record.
        db_siblings: Optional list of File records from the same folder
                     (fetched from DB, already enriched with AI descriptions).
    """
    path = Path(file_rec.path)
    lines: list[str] = []

    # --- folder chain ---
    chain = _folder_chain(path)
    if chain:
        readable = " / ".join(reversed(chain))  # root→leaf order
        lines.append(f"Folder path: {readable}")
        tokens = []
        for folder in chain:
            tokens.extend(_tokenise(folder))
        if tokens:
            lines.append(f"Folder keywords: {', '.join(tokens)}")

    # --- the file itself ---
    lines.append(f"Filename: {path.name}")
    stem_tokens = _tokenise(path.stem)
    if stem_tokens:
        lines.append(f"Filename keywords: {', '.join(stem_tokens)}")

    # --- dates ---
    if file_rec.date_best:
        lines.append(f"Date (best guess): {file_rec.date_best.strftime('%Y-%m-%d')}")
    elif file_rec.date_modified:
        lines.append(f"Date modified: {file_rec.date_modified.strftime('%Y-%m-%d')}")

    # --- file properties ---
    if file_rec.mime_type:
        lines.append(f"File type: {file_rec.mime_type}")
    if file_rec.size_bytes:
        kb = file_rec.size_bytes / 1024
        mb = kb / 1024
        size_str = f"{mb:.1f} MB" if mb >= 1 else f"{kb:.0f} KB"
        lines.append(f"File size: {size_str}")

    # --- siblings on disk ---
    sibling_str = _sibling_summary(path)
    if sibling_str:
        lines.append(f"Other files in same folder: {sibling_str}")

    # --- DB siblings with AI descriptions (gold) ---
    if db_siblings:
        descs = [
            f"  • {Path(s.path).name}: {s.ai_description}"
            for s in db_siblings
            if s.ai_description and s.path != file_rec.path
        ][:5]
        if descs:
            lines.append("Nearby files with known descriptions:")
            lines.extend(descs)

    return "\n".join(lines)



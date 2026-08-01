"""
Folder summary tree — aggregates file-level metadata into per-folder
summaries, detects outlier files, and renders the tree for the LLM prompt.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, FileStatus
from donedatahoarder.db.session import get_engine


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
            from donedatahoarder.db.models import UserSession
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

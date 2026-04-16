"""
Folder organizer — uses LLM to suggest folder-level reorganization.

Two-phase approach:
1. Build a compact folder summary tree from analyzed file metadata
2. Ask the LLM to propose MOVE operations for better organization
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from datahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType,
)
from datahoarder.db.session import get_engine


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
        query = db.query(File).filter(
            File.session_id == session_id,
            File.status.in_([
                FileStatus.ENRICHED,
                FileStatus.ANALYZED,
                FileStatus.PROPOSED,
                FileStatus.APPLIED,
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

            # MIME breakdown
            mime = f.mime_type or "unknown"
            mime_group = mime.split("/")[0] if "/" in mime else mime
            fs.mime_breakdown[mime_group] = fs.mime_breakdown.get(mime_group, 0) + 1

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

        # Second pass: per-folder outlier detection. A file is an outlier if its
        # mime group differs from the folder's dominant mime group (>=60%), or
        # if it has tags but shares none of them with the folder's top tags.
        # Only run outlier detection on folders with >=4 files (statistical signal).
        for path, fs in folders.items():
            folder_files = files_by_folder.get(path, [])
            if len(folder_files) < 4:
                continue

            total = fs.file_count or 1
            dominant_mime = None
            if fs.mime_breakdown:
                top_mime, top_count = max(fs.mime_breakdown.items(), key=lambda kv: kv[1])
                if top_count / total >= 0.6:
                    dominant_mime = top_mime

            theme_tags = set(fs.top_tags)

            outliers: list[tuple[int, dict]] = []  # (priority, info) for sorting
            for f in folder_files:
                mime = f.mime_type or "unknown"
                mime_group = mime.split("/")[0] if "/" in mime else mime

                file_tags: list[str] = []
                if f.ai_tags:
                    try:
                        parsed = json.loads(f.ai_tags)
                        if isinstance(parsed, list):
                            file_tags = [str(t).lower() for t in parsed]
                    except (json.JSONDecodeError, TypeError):
                        pass

                is_mime_outlier = bool(
                    dominant_mime
                    and mime_group != dominant_mime
                    and mime_group != "unknown"
                )
                # Tag outlier: file has tags, folder has a theme, zero overlap
                is_tag_outlier = bool(
                    file_tags
                    and theme_tags
                    and not (set(file_tags) & theme_tags)
                )

                if not (is_mime_outlier or is_tag_outlier):
                    continue

                # Priority: mime outliers are more reliable signals than tag outliers
                priority = 0 if is_mime_outlier else 1
                reason_bits = []
                if is_mime_outlier:
                    reason_bits.append(f"type={mime_group} (folder is {dominant_mime})")
                if is_tag_outlier:
                    reason_bits.append("tags unrelated to folder theme")

                outliers.append((priority, {
                    "filename": f.filename or Path(f.path).name,
                    "mime_group": mime_group,
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
        return {"error": f"LLM call failed: {exc}", **counts}

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

    return counts

"""
Core reorganization loop — builds the folder tree, asks the LLM for MOVE /
MERGE / RENAME_FOLDER suggestions, parses them into Proposal records, then
runs the deterministic backstops.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType,
)
from donedatahoarder.db.session import get_engine

from .backstops import (
    _backstop_generic_folders,
    _backstop_mojibake_folders,
    _backstop_nonlatin_folders,
    _emit_relation_group_moves,
    _propagate_moves_to_skipped_siblings,
    _file_category,
    _folder_content_label,
)
from .prompts import REORG_SYSTEM_PROMPT
from .text_utils import _normalize_folder_name
from .tree import _format_tree_for_prompt, build_folder_tree


_DESTINATION_GENERIC = {
    "file", "files", "document", "documents", "image", "images", "photo",
    "photos", "group", "collection", "archive", "folder", "project", "new",
}
_PROJECT_EXTENSIONS = {
    ".ai", ".psd", ".blend", ".blend1", ".fbx", ".obj", ".mtl",
    ".dwg", ".dxf", ".bak", ".shx", ".3ds", ".skp", ".rvt",
    ".indd", ".prproj", ".aep", ".unity", ".uproject", ".3dm",
    ".3dmbak", ".max", ".sln", ".csproj", ".xcodeproj",
}
_PROJECT_MARKERS = {"package.json", "pyproject.toml", "requirements.txt",
                    "project.json", "makefile", "composer.json",
                    "cargo.toml", "go.mod"}
_PROJECT_MANIFEST_SUFFIXES = {
    ".sln", ".slnx", ".csproj", ".fsproj", ".vbproj", ".vcxproj",
}
_COLLECTION_FOLDERS = {"downloads", "inbox", "loose", "misc", "mixed", "unsorted",
                       "to_sort", "new folder", "files", "documents", "stuff", "temp"}


def _project_roots(root: Path, files, protection) -> set[Path]:
    """Find the nearest directory with a manifest or related editable assets.

    A lone PSD in Downloads is not enough to freeze the whole Downloads tree.
    The resource itself remains protected by ProtectionIndex.
    """
    markers: set[Path] = set()
    resources: dict[Path, list[Path]] = defaultdict(list)
    child_dirs: dict[Path, set[Path]] = defaultdict(set)
    checked_git: set[Path] = set()
    if (root / ".git").is_dir():
        return {root}
    for file_rec in files:
        source = Path(file_rec.path)
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        if (source.name.casefold() in _PROJECT_MARKERS
                or source.suffix.casefold() in _PROJECT_MANIFEST_SUFFIXES):
            markers.add(source.parent)
        if (source.name.casefold() == "project.pbxproj"
                and source.parent.suffix.casefold() == ".xcodeproj"):
            markers.add(source.parent.parent)
        if len(relative.parts) < 2:
            continue
        parent = source.parent
        if parent not in checked_git:
            checked_git.add(parent)
            if (parent / ".git").is_dir():
                markers.add(parent)
        if source.suffix.casefold() in _PROJECT_EXTENSIONS:
            resources[parent].append(source)
        for ancestor in source.parents:
            if ancestor == root or root not in ancestor.parents:
                break
            child_dirs[ancestor.parent].add(ancestor)
    result = set(markers)
    for folder, paths in resources.items():
        stems = Counter(path.stem.casefold() for path in paths)
        has_bundle = any(count >= 2 for count in stems.values())
        has_structure = any(child.name.casefold() in {"assets", "textures", "renders", "src", "source"}
                            for child in child_dirs.get(folder, set()))
        if folder.name.casefold() not in _COLLECTION_FOLDERS and (has_bundle or has_structure):
            result.add(folder)
    return result


def _inside_project(source: Path, project_roots: set[Path]) -> bool:
    return source in project_roots or any(parent in project_roots for parent in source.parents)


def _loose_source(file_rec: File, root: Path,
                  parent_categories: dict[Path, set[str]],
                  project_roots: set[Path]) -> bool:
    """Only a root file or an explicitly generic folder chain is standalone.

    A named folder containing several file types can still be a coherent
    document project. Category variety alone is not evidence of independence.
    """
    source = Path(file_rec.path)
    if _inside_project(source, project_roots):
        return False
    if source.parent == root:
        return True
    try:
        relative = source.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) < 2:
        return False
    return all(part.casefold() in _COLLECTION_FOLDERS
               for part in relative.parts[:-1])


def _words(value: str) -> set[str]:
    import re
    words = {word.casefold() for word in re.findall(r"[^\W_]+", value) if len(word) >= 3}
    return {word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss")
            else word for word in words}


def _organizer_move_allowed(file_rec: File, destination: Path, root: Path,
                            protected_index, sequence_ids: set[int],
                            project_roots: set[Path] | None = None) -> bool:
    """Keep project subtrees intact; only collect loose independent files."""
    source = Path(file_rec.path)
    try:
        source_relative = source.relative_to(root)
        dest_relative = destination.relative_to(root)
    except ValueError:
        return False
    if file_rec.id in sequence_ids or protected_index.assess(source).protected:
        return False
    if project_roots and _inside_project(source, project_roots):
        return False
    if not dest_relative.parts or not source_relative.parts:
        return False
    if len(source_relative.parts) > 1:
        if project_roots and _inside_project(source, project_roots):
            return False
        if (dest_relative.parts[0].casefold() == source_relative.parts[0].casefold()
                and len(dest_relative.parts) > len(source_relative.parts)):
            source_context = _words(str(source_relative.parent)) | _words(source.stem)
            new_subjects = (_words(str(dest_relative.parent)) - source_context
                            - _DESTINATION_GENERIC)
            if not new_subjects:
                return True
            verified = (getattr(file_rec, "analysis_outcome", None) == "content_verified"
                        and getattr(file_rec, "analysis_evidence_source", None) in {"text", "vision"})
            observed = source_context | _words(file_rec.ai_description or "") | _words(file_rec.ai_tags or "")
            return verified and new_subjects <= observed
    if dest_relative.parts[0].casefold() != "independent_files":
        return False
    if not _loose_source(file_rec, root, {}, project_roots or set()):
        return False
    if len(dest_relative.parts) < 3 or len(dest_relative.parts) > 5:
        return False
    category = _file_category(file_rec.mime_type, file_rec.extension)
    label = _folder_content_label({category: 1}) if category != "other" else None
    if not label or dest_relative.parts[1].casefold() != label.casefold():
        return False
    folders = list(dest_relative.parts[2:-1])
    if folders and folders[0].isdigit():
        if folders.pop(0) != _standalone_year(file_rec):
            return False
    return not folders or (len(folders) == 1 and
                           folders[0].casefold() == (_standalone_subject(file_rec) or "").casefold())


def _standalone_subject(file_rec: File) -> str | None:
    """Choose a broad topic only from filename or the file's own analysis."""
    generic = _DESTINATION_GENERIC | {
        "final", "copy", "draft", "untitled", "scan", "img", "dsc", "edited",
        "export", "backup", "version", "original", "independent", "file",
    }
    stem = Path(file_rec.path).stem
    filename_words = [w.casefold() for w in re.findall(r"[^\W_]+", stem)
                      if len(w) >= 4 and not any(c.isdigit() for c in w)]
    words = [w for w in filename_words if w not in generic]
    if not words and getattr(file_rec, "analysis_outcome", None) == "content_verified":
        try:
            import json
            tags = json.loads(file_rec.ai_tags or "[]")
        except (ValueError, TypeError):
            tags = []
        if isinstance(tags, list):
            words = [str(tag).casefold() for tag in tags
                     if isinstance(tag, str) and re.fullmatch(r"[\w -]{4,24}", tag)
                     and str(tag).casefold() not in generic]
    return words[0].title().replace(" ", "_") if words else None


def _standalone_year(file_rec: File) -> str | None:
    # EXIF is source evidence. Filesystem mtimes on extracted collections are
    # often an extraction timestamp, so they are deliberately not used here.
    dt = getattr(file_rec, "date_exif", None)
    return str(dt.year) if dt and 1970 <= dt.year <= 2100 else None


def _emit_standalone_moves(session_id: str, root_path: str) -> int:
    """Offer type/date/topic organization for independent loose files.

    A topic level appears only when at least two files share it. Each move
    remains a separate proposal; no project directory is traversed or moved.
    """
    from donedatahoarder.core.dependency_protection import ProtectionIndex

    root = Path(root_path).resolve()
    protection = ProtectionIndex(root)
    made = 0
    with Session(get_engine()) as db:
        query = db.query(File).filter(File.session_id == session_id)
        projects = _project_roots(root, query.yield_per(1000), protection)
        parent_categories: dict[Path, set[str]] = defaultdict(set)
        for f in query.yield_per(1000):
            parent_categories[Path(f.path).parent].add(_file_category(f.mime_type, f.extension))
        subjects = Counter(
            _standalone_subject(f) for f in query.yield_per(1000)
            if _loose_source(f, root, parent_categories, projects)
        )
        existing = {fid for (fid,) in db.query(Proposal.file_id).join(File, Proposal.file_id == File.id)
                    .filter(File.session_id == session_id,
                            Proposal.proposal_type == ProposalType.MOVE,
                            Proposal.status.in_([ProposalStatus.PENDING, ProposalStatus.APPROVED,
                                                 ProposalStatus.MODIFIED])).all()}
        reserved: set[str] = set()
        for f in query.yield_per(1000):
            if not _loose_source(f, root, parent_categories, projects):
                continue
            if f.id in existing or f.status not in {FileStatus.ANALYZED, FileStatus.PROPOSED}:
                continue
            if protection.assess(Path(f.path)).protected:
                continue
            category = _file_category(f.mime_type, f.extension)
            if category == "other":
                continue
            label = _folder_content_label({category: 1})
            if not label:
                continue
            pieces = ["Independent_Files", label]
            if year := _standalone_year(f):
                pieces.append(year)
            subject = _standalone_subject(f)
            if subject and subjects[subject] >= 2:
                pieces.append(subject)
            destination = root.joinpath(*pieces, Path(f.path).name)
            if destination.exists() or str(destination).casefold() in reserved:
                continue
            if not _organizer_move_allowed(f, destination, root, protection, set(), projects):
                continue
            reserved.add(str(destination).casefold())
            db.add(Proposal(
                file_id=f.id, proposal_type=ProposalType.MOVE,
                current_value=f.path, proposed_value=str(destination),
                reasoning="Independent loose file grouped by file type"
                          + (", EXIF year" if year else "")
                          + (", shared filename or verified topic" if subject and subjects[subject] >= 2 else "")
                          + ". Review before applying.",
                confidence=0.55, status=ProposalStatus.PENDING,
            ))
            made += 1
        db.commit()
    return made


def _folder_rename_keeps_identity(source: Path, destination: Path) -> bool:
    """Keep milestone/version numbers and descriptive folder subjects."""
    import re
    if source.parent != destination.parent or source == destination:
        return False
    source_name = source.name
    destination_name = destination.name
    source_numbers = set(re.findall(r"\d+", source_name))
    destination_numbers = set(re.findall(r"\d+", destination_name))
    if not source_numbers <= destination_numbers:
        return False
    source_codes = {
        token.casefold() for token in re.findall(r"[^\W_]+", source_name)
        if (len(token) >= 2 and token.isupper())
        or (any(char.isalpha() for char in token) and any(char.isdigit() for char in token))
    }
    destination_codes = {token.casefold() for token in re.findall(r"[^\W_]+", destination_name)}
    if not source_codes <= destination_codes:
        return False
    source_subjects = _words(source_name) - _DESTINATION_GENERIC
    destination_subjects = _words(destination_name)
    return source_subjects <= destination_subjects


def _grouping_move_key(proposal: Proposal) -> tuple[str, str, str] | None:
    """Identify deterministic group moves, without constraining direct file moves."""
    reasoning = proposal.reasoning or ""
    if reasoning.startswith("Cluster move "):
        kind = "cluster"
    elif reasoning.startswith("Root-level ") and " grouping with other " in reasoning:
        kind = "backstop"
    else:
        return None
    destination = Path(proposal.proposed_value or "")
    return kind, reasoning if kind == "cluster" else "", str(destination.parent).casefold()


def _suppress_unsafe_organizer_proposals(session_id: str, root_path: str) -> dict:
    """Last gate catches LLM, cluster and deterministic post-pass proposals."""
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.db.models import RelationGroup, RelationMember

    root = Path(root_path).resolve()
    protected_index = ProtectionIndex(root)
    removed = {"move": 0, "rename_folder": 0}
    reasons: dict[str, int] = {}
    examples: list[dict[str, str]] = []
    with Session(get_engine()) as db:
        project_roots = _project_roots(
            root, db.query(File).filter(File.session_id == session_id).yield_per(1000),
            protected_index,
        )
        sequence_ids = {
            fid for (fid,) in (
                db.query(RelationMember.file_id)
                .join(RelationGroup, RelationMember.group_id == RelationGroup.id)
                .filter(RelationGroup.session_id == session_id,
                        RelationGroup.label.like("frame_sequence_%"))
            )
        }
        sequence_dirs = {
            Path(path).parent for (path,) in (
                db.query(File.path)
                .join(RelationMember, RelationMember.file_id == File.id)
                .join(RelationGroup, RelationMember.group_id == RelationGroup.id)
                .filter(RelationGroup.session_id == session_id,
                        RelationGroup.label.like("frame_sequence_%"))
            )
        }
        proposals = (
            db.query(Proposal, File)
            .join(File, Proposal.file_id == File.id)
            .filter(File.session_id == session_id, Proposal.status == ProposalStatus.PENDING,
                    Proposal.proposal_type.in_([ProposalType.MOVE, ProposalType.RENAME_FOLDER]))
            .all()
        )
        folder_destinations: dict[str, int] = {}
        for proposal, _ in proposals:
            if proposal.proposal_type == ProposalType.RENAME_FOLDER and proposal.proposed_value:
                destination_key = str(Path(proposal.proposed_value)).casefold()
                folder_destinations[destination_key] = folder_destinations.get(destination_key, 0) + 1
        allowed_by_id: dict[int, bool] = {}
        reason_by_id: dict[int, str] = {}
        for proposal, file_rec in proposals:
            if proposal.proposal_type == ProposalType.MOVE:
                source = Path(file_rec.path)
                protection = protected_index.assess(source)
                allowed = bool(proposal.proposed_value) and _organizer_move_allowed(
                    file_rec, Path(proposal.proposed_value), root, protected_index,
                    sequence_ids, project_roots,
                )
                destination = Path(proposal.proposed_value or "")
                grouping = _grouping_move_key(proposal)
                redundant_nesting = bool(
                    grouping and source.parent == destination.parent.parent
                    and source.parent.name.casefold() == destination.parent.name.casefold()
                )
                if redundant_nesting:
                    allowed = False
                reason = (
                    "Numbered frame sequence must keep its folder and order" if file_rec.id in sequence_ids
                    else f"Protected resource: {protection.reason}" if protection.protected
                    else "Grouping would repeat the existing folder name" if redundant_nesting
                    else "Named source folder is not a loose collection"
                    if destination.is_relative_to(root / "Independent_Files")
                    and not _loose_source(file_rec, root, {}, project_roots)
                    else "Destination project or subject lacks source evidence"
                )
            else:
                source = Path(proposal.current_value or "")
                protection = protected_index.assess(source)
                try:
                    relative = source.relative_to(root)
                except ValueError:
                    allowed = False
                else:
                    # Preserve project roots and folders containing protected
                    # references or a numbered frame sequence.
                    allowed = (
                        len(relative.parts) > 1
                        and not _inside_project(source, project_roots)
                        and not any(source in project.parents for project in project_roots)
                        and not protection.protected
                        and bool(proposal.proposed_value)
                        and _folder_rename_keeps_identity(source, Path(proposal.proposed_value))
                        and folder_destinations.get(str(Path(proposal.proposed_value)).casefold(), 0) == 1
                        and not any(directory == source or source in directory.parents
                                    for directory in sequence_dirs)
                    )
                reason = (
                    f"Protected resource: {protection.reason}" if protection.protected
                    else "Numbered frame sequence folder must keep its identity"
                    if any(directory == source or source in directory.parents for directory in sequence_dirs)
                    else "Folder rename loses identity, collides, or has an invalid destination"
                )
            allowed_by_id[proposal.id] = allowed
            reason_by_id[proposal.id] = reason

        # A grouping only makes sense if at least two members survive the
        # evidence/dependency gate. Direct per-file moves are unaffected.
        surviving_groups: dict[tuple[str, str, str], int] = {}
        for proposal, _ in proposals:
            if proposal.proposal_type != ProposalType.MOVE or not allowed_by_id[proposal.id]:
                continue
            key = _grouping_move_key(proposal)
            if key is not None:
                surviving_groups[key] = surviving_groups.get(key, 0) + 1

        for proposal, file_rec in proposals:
            allowed = allowed_by_id[proposal.id]
            reason = reason_by_id[proposal.id]
            if allowed and proposal.proposal_type == ProposalType.MOVE:
                key = _grouping_move_key(proposal)
                if key is not None and surviving_groups[key] < 2:
                    allowed = False
                    reason = "Grouping would leave a one-file folder after safety filters"
            if not allowed:
                removed[proposal.proposal_type.value] += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                if len(examples) < 12:
                    examples.append({"source": str(proposal.current_value or file_rec.path),
                                     "destination": proposal.proposed_value or "",
                                     "reason": reason})
                db.delete(proposal)
        db.commit()
    return {**removed, "reasons": reasons, "examples": examples}

logger = logging.getLogger(__name__)

# LIKE escape that will not show up as a path separator.
_LIKE_ESCAPE = "!"


def _folder_child_like(folder: str) -> str:
    """LIKE pattern for children of ``folder``.

    A trailing separator is required so ``tax`` does not match ``tax_archive``.
    ``%`` and ``_`` in the folder name are escaped.
    """
    root = str(Path(folder))
    escaped = (
        root.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return escaped + os.sep + "%"


def _path_within_folder(folder: str, file_path: str) -> bool:
    """True when ``file_path`` is ``folder`` or a path inside it."""
    try:
        Path(file_path).relative_to(folder)
    except ValueError:
        return False
    return True


def _generate_reorg_proposals_impl(session_id: str) -> dict:
    """
    Analyze the folder tree and generate MOVE and RENAME_FOLDER proposals for reorganization.

    Returns summary dict with proposal counts.
    """
    from donedatahoarder.ai.router import get_client

    engine = get_engine()
    counts = {"move": 0, "rename_folder": 0, "skipped": 0, "errors": 0}

    # Get root path and language preference from session
    with Session(engine) as db:
        from donedatahoarder.db.models import UserSession
        us = db.get(UserSession, session_id)
        if not us:
            return {"error": "Session not found", **counts}
        root_path = us.root_path
        preferred_language = us.preferred_language or "leave_as_is"

    # Clear any previous PENDING organizer proposals for this session so that
    # re-running Organize always starts from a clean slate.  Applied/rejected
    # proposals are preserved — we only discard ones the user hasn't acted on yet.
    with Session(engine) as db:
        file_ids = db.query(File.id).filter(File.session_id == session_id)
        db.query(Proposal).filter(
            Proposal.file_id.in_(file_ids),
            Proposal.status == ProposalStatus.PENDING,
            Proposal.proposal_type.in_([
                ProposalType.MOVE, ProposalType.RENAME_FOLDER,
            ]),
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
                new_name = _normalize_folder_name(new_name)
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

                # Find a representative file in this folder to anchor the proposal.
                # Children only: `tax` must not pick up `tax_archive`.
                anchor_file = db.query(File).filter(
                    File.session_id == session_id,
                    File.path.like(_folder_child_like(src_abs), escape=_LIKE_ESCAPE),
                ).first()
                if not anchor_file or not _path_within_folder(src_abs, anchor_file.path):
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

                src_abs = str(Path(root_path) / _normalize_folder_name(src_folder))
                dst_abs = str(Path(root_path) / _normalize_folder_name(dst_folder))

                # Look up each named file in the source folder.
                # Children only: `tax` must not pick up `tax_archive`.
                for fname in filenames:
                    file_rec = db.query(File).filter(
                        File.session_id == session_id,
                        File.path.like(_folder_child_like(src_abs), escape=_LIKE_ESCAPE),
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
                    if not _path_within_folder(src_abs, file_rec.path):
                        # Outside the source folder. Skip it; do not flatten
                        # the path down to the bare filename.
                        counts["skipped"] += 1
                        continue
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

            # Resolve to absolute paths (normalize folder names for consistency)
            src_abs = str(Path(root_path) / _normalize_folder_name(src_folder))
            dst_abs = str(Path(root_path) / _normalize_folder_name(dst_folder))

            # Find files in source folder.
            # Children only: `tax` must not pick up `tax_archive`.
            query = db.query(File).filter(
                File.session_id == session_id,
                File.path.like(_folder_child_like(src_abs), escape=_LIKE_ESCAPE),
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

                # Build destination path (preserve the path under the source folder).
                # A path that is not under the source folder is skipped, not
                # collapsed to just the filename.
                src_path = Path(file_rec.path)
                try:
                    rel_to_src = src_path.relative_to(src_abs)
                except ValueError:
                    continue
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

    # Deterministic backstop: propagate MOVE destinations to SKIPPED siblings
    # whose primary (same stem in the same directory) is being moved. Without
    # this, files like `10.8.bak` (SKIPPED — backup of `10.8.dwg`) get stranded
    # at root while `10.8.dwg` ships off to a project folder. The Namer pass
    # `_propagate_renames_to_siblings` does the same job for RENAME proposals;
    # this is the MOVE counterpart.
    try:
        skipped_sibling_moves = _propagate_moves_to_skipped_siblings(session_id)
        if skipped_sibling_moves:
            counts["skipped_sibling_moves"] = skipped_sibling_moves
            counts["move"] = counts.get("move", 0) + skipped_sibling_moves
    except Exception:
        # Best-effort — never break the pipeline if propagation errors.
        pass

    # Deterministic backstop: emit RENAME_FOLDER for non-Latin folder names
    # when the user requested English filenames. The LLM is *told* to translate
    # in the prompt but routinely misses Hebrew/Arabic/CJK directories — we've
    # observed this on Hebrew CAD archives where folders like `צרפתי/` and
    # `תכניות עדכניות/` were preserved verbatim. This backstop guarantees a
    # rename is always proposed when the language preference asks for it.
    try:
        nonlatin_fixed = _backstop_nonlatin_folders(
            session_id, root_path, preferred_language
        )
        if nonlatin_fixed:
            counts["nonlatin_renames"] = nonlatin_fixed
            counts["rename_folder"] = counts.get("rename_folder", 0) + nonlatin_fixed
    except Exception:
        pass

    # Apply one evidence gate after LLM, relation-group, and deterministic
    # passes. In particular, the relation post-pass must not split the 503
    # numbered frames into a new folder or move path-dependent CAD resources.
    suppressed = _suppress_unsafe_organizer_proposals(session_id, root_path)
    if suppressed["move"] or suppressed["rename_folder"]:
        counts["suppressed_unsafe"] = suppressed["move"] + suppressed["rename_folder"]
        counts["suppression_reasons"] = suppressed["reasons"]
        counts["suppression_examples"] = suppressed["examples"]
        counts["move"] -= suppressed["move"]
        counts["rename_folder"] -= suppressed["rename_folder"]

    standalone_moves = _emit_standalone_moves(session_id, root_path)
    if standalone_moves:
        counts["standalone_moves"] = standalone_moves
        counts["move"] += standalone_moves

    return counts


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper
# ---------------------------------------------------------------------------

def generate_reorg_proposals(session_id: str) -> dict:
    """Write organization proposals under the database's cross-process lock."""
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("organize files"):
        return _generate_reorg_proposals_impl(session_id)

def generate_reorg_proposals_with_progress(
    session_id: str,
    pause_event=None,
    cancel_check=None,
):
    """
    Background-job-friendly wrapper around generate_reorg_proposals().

    Organize is dominated by a single LLM call (fold tree → reorg suggestions),
    so we run it in a worker thread and emit periodic heartbeats while waiting.
    Pause/cancel is honored before launching the worker; once the LLM call is
    in flight we can't cleanly interrupt it (Ollama HTTP request).

    Yields:
      {"phase": "starting"}
      {"phase": "running", "heartbeat": True}              every ~2s
      {"cancelled": True}                                   on cancel (before start)
      {"done": True, "move": ..., "folder_rename": ..., ...} terminal
    """
    import contextlib
    import contextvars
    import io
    import queue
    import threading

    if pause_event is not None:
        pause_event.wait()
    if cancel_check and cancel_check():
        yield {"cancelled": True}
        return

    yield {"phase": "starting"}

    result_queue: "queue.Queue" = queue.Queue(maxsize=4)
    sentinel_done = object()
    sentinel_error = object()
    final_result: dict = {}
    error_holder: list = [None]

    def _runner():
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                summary = generate_reorg_proposals(session_id=session_id)
            final_result.update(summary)
            result_queue.put(sentinel_done)
        except Exception as exc:
            error_holder[0] = exc
            result_queue.put(sentinel_error)

    worker = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_runner,),
        daemon=True,
        name="organize-worker",
    )
    from donedatahoarder.core.jobs import job_manager
    job_manager.start_tracked_worker(worker)

    while True:
        try:
            msg = result_queue.get(timeout=2.0)
        except queue.Empty:
            if cancel_check and cancel_check():
                # Worker (LLM call) can't be cleanly interrupted; let it finish
                # in the background and just stop streaming.
                yield {"cancelled": True, "phase": "running"}
                return
            yield {"phase": "running", "heartbeat": True}
            continue

        if msg is sentinel_done:
            break
        if msg is sentinel_error:
            raise error_holder[0] if error_holder[0] else RuntimeError("organize failed")

    worker.join(timeout=5.0)
    yield {"done": True, **final_result}

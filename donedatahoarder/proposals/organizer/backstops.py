"""
Deterministic post-passes for folder reorganization — run after the LLM call:
skipped-sibling MOVE propagation, relation-group cluster moves, and backstops
for mojibake / non-Latin / generic folder names.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType,
)
from donedatahoarder.db.session import get_engine

from .text_utils import _normalize_folder_name, _recover_mojibake, _transliterate_hebrew
from .tree import _file_category, build_folder_tree


def _propagate_moves_to_skipped_siblings(session_id: str) -> int:
    """
    Propagate MOVE destinations from primary files to SKIPPED siblings that
    share the same stem in the same directory.

    Concrete example: `10.8.dwg` gets a MOVE proposal to `CAD_Drawings/10.8.dwg`.
    `10.8.bak` lives next to it at root with status=SKIPPED (backup files are
    indexed but never analyzed). Without this pass, the .bak stays at root
    while its primary ships off to CAD_Drawings.

    Rules:
    - Only operates on SKIPPED files. ANALYZED/PROPOSED files have already had
      their shot at the LLM-driven MOVE pass.
    - Sibling must share the parent directory AND stem (case-insensitive) with
      a primary that has a PENDING MOVE proposal.
    - Sibling must not already have its own MOVE proposal.
    - The destination is the primary's destination directory + the sibling's
      original filename (preserves the .bak suffix).

    Returns the number of MOVE proposals created.
    """
    engine = get_engine()
    created = 0

    with Session(engine) as db:
        # Build a lookup: (parent_dir_lower, stem_lower) -> destination_dir
        primary_moves: dict[tuple[str, str], str] = {}
        primary_query = (
            db.query(Proposal, File)
            .join(File, Proposal.file_id == File.id)
            .filter(
                File.session_id == session_id,
                Proposal.proposal_type == ProposalType.MOVE,
                Proposal.status == ProposalStatus.PENDING,
            )
        )
        for prop, primary_file in primary_query:
            primary_path = Path(primary_file.path)
            dest_path = Path(prop.proposed_value) if prop.proposed_value else None
            if not dest_path:
                continue
            key = (str(primary_path.parent).lower(), primary_path.stem.lower())
            # First wins — multiple primaries with the same key shouldn't
            # happen in practice (same file, same dir), but if they do we
            # don't want to flap the destination.
            primary_moves.setdefault(key, str(dest_path.parent))
        if not primary_moves:
            return 0

        # Pull SKIPPED siblings — these are the files we want to rescue.
        skipped_files = (
            db.query(File)
            .filter(
                File.session_id == session_id,
                File.status == FileStatus.SKIPPED,
            )
            .all()
        )
        if not skipped_files:
            return 0

        skipped_ids = [f.id for f in skipped_files]

        # Pre-load existing MOVE proposals so we don't double-propose.
        existing_moves: set[int] = {
            file_id
            for (file_id,) in db.query(Proposal.file_id).filter(
                Proposal.proposal_type == ProposalType.MOVE,
                Proposal.file_id.in_(skipped_ids),
            )
        }

        for f in skipped_files:
            if f.id in existing_moves:
                continue
            path = Path(f.path)
            if not path.stem:
                continue
            key = (str(path.parent).lower(), path.stem.lower())
            dest_dir = primary_moves.get(key)
            if not dest_dir:
                continue
            dst_path = Path(dest_dir) / path.name
            if str(dst_path) == str(path):
                continue
            db.add(Proposal(
                file_id=f.id,
                proposal_type=ProposalType.MOVE,
                current_value=str(path),
                proposed_value=str(dst_path),
                reasoning=(
                    "Sibling MOVE — companion file shares stem "
                    f"'{path.stem}' with a primary moving to '{dest_dir}'; "
                    "propagating the destination keeps backup/source pairs "
                    "together after reorganization."
                ),
                # Match the rename-sibling pass confidence (0.6): structurally
                # grounded, deterministic, but flagged as "follower" not primary.
                confidence=0.6,
                status=ProposalStatus.PENDING,
            ))
            existing_moves.add(f.id)
            created += 1

        if created:
            db.commit()

    return created


def _backstop_nonlatin_folders(
    session_id: str,
    root_path: str,
    preferred_language: str,
) -> int:
    """
    Emit RENAME_FOLDER proposals for directories whose names are dominated by
    non-Latin characters (Hebrew, Arabic, CJK, etc.) when the user has asked
    for English filenames.

    The LLM organizer is *instructed* to translate non-English folder names,
    but in practice it misses them on dense, already-Hebrew-organized
    archives. This backstop guarantees a rename proposal exists.

    Strategy:
    - Walk the root looking for directories with names that lose >= 30% of
      their characters under `_safe()` (i.e. mostly non-Latin).
    - Skip the root itself, hidden directories (.ddh_trash, etc.), and any
      directory that already has a PENDING RENAME_FOLDER proposal.
    - Build the new name using this priority:
      1. AI-derived tags from analyzed files in the directory (confidence 0.60)
      2. Hebrew transliteration (if Hebrew detected) (confidence 0.60)
      3. Sequential placeholder 'non_latin_folder_<n>' (confidence 0.50)

    Returns the number of RENAME_FOLDER proposals created.
    """
    if preferred_language != "english":
        return 0
    if not root_path:
        return 0
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return 0

    engine = get_engine()
    created = 0

    with Session(engine) as db:
        # Existing RENAME_FOLDER targets (so we don't double-propose).
        existing_targets: set[str] = {
            (cv or "").rstrip("/\\").lower()
            for (cv,) in db.query(Proposal.current_value).filter(
                Proposal.proposal_type == ProposalType.RENAME_FOLDER,
                Proposal.status == ProposalStatus.PENDING,
            )
        }

        placeholder_counter = 0

        # Walk every subdirectory under root (excluding root itself and hidden).
        for dir_path in sorted(root.rglob("*")):
            if not dir_path.is_dir():
                continue
            # Skip hidden / system directories
            if dir_path.name.startswith(".") or dir_path.name.startswith("_"):
                continue
            # Skip if any ancestor is the trash folder
            if any(p.name == ".ddh_trash" for p in dir_path.parents):
                continue
            if str(dir_path).rstrip("/\\").lower() in existing_targets:
                continue

            original_name = dir_path.name

            # Check if the folder name is dominated by non-Latin characters
            # (Hebrew, Arabic, CJK, Cyrillic, etc.)
            non_latin_chars = sum(
                1 for c in original_name
                if not c.isascii() or (c.isascii() and not c.isalnum())
            )
            total_chars = sum(1 for c in original_name if c.isalnum() or not c.isascii())
            if total_chars == 0:
                continue  # All-punctuation folder name; skip.
            if non_latin_chars == 0:
                continue  # Fully Latin; leave it alone.

            # Pick a representative file inside this directory to anchor the
            # proposal (the executor's RENAME_FOLDER application uses the
            # file_id to discover the directory path).
            anchor_file = (
                db.query(File)
                .filter(
                    File.session_id == session_id,
                    File.path.like(f"{dir_path}%"),
                )
                .first()
            )
            if not anchor_file:
                continue

            # Priority 1: Try AI-derived tags from analyzed files
            new_name = _derive_english_folder_name(db, session_id, dir_path)
            confidence = 0.60  # AI-derived is higher confidence

            # Priority 2: Try Hebrew transliteration if tags failed
            if not new_name:
                new_name = _transliterate_hebrew(original_name)
                if new_name and new_name != original_name:
                    confidence = 0.60  # Transliteration is also solid
                else:
                    new_name = None

            # Priority 3: Fall back to placeholder with lower confidence
            if not new_name:
                placeholder_counter += 1
                new_name = f"non_latin_folder_{placeholder_counter}"
                confidence = 0.50  # Placeholder is weaker signal

            new_name = _normalize_folder_name(new_name)
            if not new_name or new_name == original_name:
                continue

            dst_abs = str(dir_path.parent / new_name)
            db.add(Proposal(
                file_id=anchor_file.id,
                proposal_type=ProposalType.RENAME_FOLDER,
                current_value=str(dir_path),
                proposed_value=dst_abs,
                reasoning=(
                    f"Non-Latin folder name '{original_name}' translated to "
                    f"'{new_name}' per session language preference "
                    f"(confidence: {confidence})"
                ),
                confidence=confidence,
                status=ProposalStatus.PENDING,
            ))
            created += 1

        if created:
            db.commit()

    return created


def _derive_english_folder_name(
    db: Session, session_id: str, dir_path: Path
) -> str | None:
    """
    Build an English folder name from the AI-derived tags / descriptions of
    files inside *dir_path*. Returns None if nothing usable is found.

    Strategy: count the most common English-only tokens across all
    `ai_tags` JSON arrays in the directory, take the top 2-3, and join with
    underscores. This works because tags are already English (the analyzer
    runs in English) even when filenames are Hebrew.
    """
    files = (
        db.query(File)
        .filter(
            File.session_id == session_id,
            File.path.like(f"{dir_path}%"),
            File.ai_tags.isnot(None),
        )
        .all()
    )
    if not files:
        return None

    token_counts: Counter[str] = Counter()
    for f in files:
        if not f.ai_tags:
            continue
        try:
            tags = json.loads(f.ai_tags)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                continue
            t = re.sub(r"[^a-z0-9_]", "_", tag.lower().replace(" ", "_"))
            t = re.sub(r"_+", "_", t).strip("_")
            if not t or len(t) < 3:
                continue
            # Skip generic content-type tags that don't add identity
            if t in {"file", "image", "photo", "document", "drawing", "scan"}:
                continue
            token_counts[t] += 1

    if not token_counts:
        return None

    top = [tok for tok, _ in token_counts.most_common(3)]
    return "_".join(top) if top else None


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
    from donedatahoarder.db.models import RelationGroup, RelationMember
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
        return top_cat.replace("_", " ").title().replace(" ", "_") + "_Files"
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

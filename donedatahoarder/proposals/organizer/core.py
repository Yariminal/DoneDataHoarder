"""
Core reorganization loop — builds the folder tree, asks the LLM for MOVE /
MERGE / RENAME_FOLDER suggestions, parses them into Proposal records, then
runs the deterministic backstops.
"""
from __future__ import annotations

import logging
import os
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
)
from .prompts import REORG_SYSTEM_PROMPT
from .text_utils import _normalize_folder_name
from .tree import _format_tree_for_prompt, build_folder_tree

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


def generate_reorg_proposals(session_id: str) -> dict:
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

    return counts


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper
# ---------------------------------------------------------------------------

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
    worker.start()

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

"""
Core proposal loop — creates RENAME / ADD_TAGS proposals for analyzed files,
then runs the post-passes (sibling propagation, disambiguation, fallbacks,
spelling normalisation, near-duplicate flagging).
"""
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession
from donedatahoarder.db.session import get_engine

from .llm import translate_filename
from .naming import _ensure_prefix, _resolve_collision, build_new_name
from .postpass import (
    _disambiguate_generic_stems_in_dir,
    _flag_near_duplicate_proposals,
    _generate_fallback_for_useless_stems,
    _generate_hygiene_fallback,
    _normalize_spelling_in_proposals,
    _propagate_renames_to_siblings,
    _propagate_renames_via_relation_groups,
)


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


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper
# ---------------------------------------------------------------------------

def generate_proposals_with_progress(
    session_id: str | None = None,
    pause_event=None,
    cancel_check=None,
):
    """
    Background-job-friendly wrapper around generate_proposals().

    generate_proposals() is a single coarse-grained operation (typically
    10–60 s on 10K files because it's reformatting existing analysis,
    not calling the LLM). We run it in a worker thread and yield periodic
    heartbeats while the consumer waits for the final summary.

    Pause is implemented by checking pause_event before launching the
    worker (so the user can pause the unattended run before propose
    starts). Cancel works similarly — once the worker is running, it
    cannot be interrupted mid-batch (the operation is short enough that
    coarse-grained cancel between phases is acceptable).

    Yields:
      {"phase": "starting"}
      {"phase": "running", "heartbeat": True}              every ~2s
      {"cancelled": True}                                  on cancel (before start)
      {"done": True, "rename": ..., "tags": ..., ...}     terminal
    """
    import contextlib
    import contextvars
    import io
    import queue
    import threading

    # Honor pause/cancel BEFORE the long-running work begins
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
                summary = generate_proposals(session_id=session_id)
            final_result.update(summary)
            result_queue.put(sentinel_done)
        except Exception as exc:
            error_holder[0] = exc
            result_queue.put(sentinel_error)

    # Run the worker inside a copy of the caller's context: the AI provider
    # (used by translate_filename) is bound via a contextvar, and contextvars
    # do not propagate into new threads on their own.
    ctx = contextvars.copy_context()
    worker = threading.Thread(
        target=ctx.run, args=(_runner,), daemon=True, name="propose-worker"
    )
    worker.start()

    while True:
        try:
            msg = result_queue.get(timeout=2.0)
        except queue.Empty:
            # Heartbeat — keeps SSE alive and lets cancel propagate
            if cancel_check and cancel_check():
                # Worker can't be interrupted mid-flight; just stop streaming
                # and let it finish in the background. The proposals it wrote
                # before cancel are still valid.
                yield {"cancelled": True, "phase": "running"}
                return
            yield {"phase": "running", "heartbeat": True}
            continue

        if msg is sentinel_done:
            break
        if msg is sentinel_error:
            raise error_holder[0] if error_holder[0] else RuntimeError("propose failed")

    worker.join(timeout=5.0)
    yield {"done": True, **final_result}

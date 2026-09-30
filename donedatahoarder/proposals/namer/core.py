"""
Core proposal loop — creates RENAME / ADD_TAGS proposals for analyzed files,
then runs the post-passes (sibling propagation, disambiguation, fallbacks,
spelling normalisation, near-duplicate flagging).
"""
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType,
    RelationGroup, RelationMember, UserSession,
)
from donedatahoarder.db.session import get_engine

from .llm import translate_filename
from .naming import (
    _ensure_prefix, _is_useless_stem, _name_date_provenance,
    _resolve_collision, build_new_name,
)
from .postpass import (
    _disambiguate_generic_stems_in_dir,
    _generate_fallback_for_useless_stems,
    _generate_hygiene_fallback,
    _normalize_spelling_in_proposals,
    _propagate_renames_to_siblings,
    _propagate_renames_via_relation_groups,
)


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------

_IDENTITY_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_GENERIC_SOURCE_TOKENS = {"img", "dsc", "image", "photo", "file", "untitled", "scan"}
_SIGNED_TECH_RE = re.compile(
    r"(?i)(?:^|[\s_])(?:level|elev(?:ation)?|height|ceiling|floor)[\s_]*[−-]\d+(?:\.\d+)?"
)


def _unsupported_name_inference(file_rec: File, proposed_stem: str) -> str | None:
    """Catch specific model interpretations that readable content cannot certify.

    These patterns caused observed false names: a signed plan elevation became
    an ordinal floor, and an ambiguous render became an L-shaped object. Keep
    the proposal out of the actionable queue when the original identity does
    not independently establish that interpretation.
    """
    original = Path(file_rec.path).stem.casefold()
    proposed = proposed_stem.casefold().replace("-", "_")
    # A dotted numeric stem may be a drawing, sheet or version identifier.
    # Neither model prose nor a date guessed from metadata establishes that
    # `15.12` and `15_...` are interchangeable.
    if re.fullmatch(r"\d{1,4}(?:\.\d{1,4})+", original):
        if original not in proposed_stem.casefold():
            return "source_numeric_identifier_lost"
    if re.search(r"(?:^|_)\d+(?:st|nd|rd|th)(?:_|$)", proposed):
        if not re.search(r"\d+(?:st|nd|rd|th)", original):
            return "unverified_ordinal"
    if re.search(r"(?:^|_)(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)(?:_|$)", proposed):
        if not re.search(r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b", original):
            return "unverified_ordinal"
    if re.search(r"(?:^|_)(?:קומה|מפלס)_(?:ראשונה|שנייה|שלישית|רביעית|חמישית|שישית|שביעית|שמינית|תשיעית|עשירית|\d+)(?:_|$)", proposed):
        if not re.search(r"(?:קומה|מפלס)[\s_-]?(?:ראשונה|שנייה|שלישית|רביעית|חמישית|שישית|שביעית|שמינית|תשיעית|עשירית|\d+)", original):
            return "unverified_technical_level"
    if re.search(r"(?:^|_)(?:floor|level|storey|story)_?\d+", proposed):
        if not re.search(r"(?:floor|level|storey|story)[\s_-]?\d+", original):
            return "unverified_technical_level"
    # A single view of a render is weak evidence for a geometric shape label.
    # Keep established identifiers such as an existing 'L-shape' filename.
    if re.search(r"(?:^|_)(?:l|u|v|t|s|c|z)_?shap(?:e|ed)(?:_|$)", proposed):
        if not re.search(r"(?:^|[\s_-])(?:l|u|v|t|s|c|z)[\s_-]?shap(?:e|ed)", original):
            return "uncertain_shape_interpretation"
    return None


def _preserves_descriptive_identity(original_stem: str, proposed_stem: str) -> bool:
    """Do not trade an informative project filename for a generic AI label."""
    signed = _SIGNED_TECH_RE.search(original_stem)
    if signed and signed.group().strip().casefold() not in proposed_stem.casefold():
        return False
    compact_id = any(
        (len(token) >= 2 and token.isupper())
        or (any(char.isalpha() for char in token) and any(char.isdigit() for char in token))
        for token in _IDENTITY_TOKEN_RE.findall(original_stem)
    )
    if _is_useless_stem(original_stem) and not compact_id:
        return True
    raw_tokens = _IDENTITY_TOKEN_RE.findall(original_stem)
    original_tokens = [token.casefold() for token in raw_tokens]
    meaningful = [
        token.casefold() for token in raw_tokens
        if (len(token) >= 3 or token.isdigit() or re.fullmatch(r"v\d+", token.casefold())
            or (len(token) >= 2 and token.isupper())
            or (any(char.isalpha() for char in token) and any(char.isdigit() for char in token)))
        and token.casefold() not in _GENERIC_SOURCE_TOKENS
    ]
    if not meaningful:
        return True
    proposed_tokens = {token.casefold() for token in _IDENTITY_TOKEN_RE.findall(proposed_stem)}
    return all(token in proposed_tokens for token in meaningful)


def _restore_descriptive_identity(original_stem: str, proposed_stem: str) -> str:
    if _preserves_descriptive_identity(original_stem, proposed_stem):
        return proposed_stem
    signed = _SIGNED_TECH_RE.search(original_stem)
    if signed and signed.group().strip().casefold() not in proposed_stem.casefold():
        return f"{original_stem}_{proposed_stem}"
    existing = {token.casefold() for token in _IDENTITY_TOKEN_RE.findall(proposed_stem)}
    original = [token for token in _IDENTITY_TOKEN_RE.findall(original_stem)
                if token.casefold() not in _GENERIC_SOURCE_TOKENS]
    missing = [token for token in original if token.casefold() not in existing]
    return "_".join([*missing, proposed_stem]) if missing else proposed_stem


def _borrowed_name_supported(file_rec: File, proposed_stem: str) -> bool:
    """Own verified content must support every new semantic term borrowed."""
    if not _content_verified_for_naming(file_rec):
        return False
    proposed = {token.casefold() for token in _IDENTITY_TOKEN_RE.findall(proposed_stem)}
    original = {token.casefold() for token in _IDENTITY_TOKEN_RE.findall(Path(file_rec.path).stem)}
    generic = {"file", "document", "image", "photo", "source", "sibling", "export", "backup"}
    new_semantics = proposed - original - generic
    evidence = {token.casefold() for token in _IDENTITY_TOKEN_RE.findall(
        (file_rec.ai_description or "") + " " + (file_rec.ai_tags or "")
    )}
    description_tokens = [token.casefold() for token in _IDENTITY_TOKEN_RE.findall(
        file_rec.ai_description or ""
    )]
    for index, token in enumerate(description_tokens):
        if token in new_semantics and {"not", "no", "without"} & set(
            description_tokens[max(0, index - 3):index]
        ):
            return False
    return bool(new_semantics) and new_semantics <= evidence


def _content_verified_for_naming(file_rec: File) -> bool:
    return (
        getattr(file_rec, "analysis_outcome", None) == "content_verified"
        and getattr(file_rec, "analysis_evidence_source", None) in {"text", "vision"}
        and file_rec.ai_confidence is not None
        and file_rec.ai_confidence > 0
    )


def _suppress_unsafe_rename_postpasses(session_id: str | None,
                                       sequence_ids: set[int], *,
                                       file_ids: set[int] | None = None) -> dict[str, int]:
    """Final gate applies to every naming post-pass, not only the AI pass."""
    if not session_id:
        return {}
    removed: dict[str, int] = {}
    with Session(get_engine()) as db:
        from donedatahoarder.core.dependency_protection import ProtectionIndex
        from donedatahoarder.proposals.organizer.core import _inside_project, _project_roots
        owner = db.get(UserSession, session_id)
        protection = ProtectionIndex(Path(owner.root_path)) if owner and owner.root_path else None
        project_roots = (
            _project_roots(Path(owner.root_path),
                           db.query(File).filter(File.session_id == session_id).yield_per(1000),
                           protection)
            if protection else set()
        )
        query = (
            db.query(Proposal, File).join(File, Proposal.file_id == File.id)
            .filter(File.session_id == session_id,
                    Proposal.proposal_type == ProposalType.RENAME,
                    Proposal.status == ProposalStatus.PENDING)
        )
        if file_ids is not None:
            proposals = []
            selected_ids = sorted(file_ids)
            for start in range(0, len(selected_ids), 500):
                proposals.extend(query.filter(
                    File.id.in_(selected_ids[start:start + 500])).all())
        else:
            proposals = query.all()
        reserved = {Path(p.proposed_value) for p, _ in proposals if p.proposed_value}
        next_suffixes: dict[Path, int] = {}
        for proposal, file_rec in proposals:
            original = Path(file_rec.path)
            proposed = Path(proposal.proposed_value or "")
            borrowed = (proposal.reasoning or "").startswith((
                "RelationGroup propagation", "Sibling rename",
            ))
            if file_rec.id in sequence_ids:
                reason = "numbered_sequence"
            elif _inside_project(original, project_roots):
                reason = "project_subtree_preserved"
            elif protection and protection.assess(original).protected:
                reason = "protected_resource"
            elif (specificity := _unsupported_name_inference(file_rec, proposed.stem)):
                reason = specificity
            elif borrowed and not _borrowed_name_supported(file_rec, proposed.stem):
                # Related paths or a shared stem do not establish the same
                # content. A target's own verified description must support
                # the transferred semantic name.
                reason = "unsupported_relation_name_transfer"
            else:
                restored = _restore_descriptive_identity(original.stem, proposed.stem)
                if len(restored) > 96:
                    # An identity-preserving suggestion must still be usable.
                    # Keep the existing descriptive name for human review.
                    reason = "name_too_long_after_identity_retention"
                    removed[reason] = removed.get(reason, 0) + 1
                    db.delete(proposal)
                    continue
                if restored != proposed.stem:
                    destination = proposed.with_name(restored + proposed.suffix)
                    if destination in reserved or destination.exists():
                        destination = _resolve_collision(destination, original,
                                                         reserved_names=reserved,
                                                         next_suffixes=next_suffixes)
                    reserved.add(destination)
                    proposal.proposed_value = str(destination)
                    proposal.reasoning = ((proposal.reasoning or "")
                                          + " Original project/file identity retained.")
                continue
            removed[reason] = removed.get(reason, 0) + 1
            db.delete(proposal)
        db.commit()
    return removed

def _generate_proposals_impl(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    session_id: str | None = None,
) -> dict:
    """
    Create Proposal records for all ANALYZED files.
    Optionally translates filenames based on session's preferred_language setting.

    Args:
        limit: Maximum number of analyzed files to process.
        offset: Skip first N analyzed files in stable ID order.
        session_id: Restrict to files belonging to this session.

    Explicit slices omit collection-wide propagation and cleanup post-passes
    so a debug request cannot create proposals outside the selected slice.

    Returns:
        Summary dict with proposal counts.
    """
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn,
    )

    engine = get_engine()
    counts = {"rename": 0, "tags": 0, "skipped": 0}
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if offset is not None and offset < 0:
        raise ValueError("offset must be non-negative")
    processed = 0
    processed_by_session: dict[str, set[int]] = {}

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
        from donedatahoarder.core.dependency_protection import ProtectionIndex
        from donedatahoarder.proposals.organizer.core import _inside_project, _project_roots

        project_roots = (
            _project_roots(Path(session_root_path),
                           session.query(File).filter(File.session_id == session_id).yield_per(1000),
                           ProtectionIndex(Path(session_root_path)))
            if session_id and session_root_path else set()
        )
        sequence_ids = {
            file_id for (file_id,) in (
                session.query(RelationMember.file_id)
                .join(RelationGroup, RelationMember.group_id == RelationGroup.id)
                .filter(RelationGroup.session_id == session_id,
                        RelationGroup.label.like("frame_sequence_%"))
            )
        } if session_id else set()
        query = session.query(File).filter(File.status == FileStatus.ANALYZED)
        if session_id:
            query = query.filter(File.session_id == session_id)
        query = query.order_by(File.id)
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        total = query.count()


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
        next_suffixes: dict[Path, int] = {}

        while True:
            # Build names inside a short read. translate_filename calls the LLM,
            # so it runs only after this transaction is closed.
            prepared: list[dict] = []
            with Session(engine) as session:
                p_q = session.query(File).filter(File.status == FileStatus.ANALYZED)
                if session_id:
                    p_q = p_q.filter(File.session_id == session_id)
                # Processed rows leave ANALYZED; the skipped prefix remains
                # eligible and must be excluded on every subsequent batch.
                p_q = p_q.order_by(File.id)
                if offset:
                    p_q = p_q.offset(offset)
                take = 100 if limit is None else min(100, limit - processed)
                if take <= 0:
                    break
                batch = p_q.limit(take).all()
                if not batch:
                    break

                for file_rec in batch:
                    path = Path(file_rec.path)
                    verified = _content_verified_for_naming(file_rec)
                    new_name = (
                        build_new_name(file_rec, root_path=session_root_path)
                        if verified and file_rec.id not in sequence_ids
                        and not _inside_project(path, project_roots) else None
                    )
                    rename_name = None
                    if new_name and new_name != path.name:
                        # Ensure prefix is preserved if original had one
                        new_stem = Path(new_name).stem
                        new_stem = _ensure_prefix(new_stem, path.stem)
                        new_stem = _restore_descriptive_identity(path.stem, new_stem)
                        if len(new_stem) <= 96 and not _unsupported_name_inference(file_rec, new_stem):
                            rename_name = f"{new_stem}{Path(new_name).suffix}"
                    prepared.append({
                        "id": file_rec.id,
                        "path": path,
                        "new_name": rename_name,
                        "ai_description": file_rec.ai_description,
                        "ai_confidence": file_rec.ai_confidence,
                        "ai_tags": file_rec.ai_tags if verified else None,
                    })

            for item in prepared:
                new_name = item["new_name"]
                if new_name and preferred_language != "leave_as_is":
                    new_name = translate_filename(new_name, preferred_language)
                    item["new_name"] = new_name
                if new_name:
                    proposed_path = _resolve_collision(
                        item["path"].parent / new_name,
                        item["path"],
                        reserved_names=reserved_names,
                        next_suffixes=next_suffixes,
                    )
                    reserved_names.add(proposed_path)
                    item["proposed_path"] = proposed_path
                else:
                    item["proposed_path"] = None

            with Session(engine) as session:
                batch_ids = [item["id"] for item in prepared]
                # One query for the batch instead of two SELECTs per file.
                existing_proposals: set[tuple[int, ProposalType]] = {
                    (p.file_id, p.proposal_type)
                    for p in session.query(Proposal.file_id, Proposal.proposal_type)
                    .filter(Proposal.file_id.in_(batch_ids))
                }
                files_by_id = {
                    f.id: f
                    for f in session.query(File).filter(File.id.in_(batch_ids))
                }

                for item in prepared:
                    file_rec = files_by_id.get(item["id"])
                    path: Path = item["path"]
                    made_proposal = False
                    if file_rec is None:
                        progress.advance(task)
                        continue

                    # --- RENAME proposal ---
                    proposed_path = item["proposed_path"]
                    if proposed_path is not None:
                        if (file_rec.id, ProposalType.RENAME) not in existing_proposals:
                            session.add(Proposal(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.RENAME,
                                current_value=str(path),
                                proposed_value=str(proposed_path),
                                reasoning=(
                                    f"Renamed based on AI description: "
                                    f"{(item['ai_description'] or '')[:120]}"
                                    + _name_date_provenance(file_rec, proposed_path.stem)
                                ),
                                confidence=(item["ai_confidence"] if item["ai_confidence"] is not None else 0.5),
                                status=ProposalStatus.PENDING,
                            ))
                            counts["rename"] += 1
                            made_proposal = True

                    # --- ADD_TAGS proposal ---
                    if item["ai_tags"]:
                        if (file_rec.id, ProposalType.ADD_TAGS) not in existing_proposals:
                            session.add(Proposal(
                                file_id=file_rec.id,
                                proposal_type=ProposalType.ADD_TAGS,
                                current_value=None,
                                proposed_value=item["ai_tags"],
                                reasoning="Tags generated by AI analysis",
                                confidence=(item["ai_confidence"] if item["ai_confidence"] is not None else 0.5),
                                status=ProposalStatus.PENDING,
                            ))
                            counts["tags"] += 1
                            made_proposal = True

                    # Update file status
                    file_rec.status = FileStatus.PROPOSED
                    processed += 1
                    processed_by_session.setdefault(file_rec.session_id, set()).add(file_rec.id)
                    if not made_proposal:
                        counts["skipped"] += 1

                    progress.advance(task)

                session.commit()

    if limit is not None or offset:
        suppressed = {}
        with Session(engine) as db:
            sequences_by_session = {
                sid: {fid for (fid,) in (
                    db.query(RelationMember.file_id)
                    .join(RelationGroup, RelationMember.group_id == RelationGroup.id)
                    .filter(RelationGroup.session_id == sid,
                            RelationGroup.label.like("frame_sequence_%"))
                )}
                for sid in processed_by_session
            }
        for sid, file_ids in processed_by_session.items():
            for reason, number in _suppress_unsafe_rename_postpasses(
                sid, sequences_by_session[sid], file_ids=file_ids,
            ).items():
                suppressed[reason] = suppressed.get(reason, 0) + number
        if suppressed:
            counts["suppressed_unsafe_renames"] = sum(suppressed.values())
            counts["rename_suppression_reasons"] = suppressed
        return counts

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

    if session_id:
        suppressed = _suppress_unsafe_rename_postpasses(session_id, sequence_ids)
    else:
        # Older `ddh pipeline` and direct library callers may omit a session ID.
        # Apply the same final gate to every affected session, including its
        # own numbered-frame membership and dependency root.
        suppressed = {}
        with Session(engine) as db:
            affected = [sid for (sid,) in (
                db.query(File.session_id).join(Proposal, Proposal.file_id == File.id)
                .filter(Proposal.proposal_type == ProposalType.RENAME,
                        Proposal.status == ProposalStatus.PENDING)
                .distinct()
            ) if sid]
            sequences_by_session = {
                sid: {fid for (fid,) in (
                    db.query(RelationMember.file_id)
                    .join(RelationGroup, RelationMember.group_id == RelationGroup.id)
                    .filter(RelationGroup.session_id == sid,
                            RelationGroup.label.like("frame_sequence_%"))
                )}
                for sid in affected
            }
        for sid in affected:
            for reason, number in _suppress_unsafe_rename_postpasses(
                sid, sequences_by_session[sid]
            ).items():
                suppressed[reason] = suppressed.get(reason, 0) + number
    if suppressed:
        counts["suppressed_unsafe_renames"] = sum(suppressed.values())
        counts["rename_suppression_reasons"] = suppressed
        if suppressed.get("numbered_sequence"):
            counts["sequence_renames_suppressed"] = suppressed["numbered_sequence"]

    return counts


# ---------------------------------------------------------------------------
# Background-job-friendly wrapper
# ---------------------------------------------------------------------------

def generate_proposals(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    session_id: str | None = None,
) -> dict:
    """Write naming proposals under the database's cross-process lock."""
    from donedatahoarder.core.process_lock import operation_lock

    with operation_lock("generate proposals"):
        return _generate_proposals_impl(limit, offset, session_id)

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

    worker = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_runner,),
        daemon=True,
        name="propose-worker",
    )
    from donedatahoarder.core.jobs import job_manager
    job_manager.start_tracked_worker(worker)

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

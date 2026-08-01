"""
Post-pass helpers for rename proposals — run after the main proposal loop:
sibling propagation, relation-group propagation, generic-stem disambiguation,
useless-stem / hygiene fallbacks, spelling normalisation, and near-duplicate
flagging.
"""
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalStatus, ProposalType
from donedatahoarder.db.session import get_engine

from .naming import (
    _GENERIC_STEM_TOKENS,
    _extract_distinguishing_prefix,
    _folder_context_fallback,
    _hygienic_stem,
    _is_useless_stem,
    _needs_hygiene,
    _resolve_collision,
)


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

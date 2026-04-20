"""
Filename-based semantic grouping of related files (the `relate` pipeline step).

Runs after Scan. For each directory (per_directory scope) or the whole tree
(cross_directory scope), asks the LLM to group files that are "conceptually
one thing" — e.g. a .dwg AutoCAD source with its .bak backup and PDF exports,
a .psd with its exported .jpg, a multi-version plan across several versions.

If the LLM returns nothing for a directory (call fails, empty response, or
parsing fails), a regex-based prefix clustering backstop runs. The backstop
detects files that share a leading numeric prefix like `10.8` or `24.8#2`,
which is common for date-stamped CAD / submission workflows. Backstop groups
get confidence=0.3 so downstream consumers (Namer, Organizer) can distinguish
them from LLM-reasoned groups (confidence=0.8).

The output lives in RelationGroup + RelationMember. Files themselves are not
modified. Consumers (Phase 2) will read these groups to drive sibling rename
propagation and folder clustering.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from datahoarder.db.models import (
    File, FileStatus, RelationGroup, RelationMember, RelationRole,
)
from datahoarder.db.session import get_engine

logger = logging.getLogger(__name__)

# LLM groups at 0.8, backstop at 0.3 — consumers can filter on confidence.
LLM_CONFIDENCE = 0.8
BACKSTOP_CONFIDENCE = 0.3

# Cap on filenames sent to the LLM in one call to avoid prompt AND response
# overruns. Smaller dirs get one call; larger dirs get chunked (see _chunk).
# Empirically: even at 200 input files Ollama responses can hit a default
# token budget on large clusters (e.g. 339 SHX fonts producing 30+ groups
# truncates the JSON mid-array). 100 keeps the worst-case response size
# safely inside a typical 4k-token completion budget.
MAX_FILES_PER_CALL = 100

# Leading numeric prefix: "10.8", "24.8", "3.9.1", etc. Stops at the first
# non-digit/non-dot character. Optional trailing "#N" version tag is part
# of the stem's identity but NOT of the shared prefix (so 24.8 and 24.8#2
# cluster together).
_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)")

_ROLE_BY_STRING = {r.value: r for r in RelationRole}


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

RELATE_SYSTEM_PROMPT = """\
You are a file-grouping assistant for a digital archivist.

You will see a listing of files in a directory (or across a tree). Group
files that are CONCEPTUALLY ONE THING — same document in different formats,
same CAD drawing with its backup and PDF exports, different versions of the
same plan, related photos from one event, a source file with its derivatives.

Rules:
- Minimum group size: 2 files. Singletons are NOT groups.
- Do NOT invent filenames. Only use filenames from the input exactly.
- Each filename belongs to AT MOST ONE group. Omit files that don't group.
- A group must share a real identity — not just a common extension or a
  similar-looking name. "Two random JPGs" is not a group. "Two JPGs from
  the same photo shoot indicated by date and subject" is.
- Assign a role to each member:
    "source"  — master / canonical file (e.g. .dwg, .psd, .ai, .3dm)
    "export"  — derivative output (e.g. PDF/JPG exported from a source)
    "backup"  — automatic backup sibling (e.g. .bak, .3dmbak)
    "version" — revised variant of another member (e.g. file_v2)
    "sibling" — related peer without a clear source/export hierarchy
- Use "sibling" when unsure.

Return STRICT JSON: an array of groups.
Schema:
[
  {
    "label": "short_snake_case_slug (<=40 chars, folder-safe)",
    "reason": "one sentence: what binds these files together",
    "members": [
      {"filename": "exact_filename.ext", "role": "source|export|backup|version|sibling"}
    ]
  }
]

If there are no real groups, return [].
"""


# ---------------------------------------------------------------------------
# Slug / label utilities
# ---------------------------------------------------------------------------

_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9_\-]+")
_SLUG_COLLAPSE = re.compile(r"[_\-]{2,}")


def _slugify(text: str, max_len: int = 40) -> str:
    """Normalize an LLM-emitted label into a safe folder-name slug."""
    s = (text or "").strip().lower()
    # Replace whitespace/slashes with underscores first
    s = re.sub(r"[\s/\\.]+", "_", s)
    s = _SLUG_BAD_CHARS.sub("", s)
    s = _SLUG_COLLAPSE.sub("_", s).strip("_-")
    if not s:
        s = "group"
    return s[:max_len].rstrip("_-")


def _deduplicate_labels(groups: list[dict]) -> None:
    """Ensure every group label is unique within the session (in-place)."""
    seen: dict[str, int] = {}
    for g in groups:
        base = g["label"] or "group"
        if base not in seen:
            seen[base] = 1
            continue
        seen[base] += 1
        g["label"] = f"{base}_{seen[base]}"


# ---------------------------------------------------------------------------
# Backstop: regex-based prefix clustering
# ---------------------------------------------------------------------------

def _numeric_prefix(stem: str) -> Optional[str]:
    """Return the leading numeric prefix of a stem, or None if no digits lead."""
    m = _PREFIX_RE.match(stem)
    return m.group(1) if m else None


def _group_prefix(group: dict) -> Optional[str]:
    """
    Return the single shared numeric prefix of a group, or None.

    A group has a "shared prefix" if at least 2 members carry a leading
    numeric prefix AND they all agree on the same one. Members without
    any numeric prefix are ignored (they don't block, they don't help).
    """
    prefixes: set[str] = set()
    count = 0
    for m in group["members"]:
        stem = Path(m["filename"]).stem
        pfx = _numeric_prefix(stem)
        if pfx:
            prefixes.add(pfx)
            count += 1
    if count >= 2 and len(prefixes) == 1:
        return next(iter(prefixes))
    return None


def _merge_groups_by_prefix(groups: list[dict]) -> list[dict]:
    """
    Post-pass on LLM output: merge groups that share a leading numeric prefix.

    The LLM frequently over-segments date-prefixed CAD project clusters,
    splitting e.g. `10.8.dwg + 10.8.bak` (sources/backup) from
    `10.8-binoy -1.pdf + 10.8-binoy -2.pdf + 10.8-binoy -3.pdf` (exports)
    even though both groups belong to project "10.8". This pass detects
    that and merges them into one group, preserving member roles.

    Primary-group selection (whose label/reason wins): prefer a group with
    a "source" role member; tie-break by member count.

    Returns a new list. Original groups are not mutated.
    """
    if len(groups) < 2:
        return list(groups)

    by_prefix: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        pfx = _group_prefix(g)
        if pfx:
            by_prefix[pfx].append(i)

    # primary_idx -> list of indices to fold into it
    merge_targets: dict[int, list[int]] = {}
    consumed: set[int] = set()
    for pfx, indices in by_prefix.items():
        if len(indices) < 2:
            continue

        def _score(i: int) -> tuple[int, int]:
            g = groups[i]
            has_source = any(m["role"] == "source" for m in g["members"])
            return (1 if has_source else 0, len(g["members"]))

        indices_sorted = sorted(indices, key=_score, reverse=True)
        primary = indices_sorted[0]
        merge_targets[primary] = indices_sorted[1:]
        consumed.update(indices_sorted[1:])

    if not merge_targets:
        return list(groups)

    out: list[dict] = []
    for i, g in enumerate(groups):
        if i in consumed:
            continue
        if i not in merge_targets:
            out.append(g)
            continue
        merged_members = list(g["members"])
        seen_fn = {m["filename"] for m in merged_members}
        extra_reasons: list[str] = []
        for j in merge_targets[i]:
            other = groups[j]
            for m in other["members"]:
                if m["filename"] in seen_fn:
                    continue
                merged_members.append(m)
                seen_fn.add(m["filename"])
            if other.get("reason"):
                extra_reasons.append(other["reason"])
        merged = {
            "label": g["label"],
            "reason": g.get("reason") or "",
            "members": merged_members,
            "_confidence": g.get("_confidence", LLM_CONFIDENCE),
        }
        if extra_reasons:
            base = merged["reason"] or ""
            joined = " | ".join([base] + extra_reasons).strip(" |")
            merged["reason"] = joined[:500]
        logger.info(
            "Relate: merged %d LLM groups sharing prefix into '%s' (%d members)",
            1 + len(merge_targets[i]), merged["label"], len(merged_members),
        )
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Cross-script / canonical-token pass (Hebrew ↔ English and synonyms)
# ---------------------------------------------------------------------------

# A bilingual LLM prompt asks the model to emit a canonical English token for
# each filename then cluster by matching tokens. This catches cases no purely
# lexical method can: `מידול.dwg` and `midul.3dm` both canonicalise to
# "modeling" and should cluster; `Plans.pdf` and `drawings.pdf` both hit
# "plan" and cluster. Strict JSON schema, same shape as the primary prompt.
CROSS_SCRIPT_SYSTEM_PROMPT = """\
You are a filename clustering assistant for a digital archivist.

Each filename you are shown is a SINGLETON — it is not part of any existing
group from a previous pass. Your job: identify singletons that describe the
SAME subject in different languages, scripts, or synonym words, and group
them together.

For each filename, mentally compute a canonical English "subject token"
(1–3 words, lowercase, snake_case) — what is this file ABOUT, independent
of language. Examples:
    'מידול.dwg'                     -> 'modeling'
    'midul.3dm'                     -> 'modeling'
    'Plans_rev3.pdf'                -> 'plans'
    'drawings_final.pdf'            -> 'plans'
    'Fonts.rar'                     -> 'fonts'
    'font_library.zip'              -> 'fonts'

Then group filenames whose canonical tokens MATCH. A group needs ≥2 members.
Do NOT invent filenames. Do NOT group by file type alone (two PDFs is not a
group). Group only when the subject MATCHES.

Return STRICT JSON: an array of groups, same schema as the primary pass.
[
  {
    "label": "short_snake_case_slug (<=40 chars)",
    "reason": "one sentence explaining the canonical token",
    "members": [
      {"filename": "exact_filename.ext", "role": "sibling"}
    ]
  }
]
If no cross-script / synonym clusters exist, return [].
"""

# Confidence for cross-script groups: between main-LLM (0.8) and backstop
# (0.3). Lower than main-LLM because the cluster is weaker evidence (single
# subject-token match), higher than backstop because it came from LLM
# reasoning not a regex.
CROSS_SCRIPT_CONFIDENCE = 0.6

# Budget guards for the cross-script pass
CROSS_SCRIPT_MIN_SINGLETONS = 5
CROSS_SCRIPT_MAX_SINGLETONS = 2000


def _call_llm_cross_script(
    client,
    files: list[File],
    model: Optional[str] = None,
) -> list[dict]:
    """One cross-script LLM call on a chunk of singletons."""
    if len(files) < 2:
        return []

    user_prompt_lines = [
        f"Singleton filenames ({len(files)}):",
    ]
    user_prompt_lines.extend(_format_file_line(f) for f in files)
    user_prompt_lines.append("")
    user_prompt_lines.append(
        "Group filenames whose canonical English subject tokens match. "
        "Return JSON array only."
    )
    user_prompt = "\n".join(user_prompt_lines)

    try:
        kwargs = {
            "system": CROSS_SCRIPT_SYSTEM_PROMPT,
            "temperature": 0.0,
            "seed": 42,
        }
        if model:
            kwargs["model"] = model
        raw = client.generate_json(user_prompt, **kwargs)
    except Exception as exc:
        logger.warning("Relate cross-script LLM call failed (model=%s): %s", model, exc)
        return []

    if isinstance(raw, dict):
        raw = raw.get("groups") or raw.get("result") or []
    if not isinstance(raw, list):
        logger.warning(
            "Cross-script LLM returned non-list (%s) — skipping",
            type(raw).__name__,
        )
        return []

    known_filenames = {f.filename for f in files}
    cleaned: list[dict] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        raw_members = g.get("members") or []
        if not isinstance(raw_members, list):
            continue
        valid: list[dict] = []
        seen: set[str] = set()
        for m in raw_members:
            if isinstance(m, str):
                fn, role = m, "sibling"
            elif isinstance(m, dict):
                fn = m.get("filename") or m.get("name") or ""
                role = (m.get("role") or "sibling").lower()
            else:
                continue
            if fn not in known_filenames or fn in seen:
                continue
            if role not in _ROLE_BY_STRING:
                role = "sibling"
            seen.add(fn)
            valid.append({"filename": fn, "role": role})
        if len(valid) < 2:
            continue
        label = _slugify(str(g.get("label") or "cross_script"))
        reason = (g.get("reason") or "").strip()[:500]
        cleaned.append({
            "label": label,
            "reason": reason,
            "members": valid,
            "_confidence": CROSS_SCRIPT_CONFIDENCE,
        })

    # Enforce at-most-one-group-per-file
    placed: set[str] = set()
    final: list[dict] = []
    for g in cleaned:
        filtered = [m for m in g["members"] if m["filename"] not in placed]
        if len(filtered) < 2:
            continue
        for m in filtered:
            placed.add(m["filename"])
        g["members"] = filtered
        final.append(g)
    return final


def _run_cross_script_pass(
    session: Session,
    session_id: str,
    client,
    placed_file_ids: set[int],
    model: Optional[str] = None,
) -> tuple[int, int]:
    """
    Run the cross-script LLM pass on every session file not already in a
    group. Persists new groups with scope='cross_script' and dir_path=None.

    Returns (groups_saved, members_added).
    """
    singletons = (
        session.query(File)
        .filter(
            File.session_id == session_id,
            File.status != FileStatus.ERROR,
            ~File.id.in_(placed_file_ids) if placed_file_ids else True,
        )
        .all()
    )
    if placed_file_ids:
        singletons = [f for f in singletons if f.id not in placed_file_ids]

    n = len(singletons)
    if n < CROSS_SCRIPT_MIN_SINGLETONS:
        logger.info(
            "Relate cross-script pass: skipping, only %d singletons (<%d)",
            n, CROSS_SCRIPT_MIN_SINGLETONS,
        )
        return (0, 0)
    if n > CROSS_SCRIPT_MAX_SINGLETONS:
        logger.info(
            "Relate cross-script pass: skipping, %d singletons (>%d) — too expensive",
            n, CROSS_SCRIPT_MAX_SINGLETONS,
        )
        return (0, 0)

    # Build filename→file_id map. Cross-scope: two singletons in different
    # directories may share the same basename. Keep the first — the LLM
    # sees basenames only, and the user intent (cluster by subject) doesn't
    # care which concrete inode gets linked.
    fn_to_id: dict[str, int] = {}
    for f in singletons:
        fn_to_id.setdefault(f.filename, f.id)

    all_groups: list[dict] = []
    for chunk in _chunk(singletons, MAX_FILES_PER_CALL):
        if len(chunk) < 2:
            continue
        all_groups.extend(_call_llm_cross_script(client, chunk, model=model))

    # Enforce at-most-one-group-per-file ACROSS chunks
    placed: set[str] = set()
    merged: list[dict] = []
    for g in all_groups:
        filtered = [m for m in g["members"] if m["filename"] not in placed]
        if len(filtered) < 2:
            continue
        for m in filtered:
            placed.add(m["filename"])
        g["members"] = filtered
        merged.append(g)

    _deduplicate_labels(merged)
    saved = _save_groups(session, merged, session_id, "cross_script", None, fn_to_id)
    members_added = sum(len(g["members"]) for g in merged)
    if saved:
        logger.info(
            "Relate cross-script pass: saved %d groups with %d members",
            saved, members_added,
        )
    return (saved, members_added)


# ---------------------------------------------------------------------------
# Singleton-to-folder linkage
# ---------------------------------------------------------------------------

_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]{4,}")


def _link_singletons_to_folder_groups(
    session: Session,
    session_id: str,
) -> int:
    """
    For each file still not in any RelationGroup, try to attach it as a
    SIBLING member of an existing LLM-quality group whose label matches
    either the singleton's numeric prefix (e.g. `108` in `108_project_files`
    → group `project_10_8`) or its first ≥4-char alpha token (e.g. `Fonts`
    in `Fonts.rar` → group `font_configurations`).

    Returns the number of singletons linked.
    """
    from datahoarder.db.models import RelationMember

    # Existing group labels with confidence ≥0.5 (skip weak backstop-only)
    groups = (
        session.query(RelationGroup)
        .filter(
            RelationGroup.session_id == session_id,
            RelationGroup.confidence >= 0.5,
        )
        .all()
    )
    if not groups:
        return 0

    label_by_group = {g.id: (g.label or "").lower() for g in groups}

    # Files already in any RelationMember for this session
    placed_file_ids: set[int] = {
        row[0] for row in session.query(RelationMember.file_id)
        .join(RelationGroup, RelationGroup.id == RelationMember.group_id)
        .filter(RelationGroup.session_id == session_id)
    }

    singletons = (
        session.query(File)
        .filter(
            File.session_id == session_id,
            File.status != FileStatus.ERROR,
            ~File.id.in_(placed_file_ids) if placed_file_ids else True,
        )
        .all()
    )
    if placed_file_ids:
        singletons = [f for f in singletons if f.id not in placed_file_ids]

    if not singletons:
        return 0

    linked = 0
    # Track group membership count so we don't over-attach to one group
    # (e.g. don't dump every .zip in the session into project_10_8)
    per_group_added: dict[int, int] = defaultdict(int)
    _MAX_LINKED_PER_GROUP = 4  # cap: avoids pathological over-attachment

    for f in singletons:
        stem = Path(f.filename).stem.lower()

        # Candidate tokens from the singleton
        num_prefix = _numeric_prefix(stem)  # "108" or "10.8" or None
        num_prefix_underscored = num_prefix.replace(".", "_") if num_prefix else None
        # Also consider "108" → "10_8" (a common convention for dates
        # without a separator: 108 == 10.8 == 10_8)
        implicit_date_variant = None
        if num_prefix and "." not in num_prefix and len(num_prefix) in (3, 4):
            # "108" → "10_8"; "1008" → "10_08"; etc.
            split = len(num_prefix) // 2 if len(num_prefix) == 4 else 2
            implicit_date_variant = f"{num_prefix[:split]}_{num_prefix[split:]}"

        alpha_tokens = [t.lower() for t in _ALPHA_TOKEN_RE.findall(stem)]

        best_group_id: Optional[int] = None
        best_reason: str = ""

        for g in groups:
            if per_group_added[g.id] >= _MAX_LINKED_PER_GROUP:
                continue
            lbl = label_by_group[g.id]
            matched = False
            # Match 1: numeric prefix appears as `_<prefix>_` or at the
            # start after `project_`.
            for candidate in filter(None, (num_prefix_underscored, implicit_date_variant)):
                if (
                    f"_{candidate}_" in f"_{lbl}_"
                    or lbl.startswith(f"project_{candidate}_")
                    or lbl == f"project_{candidate}"
                ):
                    matched = True
                    best_reason = f"numeric prefix match ({candidate})"
                    break
            # Match 2: first alpha token (≥4 chars) matches a token in label
            if not matched:
                for tok in alpha_tokens:
                    if tok in lbl.split("_"):
                        matched = True
                        best_reason = f"alpha token match ({tok})"
                        break
            if matched:
                best_group_id = g.id
                break

        if best_group_id is None:
            continue

        session.add(RelationMember(
            group_id=best_group_id,
            file_id=f.id,
            role=_ROLE_BY_STRING["sibling"],
        ))
        per_group_added[best_group_id] += 1
        linked += 1
        logger.info(
            "Relate: linked singleton %r to group %r (%s)",
            f.filename, label_by_group[best_group_id], best_reason,
        )

    if linked:
        session.commit()
    return linked


# ---------------------------------------------------------------------------
# Backstop (regex prefix clustering)
# ---------------------------------------------------------------------------


def _prefix_cluster_backstop(files: list[File]) -> list[dict]:
    """
    Regex-only fallback: group files sharing a leading numeric prefix.

    Example: `10.8.dwg`, `10.8.bak`, `10.8-binoy -1.pdf`, `10.8 - Standard.zip`
    all share prefix "10.8" → one group.

    Returns groups in the same dict shape as the LLM path, with role=sibling
    for everything (we don't try to guess source/export from filenames alone
    in the backstop).
    """
    buckets: dict[str, list[File]] = defaultdict(list)
    for f in files:
        stem = Path(f.filename).stem
        pfx = _numeric_prefix(stem)
        if pfx:
            buckets[pfx].append(f)

    out: list[dict] = []
    for pfx, members in buckets.items():
        if len(members) < 2:
            continue
        out.append({
            "label": _slugify(f"prefix_{pfx}"),
            "reason": f"Files sharing numeric prefix '{pfx}' (regex backstop).",
            "members": [
                {"filename": m.filename, "role": "sibling"} for m in members
            ],
            "_confidence": BACKSTOP_CONFIDENCE,
        })
    return out


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

def _format_file_line(f: File) -> str:
    ext = (f.extension or "").lower()
    if f.size_bytes is not None:
        kb = f.size_bytes / 1024
        size_str = f"{kb:.0f}KB" if kb < 1024 else f"{kb/1024:.1f}MB"
    else:
        size_str = "?"
    return f"  {f.filename}  [{ext or '(no ext)'}, {size_str}]"


def _chunk(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _call_llm_for_group(
    client,
    dir_label: str,
    files: list[File],
    model: Optional[str] = None,
) -> list[dict]:
    """
    One LLM call. Returns parsed groups (already validated) or [] on failure.
    Each group dict has keys: label, reason, members:[{filename, role}].

    When `model` is provided, it is passed explicitly to `generate_json` so
    the Relate step doesn't inherit the last-initialised model (which may be
    a vision model unsuited for structured-JSON reasoning).
    """
    if len(files) < 2:
        return []

    user_prompt_lines = [f"Directory: {dir_label}", f"Files ({len(files)}):"]
    user_prompt_lines.extend(_format_file_line(f) for f in files)
    user_prompt_lines.append("")
    user_prompt_lines.append(
        "Group files that are conceptually ONE thing. Return JSON array only."
    )
    user_prompt = "\n".join(user_prompt_lines)

    try:
        # generate_json returns whatever the LLM emitted (array or dict).
        kwargs = {
            "system": RELATE_SYSTEM_PROMPT,
            "temperature": 0.0,
            "seed": 42,
        }
        if model:
            kwargs["model"] = model
        raw = client.generate_json(user_prompt, **kwargs)
    except Exception as exc:
        logger.warning("Relate LLM call failed for %s (model=%s): %s",
                       dir_label, model, exc)
        return []

    # Expected: list[dict]. Accept {"groups": [...]} wrapping too.
    if isinstance(raw, dict):
        # Some clients wrap a fallback raw_response when parsing fails —
        # log it so the caller can see what the model actually returned.
        if "raw_response" in raw and not (raw.get("groups") or raw.get("result")):
            logger.warning(
                "Relate LLM for %s returned unparseable output (model=%s): %s",
                dir_label, model, str(raw.get("raw_response"))[:300],
            )
        raw = raw.get("groups") or raw.get("result") or []
    if not isinstance(raw, list):
        logger.warning(
            "Relate LLM for %s returned non-list (%s) — will backstop (model=%s)",
            dir_label, type(raw).__name__, model,
        )
        return []
    if not raw:
        logger.info("Relate LLM for %s returned empty list (model=%s)", dir_label, model)

    # Validate + normalize
    known_filenames = {f.filename: f for f in files}
    cleaned: list[dict] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        raw_members = g.get("members") or []
        if not isinstance(raw_members, list):
            continue

        valid_members: list[dict] = []
        for m in raw_members:
            if isinstance(m, str):
                # LLM sometimes omits role and just lists filenames
                filename, role = m, "sibling"
            elif isinstance(m, dict):
                filename = m.get("filename") or m.get("name") or ""
                role = (m.get("role") or "sibling").lower()
            else:
                continue
            # Reject hallucinated names — must match input exactly
            if filename not in known_filenames:
                continue
            if role not in _ROLE_BY_STRING:
                role = "sibling"
            valid_members.append({"filename": filename, "role": role})

        # Dedupe by filename within a group
        seen_fn = set()
        uniq_members = []
        for m in valid_members:
            if m["filename"] in seen_fn:
                continue
            seen_fn.add(m["filename"])
            uniq_members.append(m)
        if len(uniq_members) < 2:
            continue

        label = _slugify(str(g.get("label") or "group"))
        reason = (g.get("reason") or "").strip()[:500]
        cleaned.append({
            "label": label,
            "reason": reason,
            "members": uniq_members,
            "_confidence": LLM_CONFIDENCE,
        })

    # Enforce at-most-one-group-per-file across THIS response (LLM sometimes
    # cross-lists). First group wins.
    seen_files: set[str] = set()
    final: list[dict] = []
    for g in cleaned:
        filtered = [m for m in g["members"] if m["filename"] not in seen_files]
        if len(filtered) < 2:
            continue
        for m in filtered:
            seen_files.add(m["filename"])
        g["members"] = filtered
        final.append(g)
    return final


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _wipe_existing_groups(session: Session, session_id: str) -> int:
    """Delete all existing RelationGroups for this session. Idempotent re-run."""
    existing = session.query(RelationGroup).filter(
        RelationGroup.session_id == session_id,
    ).all()
    n = len(existing)
    for g in existing:
        session.delete(g)
    session.commit()
    return n


def _save_groups(
    session: Session,
    groups: list[dict],
    session_id: str,
    scope: str,
    dir_path: Optional[str],
    filename_to_file_id: dict[str, int],
) -> int:
    """Persist groups. Returns number of groups saved."""
    saved = 0
    for g in groups:
        member_file_ids = []
        for m in g["members"]:
            fid = filename_to_file_id.get(m["filename"])
            if fid is None:
                continue
            member_file_ids.append((fid, m["role"]))
        if len(member_file_ids) < 2:
            continue

        grp = RelationGroup(
            session_id=session_id,
            label=g["label"],
            reason=g.get("reason"),
            confidence=float(g.get("_confidence", LLM_CONFIDENCE)),
            scope=scope,
            dir_path=dir_path,
        )
        session.add(grp)
        session.flush()  # need grp.id

        for fid, role in member_file_ids:
            session.add(RelationMember(
                group_id=grp.id,
                file_id=fid,
                role=_ROLE_BY_STRING[role],
            ))
        saved += 1
    session.commit()
    return saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def relate(
    session_id: str,
    scope: str = "per_directory",
    client=None,
    model: Optional[str] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """
    Run the relate step on a session.

    Args:
        session_id: session to process
        scope: "per_directory" (one LLM call per dir) or "cross_directory"
               (one LLM call for the whole tree).
        client: optional injected LLM client. If None, uses ai.router.get_client().
        model:  optional explicit model tag (e.g. "gemma3:12b") passed to every
                LLM call. Overrides the client's default text_model. Use this
                when the caller needs a reasoning-capable model regardless of
                which model was last initialised via init_ai().
        progress_cb: optional callback invoked with {"dir": str, "done": N, "total": M}
                     after each directory is processed.

    Returns:
        Summary dict: {"directories": int, "groups": int, "members": int,
                       "llm_groups": int, "backstop_groups": int}
    """
    if scope not in ("per_directory", "cross_directory"):
        raise ValueError(f"Invalid scope: {scope!r}")

    if client is None:
        from datahoarder.ai.router import get_client
        try:
            client = get_client()
        except RuntimeError:
            client = None  # LLM unavailable; backstop-only mode

    engine = get_engine()
    summary = {
        "directories": 0,
        "groups": 0,
        "members": 0,
        "llm_groups": 0,
        "backstop_groups": 0,
    }

    with Session(engine) as session:
        # Wipe previous groups for this session so re-runs don't accumulate
        wiped = _wipe_existing_groups(session, session_id)
        if wiped:
            logger.info("Wiped %d stale RelationGroups for session %s", wiped, session_id)

        # Pull all scanned-or-later files (we work on filenames, not content,
        # so PENDING / ENRICHED / ANALYZED / PROPOSED / SKIPPED all qualify;
        # APPLIED files already moved on disk but the DB still holds their
        # identity — we include them so re-runs don't lose groupings).
        files = (
            session.query(File)
            .filter(File.session_id == session_id)
            .filter(File.status != FileStatus.ERROR)
            .all()
        )
        if not files:
            return summary

        # Bucket by directory
        dir_buckets: dict[str, list[File]] = defaultdict(list)
        for f in files:
            dir_buckets[str(Path(f.path).parent)].append(f)

        # Determine the call pattern based on scope
        if scope == "cross_directory":
            call_units = [("<whole tree>", files, None)]
        else:
            call_units = [
                (d, fs, d) for d, fs in dir_buckets.items() if len(fs) >= 2
            ]

        summary["directories"] = len(call_units)
        if not call_units:
            return summary

        for i, (dir_label, dir_files, dir_path) in enumerate(call_units):
            # Map filename (basename) → file_id for this unit.
            # For per_directory scope, filenames are unique within the dir.
            # For cross_directory, collisions across dirs are possible — the
            # LLM sees the basenames only, so we prefer the first occurrence
            # and let the backstop handle the rest.
            fn_to_id: dict[str, int] = {}
            for f in dir_files:
                fn_to_id.setdefault(f.filename, f.id)

            llm_groups: list[dict] = []
            if client is not None:
                # Chunk very large dirs into multiple LLM calls
                for chunk in _chunk(dir_files, MAX_FILES_PER_CALL):
                    if len(chunk) < 2:
                        continue
                    llm_groups.extend(
                        _call_llm_for_group(client, dir_label, chunk, model=model)
                    )

            # Post-pass: merge groups that share a leading numeric prefix
            # (LLM tends to over-segment date-stamped CAD project clusters).
            llm_groups = _merge_groups_by_prefix(llm_groups)

            # Deduplicate labels across all groups from this unit
            _deduplicate_labels(llm_groups)
            summary["llm_groups"] += len(llm_groups)
            saved_llm = _save_groups(
                session, llm_groups, session_id, scope, dir_path, fn_to_id,
            )

            # Backstop runs on files NOT already placed in an LLM group
            placed = {m["filename"] for g in llm_groups for m in g["members"]}
            leftover = [f for f in dir_files if f.filename not in placed]
            backstop_groups = _prefix_cluster_backstop(leftover) if leftover else []
            _deduplicate_labels(backstop_groups)
            summary["backstop_groups"] += len(backstop_groups)
            saved_backstop = _save_groups(
                session, backstop_groups, session_id, scope, dir_path, fn_to_id,
            )

            summary["groups"] += saved_llm + saved_backstop
            summary["members"] += sum(
                len(g["members"]) for g in llm_groups + backstop_groups
            )

            if progress_cb:
                progress_cb({
                    "dir": dir_label,
                    "done": i + 1,
                    "total": len(call_units),
                    **summary,
                })

        # -------------------------------------------------------------
        # Session-wide post-passes: cross-script clustering, then
        # singleton-to-folder linkage.
        # -------------------------------------------------------------

        # Build the set of file_ids already in any RelationGroup for this
        # session (needed by the cross-script pass to know which files are
        # still singletons). Pull fresh from DB — the per-directory loop
        # above persisted groups, so file_id membership is now authoritative
        # on disk, not in any in-memory `placed` set.
        from datahoarder.db.models import RelationMember as _RelMember
        placed_file_ids: set[int] = {
            row[0] for row in session.query(_RelMember.file_id)
            .join(RelationGroup, RelationGroup.id == _RelMember.group_id)
            .filter(RelationGroup.session_id == session_id)
        }

        if client is not None:
            try:
                cs_groups, cs_members = _run_cross_script_pass(
                    session, session_id, client, placed_file_ids, model=model,
                )
                if cs_groups:
                    summary["groups"] += cs_groups
                    summary["members"] += cs_members
                    summary["cross_script_groups"] = cs_groups
            except Exception as exc:
                logger.warning("Cross-script pass failed: %s", exc)

        try:
            linked = _link_singletons_to_folder_groups(session, session_id)
            if linked:
                summary["members"] += linked
                summary["singleton_links"] = linked
        except Exception as exc:
            logger.warning("Singleton-linkage pass failed: %s", exc)

    return summary

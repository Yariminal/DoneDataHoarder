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

# Cap on filenames sent to the LLM in one call to avoid prompt overruns.
# Typical architect/photographer dumps stay well under this; large mixed
# dirs get chunked into multiple calls (see _chunk).
MAX_FILES_PER_CALL = 200

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

    return summary

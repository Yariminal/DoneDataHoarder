"""Proposal-only quality audit on SQLite backups of earlier validation runs.

Never changes source databases or corpus files. The private audit directory
contains copied databases and candidate paths/descriptions for human review.
No AI inference or file-operation executor is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import init_db
from donedatahoarder.proposals.namer.core import generate_proposals
from donedatahoarder.proposals.organizer.core import _emit_standalone_moves
from donedatahoarder.web.api.pipeline import get_organize_coverage


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _latest_runs(parent: Path) -> list[tuple[str, Path]]:
    by_corpus: dict[str, Path] = {}
    for run in sorted(parent.glob("DDH-validation-*")):
        manifest = run / "run.json"
        db = run / "state" / "corpus.sqlite"
        if not (manifest.is_file() and db.is_file()):
            continue
        try:
            corpus = json.loads(manifest.read_text(encoding="utf-8"))["corpus"]
        except (OSError, ValueError, KeyError):
            continue
        by_corpus[corpus] = run
    return sorted(by_corpus.items())


def _backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def _needs_review_reasons(engine, sid: str, root_path: str) -> dict[str, int]:
    """Explain the coverage bucket, with mutually exclusive safety-first reasons."""
    from donedatahoarder.core.dependency_protection import ProtectionIndex
    from donedatahoarder.proposals.organizer.core import (
        _inside_project, _loose_source, _project_roots,
    )
    from donedatahoarder.proposals.organizer.tree import _file_category

    root = Path(root_path).resolve()
    with Session(engine) as db:
        records = db.query(File).filter(File.session_id == sid).all()
        protection = ProtectionIndex(root)
        projects = _project_roots(root, records, protection)
        categories = defaultdict(set)
        for record in records:
            categories[Path(record.path).parent].add(
                _file_category(record.mime_type, record.extension)
            )
        move_ids = {
            fid for fid, destination in
            db.query(Proposal.file_id, Proposal.proposed_value)
            .join(File, File.id == Proposal.file_id)
            .filter(File.session_id == sid, Proposal.proposal_type == ProposalType.MOVE,
                    Proposal.status.in_([ProposalStatus.PENDING, ProposalStatus.MODIFIED,
                                         ProposalStatus.APPROVED, ProposalStatus.APPLIED]))
            if destination and Path(destination).is_relative_to(root / "Independent_Files")
        }
        reasons: Counter[str] = Counter()
        for record in records:
            source = Path(record.path)
            if _inside_project(source, projects) or record.id in move_ids:
                continue
            if source.is_relative_to(root / "Independent_Files") or (
                source.parent != root and not _loose_source(
                    record, root, categories, projects
                )
            ):
                continue
            if protection.assess(source).protected:
                reasons["protected_linked_resource"] += 1
            elif record.status == FileStatus.SKIPPED or record.analysis_outcome in {
                "sampled", "unsupported", "skipped",
            }:
                reasons["unsupported_or_skipped"] += 1
            elif record.analysis_outcome != "content_verified":
                reasons["unverified_analysis"] += 1
            else:
                reasons["other_content_verified"] += 1
    return dict(reasons)


def audit(parent: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    runs = _latest_runs(parent)
    if len(runs) != 6:
        raise RuntimeError(f"Expected six prior corpus runs, found {len(runs)}")
    summaries = []
    examples = []
    for number, (corpus, run) in enumerate(runs, start=1):
        source_db = run / "state" / "corpus.sqlite"
        source_before = _sha256(source_db)
        work = output / f"corpus_{number}"
        work.mkdir()
        copy_db = work / "audit.sqlite"
        _backup(source_db, copy_db)
        os.environ["DDH_DATA_DIR"] = str(work / "state")
        engine = init_db(copy_db)
        with Session(engine) as db:
            owners = db.query(UserSession).all()
            if len(owners) != 1:
                raise RuntimeError(f"Expected one session in {run.name}, found {len(owners)}")
            owner = owners[0]
            sid = owner.id
            root_path = owner.root_path
            original_language = owner.preferred_language
            # A translation request would call a model. Keep this an offline
            # proposal-only replay and record the audit override explicitly.
            owner.preferred_language = "leave_as_is"
            total = db.query(File).filter(File.session_id == sid).count()
            prior_status = dict(Counter(
                status.value for (status,) in db.query(File.status)
                .filter(File.session_id == sid)
            ))
            removed = db.query(Proposal).filter(
                Proposal.file_id.in_(db.query(File.id).filter(File.session_id == sid)),
                Proposal.status == ProposalStatus.PENDING,
                Proposal.proposal_type.in_([
                    ProposalType.RENAME, ProposalType.MOVE, ProposalType.RENAME_FOLDER,
                ]),
            ).delete(synchronize_session=False)
            db.query(File).filter(
                File.session_id == sid, File.status == FileStatus.PROPOSED,
            ).update({File.status: FileStatus.ANALYZED}, synchronize_session=False)
            db.commit()

        naming = generate_proposals(session_id=sid)
        standalone_moves = _emit_standalone_moves(sid, str(Path(root_path)))
        coverage = get_organize_coverage(sid)
        needs_review_reasons = _needs_review_reasons(engine, sid, root_path)
        if sum(needs_review_reasons.values()) != coverage["needs_review"]:
            raise RuntimeError("Needs-review reason buckets do not match coverage")
        with Session(engine) as db:
            rename_rows = (
                db.query(Proposal, File).join(File, Proposal.file_id == File.id)
                .filter(File.session_id == sid, Proposal.proposal_type == ProposalType.RENAME,
                        Proposal.status == ProposalStatus.PENDING)
                .order_by(Proposal.id).all()
            )
            move_rows = db.query(Proposal).join(File, Proposal.file_id == File.id).filter(
                File.session_id == sid, Proposal.proposal_type == ProposalType.MOVE,
                Proposal.status == ProposalStatus.PENDING,
            ).count()
            verified = db.query(File).filter(File.session_id == sid,
                File.analysis_outcome == "content_verified").count()
            sampled = db.query(File).filter(File.session_id == sid,
                File.analysis_outcome == "sampled").count()
            for proposal, file_rec in rename_rows[:12]:
                examples.append({
                    "corpus_slot": number,
                    "file_id": file_rec.id,
                    "source_path": file_rec.path,
                    "proposed_path": proposal.proposed_value,
                    "own_description": file_rec.ai_description,
                    "analysis_outcome": file_rec.analysis_outcome,
                    "evidence_source": file_rec.analysis_evidence_source,
                    "content_chars": file_rec.analysis_content_chars,
                    "reasoning": proposal.reasoning,
                })
        source_after = _sha256(source_db)
        if source_after != source_before:
            raise RuntimeError(f"Source database changed during audit: {run.name}")
        summaries.append({
            "corpus_slot": number, "corpus_private": corpus,
            "source_run": str(run), "copied_database": str(copy_db),
            "source_db_sha256_before": source_before,
            "source_db_sha256_after": source_after,
            "indexed_files": total, "verified_analyses": verified,
            "sampled_analyses": sampled,
            "prior_status": prior_status,
            "pending_file_ops_removed_from_copy": removed,
            "language_audit_override_from": original_language,
            "naming_summary": naming,
            "pending_renames_after_replay": len(rename_rows),
            "pending_moves_after_replay": move_rows,
            "standalone_moves_added": standalone_moves,
            "organization_coverage": coverage,
            "needs_review_reasons": needs_review_reasons,
        })
        print(f"slot {number}: indexed={total}, preserved={coverage['project_preserved']}, "
              f"independent proposed={coverage['independent_proposed']}, "
              f"pending renames={len(rename_rows)}, suppressions="
              f"{naming.get('rename_suppression_reasons', {})}", flush=True)
    result = {"schema": 1, "kind": "private proposal-only audit",
              "runs": summaries, "candidate_examples": examples[:12]}
    (output / "audit.json").write_text(json.dumps(result, ensure_ascii=False,
                                                  indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-root", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="DDH-naming-audit-", dir=args.output_parent))
    # mkdtemp creates the directory; audit creates it to assert a clean target.
    output.rmdir()
    audit(args.validation_root, output)
    print(f"Private audit: {output / 'audit.json'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

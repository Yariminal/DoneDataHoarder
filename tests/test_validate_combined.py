"""Combined harness keeps all source ZIPs and every namespace distinct."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy.orm import Session

from scripts import validate_combined
from scripts import validate_corpus
from donedatahoarder.db.models import (
    File, FileStatus, Proposal, ProposalStatus, ProposalType, UserSession,
)
from donedatahoarder.db.session import init_db


def test_relate_heartbeat_exposes_numeric_progress_without_private_paths(
        tmp_path, monkeypatch, capsys):
    calls = []

    def fake_relate(*, session_id, scope, model, progress_cb):
        calls.append((session_id, scope, model))
        progress_cb({"phase": "grouping", "dir": "/private/project/secret-file.shx",
                     "done": 0, "directory_index": 1, "total": None,
                     "chunk_done": 1, "chunk_total": 2,
                     "groups": 0, "llm_groups": 0, "backstop_groups": 0})
        progress_cb({"phase": "directory_complete", "dir": "/private/project",
                     "done": 1, "total": None, "groups": 2,
                     "llm_groups": 1, "backstop_groups": 1})
        return {"directories": 1, "groups": 2, "members": 3,
                "llm_groups": 1, "backstop_groups": 1}

    monkeypatch.setattr("donedatahoarder.core.relate.relate", fake_relate)
    summary = validate_combined._relate_with_heartbeat(tmp_path, "fixture-session", "fixture-model")
    sidecar = (tmp_path / "reports" / "relate-progress.json").read_text(encoding="utf-8")
    assert summary["groups"] == 2
    assert calls == [("fixture-session", "per_directory", "fixture-model")]
    assert json.loads(sidecar)["phase"] == "completed"
    assert json.loads(sidecar)["directories_done"] == 1
    assert json.loads(sidecar)["chunk_done"] is None
    assert json.loads(sidecar)["chunk_total"] is None
    assert json.loads(sidecar)["directory_index"] is None
    assert "private" not in sidecar and "secret-file" not in sidecar
    assert "secret-file" not in capsys.readouterr().out


@pytest.mark.parametrize("behavior", [
    "success", "crlf", "source_mismatch", "mutate_retained", "leave_error",
    "reskipped", "preexisting_proposal"])
def test_guarded_combined_continuation_preserves_original_success_and_report(
        tmp_path, monkeypatch, capsys, behavior):
    corpus = tmp_path / "corpus"
    root = corpus / "DDH-combined-fixture"
    data, state, reports = (root / name for name in ("data", "state", "reports"))
    for path in (data, state, reports):
        path.mkdir(parents=True, exist_ok=True)
    contents = {"done.txt": "already analyzed", "failed.txt": "retry provider",
                "reviewed.txt": "newly supported"}
    source = corpus / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
            (data / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "isolate", lambda _root, _state: None)
    engine = init_db(state / "corpus.sqlite")
    with Session(engine) as db:
        owner = UserSession(root_path=str(data), name="combined-fixture",
                            backend="ollama", model="gemma4:26b")
        db.add(owner)
        db.flush()
        rows = []
        for name, status, outcome, reason in (
                ("done.txt", FileStatus.ANALYZED, "content_verified", None),
                ("failed.txt", FileStatus.ERROR, "failed", "provider_invalid_response"),
                ("reviewed.txt", FileStatus.SKIPPED, "skipped", "unsupported_type")):
            row = File(session_id=owner.id, path=str(data / name), filename=name,
                       size_bytes=len(contents[name]), status=status,
                       analysis_outcome=outcome, analysis_reason=reason,
                       analysis_model_digest="fixture-digest")
            db.add(row)
            rows.append(row)
        db.commit()
        sid = owner.id
        ids = {row.filename: row.id for row in rows}
        retained_hash = validate_combined._file_evidence_hash(rows[0])
        if behavior == "preexisting_proposal":
            db.add(Proposal(file_id=ids["done.txt"], proposal_type=ProposalType.RENAME,
                            current_value=str(data / "done.txt"),
                            proposed_value=str(data / "renamed.txt"),
                            status=ProposalStatus.PENDING, confidence=0.9))
            db.commit()
    validate_corpus.write_json(root / "run.json", {
        "schema": 1, "run_dir": str(root.resolve()), "files": 3,
        "logical_bytes": sum(len(s) for s in contents.values()),
        "archives": [{"namespace": "Corpus", "source": str(source),
                      "source_sha256": validate_corpus.digest(source)}],
    })
    validate_corpus.write_json(reports / "baseline.json", [
        {"archive": "Corpus", **item} for item in validate_corpus.current_manifest(data)])
    validate_corpus.write_json(reports / "directories.json",
                               validate_corpus.directory_manifest(data))
    validate_corpus.write_json(reports / "indexed-baseline.json", sorted(contents))
    validate_corpus.write_json(reports / "indexed-records.json", sorted(
        ({"id": fid, "path": name} for name, fid in ids.items()), key=lambda row: row["id"]))
    original = {"run_dir": str(root), "session_id": sid, "mode": "full",
                "model": "gemma4:26b", "workers": 1,
                "steps": {name: {} for name in (
                    "preflight_coverage", "scan_coverage", "enrich_coverage",
                    "dedup_exact", "dedup_perceptual", "dedup_text")}}
    original["steps"]["analyze"] = {"result": {"analyzed": 1, "errors": 1}}
    validate_corpus.write_json(reports / "pipeline.json", original)
    original_source = b"fixture\noriginal harness\n"
    fingerprint_bytes = (original_source.replace(b"\n", b"\r\n")
                         if behavior == "crlf" else original_source)
    if behavior == "source_mismatch":
        fingerprint_bytes = b"unrelated source"
    validate_corpus.write_json(reports / "astra-source-fingerprint-start.json", {
        "commit": "a" * 40,
        "source_sha256": {"scripts/validate_combined.py":
                          hashlib.sha256(fingerprint_bytes).hexdigest()},
    })
    original_bytes = (reports / "pipeline.json").read_bytes()
    monkeypatch.setattr(validate_combined.subprocess, "run", lambda cmd, **kw:
                        SimpleNamespace(stdout=("b" * 40 + "\n" if "rev-parse" in cmd
                                                else original_source if "show" in cmd else "")))
    from donedatahoarder.ai.ollama_client import OllamaClient
    monkeypatch.setattr(OllamaClient, "model_digest", lambda self, model: "fixture-digest")
    monkeypatch.setattr(OllamaClient, "list_models", lambda self: ["gemma4:26b"])
    from donedatahoarder.ai import provider
    monkeypatch.setattr(provider, "init_ai", lambda **kw: None)
    monkeypatch.setattr(validate_combined, "require_local_provider", lambda model: None)
    from donedatahoarder.analyzers import pipeline as analyzer_pipeline

    def fake_analyze(**kwargs):
        assert kwargs["retry_errors"] is True
        with Session(engine) as db:
            for fid in ((ids["reviewed.txt"],) if behavior == "leave_error"
                        else (ids["failed.txt"], ids["reviewed.txt"])):
                row = db.get(File, fid)
                row.status = (FileStatus.SKIPPED if behavior == "reskipped"
                              and fid == ids["reviewed.txt"] else FileStatus.ANALYZED)
                row.analysis_outcome = ("skipped" if row.status == FileStatus.SKIPPED
                                        else "content_verified")
                row.analysis_reason = None
            if behavior == "mutate_retained":
                db.get(File, ids["done.txt"]).ai_description = "unexpected mutation"
            db.commit()
        return {"analyzed": 1 if behavior in {"leave_error", "reskipped"} else 2,
                "cached": 0, "sampled": 0, "skipped": 1 if behavior == "reskipped" else 0,
                "errors": 1 if behavior == "leave_error" else 0}

    monkeypatch.setattr(analyzer_pipeline, "analyze", fake_analyze)
    from donedatahoarder.core import dedup, relate
    downstream_calls = []
    monkeypatch.setattr(dedup, "find_semantic_duplicates", lambda **kw:
                        downstream_calls.append("semantic") or {})
    monkeypatch.setattr(dedup, "generate_dedup_proposals", lambda **kw: {})
    monkeypatch.setattr(relate, "relate", lambda **kw: {
        "directories": 0, "groups": 0, "members": 0,
        "llm_groups": 0, "backstop_groups": 0,
    })
    from donedatahoarder.proposals import namer, organizer
    monkeypatch.setattr(namer, "generate_proposals", lambda **kw: {})
    monkeypatch.setattr(organizer, "generate_reorg_proposals", lambda **kw: None)
    from donedatahoarder import executor
    monkeypatch.setattr(executor, "execute", lambda **kw: {"applied": 0, "failed": 0})
    args = argparse.Namespace(run_dir=str(root), original_commit="a" * 40,
                              recovery_commit="b" * 40, reviewed_skipped_ids=[ids["reviewed.txt"]],
                              model="gemma4:26b", ollama_host="http://127.0.0.1:11434")
    if behavior in {"success", "crlf"}:
        validate_combined.continue_analysis(args)
    elif behavior == "source_mismatch":
        with pytest.raises(ValueError, match="recorded source bytes differ"):
            validate_combined.continue_analysis(args)
        assert not (reports / "continuation-1.json").exists()
        assert not (reports / "continuation-before.sqlite").exists()
        assert (reports / "pipeline.json").read_bytes() == original_bytes
        return
    elif behavior == "preexisting_proposal":
        with pytest.raises(ValueError, match="already has proposals"):
            validate_combined.continue_analysis(args)
        assert not (reports / "continuation-1.json").exists()
        assert (reports / "continuation-before.sqlite").exists()
        assert (reports / "pipeline.json").read_bytes() == original_bytes
        return
    else:
        with pytest.raises(RuntimeError):
            validate_combined.continue_analysis(args)
    capsys.readouterr()
    result = validate_corpus.read_json(reports / "continuation-1.json")
    assert result["status"] == ("complete" if behavior in {"success", "crlf"} else "failed")
    if behavior not in {"success", "crlf"}:
        assert downstream_calls == []
        assert (reports / "pipeline.json").read_bytes() == original_bytes
        assert (reports / "continuation-before.sqlite").exists()
        return
    assert result["coverage"] == {"fresh": 3, "cached": 0, "sampled": 0,
                                  "skipped": 0, "errors": 0, "unprocessed": 0,
                                  "metadata_only_unanalyzed": 0, "indexed": 3}
    assert result["selected_ids"] == sorted([ids["failed.txt"], ids["reviewed.txt"]])
    assert result["original_source_match_counts"] == ({
        "raw_git_blob": 0, "crlf_checkout": 1} if behavior == "crlf" else {
        "raw_git_blob": 1, "crlf_checkout": 0})
    assert result["retained_evidence_unchanged"] is True
    assert (reports / "pipeline.json").read_bytes() == original_bytes
    assert (reports / "continuation-before.sqlite").exists()
    with Session(engine) as db:
        assert validate_combined._file_evidence_hash(db.get(File, ids["done.txt"])) == retained_hash
    assert validate_combined._pipeline_report(root, "continuation-1.json")[1]["status"] == "complete"
    validate_combined.audit(argparse.Namespace(
        run_dir=str(root), organized=False, pipeline_report="continuation-1.json"))
    assert validate_corpus.read_json(reports / "audit-restored.json")["pass"] is True
    with pytest.raises(ValueError, match="unsupported pipeline report basename"):
        validate_combined._pipeline_report(root, "../pipeline.json")
    with pytest.raises(ValueError, match="already attempted"):
        validate_combined.continue_analysis(args)
    (reports / "pipeline.json").write_bytes(original_bytes + b" ")
    with pytest.raises(ValueError, match="provenance differs"):
        validate_combined._pipeline_report(root, "continuation-1.json")


def test_prepare_six_namespaced_archives_and_audit(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    sources = []
    for i in range(6):
        name = f"archive-{i}.zip"
        path = corpus / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("same-name.txt", f"content-{i}")
        if i < 5:
            sources.append((f"Part{i}", name))
    monkeypatch.setattr(validate_corpus, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "STANDARD_ARCHIVES", tuple(sources))
    validate_combined.prepare(argparse.Namespace())
    info = json.loads(capsys.readouterr().out)
    assert info["files"] == 6
    run_dir = info["run_dir"]
    for i in range(6):
        namespace = f"Part{i}" if i < 5 else "Corpus"
        assert (Path(run_dir) / "data" / namespace / "same-name.txt").read_text() == f"content-{i}"
    validate_combined.audit(argparse.Namespace(run_dir=run_dir, organized=False))
    result = json.loads(capsys.readouterr().out)
    assert result["pass"] is True
    assert all(result["source_zip_unchanged"].values())


def test_combined_prepare_rejects_unsafe_zip_before_creating_run(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    sources = []
    for i in range(5):
        name = f"safe-{i}.zip"
        with zipfile.ZipFile(corpus / name, "w") as archive:
            archive.writestr("safe.txt", "safe")
        sources.append((f"Part{i}", name))
    path = corpus / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    monkeypatch.setattr(validate_corpus, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "STANDARD_ARCHIVES", tuple(sources))
    with pytest.raises(ValueError, match="unsafe ZIP path"):
        validate_combined.prepare(argparse.Namespace(corpus_zip=path.name))
    assert not list(corpus.glob("DDH-combined-*"))


def test_prepare_requires_explicit_additional_zip_when_discovery_is_ambiguous(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    sources = []
    for i in range(5):
        name = f"standard-{i}.zip"
        with zipfile.ZipFile(corpus / name, "w") as archive:
            archive.writestr("a.txt", "a")
        sources.append((f"Part{i}", name))
    for name in ("extra-one.zip", "extra-two.zip"):
        with zipfile.ZipFile(corpus / name, "w") as archive:
            archive.writestr("b.txt", "b")
    monkeypatch.setattr(validate_corpus, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)
    monkeypatch.setattr(validate_combined, "STANDARD_ARCHIVES", tuple(sources))
    with pytest.raises(ValueError, match="specify exactly one with --corpus-zip"):
        validate_combined.prepare(argparse.Namespace())
    assert not list(corpus.glob("DDH-combined-*"))
    selected = validate_combined.selected_archives("extra-two.zip")
    assert len(selected) == 6
    assert selected[-1] == ("Corpus", corpus / "extra-two.zip")


def test_pipeline_coverage_gates_scan_enrich_and_final_analysis(tmp_path):
    root = tmp_path / "DDH-combined-fixture"
    data = root / "data"
    data.mkdir(parents=True)
    path = data / "notes.txt"
    path.write_text("fixture", encoding="utf-8")
    (data / "excluded.tmp").write_text("not indexed", encoding="utf-8")
    validate_corpus.write_json(root / "run.json", {"files": 2})
    with pytest.raises(RuntimeError, match="could not read the whole"):
        validate_combined._preflight_coverage(
            {"size_estimate_complete": False}, {"files": 2, "logical_bytes": 18})
    with pytest.raises(RuntimeError, match="differ from extraction baseline"):
        validate_combined._preflight_coverage(
            {"size_estimate_complete": True, "full_collection_files": 1,
             "logical_bytes": 18}, {"files": 2, "logical_bytes": 18})
    engine = init_db(tmp_path / "index.sqlite")
    with Session(engine) as db:
        owner = UserSession(root_path=str(data))
        db.add(owner)
        db.commit()
        sid = owner.id

    with pytest.raises(RuntimeError, match="scan had 1 errors"):
        validate_combined._scan_coverage(engine, sid, data, {"errors": 1})
    with pytest.raises(RuntimeError, match="missing=1"):
        validate_combined._scan_coverage(engine, sid, data, {"errors": 0})
    with Session(engine) as db:
        row = File(session_id=sid, path=str(path), filename=path.name,
                   status=FileStatus.PENDING)
        db.add(row)
        db.commit()
        file_id = row.id
    assert validate_combined._scan_coverage(engine, sid, data, {"errors": 0}) == {
        "extracted": 2, "index_eligible": 1, "indexed": 1,
        "excluded_from_index": 1,
    }
    indexed = {"notes.txt"}
    with pytest.raises(RuntimeError, match="enrichment had 1 errors"):
        validate_combined._enrich_coverage(engine, sid, data, indexed, {"errors": 1})
    with pytest.raises(RuntimeError, match="unprocessed indexed rows"):
        validate_combined._enrich_coverage(engine, sid, data, indexed, {"errors": 0})
    with Session(engine) as db:
        db.get(File, file_id).status = FileStatus.ENRICHED
        db.commit()
    assert validate_combined._enrich_coverage(engine, sid, data, indexed,
                                              {"errors": 0})["enriched"] == 1
    with pytest.raises(RuntimeError, match="final coverage incomplete"):
        validate_combined._final_coverage(engine, sid, data, indexed, "full",
                                          {"analyzed": 1})
    with Session(engine) as db:
        row = db.get(File, file_id)
        row.status = FileStatus.SKIPPED
        row.analysis_outcome = "sampled"
        db.commit()
    coverage = validate_combined._final_coverage(
        engine, sid, data, indexed, "representative", {"sampled": 1})
    assert coverage["sampled"] == 1
    assert coverage["fresh"] == coverage["cached"] == coverage["skipped"] == 0
    with pytest.raises(RuntimeError, match="differs from run counters"):
        validate_combined._final_coverage(
            engine, sid, data, indexed, "representative", {"sampled": 0})


def test_execute_refuses_incomplete_pipeline_report(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    root = corpus / "DDH-combined-fixture"
    data = root / "data"
    state = root / "state"
    reports = root / "reports"
    for path in (data, state, reports):
        path.mkdir(parents=True)
    source = corpus / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("file.txt", "fixture")
    file = data / "file.txt"
    file.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)
    validate_corpus.write_json(root / "run.json", {
        "schema": 1, "run_dir": str(root.resolve()),
        "archives": [{"namespace": "Corpus", "source": str(source),
                      "source_sha256": validate_corpus.digest(source)}],
    })
    validate_corpus.write_json(reports / "baseline.json", [
        {"archive": "Corpus", **row}
        for row in validate_corpus.current_manifest(data)
    ])
    validate_corpus.write_json(reports / "pipeline.json", {"session_id": "fixture"})
    with pytest.raises(ValueError, match="pipeline incomplete"):
        validate_combined.execute_cycle(argparse.Namespace(
            run_dir=str(root), proposal_ids=[1], retain_organized=False))
    assert not (reports / "execution.json").exists()


def _reviewed_rename_fixture(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    root = corpus / "DDH-combined-fixture"
    data, state, reports = (root / name for name in ("data", "state", "reports"))
    for path in (data, state, reports, state / "datahoarder"):
        path.mkdir(parents=True, exist_ok=True)
    source = corpus / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("original.txt", "a reviewed fixture")
    original = data / "original.txt"
    original.write_text("a reviewed fixture", encoding="utf-8")
    renamed = data / "renamed.txt"
    monkeypatch.setattr(validate_combined, "CORPUS_DIR", corpus)

    def isolate_fixture(_root, selected_state):
        monkeypatch.setenv("DDH_DATA_DIR", str(selected_state / "datahoarder"))
        monkeypatch.setenv("DDH_DB", str(selected_state / "corpus.sqlite"))

    monkeypatch.setattr(validate_combined, "isolate", isolate_fixture)
    isolate_fixture(root, state)
    engine = init_db(state / "corpus.sqlite")
    with Session(engine) as db:
        owner = UserSession(root_path=str(data), name="review-fixture")
        db.add(owner)
        db.flush()
        row = File(session_id=owner.id, path=str(original), filename=original.name,
                   size_bytes=original.stat().st_size, status=FileStatus.PROPOSED)
        db.add(row)
        db.flush()
        proposal = Proposal(file_id=row.id, proposal_type=ProposalType.RENAME,
                            current_value=str(original), proposed_value=str(renamed),
                            status=ProposalStatus.PENDING, confidence=0.9)
        db.add(proposal)
        db.commit()
        sid, file_id, proposal_id = owner.id, row.id, proposal.id
    validate_corpus.write_json(root / "run.json", {
        "schema": 1, "run_dir": str(root.resolve()), "files": 1,
        "logical_bytes": original.stat().st_size,
        "archives": [{"namespace": "Corpus", "source": str(source),
                      "source_sha256": validate_corpus.digest(source)}],
    })
    validate_corpus.write_json(reports / "baseline.json", [
        {"archive": "Corpus", **item}
        for item in validate_corpus.current_manifest(data)
    ])
    validate_corpus.write_json(reports / "directories.json",
                               validate_corpus.directory_manifest(data))
    validate_corpus.write_json(reports / "indexed-baseline.json", ["original.txt"])
    validate_corpus.write_json(reports / "indexed-records.json", [
        {"id": file_id, "path": "original.txt"},
    ])
    validate_corpus.write_json(reports / "pipeline.json", {
        "session_id": sid, "final": {"files": 1},
    })
    return root, data, state, sid, file_id, proposal_id, original, renamed, engine


@pytest.mark.parametrize("retain", [False, True])
def test_execute_reviewed_rename_requires_successful_audit(tmp_path, monkeypatch, retain):
    root, data, state, sid, file_id, proposal_id, original, renamed, engine = (
        _reviewed_rename_fixture(tmp_path, monkeypatch))
    validate_combined.execute_cycle(argparse.Namespace(
        run_dir=str(root), proposal_ids=[proposal_id], retain_organized=retain))
    execution = validate_corpus.read_json(root / "reports" / "execution.json")
    assert execution["pass"] is True
    assert execution["audit_status"] == "passed"
    assert execution["audit"]["foreign_keys_clean"] is True
    if retain:
        assert renamed.read_text(encoding="utf-8") == "a reviewed fixture"
        assert not original.exists()
        assert validate_corpus.read_json(root / "reports" / "audit-organized.json")["pass"]
    else:
        assert original.read_text(encoding="utf-8") == "a reviewed fixture"
        assert not renamed.exists()
        assert execution["db_paths_restored"] is True
        assert validate_corpus.read_json(root / "reports" / "audit-restored.json")["pass"]


def test_organized_audit_rejects_orphan_and_missing_indexed_row(tmp_path, monkeypatch):
    root, data, state, sid, file_id, proposal_id, original, renamed, engine = (
        _reviewed_rename_fixture(tmp_path, monkeypatch))
    validate_combined.execute_cycle(argparse.Namespace(
        run_dir=str(root), proposal_ids=[proposal_id], retain_organized=True))
    with sqlite3.connect(state / "corpus.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO relation_members (group_id, file_id, role) "
                     "VALUES (999999, 999999, 'SIBLING')")
        conn.commit()
    with pytest.raises(RuntimeError, match="combined audit failed"):
        validate_combined.audit(argparse.Namespace(run_dir=str(root), organized=True))
    orphan_audit = validate_corpus.read_json(root / "reports" / "audit-organized.json")
    assert orphan_audit["foreign_keys_clean"] is False
    with sqlite3.connect(state / "corpus.sqlite") as conn:
        conn.execute("DELETE FROM relation_members WHERE group_id=999999")
        conn.execute("DELETE FROM proposals WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()
    with pytest.raises(RuntimeError, match="combined audit failed"):
        validate_combined.audit(argparse.Namespace(run_dir=str(root), organized=True))
    missing_audit = validate_corpus.read_json(root / "reports" / "audit-organized.json")
    assert missing_audit["content_match"] is True
    assert missing_audit["foreign_keys_clean"] is True
    assert missing_audit["db_paths_on_disk"] is False

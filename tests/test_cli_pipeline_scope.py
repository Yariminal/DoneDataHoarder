"""The all-in-one CLI command must keep each collection's work separate."""

from pathlib import Path

from sqlalchemy.orm import Session
from typer.testing import CliRunner

from donedatahoarder import cli
from donedatahoarder.db.models import File, FileStatus, Proposal, UserSession
from donedatahoarder.db.session import get_engine, init_db


def test_pipeline_scopes_every_stage_to_its_new_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_maybe_show_welcome", lambda: None)
    monkeypatch.setattr(cli, "_init_ai", lambda *_: (_ for _ in ()).throw(
        AssertionError("--skip-analyze must not initialize a model")
    ))

    first_root = tmp_path / "existing"
    second_root = tmp_path / "new"
    first_root.mkdir()
    second_root.mkdir()
    pending_path = first_root / "pending.txt"
    analyzed_path = first_root / "1.txt"
    new_path = second_root / "2.txt"
    pending_path.write_text("existing pending", encoding="utf-8")
    analyzed_path.write_text("existing analyzed", encoding="utf-8")
    new_path.write_text("new collection", encoding="utf-8")

    db_path = tmp_path / "index.db"
    engine = init_db(db_path)
    with Session(engine) as db:
        existing = UserSession(root_path=str(first_root), name="existing")
        db.add(existing)
        db.flush()
        existing_id = existing.id
        db.add_all([
            File(session_id=existing_id, path=str(pending_path),
                 filename=pending_path.name, status=FileStatus.PENDING),
            File(session_id=existing_id, path=str(analyzed_path),
                 filename=analyzed_path.name, status=FileStatus.ANALYZED),
        ])
        db.commit()

    result = CliRunner().invoke(cli.app, [
        "pipeline", str(second_root), "--db", str(db_path), "--skip-analyze",
    ])
    assert result.exit_code == 0, result.output

    with Session(get_engine()) as db:
        sessions = db.query(UserSession).all()
        assert len(sessions) == 2
        new_id = next(s.id for s in sessions if s.id != existing_id)
        assert db.get(UserSession, new_id).root_path == str(second_root)
        existing_files = db.query(File).filter(File.session_id == existing_id).all()
        assert {f.status for f in existing_files} == {FileStatus.PENDING, FileStatus.ANALYZED}
        assert db.query(Proposal).join(File).filter(File.session_id == existing_id).count() == 0
        new_files = db.query(File).filter(File.session_id == new_id).all()
        assert len(new_files) == 1
        assert Path(new_files[0].path) == new_path
        assert new_files[0].status in {FileStatus.ENRICHED, FileStatus.PROPOSED}
        assert db.query(Proposal).join(File).filter(File.session_id == new_id).count() >= 1
    assert new_id in result.output

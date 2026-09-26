"""A session owns its files and its duplicate groups."""
from pathlib import Path

from sqlalchemy.orm import Session

from donedatahoarder.core.dedup import find_exact_duplicates
from donedatahoarder.db.models import (
    DuplicateGroup,
    File,
    FileStatus,
    UserSession,
)
from donedatahoarder.db.session import get_engine, init_db


def _boot(tmp_path: Path):
    init_db(tmp_path / "t.db")
    return get_engine()


def _session(engine, name: str, root: Path) -> str:
    with Session(engine) as db:
        row = UserSession(name=name, root_path=str(root))
        db.add(row)
        db.commit()
        return row.id


def test_two_sessions_can_index_the_same_path(tmp_path):
    engine = _boot(tmp_path)
    first = _session(engine, "one", tmp_path)
    second = _session(engine, "two", tmp_path)
    with Session(engine) as db:
        for sid in (first, second):
            db.add(File(
                session_id=sid,
                path=str(tmp_path / "note.txt"),
                filename="note.txt",
                status=FileStatus.PENDING,
            ))
        db.commit()
        assert db.query(File).filter_by(path=str(tmp_path / "note.txt")).count() == 2


def test_exact_duplicates_do_not_cross_sessions(tmp_path):
    engine = _boot(tmp_path)
    first = _session(engine, "one", tmp_path)
    second = _session(engine, "two", tmp_path)
    with Session(engine) as db:
        for sid, name in ((first, "a"), (second, "b")):
            for copy in (1, 2):
                db.add(File(
                    session_id=sid,
                    path=str(tmp_path / f"{name}-{copy}.txt"),
                    filename=f"{name}-{copy}.txt",
                    status=FileStatus.ENRICHED,
                    hash_md5="abc123",
                ))
        db.commit()

    assert find_exact_duplicates(session_id=first)["groups"] == 1
    assert find_exact_duplicates(session_id=second)["groups"] == 1
    with Session(engine) as db:
        groups = db.query(DuplicateGroup).all()
        assert {group.session_id for group in groups} == {first, second}

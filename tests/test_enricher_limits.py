"""Bounded enrichment never reads files outside the requested budget."""
import pytest
from sqlalchemy.orm import Session

from donedatahoarder.core.enricher import enrich, enrich_with_progress
from donedatahoarder.db.models import File, FileStatus, UserSession
from donedatahoarder.db.session import init_db


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("limit", [0, 1, -1])
def test_enrichment_honors_file_budget(tmp_path, monkeypatch, progress, limit):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    engine = init_db(tmp_path / "index.db")
    root = tmp_path / "files"
    root.mkdir()
    with Session(engine) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.flush()
        for number in range(2):
            path = root / f"file-{number}.txt"
            path.write_text("synthetic content", encoding="utf-8")
            db.add(File(session_id=owner.id, path=str(path), filename=path.name,
                        extension=".txt", status=FileStatus.PENDING))
        db.commit()
        sid = owner.id

    def run():
        if progress:
            return list(enrich_with_progress(session_id=sid, limit=limit))[-1]
        return enrich(session_id=sid, limit=limit)

    if limit < 0:
        with pytest.raises(ValueError, match="non-negative"):
            run()
    else:
        assert run()["enriched"] == limit
    with Session(engine) as db:
        enriched = db.query(File).filter(File.status == FileStatus.ENRICHED).all()
        assert len(enriched) == max(0, limit)
        assert all(row.hash_sha256 for row in enriched)
        pending = db.query(File).filter(File.status == FileStatus.PENDING).all()
        assert all(row.hash_sha256 is None for row in pending)

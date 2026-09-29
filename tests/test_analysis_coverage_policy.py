"""Inventory and analysis coverage stay separate for mixed project collections."""
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from donedatahoarder.core.preflight import estimate_collection
from donedatahoarder.core.scanner import walk_files
from donedatahoarder.db.models import File, FileStatus, UserSession
from donedatahoarder.db.session import init_db
from donedatahoarder.web.app import create_app


def test_inventory_keeps_project_resources_and_excludes_transients(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    retained = {"drawing.3dmbak", "plot.ctb", "output.plt", "notes.log",
                "archive.db", "archive.db-wal", "archive.db-shm",
                "archive.db-journal", "settings.ini", "plugin.dll", "shortcut.lnk"}
    excluded = {"partial.part", "scratch.tmp", "Thumbs.db", ".DS_Store"}
    for name in retained | excluded:
        (root / name).write_bytes(b"data")
    indexed = {path.name for path in walk_files(root)}
    assert retained <= indexed
    assert not (excluded & indexed)
    assert len(indexed) == len(retained)


def test_active_database_is_not_indexed_inside_selected_root(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    active = root / "active.db"
    init_db(active)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{active}{suffix}")
        if not sidecar.exists():
            sidecar.write_bytes(b"active sidecar")
    (root / "archived.db").write_bytes(b"archived database")
    (root / "archived.db-wal").write_bytes(b"archived WAL")
    paths = {path.name for path in walk_files(root)}
    assert "active.db" not in paths
    assert not any(name.startswith("active.db-") for name in paths)
    assert "archived.db" in paths
    assert "archived.db-wal" in paths


def test_scan_and_enrich_hash_retained_resources_without_ai(tmp_path):
    from donedatahoarder.core.enricher import enrich
    from donedatahoarder.core.scanner import scan

    root = tmp_path / "collection"
    root.mkdir()
    source = {name: (name + " bytes").encode("utf-8") for name in (
        "drawing.3dmbak", "plot.ctb", "output.plt", "notes.log",
    )}
    for name, payload in source.items():
        (root / name).write_bytes(payload)
    (root / "scratch.tmp").write_bytes(b"temporary")
    engine = init_db(tmp_path / "index.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.commit()
        sid = owner.id
    assert scan(root, session_id=sid)["new"] == 4
    assert enrich(session_id=sid)["enriched"] == 4
    with Session(engine) as db:
        rows = db.query(File).filter(File.session_id == sid).all()
        assert {row.filename for row in rows} == set(source)
        for row in rows:
            assert row.hash_sha256 == hashlib.sha256(source[row.filename]).hexdigest()
            assert row.analysis_outcome is None


def test_preflight_names_likely_handlers_and_true_upper_bound(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    for name in ("scene.max", "drawing.3dm", "art.ai", "readme.txt",
                 "bundle.rar", "font.shx", "plot.ctb"):
        (root / name).write_bytes(b"x")
    result = estimate_collection(root)
    assert result["files"] == 7
    assert result["ai_candidate_files_estimate"] == 4
    assert result["ai_candidate_files_upper"] == 7
    assert result["unsupported_files_estimate"] == 3
    assert result["estimated_ai_calls_range"] == [4, 4]


def test_preflight_includes_numbered_bmp_sampling(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    for number in range(1, 5):
        (root / f"frame_{number:04d}.bmp").write_bytes(b"bitmap")
    result = estimate_collection(root, mode="representative",
                                 sequence_sample_stride=10)
    assert result["numbered_visual_candidates_upper"] == 4
    assert result["estimated_sampled_upper"] == 3
    assert result["estimated_ai_calls_range"] == [1, 4]


def test_analysis_coverage_endpoint_groups_outcomes_without_source_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "journal"))
    root = tmp_path / "collection"
    root.mkdir()
    app = create_app(tmp_path / "coverage.db")
    with Session(init_db(tmp_path / "coverage.db")) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.flush()
        for i, (extension, status, outcome, reason) in enumerate((
            (".shx", FileStatus.SKIPPED, "skipped", "unsupported_type"),
            (".shx", FileStatus.SKIPPED, "skipped", "unsupported_type"),
            (".ai", FileStatus.SKIPPED, "skipped", "unreadable_content"),
            (".jpg", FileStatus.ANALYZED, "content_verified", None),
            (".max", FileStatus.ANALYZED, "context_only", "unreadable_content"),
            (".txt", FileStatus.ENRICHED, None, None),
            (".log", FileStatus.SKIPPED, None, None),
            (".jpg", FileStatus.ERROR, None, None),
            (".pdf", FileStatus.ANALYZED, None, None),
        )):
            path = root / f"item{i}{extension}"
            db.add(File(session_id=owner.id, path=str(path), filename=path.name,
                        extension=extension, status=status,
                        analysis_outcome=outcome, analysis_reason=reason))
        db.commit()
        sid = owner.id
    with TestClient(app) as client:
        response = client.get("/api/pipeline/analysis/coverage",
                              params={"session_id": sid})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total_indexed"] == 9
    assert result["by_outcome"] == {
        "unprocessed": 1, "content_verified": 1, "context_only": 1,
        "skipped": 4, "failed": 1, "unknown_provenance": 1,
    }
    assert result["skip_reasons"] == {"unsupported_type": 2,
                                       "unreadable_content": 1,
                                       "unspecified": 1}
    assert result["skipped_by_extension"][0] == {
        "extension": ".shx", "reason": "unsupported_type", "count": 2,
    }
    assert result["other_skipped_formats"] == 0

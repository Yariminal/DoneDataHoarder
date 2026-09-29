"""Sequence review economy and linked-resource path safety."""
import json
import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session

from donedatahoarder.core.dedup import (
    _confirmed_sequence_families, _sequence_identity,
    _perceptual_candidate_pairs,
    _read_bounded_text,
    closest_perceptual_peer,
    find_exact_duplicates, find_perceptual_duplicates, find_semantic_duplicates,
    find_text_near_duplicates,
    generate_dedup_proposals, refresh_group_proposals,
    sequence_comparison_metadata,
)
from donedatahoarder.core.dependency_protection import ProtectionIndex
from donedatahoarder.db.models import (
    DuplicateGroup, DuplicateMember, DupeType, File, FileStatus,
    Proposal, ProposalType, UserSession,
    ProposalStatus,
)
from donedatahoarder.db.session import get_engine, init_db


def test_numbered_frame_review_is_sampled_but_exact_copies_remain(tmp_path):
    init_db(tmp_path / "quality.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="frames", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        frames = []
        for number in range(1, 101):
            file = File(session_id=run.id, path=str(tmp_path / f"render_{number:04d}.png"),
                        filename=f"render_{number:04d}.png", status=FileStatus.ANALYZED,
                        mime_type="image/png", size_bytes=100)
            db.add(file)
            frames.append(file)
        copy = File(session_id=run.id, path=str(tmp_path / "copy.png"), filename="copy.png",
                    status=FileStatus.ANALYZED, mime_type="image/png", size_bytes=100)
        db.add(copy)
        db.flush()
        near = DuplicateGroup(session_id=run.id, dupe_type=DupeType.PERCEPTUAL,
                              group_hash="frame-group", keep_file_id=frames[0].id)
        exact = DuplicateGroup(session_id=run.id, dupe_type=DupeType.EXACT,
                               group_hash="exact-group", keep_file_id=frames[0].id)
        db.add_all([near, exact])
        db.flush()
        db.add_all(DuplicateMember(group_id=near.id, file_id=f.id, similarity_score=0.95)
                   for f in frames)
        db.add_all([DuplicateMember(group_id=exact.id, file_id=frames[0].id,
                                    similarity_score=1),
                    DuplicateMember(group_id=exact.id, file_id=copy.id,
                                    similarity_score=1)])
        db.commit()
        sid = run.id
        copy_id = copy.id
        frame_ids = (frames[0].id, frames[1].id)

    counts = generate_dedup_proposals(session_id=sid)
    assert counts["created"] == 4
    assert counts["sequence_sampled"] == 3
    assert counts["sequence_deferred"] == 96
    with Session(engine) as db:
        proposals = db.query(Proposal).filter(Proposal.proposal_type == ProposalType.MARK_DUPLICATE).all()
        assert len(proposals) == 4
        assert any(p.file_id == copy_id and p.confidence == 1 for p in proposals)
        assert all(p.confidence is None for p in proposals if p.file_id != copy_id)
        assert sequence_comparison_metadata(db.get(File, frame_ids[1]), db.get(File, frame_ids[0]))["ordinal"] == 2
        assert sequence_comparison_metadata(db.get(File, copy_id), db.get(File, frame_ids[0])) is None


def test_keeper_change_preserves_sequence_review_cap(tmp_path):
    init_db(tmp_path / "keeper_sequence.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="keeper-sequence", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        files = []
        for number in range(1, 121):
            file = File(session_id=run.id, path=str(tmp_path / f"frame_{number:04d}.jpg"),
                        filename=f"frame_{number:04d}.jpg", status=FileStatus.ANALYZED,
                        mime_type="image/jpeg", hash_perceptual="ab" * 8, size_bytes=100)
            db.add(file)
            files.append(file)
        db.flush()
        group = DuplicateGroup(session_id=run.id, dupe_type=DupeType.PERCEPTUAL,
                               group_hash="all-frames", keep_file_id=files[0].id)
        db.add(group)
        db.flush()
        db.add_all(DuplicateMember(group_id=group.id, file_id=file.id,
                                   similarity_score=1.0, distance_to_keeper=0)
                   for file in files)
        db.commit()
        sid, group_id, new_keeper_id = run.id, group.id, files[60].id
    assert generate_dedup_proposals(session_id=sid)["created"] == 3
    with Session(engine) as db:
        db.get(DuplicateGroup, group_id).keep_file_id = new_keeper_id
        refresh_group_proposals(db, group_id)
        db.commit()
    with Session(engine) as db:
        pending = db.query(Proposal).filter(Proposal.duplicate_group_id == group_id,
                                            Proposal.status == ProposalStatus.PENDING).all()
        assert len(pending) <= 3
        assert all("sequence comparison sampled" in p.reasoning.lower() for p in pending)


def test_keeper_change_caps_sparse_groups_across_one_session_family(tmp_path):
    init_db(tmp_path / "sparse_keeper.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="sparse-frames", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        files = []
        for number in range(1, 61):
            file = File(session_id=run.id, path=str(tmp_path / f"frame_{number:04d}.jpg"),
                        filename=f"frame_{number:04d}.jpg", status=FileStatus.ANALYZED,
                        mime_type="image/jpeg", hash_perceptual="ab" * 8, size_bytes=100)
            db.add(file)
            files.append(file)
        db.flush()
        groups = []
        for index, subset in enumerate((files[::2], files[1::2])):
            group = DuplicateGroup(session_id=run.id, dupe_type=DupeType.PERCEPTUAL,
                                   group_hash=f"sparse-{index}", keep_file_id=subset[0].id)
            db.add(group)
            db.flush()
            db.add_all(DuplicateMember(group_id=group.id, file_id=file.id,
                                       similarity_score=1.0, distance_to_keeper=0)
                       for file in subset)
            groups.append(group)
        db.commit()
        sid, group_id, new_keeper_id = run.id, groups[0].id, files[20].id
    assert generate_dedup_proposals(session_id=sid)["created"] == 3
    with Session(engine) as db:
        db.get(DuplicateGroup, group_id).keep_file_id = new_keeper_id
        refresh_group_proposals(db, group_id)
        db.commit()
    with Session(engine) as db:
        active = db.query(Proposal).filter(
            Proposal.proposal_type == ProposalType.MARK_DUPLICATE,
            Proposal.status.in_((ProposalStatus.PENDING, ProposalStatus.APPROVED,
                                 ProposalStatus.MODIFIED)),
        ).all()
        assert len(active) <= 3


def test_bare_padded_numeric_frames_require_real_consecutive_family(tmp_path):
    paths = [tmp_path / f"{number:05d}.jpg" for number in (28, 29, 30, 31, 400)]
    identities = [_sequence_identity(str(path)) for path in paths]
    assert all(identity is not None for identity in identities)
    assert len({identity[0] for identity in identities}) == 1
    family = identities[0][0]
    assert _confirmed_sequence_families({family: {28, 29, 30, 31, 400}}) == {family}
    assert _confirmed_sequence_families({family: {28, 30, 400}}) == set()
    assert _sequence_identity(str(tmp_path / "12345.pdf")) is None


def test_closest_peer_surfaces_copy_inside_mixed_keeper_group():
    peers = [(1, "blank-phone.png", "0" * 16),
             (2, "presentation-a.png", "f" * 16),
             (3, "presentation-a-reencoded.jpg", "f" * 16)]
    nearest = closest_perceptual_peer(2, "f" * 16, peers, total_members=3)
    assert nearest["file_id"] == 3
    assert nearest["distance"] == 0
    assert nearest["comparison_coverage"] == "exact"
    sampled = closest_perceptual_peer(2, "f" * 16, peers[:2], total_members=3)
    assert sampled["comparison_coverage"] == "sampled"


def test_dense_common_tag_matching_reports_bounded_coverage(tmp_path, monkeypatch):
    init_db(tmp_path / "dense.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="dense", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        for number in range(300):
            db.add(File(session_id=run.id, path=str(tmp_path / f"photo-{number}-view.jpg"),
                        filename=f"photo-{number}-view.jpg", status=FileStatus.ANALYZED,
                        mime_type="image/jpeg", ai_description=f"building view {number}",
                        ai_tags=json.dumps(["building"]), analysis_outcome="content_verified",
                        size_bytes=100))
        db.commit()
        sid = run.id
    from donedatahoarder.core import dedup
    comparisons = 0
    original = dedup._string_similarity

    def counted(left, right):
        nonlocal comparisons
        comparisons += 1
        return original(left, right)

    monkeypatch.setattr(dedup, "_string_similarity", counted)
    result = find_semantic_duplicates(session_id=sid)
    assert comparisons < 10_000  # 44,850 all-pairs comparisons would be excessive
    assert result["candidate_coverage"] == "bounded_incomplete"
    assert result["candidate_pair_opportunities_deferred"] > 30_000


def test_filename_only_opaque_descriptions_do_not_create_semantic_candidates(tmp_path):
    init_db(tmp_path / "opaque.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="opaque", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        for name in ("scene.max", "scene-copy.max"):
            db.add(File(session_id=run.id, path=str(tmp_path / name), filename=name,
                        status=FileStatus.ANALYZED, mime_type="application/octet-stream",
                        analysis_outcome="context_only", analysis_evidence_source="filename_only",
                        ai_description="Unverified project 3D scene", ai_tags=json.dumps(["3d", "scene"]),
                        size_bytes=100))
        db.commit()
        sid = run.id
    assert find_semantic_duplicates(session_id=sid) == {"groups": 0, "duplicates": 0}


def test_dense_same_size_text_reports_unexamined_pairs(tmp_path):
    init_db(tmp_path / "dense_text.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="dense-text", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        for number in range(100):
            path = tmp_path / f"document-{number}-copy.txt"
            path.write_text(f"Report {number:03d} " + "x" * 90, encoding="utf-8")
            db.add(File(session_id=run.id, path=str(path), filename=path.name,
                        extension=".txt", status=FileStatus.ENRICHED,
                        mime_type="text/plain", size_bytes=path.stat().st_size))
        db.commit()
        sid = run.id
    result = find_text_near_duplicates(session_id=sid)
    assert result["candidate_coverage"] == "bounded_incomplete"
    assert result["candidate_pair_opportunities_deferred"] > 0


def test_text_read_uses_actual_byte_cap(tmp_path):
    path = tmp_path / "growing.txt"
    path.write_bytes(b"x" * 1001)
    assert _read_bounded_text(str(path), 1000) is None
    path.write_bytes(b"x" * 1000)
    assert _read_bounded_text(str(path), 1000) == "x" * 1000


def test_dense_perceptual_bands_report_bounded_coverage():
    # These 64-bit hashes all share the leading zero band but are distinct.
    hashes = [f"0000{number:012x}" for number in range(100)]
    stats = {}
    pairs = _perceptual_candidate_pairs(hashes, threshold=8, max_pairs=25, stats=stats)
    assert len(pairs) == 25
    assert stats["candidate_coverage"] == "bounded_incomplete"
    assert stats["candidate_pair_opportunities_deferred"] is None
    assert stats["candidate_pair_opportunities_deferred_lower_bound"] == 1


def test_large_group_avoids_sqlite_parameter_limit(tmp_path, monkeypatch):
    init_db(tmp_path / "limit.sqlite")
    engine = get_engine()
    reduced_limit_supported = hasattr(sqlite3.Connection, "setlimit")
    member_count = 130 if reduced_limit_supported else 1001

    @event.listens_for(engine, "connect")
    def lower_limit(connection, _record):
        if reduced_limit_supported:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)

    engine.dispose()
    from donedatahoarder.core import dedup
    monkeypatch.setattr(dedup, "KEEPER_QUERY_CHUNK", 32)
    with Session(engine) as db:
        run = UserSession(name="limit", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        for number in range(member_count):
            db.add(File(session_id=run.id, path=str(tmp_path / f"image-{number}-test.png"),
                        filename=f"image-{number}-test.png", status=FileStatus.ENRICHED,
                        mime_type="image/png", hash_perceptual="ab" * 8, size_bytes=100))
        db.commit()
        sid = run.id
    assert find_perceptual_duplicates(threshold=0, session_id=sid)["duplicates"] == member_count - 1
    assert generate_dedup_proposals(session_id=sid)["created"] == member_count - 1


def test_exact_hash_detects_unsupported_analyzed_files(tmp_path):
    init_db(tmp_path / "skipped.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="skipped", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        for number in (1, 2):
            db.add(File(session_id=run.id, path=str(tmp_path / f"font{number}.shx"),
                        filename=f"font{number}.shx", status=FileStatus.SKIPPED,
                        mime_type="application/octet-stream", hash_md5="a" * 32,
                        size_bytes=100))
        db.commit()
        sid = run.id
    assert find_exact_duplicates(session_id=sid) == {"groups": 1, "duplicates": 1}


def test_transitive_semantic_member_below_keeper_threshold_is_not_proposed(tmp_path):
    init_db(tmp_path / "transitive.sqlite")
    engine = get_engine()
    with Session(engine) as db:
        run = UserSession(name="transitive", root_path=str(tmp_path))
        db.add(run)
        db.flush()
        files = []
        for name in ("keeper.jpg", "near.jpg", "far.jpg"):
            file = File(session_id=run.id, path=str(tmp_path / name), filename=name,
                        status=FileStatus.ANALYZED, mime_type="image/jpeg", size_bytes=100)
            db.add(file)
            files.append(file)
        db.flush()
        group = DuplicateGroup(session_id=run.id, dupe_type=DupeType.SEMANTIC,
                               group_hash="chain", keep_file_id=files[0].id)
        db.add(group)
        db.flush()
        for file, score in zip(files, (1.0, 0.7, 0.4)):
            db.add(DuplicateMember(group_id=group.id, file_id=file.id,
                                   similarity_score=score))
        db.commit()
        sid = run.id
    result = generate_dedup_proposals(session_id=sid)
    assert result["created"] == 1
    assert result["weak_direct_evidence_deferred"] == 1


def test_html_css_and_opaque_design_references_are_protected(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    page = root / "page.html"
    stylesheet = root / "style.css"
    script = root / "app.js"
    image = root / "image.png"
    design = root / "layout.ai"
    unrelated = root / "notes.txt"
    page.write_text('<link href="style.css"><script src="app.js"></script>'
                    '<img src="image.png"><a href="https://example.com">remote</a>', encoding="utf-8")
    stylesheet.write_text('body { background: url("image.png") }', encoding="utf-8")
    script.write_text("ok", encoding="utf-8")
    image.write_bytes(b"image")
    design.write_bytes(b"opaque design source with image.png reference")
    unrelated.write_text("notes", encoding="utf-8")
    index = ProtectionIndex(root)
    for path in (page, stylesheet, script, image, design):
        assert index.assess(path).protected, path
    assert not index.assess(unrelated).protected


def test_site_root_reference_guards_assets_under_ambiguous_site_roots(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    page = root / "site" / "pages" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text('<img src="/assets/logo.png">', encoding="utf-8")
    nested_asset = root / "site" / "assets" / "logo.png"
    nested_asset.parent.mkdir()
    nested_asset.write_bytes(b"nested")
    root_asset = root / "assets" / "logo.png"
    root_asset.parent.mkdir()
    root_asset.write_bytes(b"root")
    unrelated = root / "notes.txt"
    unrelated.write_text("safe", encoding="utf-8")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "external.html").write_text("outside", encoding="utf-8")
    try:
        (root / "linked-outside").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    original_open = Path.open

    def reject_external_reads(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(external)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_external_reads)
    index = ProtectionIndex(root)
    assert index.assess(page).protected
    assert index.assess(nested_asset).protected
    assert index.assess(root_asset).protected
    assert not index.assess(unrelated).protected


def test_unreadable_web_and_design_sources_guard_possible_assets(tmp_path, monkeypatch):
    root = tmp_path / "collection"
    root.mkdir()
    page = root / "index.html"
    design = root / "layout.psd"
    image = root / "image.png"
    page.write_text('<img src="image.png">', encoding="utf-8")
    design.write_bytes(b"opaque")
    image.write_bytes(b"asset")
    original_open = Path.open

    def fail_source_reads(path, *args, **kwargs):
        if path in {page, design}:
            raise OSError("unreadable source")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_source_reads)
    index = ProtectionIndex(root)
    assert index.assess(page).protected
    assert index.assess(design).protected
    assert index.assess(image).protected


def test_escaped_and_linked_web_paths_do_not_probe_external_tree(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    external = tmp_path / "outside.png"
    external.write_bytes(b"outside")
    page = root / "page.html"
    page.write_text('<img src="../outside.png"><img src="file:///outside.png">', encoding="utf-8")
    link = root / "link.png"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pass
    else:
        page.write_text(page.read_text(encoding="utf-8") + '<img src="link.png">', encoding="utf-8")
    index = ProtectionIndex(root)
    assert index.assess(page).protected
    assert external.resolve() not in index._reasons
    if link.is_symlink():
        assert index.assess(link).protected

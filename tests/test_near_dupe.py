"""Near-duplicate candidates retain direct keeper-relative evidence."""
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from donedatahoarder import phash
from donedatahoarder.core import dedup
from donedatahoarder.core.dedup import (
    find_perceptual_duplicates,
    find_semantic_duplicates,
)
from donedatahoarder.db.models import (
    DuplicateGroup,
    DupeType,
    File,
    FileStatus,
    UserSession,
)
from donedatahoarder.db.session import get_engine, init_db


def _boot(tmp_path: Path):
    init_db(tmp_path / "t.db")
    return get_engine()


def _new_session(engine, name: str) -> str:
    with Session(engine) as db:
        row = UserSession(name=name, root_path=name)
        db.add(row)
        db.commit()
        return row.id


def _add_file(db, session_id: str, name: str, **fields) -> int:
    tags = fields.pop("tags", None)
    row = File(
        session_id=session_id,
        path=f"{session_id}/{name}",
        filename=name,
        status=fields.pop("status", FileStatus.ENRICHED),
        mime_type=fields.pop("mime_type", "image/jpeg"),
        hash_perceptual=fields.pop("hash_perceptual", None),
        ai_description=fields.pop("ai_description", None),
        ai_tags=json.dumps(tags) if tags is not None else None,
        analysis_outcome=fields.pop("analysis_outcome", "content_verified"),
        size_bytes=fields.pop("size_bytes", 100),
    )
    db.add(row)
    db.flush()
    return row.id


def _flip(hex_hash: str, *positions: int) -> str:
    value = int(hex_hash, 16)
    for pos in positions:
        value ^= 1 << pos
    return f"{value:0{len(hex_hash)}x}"


def _groups(engine, dupe_type: DupeType):
    with Session(engine) as db:
        rows = (
            db.query(DuplicateGroup)
            .filter(DuplicateGroup.dupe_type == dupe_type)
            .all()
        )
        found = []
        for group in rows:
            found.append({
                "session_id": group.session_id,
                "type": group.dupe_type,
                "keeper_id": group.keep_file_id,
                "ids": tuple(sorted(member.file_id for member in group.members)),
                "scores": {
                    member.file_id: member.similarity_score
                    for member in group.members
                },
                "distances": {
                    member.file_id: member.distance_to_keeper
                    for member in group.members
                },
            })
        return found


def _pair_sim(desc_a, tags_a, desc_b, tags_b) -> float:
    return (
        0.4 * dedup._string_similarity(desc_a, desc_b)
        + 0.6 * dedup._tags_overlap(tags_a, tags_b)
    )


def test_perceptual_chain_splits_at_keeper_threshold(tmp_path):
    """A~B~C~D cannot claim its far end matches A's keeper evidence."""
    engine = _boot(tmp_path)
    sid = _new_session(engine, "main")
    other = _new_session(engine, "other")
    threshold = 2

    anchor = "0" * 16
    hop_b = _flip(anchor, 0, 1)
    hop_c = _flip(anchor, 0, 1, 2, 3)
    hop_d = _flip(anchor, 0, 1, 2, 3, 4, 5)
    left = "0f0f0f0f0f0f0f0f"
    right = _flip(left, 9)
    far = "f" * 16

    assert phash.hash_distance(anchor, hop_b) == 2
    assert phash.hash_distance(hop_b, hop_c) == 2
    assert phash.hash_distance(hop_c, hop_d) == 2
    assert phash.hash_distance(anchor, hop_c) > threshold
    assert phash.hash_distance(anchor, hop_d) > threshold
    assert phash.hash_distance(hop_b, hop_d) > threshold
    assert phash.hash_distance(left, right) <= threshold
    for hop in (anchor, hop_b, hop_c, hop_d):
        assert phash.hash_distance(left, hop) > threshold
        assert phash.hash_distance(right, hop) > threshold
        assert phash.hash_distance(far, hop) > threshold

    with Session(engine) as db:
        id_a = _add_file(db, sid, "a.jpg", hash_perceptual=anchor)
        id_b = _add_file(db, sid, "b.jpg", hash_perceptual=hop_b, mime_type="image/png")
        id_c = _add_file(db, sid, "c.mp4", hash_perceptual=hop_c, mime_type="video/mp4")
        id_d = _add_file(db, sid, "d.jpg", hash_perceptual=hop_d)
        id_p = _add_file(db, sid, "p.jpg", hash_perceptual=left)
        id_q = _add_file(db, sid, "q.jpg", hash_perceptual=right)
        id_far = _add_file(db, sid, "far.jpg", hash_perceptual=far)
        id_pdf = _add_file(
            db, sid, "same.pdf", hash_perceptual=anchor, mime_type="application/pdf",
        )
        id_foreign = _add_file(db, other, "foreign.jpg", hash_perceptual=anchor)
        db.commit()

    counts = find_perceptual_duplicates(threshold=threshold, session_id=sid)
    assert counts == {"groups": 3, "duplicates": 3}

    found = _groups(engine, DupeType.PERCEPTUAL)
    assert {group["session_id"] for group in found} == {sid}
    sets = {group["ids"] for group in found}
    assert sets == {
        tuple(sorted((id_a, id_b))),
        tuple(sorted((id_c, id_d))),
        tuple(sorted((id_p, id_q))),
    }
    assert id_far not in {fid for group in found for fid in group["ids"]}
    assert id_pdf not in {fid for group in found for fid in group["ids"]}
    assert id_foreign not in {fid for group in found for fid in group["ids"]}
    for group in found:
        assert group["type"] == DupeType.PERCEPTUAL
        for member_id in group["ids"]:
            distance = group["distances"][member_id]
            assert distance <= threshold
            assert group["scores"][member_id] == pytest.approx(1 - distance / 64)


def test_spread_bits_within_threshold_still_group(tmp_path):
    """Eight flipped bits, one per region, stay inside a threshold of 8."""
    engine = _boot(tmp_path)
    sid = _new_session(engine, "spread")
    base = "0" * 16
    near = _flip(base, 0, 8, 16, 24, 32, 40, 48, 56)
    far = "f" * 16
    assert phash.hash_distance(base, near) == 8
    assert phash.hash_distance(base, far) == 64

    with Session(engine) as db:
        id_base = _add_file(db, sid, "base.jpg", hash_perceptual=base)
        id_near = _add_file(db, sid, "near.jpg", hash_perceptual=near)
        id_far = _add_file(db, sid, "far.jpg", hash_perceptual=far)
        db.commit()

    assert find_perceptual_duplicates(threshold=7, session_id=sid)["groups"] == 0
    assert _groups(engine, DupeType.PERCEPTUAL) == []

    counts = find_perceptual_duplicates(threshold=8, session_id=sid)
    assert counts == {"groups": 1, "duplicates": 1}
    found = _groups(engine, DupeType.PERCEPTUAL)
    assert [group["ids"] for group in found] == [tuple(sorted((id_base, id_near)))]
    assert id_far not in found[0]["ids"]


def test_threshold_zero_does_not_score_every_pair(monkeypatch, tmp_path):
    """Distinct hashes share no full-width band, so distance is not n^2."""
    engine = _boot(tmp_path)
    sid = _new_session(engine, "zero")
    calls = {"n": 0}
    real = dedup.hash_distance

    def wrapped(hash_a, hash_b):
        calls["n"] += 1
        return real(hash_a, hash_b)

    monkeypatch.setattr(dedup, "hash_distance", wrapped)
    with Session(engine) as db:
        for i in range(1, 21):
            _add_file(db, sid, f"{i}.jpg", hash_perceptual=f"{i:016x}")
        db.commit()

    assert find_perceptual_duplicates(threshold=0, session_id=sid) == {
        "groups": 0,
        "duplicates": 0,
    }
    assert calls["n"] == 0


def test_shared_band_still_obeys_hash_distance(monkeypatch, tmp_path):
    engine = _boot(tmp_path)
    sid = _new_session(engine, "gate")
    calls = []

    def wrapped(hash_a, hash_b):
        calls.append((hash_a, hash_b))
        return 50

    monkeypatch.setattr(dedup, "hash_distance", wrapped)
    base = "0" * 16
    near = _flip(base, 0)
    assert phash.hash_distance(base, near) == 1
    with Session(engine) as db:
        _add_file(db, sid, "a.jpg", hash_perceptual=base)
        _add_file(db, sid, "b.jpg", hash_perceptual=near)
        db.commit()

    assert find_perceptual_duplicates(threshold=8, session_id=sid)["groups"] == 0
    assert len(calls) == 1


def test_identical_hashes_consult_hash_distance(monkeypatch, tmp_path):
    engine = _boot(tmp_path)
    sid = _new_session(engine, "same")
    calls = {"n": 0}
    real = dedup.hash_distance

    def wrapped(hash_a, hash_b):
        calls["n"] += 1
        return real(hash_a, hash_b)

    monkeypatch.setattr(dedup, "hash_distance", wrapped)
    with Session(engine) as db:
        id_a = _add_file(db, sid, "a.jpg", hash_perceptual="ab" * 8)
        id_b = _add_file(db, sid, "b.jpg", hash_perceptual="ab" * 8)
        _add_file(db, sid, "c.jpg", hash_perceptual="cd" * 8)
        db.commit()

    counts = find_perceptual_duplicates(threshold=0, session_id=sid)
    assert counts == {"groups": 1, "duplicates": 1}
    assert calls["n"] >= 3  # candidate verification and keeper-relative evidence
    assert _groups(engine, DupeType.PERCEPTUAL)[0]["ids"] == tuple(sorted((id_a, id_b)))


def test_identical_hash_rejected_when_distance_is_past_threshold(monkeypatch, tmp_path):
    engine = _boot(tmp_path)
    sid = _new_session(engine, "reject")
    monkeypatch.setattr(dedup, "hash_distance", lambda hash_a, hash_b: 100)
    with Session(engine) as db:
        _add_file(db, sid, "a.jpg", hash_perceptual="ab" * 8)
        _add_file(db, sid, "b.jpg", hash_perceptual="ab" * 8)
        db.commit()
    assert find_perceptual_duplicates(threshold=8, session_id=sid)["groups"] == 0


def test_perceptual_does_not_call_is_near_duplicate(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("is_near_duplicate should not be called")

    monkeypatch.setattr(phash, "is_near_duplicate", boom)
    if hasattr(dedup, "is_near_duplicate"):
        monkeypatch.setattr(dedup, "is_near_duplicate", boom)

    engine = _boot(tmp_path)
    sid = _new_session(engine, "boom")
    with Session(engine) as db:
        _add_file(db, sid, "a.jpg", hash_perceptual="0" * 16)
        _add_file(db, sid, "b.jpg", hash_perceptual=_flip("0" * 16, 0))
        db.commit()
    assert find_perceptual_duplicates(threshold=8, session_id=sid)["groups"] == 1


def test_perceptual_sessions_stay_split(tmp_path):
    engine = _boot(tmp_path)
    first = _new_session(engine, "one")
    second = _new_session(engine, "two")
    with Session(engine) as db:
        a1 = _add_file(db, first, "a.jpg", hash_perceptual="11" * 8)
        a2 = _add_file(db, first, "b.jpg", hash_perceptual="11" * 8)
        b1 = _add_file(db, second, "a.jpg", hash_perceptual="11" * 8)
        b2 = _add_file(db, second, "b.jpg", hash_perceptual="11" * 8)
        db.commit()

    assert find_perceptual_duplicates(threshold=0, session_id=first)["groups"] == 1
    assert find_perceptual_duplicates(threshold=0, session_id=second)["groups"] == 1
    found = _groups(engine, DupeType.PERCEPTUAL)
    assert {group["session_id"]: group["ids"] for group in found} == {
        first: tuple(sorted((a1, a2))),
        second: tuple(sorted((b1, b2))),
    }


def test_perceptual_requires_imagehash(monkeypatch):
    monkeypatch.setattr(dedup, "_HAS_IMAGEHASH", False)
    assert find_perceptual_duplicates() == {"error": "imagehash not installed"}


def test_semantic_chain_keeps_one_group_and_a_score(monkeypatch, tmp_path):
    """Path A-B-C-D is one semantic group. Mime and tag gates stay shut."""
    engine = _boot(tmp_path)
    sid = _new_session(engine, "sem")
    chain = "chain doc"
    s_ab = _pair_sim(chain, ["t1"], chain, ["t1", "t2"])
    s_bc = _pair_sim(chain, ["t1", "t2"], chain, ["t2", "t3"])
    s_cd = _pair_sim(chain, ["t2", "t3"], chain, ["t3"])
    assert s_ab >= dedup.AI_SIMILARITY_THRESHOLD
    assert s_bc >= dedup.AI_SIMILARITY_THRESHOLD
    assert s_cd >= dedup.AI_SIMILARITY_THRESHOLD
    g_desc = "zzzz-unrelated-quark-spreadsheet-taxonomy-998877"
    g_tags = ["t1", "n1", "n2", "n3", "n4"]
    assert _pair_sim(chain, ["t1"], g_desc, g_tags) < dedup.AI_SIMILARITY_THRESHOLD
    assert _pair_sim(chain, ["t1", "t2"], g_desc, g_tags) < dedup.AI_SIMILARITY_THRESHOLD

    seen = []
    real = dedup._string_similarity

    def wrapped(desc_a, desc_b):
        seen.append((desc_a, desc_b))
        return real(desc_a, desc_b)

    monkeypatch.setattr(dedup, "_string_similarity", wrapped)

    with Session(engine) as db:
        id_a = _add_file(
            db, sid, "a.jpg", status=FileStatus.ANALYZED,
            ai_description=chain, tags=["t1"],
        )
        id_b = _add_file(
            db, sid, "b.png", status=FileStatus.ANALYZED, mime_type="image/png",
            ai_description=chain, tags=["t1", "t2"],
        )
        id_c = _add_file(
            db, sid, "c.gif", status=FileStatus.ANALYZED, mime_type="image/gif",
            ai_description=chain, tags=["t2", "t3"],
        )
        id_d = _add_file(
            db, sid, "d.jpg", status=FileStatus.PROPOSED,
            ai_description=chain, tags=["t3"],
        )
        id_pdf = _add_file(
            db, sid, "notes.pdf", status=FileStatus.ANALYZED,
            mime_type="application/pdf",
            ai_description="PDF-ONLY-DESC-ZZZ", tags=["t1"],
        )
        id_notag = _add_file(
            db, sid, "notag.jpg", status=FileStatus.ANALYZED,
            ai_description="NO-TAG-SHARE-DESC-QQQ", tags=["nope"],
        )
        id_low = _add_file(
            db, sid, "low.jpg", status=FileStatus.ANALYZED,
            ai_description=g_desc, tags=g_tags,
        )
        id_null = _add_file(
            db, sid, "null.bin", status=FileStatus.ANALYZED, mime_type=None,
            ai_description="NULL-MIME-DESC", tags=["t1"],
        )
        id_h1 = _add_file(
            db, sid, "h1.jpg", status=FileStatus.ANALYZED,
            ai_description="solo doc", tags=["solo"],
        )
        id_h2 = _add_file(
            db, sid, "h2.jpg", status=FileStatus.ANALYZED,
            ai_description="solo doc", tags=["solo"],
        )
        _add_file(
            db, sid, "enriched.jpg", status=FileStatus.ENRICHED,
            ai_description="solo doc", tags=["solo"],
        )
        id_o1 = _add_file(
            db, sid, "o1.bin", status=FileStatus.ANALYZED, mime_type=None,
            ai_description="ghost doc", tags=["ghost"],
        )
        id_o2 = _add_file(
            db, sid, "o2.bin", status=FileStatus.ANALYZED, mime_type=None,
            ai_description="ghost doc", tags=["ghost"],
        )
        db.commit()

    counts = find_semantic_duplicates(session_id=sid)
    assert counts == {"groups": 3, "duplicates": 5}

    blob = " ".join(f"{left} {right}" for left, right in seen)
    assert "PDF-ONLY-DESC-ZZZ" not in blob
    assert "NO-TAG-SHARE-DESC-QQQ" not in blob
    assert "NULL-MIME-DESC" not in blob
    assert g_desc in blob
    assert chain in blob

    found = _groups(engine, DupeType.SEMANTIC)
    by_ids = {group["ids"]: group for group in found}
    chain_ids = tuple(sorted((id_a, id_b, id_c, id_d)))
    assert set(by_ids) == {
        chain_ids,
        tuple(sorted((id_h1, id_h2))),
        tuple(sorted((id_o1, id_o2))),
    }
    # Scores are measured directly against A, never copied from transitive
    # A-B, B-C, C-D edge averages.
    assert by_ids[chain_ids]["scores"][id_a] == pytest.approx(1.0)
    assert by_ids[chain_ids]["scores"][id_b] == pytest.approx(s_ab)
    assert by_ids[chain_ids]["scores"][id_c] == pytest.approx(
        _pair_sim(chain, ["t1"], chain, ["t2", "t3"])
    )
    assert by_ids[chain_ids]["scores"][id_d] == pytest.approx(
        _pair_sim(chain, ["t1"], chain, ["t3"])
    )
    solo_ids = tuple(sorted((id_h1, id_h2)))
    assert all(
        score == pytest.approx(1.0) for score in by_ids[solo_ids]["scores"].values()
    )
    for group in found:
        assert group["type"] == DupeType.SEMANTIC
        assert group["session_id"] == sid
    outsiders = {id_pdf, id_notag, id_low, id_null}
    assert outsiders.isdisjoint(fid for group in found for fid in group["ids"])


def test_semantic_sessions_stay_split(tmp_path):
    engine = _boot(tmp_path)
    first = _new_session(engine, "one")
    second = _new_session(engine, "two")
    with Session(engine) as db:
        a1 = _add_file(
            db, first, "a.jpg", status=FileStatus.ANALYZED,
            ai_description="same note", tags=["shared"],
        )
        a2 = _add_file(
            db, first, "b.jpg", status=FileStatus.ANALYZED,
            ai_description="same note", tags=["shared"],
        )
        b1 = _add_file(
            db, second, "a.jpg", status=FileStatus.ANALYZED,
            ai_description="same note", tags=["shared"],
        )
        b2 = _add_file(
            db, second, "b.jpg", status=FileStatus.ANALYZED,
            ai_description="same note", tags=["shared"],
        )
        db.commit()

    assert find_semantic_duplicates(session_id=first)["groups"] == 1
    assert find_semantic_duplicates(session_id=second)["groups"] == 1
    found = _groups(engine, DupeType.SEMANTIC)
    assert {group["session_id"]: group["ids"] for group in found} == {
        first: tuple(sorted((a1, a2))),
        second: tuple(sorted((b1, b2))),
    }

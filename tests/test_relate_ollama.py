"""Bounded Ollama requests used by the relation grouping step."""

import json
import importlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from donedatahoarder.ai import ollama_client
from donedatahoarder.ai.json_utils import extract_json
from donedatahoarder.ai.ollama_client import OllamaClient
from donedatahoarder.core.relate import (
    _call_llm_for_group, _link_singletons_to_folder_groups,
    _llm_cross_script_cluster, _numbered_frame_groups, relate,
)
from donedatahoarder.db.models import Base, File, FileStatus, RelationGroup, RelationMember, UserSession
from donedatahoarder.db.session import init_db


def _files(count):
    return [
        SimpleNamespace(id=i, filename=f"frame_{i:03}.jpg", extension="jpg", size_bytes=100)
        for i in range(count)
    ]


def test_gemma4_default_bounds_and_explicit_overrides(monkeypatch):
    payloads = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "ok", "done_reason": "stop"}

    class FakeHTTPClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, _url, json):
            payloads.append(json)
            return FakeResponse()

    monkeypatch.setattr(ollama_client.httpx, "Client", FakeHTTPClient)
    client = OllamaClient(text_model="gemma4:26b", vision_model="gemma4:26b")

    assert client.generate("first") == "ok"
    assert payloads[-1]["options"]["num_predict"] == 4096
    assert payloads[-1]["think"] is False

    assert client.generate_with_image("image", image_bytes=b"picture") == "ok"
    assert payloads[-1]["options"]["num_predict"] == 4096
    assert payloads[-1]["think"] is False

    assert client.generate("second", num_predict=512, think=True) == "ok"
    assert payloads[-1]["options"]["num_predict"] == 512
    assert payloads[-1]["think"] is True
    assert client.generate_with_image(
        "image", image_bytes=b"picture", num_predict=1024, think=True
    ) == "ok"
    assert payloads[-1]["options"]["num_predict"] == 1024
    assert payloads[-1]["think"] is True

    # Selecting a non-Gemma model for one call keeps that family's defaults.
    assert client.generate("third", model="gemma3:12b") == "ok"
    assert "num_predict" not in payloads[-1]["options"]
    assert "think" not in payloads[-1]
    assert client.generate_with_image("image", image_bytes=b"picture", model="llava") == "ok"
    assert "num_predict" not in payloads[-1]["options"]
    assert "think" not in payloads[-1]


def test_ollama_reports_length_truncation_even_if_text_looks_valid(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": '{"groups": []}', "done_reason": "length"}

    class FakeHTTPClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, _url, json):
            return FakeResponse()

    monkeypatch.setattr(ollama_client.httpx, "Client", FakeHTTPClient)
    with pytest.raises(ValueError, match="output token limit"):
        OllamaClient(text_model="gemma4:26b").generate("prompt", num_predict=4096)


def test_relate_passes_bounded_options_only_to_ollama_gemma4(monkeypatch):
    client = OllamaClient(text_model="gemma4:26b")
    received = []
    monkeypatch.setattr(client, "generate_json", lambda _prompt, **kw: received.append(kw) or [])
    monkeypatch.setattr(client, "generate", lambda _prompt, **kw: received.append(kw) or "[]")

    _call_llm_for_group(client, "frames", _files(100), model="gemma4:26b")
    assert received[-1]["model"] == "gemma4:26b"
    assert received[-1]["timeout"] == 300
    assert received[-1]["num_predict"] == 4096
    assert received[-1]["think"] is False

    _llm_cross_script_cluster(client, _files(5), model="gemma4:26b")
    assert received[-1]["model"] == "gemma4:26b"
    assert received[-1]["timeout"] == 300
    assert received[-1]["num_predict"] == 4096
    assert received[-1]["think"] is False

    _call_llm_for_group(client, "frames", _files(2), model="gemma3:12b")
    assert received[-1]["num_predict"] == 4096
    assert "think" not in received[-1]


def test_relate_does_not_send_ollama_options_to_other_providers():
    class OtherClient:
        def generate_json(self, _prompt, **kwargs):
            assert "num_predict" not in kwargs
            assert "think" not in kwargs
            return []

    _call_llm_for_group(OtherClient(), "frames", _files(2), model="gemma4:26b")


def test_cross_script_parse_failure_is_logged(caplog):
    client = OllamaClient(text_model="gemma4:26b")
    client.generate = lambda _prompt, **_kwargs: "not JSON"

    assert _llm_cross_script_cluster(client, _files(5), model="gemma4:26b") == []
    assert "Relate cross-script LLM call failed" in caplog.text
    assert "gemma4:26b" in caplog.text


def test_cross_script_accepts_fenced_json_but_rejects_tab_invented_name(monkeypatch):
    # Shape observed in the Medium corpus: a fenced JSON array with a literal
    # tab embedded in an invented filename. Never normalize that name to a
    # real file; membership must still match the input exactly.
    payload = [{
        "canonical_token": "rendering",
        "filenames": ["00182.jpg", "00183.jpg", "00\t0182.jpg"],
    }]
    raw = "```json\n" + json.dumps(payload).replace("\\t", "\t") + "\n```"
    with pytest.raises(ValueError, match="Could not extract valid JSON"):
        extract_json(raw)
    assert extract_json(raw, allow_control_chars=True)[0]["filenames"][-1] == "00\t0182.jpg"

    files = _files(5)
    files[0].filename = "00182.jpg"
    files[1].filename = "00183.jpg"
    client = OllamaClient(text_model="gemma4:26b")
    monkeypatch.setattr(client, "generate", lambda _prompt, **_kwargs: raw)
    # Exact membership filtering is retained, but adjacent numeric names
    # alone no longer establish a semantic relation.
    groups = _llm_cross_script_cluster(client, files, model="gemma4:26b")
    assert groups == []


def test_numbered_medium_frames_are_one_ordered_group_before_llm_chunks():
    files = [
        SimpleNamespace(id=i, filename=f"{i:05}.jpg", path=f"/project/סרטון פנים/{i:05}.jpg")
        for i in range(28, 531)
    ]
    groups, placed = _numbered_frame_groups(files)
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 503
    assert [member["filename"] for member in groups[0]["members"]] == [file.filename for file in files]
    assert placed == {file.id for file in files}

    # A separate project directory cannot be swept into the sequence.
    other = SimpleNamespace(id=999, filename="00042.jpg", path="/other/00042.jpg")
    groups, placed = _numbered_frame_groups(files + [other])
    assert len(groups) == 1
    assert 999 not in placed


def test_relate_persists_one_group_for_503_frames_without_chunking_them(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'frames.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("donedatahoarder.core.relate.get_engine", lambda: engine)
    monkeypatch.setattr("donedatahoarder.db.session.get_engine", lambda: engine)
    with Session(engine) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        session_id = user.id
        db.add_all([
            File(session_id=session_id, path=str(tmp_path / "סרטון פנים" / f"{i:05}.jpg"),
                 filename=f"{i:05}.jpg", extension="jpg", status=FileStatus.ENRICHED)
            for i in range(28, 531)
        ])
        db.commit()

    class Client:
        def generate_json(self, *_args, **_kwargs):
            raise AssertionError("full sequence should not be split into LLM chunks")

        def generate(self, *_args, **_kwargs):
            raise AssertionError("full sequence should not enter cross-script grouping")

    result = relate(session_id, client=Client())
    assert result["groups"] == 1
    assert result["members"] == 503
    with Session(engine) as db:
        group = db.query(RelationGroup).filter_by(session_id=session_id).one()
        assert len(group.members) == 503
        assert group.reason.startswith("Numbered image sequence")


def test_unsupported_shx_stays_indexed_and_in_structural_companions_but_not_llm(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shx.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("donedatahoarder.core.relate.get_engine", lambda: engine)
    monkeypatch.setattr("donedatahoarder.db.session.get_engine", lambda: engine)
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        sid = owner.id
        for name, status, outcome, reason in (
            ("101.shx", FileStatus.SKIPPED, "skipped", "unsupported_type"),
            ("101.dwg", FileStatus.SKIPPED, "skipped", "unsupported_type"),
            ("101.bak", FileStatus.SKIPPED, "skipped", "unsupported_type"),
            ("notes.docx", FileStatus.ANALYZED, "content_verified", None),
            ("plan.pdf", FileStatus.ANALYZED, "content_verified", None),
        ):
            db.add(File(session_id=sid, path=str(tmp_path / name), filename=name,
                        extension=Path(name).suffix, status=status,
                        analysis_outcome=outcome, analysis_reason=reason))
        db.commit()

    prompts = []
    events = []

    class Client:
        def generate_json(self, prompt, **_kwargs):
            prompts.append(prompt)
            return []

    summary = relate(sid, client=Client(), progress_cb=events.append)
    assert len(prompts) == 1
    assert "101.shx" not in prompts[0]
    assert "101.dwg" in prompts[0] and "101.bak" in prompts[0]
    assert summary["backstop_groups"] == 1
    assert any(event["phase"] == "grouping" and event["chunk_done"] == 1
               and event["total"] is None and event["updated_utc"] for event in events)
    assert events[-1]["phase"] == "directory_complete"
    with Session(engine) as db:
        shx = db.query(File).filter_by(filename="101.shx").one()
        assert shx.status == FileStatus.SKIPPED
        assert shx.analysis_reason == "unsupported_type"
        group = db.query(RelationGroup).one()
        member_ids = {member.file_id for member in group.members}
        assert {row.filename for row in db.query(File).filter(File.id.in_(member_ids))} == {
            "101.shx", "101.dwg", "101.bak",
        }


def test_relate_emits_each_model_chunk_and_omits_unsupported_shx_from_cross_script(
        monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chunks.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("donedatahoarder.core.relate.get_engine", lambda: engine)
    monkeypatch.setattr("donedatahoarder.db.session.get_engine", lambda: engine)
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        sid = owner.id
        for number in range(202):
            name = f"note_{number:03}.txt"
            db.add(File(session_id=sid, path=str(tmp_path / name), filename=name,
                        extension=".txt", status=FileStatus.ANALYZED,
                        analysis_outcome="content_verified"))
        for number in range(6):
            name = f"font_{number:03}.shx"
            db.add(File(session_id=sid, path=str(tmp_path / name), filename=name,
                        extension=".shx", status=FileStatus.SKIPPED,
                        analysis_outcome="skipped", analysis_reason="unsupported_type"))
        db.commit()

    group_prompts = []
    cross_prompts = []
    events = []

    class Client:
        def generate_json(self, prompt, **_kwargs):
            group_prompts.append(prompt)
            return []

        def generate(self, prompt, **_kwargs):
            cross_prompts.append(prompt)
            return "[]"

    relate(sid, client=Client(), progress_cb=events.append)
    grouping = [event for event in events if event["phase"] == "grouping"]
    assert [(event["chunk_done"], event["chunk_active"]) for event in grouping] == [
        (0, 1), (1, None), (1, 2), (2, None), (2, 3), (3, None),
    ]
    assert all(event["chunk_total"] == 3 and event["done"] == 0 for event in grouping)
    assert len(group_prompts) == 3
    assert cross_prompts
    assert all(".shx" not in prompt for prompt in group_prompts + cross_prompts)


def test_relate_heartbeat_preserves_last_measured_progress(monkeypatch):
    module = importlib.import_module("donedatahoarder.core.relate")
    release = Event()

    def fake_relate(*, progress_cb, **_kwargs):
        progress_cb({"phase": "grouping", "done": 2, "directories": 3,
                     "directory_index": 3, "chunk_done": 0, "chunk_active": 1,
                     "chunk_total": 2, "groups": 4,
                     "updated_utc": "2026-01-01T00:00:00+00:00"})
        assert release.wait(5)
        progress_cb({"phase": "directory_complete", "done": 3,
                     "directories": 3, "groups": 5,
                     "updated_utc": "2026-01-01T00:00:03+00:00"})
        return {"directories": 3, "groups": 5}

    monkeypatch.setattr(module, "relate", fake_relate)
    monkeypatch.setattr("donedatahoarder.ai.router.get_client", lambda: None)
    progress = module.relate_with_progress("fixture-session")
    try:
        assert next(progress)["phase"] == "starting"
        measured = next(progress)
        assert measured["directories_done"] == 2
        assert measured["chunk_active"] == 1
        heartbeat = next(progress)
        assert heartbeat["heartbeat"] is True
        assert heartbeat["directories_done"] == 2
        assert heartbeat["groups"] == 4
        assert heartbeat["updated_utc"] == measured["updated_utc"]
        assert heartbeat["heartbeat_utc"] != measured["updated_utc"]
        release.set()
        complete = next(progress)
        assert complete["phase"] == "directory_complete"
        assert complete["directories_done"] == 3
        terminal = next(progress)
        assert terminal["done"] is True
        assert terminal["directories_done"] == 3
        assert terminal["groups"] == 5
    finally:
        release.set()
        progress.close()


def test_relate_rejects_content_identity_claim_without_matching_hashes():
    files = [
        SimpleNamespace(id=1, filename="1.jpg", path="/project/1.jpg", extension="jpg",
                        size_bytes=100, hash_sha256="a"),
        SimpleNamespace(id=2, filename="1.png", path="/project/1.png", extension="png",
                        size_bytes=110, hash_sha256="b"),
    ]

    class Client:
        def generate_json(self, _prompt, **_kwargs):
            return [{"label": "identical_image", "reason": "Identical image content in different raster formats.",
                     "members": ["1.jpg", "1.png"]}]

    assert _call_llm_for_group(Client(), "/project", files) == []


def test_cross_script_rejects_generic_numeric_names_across_formats():
    files = [
        SimpleNamespace(id=1, filename="10013543.png", path="/project/10013543.png"),
        SimpleNamespace(id=2, filename="22.dxf", path="/project/22.dxf"),
        SimpleNamespace(id=3, filename="00128.jpg", path="/project/00128.jpg"),
        SimpleNamespace(id=4, filename="00129.jpg", path="/project/00129.jpg"),
        SimpleNamespace(id=5, filename="2.png", path="/project/2.png"),
    ]

    class Client:
        def generate(self, _prompt, **_kwargs):
            return json.dumps([{"canonical_token": "numeric_sequence",
                                "filenames": [file.filename for file in files]}])

    assert _llm_cross_script_cluster(Client(), files) == []


def test_singleton_linkage_uses_bounded_indexes_and_earliest_match(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'singletons.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        groups = [RelationGroup(session_id=user.id, label=label, confidence=0.8,
                                dir_path=str(tmp_path))
                  for label in ("drawing_1080p", "project_archive", "project_108", "fonts_archive")]
        db.add_all(groups)
        db.flush()
        files = [File(session_id=user.id, path=str(tmp_path / name), filename=name,
                      status=FileStatus.ENRICHED)
                 for name in ("108_project_notes.txt", "108_random_notes.txt",
                              "font_notes.txt", "109_misc.txt")]
        db.add_all(files)
        db.commit()

        assert _link_singletons_to_folder_groups(db, user.id) == 3
        assignments = dict(db.query(File.filename, RelationMember.group_id)
                           .join(RelationMember, RelationMember.file_id == File.id).all())
        assert assignments == {
            "108_project_notes.txt": groups[1].id,  # first alpha match wins
            "108_random_notes.txt": groups[2].id,   # not drawing_1080p
            "font_notes.txt": groups[3].id,          # fonts -> font
        }


def test_relate_keeps_frame_companions_in_their_own_directories_on_rerun(tmp_path):
    engine = init_db(tmp_path / "relation.db")
    with Session(engine) as db:
        user = UserSession(root_path=str(tmp_path))
        db.add(user)
        db.flush()
        session_id = user.id
        for directory in ("project_A", "project_B"):
            parent = tmp_path / directory
            db.add_all([
                File(session_id=session_id, path=str(parent / f"{number:05}.jpg"),
                     filename=f"{number:05}.jpg", extension=".jpg",
                     status=FileStatus.ENRICHED)
                for number in range(28, 36)
            ])
            db.add(File(session_id=session_id, path=str(parent / "frame_notes.txt"),
                        filename="frame_notes.txt", extension=".txt",
                        status=FileStatus.ENRICHED))
        db.commit()

    class EmptyClient:
        def generate_json(self, *_args, **_kwargs):
            return []

        def generate(self, *_args, **_kwargs):
            return "[]"

    for _ in range(2):
        summary = relate(session_id, client=EmptyClient())
        assert summary == {"directories": 2, "groups": 2, "members": 18,
                           "llm_groups": 0, "backstop_groups": 2}
        with Session(engine) as db:
            companions = (db.query(File.path, RelationGroup.dir_path)
                          .join(RelationMember, RelationMember.file_id == File.id)
                          .join(RelationGroup, RelationGroup.id == RelationMember.group_id)
                          .filter(File.session_id == session_id,
                                  File.filename == "frame_notes.txt").all())
            assert len(companions) == 2
            assert all(str(Path(path).parent) == group_dir
                       for path, group_dir in companions)

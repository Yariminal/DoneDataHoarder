"""Bounded Ollama requests used by the relation grouping step."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from donedatahoarder.ai import ollama_client
from donedatahoarder.ai.json_utils import extract_json
from donedatahoarder.ai.ollama_client import OllamaClient
from donedatahoarder.core.relate import (
    _call_llm_for_group, _llm_cross_script_cluster, _numbered_frame_groups, relate,
)
from donedatahoarder.db.models import Base, File, FileStatus, RelationGroup, UserSession


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

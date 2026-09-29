"""A malformed model answer must never become saved file evidence."""
import json

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from donedatahoarder.ai.base_client import BaseAIClient
from donedatahoarder.ai.gemini_client import GeminiClient
from donedatahoarder.ai.json_utils import LooseDict, extract_json, generate_json_with_retry
from donedatahoarder.ai.ollama_client import OllamaClient
from donedatahoarder.analyzers import pipeline
from donedatahoarder.analyzers.document import DocumentAnalyzer
from donedatahoarder.analyzers.archive import ARCHIVE_PROMPT
from donedatahoarder.analyzers.document import DOC_PROMPT, PDF_VISION_PROMPT
from donedatahoarder.analyzers.image import VISION_PROMPT
from donedatahoarder.analyzers.threedmodel import THREED_PROMPT
from donedatahoarder.analyzers.video import AUDIO_PROMPT, VIDEO_PROMPT
from donedatahoarder.analyzers.response_schemas import (
    ArchiveAnalysisResponse, AudioAnalysisResponse, DocumentAnalysisResponse,
    ImageAnalysisResponse, ThreeDAnalysisResponse, VideoAnalysisResponse,
)
from donedatahoarder.db.models import File, FileStatus, Proposal, UserSession
from donedatahoarder.db.session import get_engine, init_db
from donedatahoarder.proposals.namer.core import generate_proposals


BASE = {
    "description": "A field survey report about a stone bridge.",
    "suggested_name": "stone_bridge_survey",
    "tags": ["stone_bridge", "survey"],
    "confidence": 0.8,
}
DOCUMENT = {**BASE, "document_type": "report", "detected_date": None, "language": "en"}


@pytest.mark.parametrize("model,fields", [
    (ImageAnalysisResponse, {"category": "photo_object", "detected_date": None}),
    (DocumentAnalysisResponse, {"document_type": "report", "detected_date": None, "language": None}),
    (VideoAnalysisResponse, {"video_type": "event", "detected_date": None}),
    (AudioAnalysisResponse, {"audio_type": "voice_memo", "detected_date": None}),
    (ArchiveAnalysisResponse, {"archive_type": "assets_pack", "detected_date": None}),
    (ThreeDAnalysisResponse, {"asset_type": "3d_prop", "software": None}),
])
def test_each_analyzer_contract_accepts_its_prompt_shape(model, fields):
    parsed = model.model_validate({**BASE, **fields})
    assert parsed.description == BASE["description"]


@pytest.mark.parametrize("prompt,model", [
    (VISION_PROMPT, ImageAnalysisResponse),
    (DOC_PROMPT, DocumentAnalysisResponse),
    (PDF_VISION_PROMPT, DocumentAnalysisResponse),
    (VIDEO_PROMPT, VideoAnalysisResponse),
    (AUDIO_PROMPT, AudioAnalysisResponse),
    (ARCHIVE_PROMPT, ArchiveAnalysisResponse),
    (THREED_PROMPT, ThreeDAnalysisResponse),
])
def test_prompt_examples_satisfy_the_response_contract(prompt, model):
    example = prompt.split("object in this shape, replacing the example values:\n", 1)[1]
    example = example.lstrip().replace("{{", "{").replace("}}", "}")
    data, _ = json.JSONDecoder().raw_decode(example)
    assert model.model_validate(data).model_dump() == data


@pytest.mark.parametrize("changes", [
    {},
    {"description": "   "},
    {"description": ["wrong type"]},
    {"suggested_name": None},
    {"tags": "stone_bridge, survey"},
    {"tags": ["stone_bridge", 3]},
    {"confidence": "0.8"},
    {"confidence": True},
    {"confidence": 1.2},
    {"confidence": float("nan")},
    {"detected_date": "2020-02-30"},
    {"detected_date": "unknown"},
    {"document_type": 7},
    {"language": 5},
    {"language": "unknown"},
])
def test_document_contract_rejects_missing_empty_and_wrong_types(changes):
    data = dict(DOCUMENT)
    if changes:
        data.update(changes)
    else:
        data = {}
    with pytest.raises(ValueError):
        DocumentAnalysisResponse.model_validate(data)


def test_response_policy_accepts_one_complete_object_and_preserves_generic_extraction():
    body = json.dumps(DOCUMENT)
    for raw in (body, f"```json\n{body}\n```", f"Here is the analysis:\n{body}"):
        result = generate_json_with_retry(lambda **_: raw, "request", DocumentAnalysisResponse)
        assert result.model_dump() == DOCUMENT
    # Relate and organizer still use the pre-existing permissive extraction.
    assert extract_json('{"a": 1} {"b": 2}') == {"a": 1}
    assert generate_json_with_retry(
        lambda **_: '{"a": 1} {"b": 2}', "request", LooseDict,
    ).model_dump() == {"a": 1}


@pytest.mark.parametrize("provider", ["base", "ollama", "gemini"])
def test_typed_provider_keeps_validated_object_even_with_list_key(provider, monkeypatch):
    class TypedWithExtra(BaseModel):
        model_config = ConfigDict(extra="allow")
        description: str

    raw = '{"description":"validated","_list":{}}'
    if provider == "base":
        class Stub:
            def generate(self, _prompt, **_kwargs):
                return raw

        generate_json = lambda model_cls: BaseAIClient.generate_json(
            Stub(), "request", model_cls=model_cls,
        )
    elif provider == "ollama":
        client = OllamaClient(text_model="test", vision_model="test")
        monkeypatch.setattr(client, "generate", lambda _prompt, **_kwargs: raw)
        generate_json = lambda model_cls: client.generate_json("request", model_cls=model_cls)
    else:
        client = object.__new__(GeminiClient)  # no SDK or external service needed
        monkeypatch.setattr(client, "generate", lambda _prompt, **_kwargs: raw)
        generate_json = lambda model_cls: client.generate_json("request", model_cls=model_cls)

    assert generate_json(TypedWithExtra) == {"description": "validated", "_list": {}}
    # The generic array contract still unwraps its internal list box.
    if provider == "base":
        Stub.generate = lambda self, _prompt, **_kwargs: '[1,2]'
        assert generate_json(LooseDict) == [1, 2]
    else:
        monkeypatch.setattr(client, "generate", lambda _prompt, **_kwargs: '[1,2]')
        assert generate_json(LooseDict) == [1, 2]


def test_analysis_schema_error_retries_to_valid_answer(monkeypatch):
    monkeypatch.setattr("donedatahoarder.ai.json_utils.time.sleep", lambda _: None)
    answers = ["{}", json.dumps(DOCUMENT)]
    calls = []

    def answer(**kwargs):
        calls.append(kwargs)
        return answers.pop(0)

    result = generate_json_with_retry(answer, "Analyze source", DocumentAnalysisResponse)
    assert result.description == DOCUMENT["description"]
    assert len(calls) == 2
    assert "field description" in calls[1]["prompt"]


@pytest.mark.parametrize("raw", [
    "{}",
    json.dumps({key: val for key, val in DOCUMENT.items() if key != "description"}),
    json.dumps({**DOCUMENT, "tags": "stone_bridge"}),
    json.dumps({**DOCUMENT, "confidence": "0.8"}),
    json.dumps({**DOCUMENT, "description": "   "}),
    json.dumps(DOCUMENT)[:-1],
    json.dumps(DOCUMENT) + ' {"second":',
    json.dumps(DOCUMENT) + ' {"second": true}',
    json.dumps(DOCUMENT) + " trailing prose.",
    json.dumps({**DOCUMENT, "_list": {}}),
    json.dumps({**DOCUMENT, "date": 2020}),
    json.dumps({**DOCUMENT, "transcript": {"fake": "source"}}),
    '{"description":"one","description":"two","suggested_name":"x","tags":[],"confidence":0.5,"document_type":"report","detected_date":null,"language":null}',
])
def test_bad_analysis_responses_exhaust_retries_without_validation(raw, monkeypatch):
    monkeypatch.setattr("donedatahoarder.ai.json_utils.time.sleep", lambda _: None)
    calls = []

    def answer(**kwargs):
        calls.append(kwargs)
        return raw

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        generate_json_with_retry(answer, "Analyze source", DocumentAnalysisResponse)
    assert len(calls) == 3
    assert all("Analyze source" in call["prompt"] for call in calls)


@pytest.mark.parametrize("raw", [
    "{}",
    json.dumps({key: val for key, val in DOCUMENT.items() if key != "description"}),
    json.dumps({**DOCUMENT, "description": "  "}),
    json.dumps({**DOCUMENT, "tags": "stone_bridge"}),
    json.dumps(DOCUMENT)[:-1],
    json.dumps(DOCUMENT) + ' {"second":',
    json.dumps({**DOCUMENT, "_list": {}}),
    json.dumps({**DOCUMENT, "date": 2020}),
    json.dumps({**DOCUMENT, "transcript": {"fake": "source"}}),
])
def test_invalid_document_response_persists_failure_without_evidence(tmp_path, monkeypatch, raw):
    monkeypatch.setattr("donedatahoarder.ai.json_utils.time.sleep", lambda _: None)
    init_db(tmp_path / "index.db")
    source = tmp_path / "bridge.txt"
    source.write_text("Stone bridge field survey with measurements and observations.\n" * 3)
    with Session(get_engine()) as db:
        user = UserSession(root_path=str(tmp_path), name="response-contract")
        db.add(user)
        db.flush()
        file = File(
            session_id=user.id, path=str(source), filename=source.name,
            extension=".txt", mime_type="text/plain", size_bytes=source.stat().st_size,
            status=FileStatus.ENRICHED,
        )
        db.add(file)
        db.commit()
        file_id, session_id = file.id, user.id

    client = OllamaClient(text_model="test-model", vision_model="test-model")
    calls = []

    def answer(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return raw

    monkeypatch.setattr(client, "generate", answer)
    _, status, error = pipeline._process_one_file(
        file_id, get_engine(), [DocumentAnalyzer(client)], client, set(), use_cache=False,
    )
    assert status == "error"
    assert "Failed to generate valid JSON after 3 attempts" in error
    assert len(calls) == 3
    with Session(get_engine()) as db:
        file = db.get(File, file_id)
        assert file.status == FileStatus.ERROR
        assert file.analysis_outcome == "failed"
        assert file.analysis_reason == "provider_invalid_response"
        assert file.analyzed_at is None
        assert file.ai_description is None
        assert file.ai_suggested_name is None
        assert file.ai_tags is None
        assert file.ai_confidence is None
        assert file.ai_model is None
        assert file.analysis_detected_date is None
    generate_proposals(session_id=session_id)
    with Session(get_engine()) as db:
        assert db.query(Proposal).filter(Proposal.file_id == file_id).count() == 0

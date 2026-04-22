"""
Unit tests for datahoarder.ai.json_utils — deliberately bad JSON recovery.
"""
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from datahoarder.ai.json_utils import (
    extract_json,
    generate_json_with_retry,
    validate_json,
    _fix_json_escapes,
    _strip_markdown_fences,
)


class DummySchema(BaseModel):
    name: str
    count: int


# ---------------------------------------------------------------------------
# _fix_json_escapes
# ---------------------------------------------------------------------------

def test_fix_json_escapes_windows_path():
    raw = r'{"path": "LOGOS\תנורים"}'  # single backslash before Hebrew — invalid JSON escape
    fixed = _fix_json_escapes(raw)
    # Must now parse after fix
    parsed = json.loads(fixed)
    assert "תנורים" in parsed["path"]


def test_fix_json_escapes_valid_escapes_preserved():
    raw = '{"msg": "line1\\nline2\\ttab"}'
    fixed = _fix_json_escapes(raw)
    # Valid escapes (\n, \t) must stay single-backslash
    assert fixed == raw
    parsed = json.loads(fixed)
    assert parsed["msg"] == "line1\nline2\ttab"


# ---------------------------------------------------------------------------
# _strip_markdown_fences
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('```json\n{"a":1}\n```', '{"a":1}'),
        ('```\n{"a":1}\n```', '{"a":1}'),
        ('```json\n[1,2,3]\n```', '[1,2,3]'),
        ('{"a":1}', '{"a":1}'),  # no fences
        ('  ```json\n{"a":1}\n```  ', '{"a":1}'),  # surrounding whitespace
    ],
)
def test_strip_markdown_fences(raw, expected):
    assert _strip_markdown_fences(raw) == expected


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"name": "test", "count": 42}') == {"name": "test", "count": 42}

    def test_plain_array(self):
        assert extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_with_markdown_fence(self):
        assert extract_json('```json\n{"name": "test"}\n```') == {"name": "test"}

    def test_with_extra_text(self):
        raw = 'Sure! Here is the JSON:\n\n{"name": "test", "count": 42}\nHope that helps!'
        assert extract_json(raw) == {"name": "test", "count": 42}

    def test_with_array_and_extra_text(self):
        raw = 'Results:\n[{"id":1},{"id":2}]\nDone.'
        assert extract_json(raw) == [{"id": 1}, {"id": 2}]

    def test_broken_escapes_fixed(self):
        raw = r'{"path": "C:\\Users\\test"}'
        assert extract_json(raw)["path"] == "C:\\Users\\test"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            extract_json("This is not JSON at all")

    def test_nested_object_with_noise(self):
        raw = 'blabla {"outer": {"inner": [1,2,3]}} more blabla'
        assert extract_json(raw) == {"outer": {"inner": [1, 2, 3]}}

    def test_multiple_objects_takes_first(self):
        raw = '{"a":1} {"b":2}'
        assert extract_json(raw) == {"a": 1}


# ---------------------------------------------------------------------------
# validate_json
# ---------------------------------------------------------------------------

def test_validate_json_success():
    data = {"name": "hello", "count": 5}
    result = validate_json(data, DummySchema)
    assert result.name == "hello"
    assert result.count == 5


def test_validate_json_failure():
    data = {"name": "hello"}  # missing count
    with pytest.raises(ValueError) as exc_info:
        validate_json(data, DummySchema)
    assert "validation failed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# generate_json_with_retry
# ---------------------------------------------------------------------------

class TestGenerateJsonWithRetry:
    def test_success_on_first_try(self):
        def fake_generate(**kwargs):
            return '{"name": "ok", "count": 1}'

        result = generate_json_with_retry(
            generate_fn=fake_generate,
            prompt="test",
            model_cls=DummySchema,
            max_retries=3,
        )
        assert result.name == "ok"
        assert result.count == 1

    def test_retries_on_bad_json_then_succeeds(self):
        attempts = []

        def fake_generate(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                return "not json"
            return '{"name": "recovered", "count": 99}'

        result = generate_json_with_retry(
            generate_fn=fake_generate,
            prompt="test",
            model_cls=DummySchema,
            max_retries=3,
        )
        assert result.name == "recovered"
        assert len(attempts) == 3
        # Temperature should increase each retry
        assert attempts[0]["temperature"] == 0.0
        assert attempts[1]["temperature"] == 0.1
        assert attempts[2]["temperature"] == 0.2

    def test_retries_on_validation_error_then_succeeds(self):
        attempts = []

        def fake_generate(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 2:
                return '{"name": "ok"}'  # missing count → ValidationError
            return '{"name": "ok", "count": 2}'

        result = generate_json_with_retry(
            generate_fn=fake_generate,
            prompt="test",
            model_cls=DummySchema,
            max_retries=3,
        )
        assert result.count == 2
        assert len(attempts) == 2

    def test_exhaustion_raises_runtime_error(self):
        def fake_generate(**kwargs):
            return "totally broken"

        with pytest.raises(RuntimeError) as exc_info:
            generate_json_with_retry(
                generate_fn=fake_generate,
                prompt="test",
                model_cls=DummySchema,
                max_retries=2,
            )
        assert "Failed to generate valid JSON after 2 attempts" in str(exc_info.value)

    def test_response_format_forwarded(self):
        received = {}

        def fake_generate(**kwargs):
            received.update(kwargs)
            return '{"name": "ok", "count": 1}'

        generate_json_with_retry(
            generate_fn=fake_generate,
            prompt="test",
            model_cls=DummySchema,
            response_format={"type": "json_object"},
        )
        assert received.get("response_format") == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Edge cases: Hebrew / Unicode
# ---------------------------------------------------------------------------

def test_hebrew_in_json():
    raw = '{"title": "תפריט אירוע", "count": 3}'
    result = extract_json(raw)
    assert result["title"] == "תפריט אירוע"


def test_hebrew_with_broken_escapes():
    raw = r'{"path": "C:\\tmp\\אירועים\\file.pdf"}'
    result = extract_json(raw)
    assert "אירועים" in result["path"]

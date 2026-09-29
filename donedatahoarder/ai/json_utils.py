"""
Shared JSON extraction utilities for AI clients.

Provides:
- Pydantic model validation for all structured LLM outputs.
- Retry loop with exponential backoff and increasing temperature on failure.
- Robust JSON extraction from model responses (markdown fences, escaped chars, partial JSON).
- Proper use of response_format={"type": "json_object"} for backends that support it.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Callable, Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

T = TypeVar("T", bound=BaseModel)


class LooseDict(BaseModel):
    """Accepts any JSON object — validates as a dict with arbitrary keys."""
    model_config = ConfigDict(extra="allow")


MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds
MAX_KEY_SEPARATOR_REPAIRS = 4
logger = logging.getLogger(__name__)


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Do not silently accept last-wins fields in an AI response."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> Any:
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _loads_unique(text: str, *, allow_control_chars: bool) -> Any:
    return json.loads(
        text,
        strict=not allow_control_chars,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_non_json_constant,
    )


def _parse_with_key_separator_repair(
    text: str, *, allow_control_chars: bool
) -> tuple[Any, int]:
    """Replace only a comma where the JSON decoder expects an object-key colon.

    The decoder supplies the grammar position; this never changes a value,
    array separator, or quoted text. The entire result must then parse, and
    callers still validate it against their output schema.
    """
    repairs = 0
    while True:
        try:
            return _loads_unique(text, allow_control_chars=allow_control_chars), repairs
        except json.JSONDecodeError as exc:
            if (
                repairs >= MAX_KEY_SEPARATOR_REPAIRS
                or exc.msg != "Expecting ':' delimiter"
                or exc.pos >= len(text)
                or text[exc.pos] != ","
            ):
                raise
            text = text[:exc.pos] + ":" + text[exc.pos + 1:]
            repairs += 1


def _fix_json_escapes(text: str) -> str:
    """
    Fix common JSON escape errors produced by LLMs.

    LLMs often produce Windows-style backslashes in paths (e.g. LOGOS\\תנורים)
    which are invalid JSON escapes. We escape any backslash that isn't part of
    a valid JSON escape sequence.
    """
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) from response text."""
    text = text.strip()
    if text.startswith("```"):
        # Split on ``` and take the middle chunk
        parts = text.split("```", 2)
        if len(parts) >= 3:
            inner = parts[1]
            if inner.lower().startswith("json"):
                inner = inner[4:]
            return inner.strip()
        # Fallback: single fence case
        text = text.lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.lstrip("`").strip()
    return text


def _extract_json_object_or_array(text: str) -> Optional[str]:
    """
    Extract the first complete outer JSON object or array from raw text.

    Never treat a nested container as an independent response when its outer
    container is malformed or truncated.
    """
    obj_start = text.find("{")
    arr_start = text.find("[")
    starts = [start for start in (obj_start, arr_start) if start != -1]
    if not starts:
        return None
    start = min(starts)
    stack: list[str] = []
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in ("{", "["):
            stack.append(ch)
        elif ch in ("}", "]"):
            if not stack or (stack[-1], ch) not in (("{", "}"), ("[", "]")):
                return None
            stack.pop()
            if not stack:
                return text[start : i + 1]
    return None


def extract_json(
    raw: str,
    fix_escapes: bool = True,
    allow_control_chars: bool = False,
) -> Any:
    """
    Extract a Python dict/list from a raw LLM response string.

    Strategy:
      1. Strip markdown fences.
      2. Try json.loads directly.
      3. Try with escape fixing.
      4. Try extracting the first JSON object/array substring.
      5. Try extraction + escape fixing.
      6. For a complete response only, replace a bounded number of commas
         at decoder-confirmed object-key colon positions.

    allow_control_chars tolerates literal control characters inside JSON
    strings when requested by a caller that validates extracted values against
    an exact allowlist. It does not alter those characters.

    Returns:
      Parsed JSON (dict/list) or raises ValueError if unrecoverable.
    """
    data, _ = _extract_json_with_repair_count(
        raw, fix_escapes=fix_escapes, allow_control_chars=allow_control_chars
    )
    return data


def _extract_json_with_repair_count(
    raw: str, *, fix_escapes: bool, allow_control_chars: bool
) -> tuple[Any, int]:
    cleaned = _strip_markdown_fences(raw)

    attempts = [cleaned]
    if fix_escapes:
        attempts.append(_fix_json_escapes(cleaned))

    last_decode_error: Optional[json.JSONDecodeError] = None
    for text in attempts:
        try:
            return _loads_unique(text, allow_control_chars=allow_control_chars), 0
        except json.JSONDecodeError as exc:
            last_decode_error = exc
        except _DuplicateKeyError as exc:
            raise ValueError(str(exc)) from exc

    # Try extracting a JSON substring
    for text in attempts:
        snippet = _extract_json_object_or_array(text)
        if snippet:
            try:
                return _loads_unique(snippet, allow_control_chars=allow_control_chars), 0
            except json.JSONDecodeError as exc:
                last_decode_error = exc
            except _DuplicateKeyError as exc:
                raise ValueError(str(exc)) from exc

    # Only repair a complete response, never an extracted prefix of a damaged
    # response. A valid envelope with unrelated trailing errors must retry.
    try:
        return _parse_with_key_separator_repair(
            cleaned, allow_control_chars=allow_control_chars
        )
    except json.JSONDecodeError as exc:
        last_decode_error = exc
    except _DuplicateKeyError as exc:
        raise ValueError(str(exc)) from exc

    raise ValueError(f"Could not extract valid JSON from response: {raw[:500]!r}") from last_decode_error


def validate_json(data: Any, model_cls: Type[T]) -> T:
    """Validate a parsed JSON dict/list against a Pydantic model."""
    # LooseDict expects a dict, but some LLM calls (e.g., relate) return
    # a JSON array. Wrap lists transparently so validation passes.
    if model_cls is LooseDict and isinstance(data, list):
        data = {"_list": data}
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"JSON validation failed: {exc}") from exc


def generate_json_with_retry(
    generate_fn: Callable[..., str],
    prompt: str,
    model_cls: Type[T],
    system: Optional[str] = None,
    temperature: float = 0.0,
    seed: int = 42,
    max_retries: int = MAX_RETRIES,
    response_format: Optional[dict[str, str]] = None,
    **generate_kwargs: Any,
) -> T:
    """
    Call an LLM with a prompt, extract JSON, validate against a Pydantic model,
    and retry with increasing temperature on failure.

    Args:
        generate_fn: Callable that takes (prompt, system, temperature, seed, ...)
                     and returns a raw string response.
        prompt: The user prompt. A JSON-only instruction is appended automatically.
        model_cls: Pydantic BaseModel subclass defining the expected schema.
        system: Optional system prompt.
        temperature: Starting temperature (increases by 0.1 each retry).
        seed: Fixed seed for determinism.
        max_retries: Maximum attempts before giving up.
        response_format: Optional dict like {"type": "json_object"} to pass to
                         the underlying API if supported (Gemini, Ollama with structured outputs).
        **generate_kwargs: Extra arguments forwarded to generate_fn.

    Returns:
        Validated Pydantic model instance.

    Raises:
        RuntimeError: if all retries are exhausted.
    """
    json_instruction = (
        "\n\nYou MUST respond with valid JSON only. "
        "No markdown, no explanation, no code fences. Just raw JSON."
    )
    full_prompt = prompt + json_instruction

    last_error: Optional[Exception] = None
    retry_feedback = ""
    for attempt in range(max_retries):
        current_temp = round(temperature + attempt * 0.1, 2)
        current_seed = seed + attempt if seed is not None else None

        try:
            kwargs = dict(generate_kwargs)
            if response_format is not None:
                kwargs["response_format"] = response_format

            raw = generate_fn(
                prompt=full_prompt + retry_feedback,
                system=system,
                temperature=current_temp,
                seed=current_seed,
                **kwargs,
            )
            data, repairs = _extract_json_with_repair_count(
                raw, fix_escapes=True, allow_control_chars=False
            )
            validated = validate_json(data, model_cls)
            if repairs:
                logger.info(
                    "Validated AI JSON with %d mechanical object-key separator repair(s)",
                    repairs,
                )
            return validated
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            cause = exc.__cause__
            if isinstance(cause, json.JSONDecodeError):
                problem = f"invalid JSON syntax: {cause.msg} at line {cause.lineno}, column {cause.colno}"
            elif isinstance(cause, ValidationError):
                first = cause.errors(include_url=False)[0]
                field = ".".join(str(part) for part in first["loc"]) or "root"
                problem = f"field {field}: {first['msg']}"
            else:
                problem = "invalid JSON or a value that does not match the required fields"
            retry_feedback = (
                f"\n\nYour previous response had {problem}. Generate a fresh, complete "
                "JSON response for the original request. Put a colon after every key, "
                "use commas only between entries, and keep the required field types."
            )
            if attempt + 1 < max_retries:
                delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)

    raise RuntimeError(
        f"Failed to generate valid JSON after {max_retries} attempts. "
        f"Last error: {last_error}"
    ) from last_error

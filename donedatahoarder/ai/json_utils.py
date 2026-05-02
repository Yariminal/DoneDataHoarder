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
    Extract the first well-formed JSON object or array from raw text.
    Returns the substring or None if nothing found.
    """
    # Try to find an object
    obj_start = text.find("{")
    arr_start = text.find("[")

    candidates = []
    if obj_start != -1:
        candidates.append((obj_start, "obj"))
    if arr_start != -1:
        candidates.append((arr_start, "arr"))

    if not candidates:
        return None

    # Prefer whichever starts first
    candidates.sort(key=lambda x: x[0])

    for start, kind in candidates:
        # Brute-force find matching end brace/bracket
        stack = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if not in_string:
                if ch in ("{", "["):
                    stack += 1
                elif ch in ("}", "]"):
                    stack -= 1
                    if stack == 0:
                        return text[start : i + 1]
    return None


def extract_json(raw: str, fix_escapes: bool = True) -> Any:
    """
    Extract a Python dict/list from a raw LLM response string.

    Strategy:
      1. Strip markdown fences.
      2. Try json.loads directly.
      3. Try with escape fixing.
      4. Try extracting the first JSON object/array substring.
      5. Try extraction + escape fixing.

    Returns:
      Parsed JSON (dict/list) or raises ValueError if unrecoverable.
    """
    cleaned = _strip_markdown_fences(raw)

    attempts = [cleaned]
    if fix_escapes:
        attempts.append(_fix_json_escapes(cleaned))

    for text in attempts:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Try extracting a JSON substring
    for text in attempts:
        snippet = _extract_json_object_or_array(text)
        if snippet:
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not extract valid JSON from response: {raw[:500]!r}")


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
    for attempt in range(max_retries):
        current_temp = round(temperature + attempt * 0.1, 2)
        current_seed = seed + attempt if seed is not None else None

        try:
            kwargs = dict(generate_kwargs)
            if response_format is not None:
                kwargs["response_format"] = response_format

            raw = generate_fn(
                prompt=full_prompt,
                system=system,
                temperature=current_temp,
                seed=current_seed,
                **kwargs,
            )
            data = extract_json(raw)
            return validate_json(data, model_cls)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(delay)
            continue

    raise RuntimeError(
        f"Failed to generate valid JSON after {max_retries} attempts. "
        f"Last error: {last_error}"
    ) from last_error

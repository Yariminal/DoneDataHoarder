"""
Gemini client — optional cloud AI backend (Google Gemini 2.0 Flash / Pro).

Usage requires:  pip install google-generativeai
and setting:     GEMINI_API_KEY environment variable (or passing api_key=).
"""
import base64
import json
import os
from pathlib import Path
from typing import Optional

try:
    import google.generativeai as genai
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ):
        if not _HAS_GENAI:
            raise ImportError(
                "google-generativeai is not installed. "
                "Run: pip install google-generativeai"
            )
        self.model_name = model
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError(
                "Gemini API key not found. "
                "Set GEMINI_API_KEY env var or pass api_key=."
            )
        genai.configure(api_key=key)
        self._model = genai.GenerativeModel(model)

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        cfg = genai.types.GenerationConfig(temperature=temperature)
        resp = self._model.generate_content(prompt, generation_config=cfg)
        return resp.text.strip()

    def generate_with_image(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
        temperature: float = 0.2,
    ) -> str:
        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is None:
            raise ValueError("Either image_path or image_bytes must be provided")

        part = {"mime_type": mime_type, "data": image_bytes}
        cfg = genai.types.GenerationConfig(temperature=temperature)
        resp = self._model.generate_content([prompt, part], generation_config=cfg)
        return resp.text.strip()

    def generate_json(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
    ) -> dict:
        json_instruction = (
            "\n\nRespond with valid JSON only. "
            "No markdown code fences, no explanation. Raw JSON."
        )
        full_prompt = prompt + json_instruction

        if image_path or image_bytes:
            raw = self.generate_with_image(
                full_prompt, image_path=image_path,
                image_bytes=image_bytes, mime_type=mime_type,
            )
        else:
            raw = self.generate(full_prompt)

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            return {"raw_response": raw}

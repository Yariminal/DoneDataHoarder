"""
Gemini client — optional cloud AI backend (Google Gemini 2.0 Flash / Pro).

Usage requires:  pip install google-generativeai
and setting:     GEMINI_API_KEY environment variable (or passing api_key=).
"""
import json
import os
from pathlib import Path
from typing import Optional, Any

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
        images_list: list[bytes] | None = None,
        mime_type: str = "image/jpeg",
        temperature: float = 0.2,
    ) -> str:
        parts: list[Any] = [prompt]

        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is not None:
            parts.append({"mime_type": mime_type, "data": image_bytes})

        if images_list:
            for img in images_list:
                parts.append({"mime_type": mime_type, "data": img})

        if len(parts) < 2:
            raise ValueError("At least one image must be provided")

        cfg = genai.types.GenerationConfig(temperature=temperature)
        resp = self._model.generate_content(parts, generation_config=cfg)
        return resp.text.strip()

    def generate_json(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        images_list: list[bytes] | None = None,
        mime_type: str = "image/jpeg",
    ) -> dict:
        json_instruction = (
            "\n\nRespond with valid JSON only. "
            "No markdown code fences, no explanation. Raw JSON."
        )
        full_prompt = prompt + json_instruction

        if image_path or image_bytes or images_list:
            raw = self.generate_with_image(
                full_prompt, image_path=image_path,
                image_bytes=image_bytes, images_list=images_list,
                mime_type=mime_type,
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

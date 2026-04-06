"""
Ollama client — talks to a local Ollama instance.

Supports:
- Text-only generation (llama3, mistral, gemma, etc.)
- Vision generation (llava, bakllava, gemma3, etc.) with base64 images

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import base64
import json
from pathlib import Path
from typing import Optional

import httpx

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TEXT_MODEL = "gemma3:12b"
DEFAULT_VISION_MODEL = "gemma3:12b"  # gemma3 is multimodal
TIMEOUT = 120  # seconds — vision inference can be slow


class OllamaClient:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        text_model: str = DEFAULT_TEXT_MODEL,
        vision_model: str = DEFAULT_VISION_MODEL,
    ):
        self.host = host.rstrip("/")
        self.text_model = text_model
        self.vision_model = vision_model
        self._client = httpx.Client(timeout=TIMEOUT)

    # ------------------------------------------------------------------
    # Connectivity check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            resp = self._client.get(f"{self.host}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            resp = self._client.get(f"{self.host}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Text generation
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        """Send a text prompt, return the response string."""
        model = model or self.text_model
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        resp = self._client.post(f"{self.host}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    # ------------------------------------------------------------------
    # Vision generation
    # ------------------------------------------------------------------

    def generate_with_image(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        """Send a prompt + image, return the response string."""
        model = model or self.vision_model

        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is None:
            raise ValueError("Either image_path or image_bytes must be provided")

        b64 = base64.b64encode(image_bytes).decode()

        payload: dict = {
            "model": model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        resp = self._client.post(f"{self.host}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    # ------------------------------------------------------------------
    # Structured JSON extraction helper
    # ------------------------------------------------------------------

    def generate_json(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
    ) -> dict:
        """
        Like generate / generate_with_image but instructs the model to return
        valid JSON and parses the result.  Falls back to raw text on parse error.
        """
        json_instruction = (
            "\n\nYou MUST respond with valid JSON only. "
            "No markdown, no explanation, no code fences. Just raw JSON."
        )
        full_prompt = prompt + json_instruction

        if image_path or image_bytes:
            raw = self.generate_with_image(
                full_prompt, image_path=image_path, image_bytes=image_bytes,
                model=model, system=system,
            )
        else:
            raw = self.generate(full_prompt, model=model, system=system)

        # Strip any accidental markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Last resort: try to extract the first {...} block
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            return {"raw_response": raw}

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

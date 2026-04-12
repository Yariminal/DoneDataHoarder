"""
Ollama client — talks to a local Ollama instance.

Supports:
- Text-only generation (llama3, mistral, gemma, etc.)
- Vision generation (llava, bakllava, gemma3, etc.) with base64 images

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import base64
import json
import re
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
        # NOTE: We create a fresh httpx client per request (via _request)
        # instead of sharing one across threads. A shared httpx.Client
        # causes connection pool deadlocks when multiple threads hit
        # Ollama concurrently — Ollama processes requests sequentially,
        # so queued requests block and the connection pool fills up,
        # causing all threads to hang indefinitely.

    # ------------------------------------------------------------------
    # Connectivity check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.host}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.host}/api/tags")
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

        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(f"{self.host}/api/generate", json=payload)
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
        images_list: list[bytes] | None = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        """Send a prompt + one or more images, return the response string."""
        model = model or self.vision_model

        b64_images = []

        # Single image from path or bytes
        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is not None:
            b64_images.append(base64.b64encode(image_bytes).decode())

        # Multiple images
        if images_list:
            for img in images_list:
                b64_images.append(base64.b64encode(img).decode())

        if not b64_images:
            raise ValueError("At least one image must be provided")

        payload: dict = {
            "model": model,
            "prompt": prompt,
            "images": b64_images,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(f"{self.host}/api/generate", json=payload)
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
        images_list: list[bytes] | None = None,
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

        if image_path or image_bytes or images_list:
            raw = self.generate_with_image(
                full_prompt, image_path=image_path, image_bytes=image_bytes,
                images_list=images_list, model=model, system=system,
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

        # LLMs often produce Windows-style backslashes in paths (e.g. LOGOS\תנורים)
        # which are invalid JSON escapes. Fix them before parsing.
        def _fix_json(text: str) -> str:
            return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)

        for attempt_raw in (raw, _fix_json(raw)):
            try:
                return json.loads(attempt_raw)
            except json.JSONDecodeError:
                pass

        # Try to extract a JSON array [...] or object {...}
        for attempt_raw in (raw, _fix_json(raw)):
            arr_start = attempt_raw.find("[")
            arr_end = attempt_raw.rfind("]")
            if arr_start != -1 and arr_end != -1:
                try:
                    return json.loads(attempt_raw[arr_start : arr_end + 1])
                except json.JSONDecodeError:
                    pass
            start = attempt_raw.find("{")
            end = attempt_raw.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(attempt_raw[start : end + 1])
                except json.JSONDecodeError:
                    pass

        return {"raw_response": raw}

    def close(self):
        pass  # No shared client to close

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

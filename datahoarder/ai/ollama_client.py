"""
Ollama client — talks to a local Ollama instance.

Supports:
- Text-only generation (llama3, mistral, gemma, etc.)
- Vision generation (llava, bakllava, gemma3, etc.) with base64 images
- Structured JSON output with retry logic via json_utils

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import base64
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from datahoarder.ai.circuit_breaker import CircuitBreaker
from datahoarder.ai.json_utils import generate_json_with_retry

DEFAULT_HOST = "http://localhost:11434"

# Ollama processes LLM requests serially. This lock prevents multiple threads
# from sending concurrent requests — they'd just queue inside Ollama and the
# extra connections waste memory. Pre-processing (text extraction, Whisper,
# image resize) still runs in parallel across worker threads; only the actual
# HTTP call to Ollama is serialised here.
_OLLAMA_REQUEST_LOCK = threading.Semaphore(1)
DEFAULT_TEXT_MODEL = "gemma3:12b"
DEFAULT_VISION_MODEL = "gemma3:12b"  # gemma3 is multimodal
TIMEOUT = 120  # seconds — vision inference can be slow


class OllamaClient:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        text_model: str = DEFAULT_TEXT_MODEL,
        vision_model: str = DEFAULT_VISION_MODEL,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ):
        self.host = host.rstrip("/")
        self.text_model = text_model
        self.vision_model = vision_model
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout,
        )
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
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Send a text prompt, return the response string."""
        if not self.circuit_breaker.is_healthy():
            raise RuntimeError(
                f"Ollama backend is unhealthy: circuit breaker is {self.circuit_breaker.state}. "
                f"Run 'datahoarder doctor' to diagnose, or switch to Gemini backend."
            )
        try:
            return self._do_generate(
                prompt, model, system, temperature, seed, **kwargs
            )
        except Exception:
            self.circuit_breaker.record_failure()
            raise

    def _do_generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Internal: actual HTTP call for text generation."""
        model = model or self.text_model
        options: dict = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if system:
            payload["system"] = system

        # Ollama structured-output support: map response_format -> format
        response_format = kwargs.get("response_format")
        if response_format is not None:
            # Ollama accepts "format": "json" or a JSON schema dict
            fmt = response_format.get("type")
            if fmt == "json_object":
                payload["format"] = "json"
            elif isinstance(response_format, dict):
                payload["format"] = response_format

        with _OLLAMA_REQUEST_LOCK:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.post(f"{self.host}/api/generate", json=payload)
                resp.raise_for_status()
                result = resp.json().get("response", "").strip()
                self.circuit_breaker.record_success()
                return result

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
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Send a prompt + one or more images, return the response string."""
        if not self.circuit_breaker.is_healthy():
            raise RuntimeError(
                f"Ollama backend is unhealthy: circuit breaker is {self.circuit_breaker.state}. "
                f"Run 'datahoarder doctor' to diagnose, or switch to Gemini backend."
            )
        try:
            return self._do_generate_with_image(
                prompt, image_path, image_bytes, images_list,
                model, system, temperature, seed, **kwargs
            )
        except Exception:
            self.circuit_breaker.record_failure()
            raise

    def _do_generate_with_image(
        self,
        prompt: str,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        images_list: list[bytes] | None = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Internal: actual HTTP call for vision generation."""
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

        options: dict = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "images": b64_images,
            "stream": False,
            "options": options,
        }
        if system:
            payload["system"] = system

        # Ollama structured-output support: map response_format -> format
        response_format = kwargs.get("response_format")
        if response_format is not None:
            fmt = response_format.get("type")
            if fmt == "json_object":
                payload["format"] = "json"
            elif isinstance(response_format, dict):
                payload["format"] = response_format

        with _OLLAMA_REQUEST_LOCK:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.post(f"{self.host}/api/generate", json=payload)
                resp.raise_for_status()
                result = resp.json().get("response", "").strip()
                self.circuit_breaker.record_success()
                return result

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
        temperature: float = 0.0,
        seed: int = 42,
        max_retries: int = 3,
    ) -> dict:
        """
        Like generate / generate_with_image but instructs the model to return
        valid JSON and parses the result.  Uses shared json_utils with retry.

        Defaults to temperature=0.0 + seed=42 for deterministic structured output.
        This makes repeated calls with the same input produce the same JSON.
        """
        from pydantic import create_model

        # Build a loose Pydantic model that accepts any dict keys so we can
        # validate JSON structure without requiring every caller to supply a schema.
        LooseDict = create_model("LooseDict", __base__=dict)

        if image_path or image_bytes or images_list:
            generate_fn = lambda **kw: self.generate_with_image(
                kw.pop("prompt", prompt),
                image_path=image_path,
                image_bytes=image_bytes,
                images_list=images_list,
                model=model,
                system=kw.pop("system", system),
                temperature=kw.pop("temperature", temperature),
                seed=kw.pop("seed", seed),
                **kw,
            )
        else:
            generate_fn = lambda **kw: self.generate(
                kw.pop("prompt", prompt),
                model=model,
                system=kw.pop("system", system),
                temperature=kw.pop("temperature", temperature),
                seed=kw.pop("seed", seed),
                **kw,
            )

        return generate_json_with_retry(
            generate_fn=generate_fn,
            prompt=prompt,
            model_cls=LooseDict,
            system=system,
            temperature=temperature,
            seed=seed,
            max_retries=max_retries,
            response_format={"type": "json_object"},
        ).model_dump()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """Return True if the circuit breaker allows traffic AND Ollama is reachable."""
        if not self.circuit_breaker.is_healthy():
            return False
        return self.is_available()

    def close(self):
        pass  # No shared client to close

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

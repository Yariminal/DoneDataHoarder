"""
Ollama client — talks to a local Ollama instance.

Supports:
- Text-only generation (llama3, mistral, gemma, etc.)
- Vision generation (llava, bakllava, gemma3, etc.) with base64 images
- Circuit breaker for resilience
- Structured JSON via shared json_utils

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import base64
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from datahoarder.ai.base_client import BaseAIClient
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

# Approximate context lengths for common Ollama models
_CONTEXT_LENGTHS: dict[str, int] = {
    "gemma3:12b": 128_000,
    "gemma3:4b": 128_000,
    "gemma3:1b": 128_000,
    "llama3": 8_192,
    "llama3:8b": 8_192,
    "llama3:70b": 8_192,
    "mistral": 32_768,
    "mixtral": 32_768,
    "qwen2": 128_000,
    "phi3": 128_000,
}


class OllamaClient(BaseAIClient):
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
    # BaseAIClient interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.host}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    def is_healthy(self) -> bool:
        """Return True if the circuit breaker allows traffic AND Ollama is reachable."""
        if not self.circuit_breaker.is_healthy():
            return False
        return self.is_available()

    def supports_vision(self) -> bool:
        vision_models = {"llava", "bakllava", "gemma3", "moondream", "cogvlm"}
        model_lower = (self.vision_model or "").lower()
        return any(vm in model_lower for vm in vision_models)

    def max_context_tokens(self) -> Optional[int]:
        return _CONTEXT_LENGTHS.get(self.text_model)

    def close(self) -> None:
        pass  # No persistent client to close

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
            return self._do_generate(prompt, model, system, temperature, seed, **kwargs)
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
        # NOTE: Some models (e.g., gemma4:26b) hang indefinitely with
        # constrained JSON decoding. Skip native format for those and rely
        # on prompt engineering + json_utils extraction instead.
        _MODELS_WITH_BROKEN_JSON_FORMAT = {"gemma4:26b", "gemma4:e2b", "gemma4:e4b", "gpt-oss:20b"}
        response_format = kwargs.get("response_format")
        if response_format is not None and model not in _MODELS_WITH_BROKEN_JSON_FORMAT:
            fmt = response_format.get("type")
            if fmt == "json_object":
                payload["format"] = "json"
            elif isinstance(response_format, dict):
                payload["format"] = response_format

        # Allow per-request timeout override (e.g., for large models like gemma4:26b)
        timeout = kwargs.get("timeout", TIMEOUT)

        with _OLLAMA_REQUEST_LOCK:
            with httpx.Client(timeout=timeout) as client:
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

        b64_images: list[str] = []
        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is not None:
            b64_images.append(base64.b64encode(image_bytes).decode())
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
        # NOTE: Some models hang with constrained JSON decoding (see text gen).
        _MODELS_WITH_BROKEN_JSON_FORMAT = {"gemma4:26b", "gemma4:e2b", "gemma4:e4b", "gpt-oss:20b"}
        response_format = kwargs.get("response_format")
        if response_format is not None and model not in _MODELS_WITH_BROKEN_JSON_FORMAT:
            fmt = response_format.get("type")
            if fmt == "json_object":
                payload["format"] = "json"
            elif isinstance(response_format, dict):
                payload["format"] = response_format

        # Allow per-request timeout override
        timeout = kwargs.get("timeout", TIMEOUT)

        with _OLLAMA_REQUEST_LOCK:
            with httpx.Client(timeout=timeout) as client:
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
        model_cls: type | None = None,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        images_list: list[bytes] | None = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.0,
        seed: int = 42,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> dict:
        """
        Like generate / generate_with_image but instructs the model to return
        valid JSON and parses the result.  Uses shared json_utils with retry.

        Defaults to temperature=0.0 + seed=42 for deterministic structured output.
        This makes repeated calls with the same input produce the same JSON.
        """
        from datahoarder.ai.json_utils import LooseDict

        if model_cls is None:
            model_cls = LooseDict

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

        validated = generate_json_with_retry(
            generate_fn=generate_fn,
            prompt=prompt,
            model_cls=model_cls,
            system=system,
            temperature=temperature,
            seed=seed,
            max_retries=max_retries,
            response_format={"type": "json_object"},
            **kwargs,
        )
        result = validated.model_dump()
        # Unwrap lists that were boxed for LooseDict validation
        if isinstance(result, dict) and "_list" in result:
            return result["_list"]
        return result

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.host}/api/tags")
                resp.raise_for_status()
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []

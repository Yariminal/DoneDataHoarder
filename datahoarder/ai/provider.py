"""
AI Provider — dependency-injectable backend selector with contextvar support.

Eliminates the global mutable state (_ollama / _gemini) that previously lived
in router.py.  Usage:

    with AIProvider(backend="auto") as provider:
        client = provider.get_client()
        client.generate("...")

Or with FastAPI-style contextvar propagation:

    import contextvars
    from datahoarder.ai.provider import set_provider, get_client

    set_provider(AIProvider(backend="auto"))
    get_client().generate("...")
"""
from __future__ import annotations

import contextvars
from typing import Optional

from datahoarder.ai.base_client import BaseAIClient
from datahoarder.ai.ollama_client import OllamaClient, DEFAULT_HOST

_ai_provider_var: contextvars.ContextVar[Optional["AIProvider"]] = contextvars.ContextVar(
    "ai_provider", default=None
)


class AIProvider:
    """
    Manages one or more AI backends and picks the best available client.

    Thread-safe: backends are created once during __init__ and then
    treated as immutable from the caller's perspective.
    """

    def __init__(
        self,
        backend: str = "auto",
        ollama_host: str = DEFAULT_HOST,
        text_model: str = "gemma3:12b",
        vision_model: str = "gemma3:12b",
        gemini_api_key: Optional[str] = None,
        gemini_model: str = "gemini-2.0-flash",
    ):
        self._ollama: Optional[OllamaClient] = None
        self._gemini: Optional[BaseAIClient] = None
        self._backend = backend

        if backend in ("ollama", "auto"):
            self._ollama = OllamaClient(
                host=ollama_host,
                text_model=text_model,
                vision_model=vision_model,
            )
            if not self._ollama.is_available():
                self._ollama = None
                if backend == "ollama":
                    raise RuntimeError(
                        f"Ollama is not reachable at {ollama_host}. "
                        "Start Ollama with: ollama serve"
                    )

        if backend in ("gemini", "auto"):
            try:
                from datahoarder.ai.gemini_client import GeminiClient
                self._gemini = GeminiClient(api_key=gemini_api_key, model=gemini_model)
            except (ImportError, ValueError):
                self._gemini = None

        if self._ollama is None and self._gemini is None:
            raise RuntimeError(
                "No AI backend available. "
                "Either start Ollama (ollama serve) or set GEMINI_API_KEY."
            )

    # ------------------------------------------------------------------
    # Client selection
    # ------------------------------------------------------------------

    def get_client(self, failover: bool = True) -> BaseAIClient:
        """
        Return the active AI client.

        Priority: Ollama (local) → Gemini (cloud).
        If *failover* is True and Ollama's circuit breaker is open,
        automatically falls back to Gemini when configured.
        """
        if self._ollama is not None:
            if self._ollama.is_healthy():
                return self._ollama
            if failover and self._gemini is not None:
                return self._gemini
        if self._gemini is not None:
            return self._gemini
        if self._ollama is not None and not self._ollama.is_healthy():
            raise RuntimeError(
                "Ollama backend is unhealthy (circuit breaker OPEN). "
                "Run 'datahoarder doctor' to diagnose, or configure Gemini backend."
            )
        raise RuntimeError("AI backend not initialised.")

    def list_clients(self) -> dict[str, Optional[BaseAIClient]]:
        """Return a mapping of backend names to their client instances."""
        return {"ollama": self._ollama, "gemini": self._gemini}

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "AIProvider":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._ollama is not None:
            self._ollama.close()
        if self._gemini is not None:
            self._gemini.close()


# ---------------------------------------------------------------------------
# Contextvar helpers (drop-in replacement for old router.py module-level API)
# ---------------------------------------------------------------------------

def set_provider(provider: AIProvider) -> None:
    """Bind an AIProvider to the current execution context."""
    _ai_provider_var.set(provider)


def get_provider() -> AIProvider:
    """Return the provider bound to the current context."""
    provider = _ai_provider_var.get()
    if provider is None:
        raise RuntimeError(
            "AI provider not set in current context. "
            "Use AIProvider as a context manager or call set_provider()."
        )
    return provider


def get_client(failover: bool = True) -> BaseAIClient:
    """Return the best client from the current context's provider."""
    return get_provider().get_client(failover=failover)


def generate(prompt: str, **kwargs) -> str:
    return get_client().generate(prompt, **kwargs)


def generate_with_image(
    prompt: str,
    image_path: Optional[Path] = None,
    image_bytes: Optional[bytes] = None,
    **kwargs,
) -> str:
    return get_client().generate_with_image(
        prompt, image_path=image_path, image_bytes=image_bytes, **kwargs
    )


def generate_json(
    prompt: str,
    image_path: Optional[Path] = None,
    image_bytes: Optional[bytes] = None,
    **kwargs,
) -> dict:
    return get_client().generate_json(
        prompt, image_path=image_path, image_bytes=image_bytes, **kwargs
    )


def init_ai(
    backend: str = "ollama",
    ollama_host: str = DEFAULT_HOST,
    text_model: str = "gemma3:12b",
    vision_model: str = "gemma3:12b",
    gemini_api_key: Optional[str] = None,
    gemini_model: str = "gemini-2.0-flash",
) -> AIProvider:
    """
    Initialise the AI backend(s) and bind them to the current context.

    This is a drop-in replacement for the old router.init_ai().
    Returns the provider so callers can also use it as a context manager.
    """
    provider = AIProvider(
        backend=backend,
        ollama_host=ollama_host,
        text_model=text_model,
        vision_model=vision_model,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
    )
    set_provider(provider)
    return provider

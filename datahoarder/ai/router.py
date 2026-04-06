"""
AI router — picks the right client based on config/availability.

Priority: Ollama (local) → Gemini (cloud) → raises RuntimeError
"""
from pathlib import Path
from typing import Optional

from datahoarder.ai.ollama_client import OllamaClient, DEFAULT_HOST


_ollama: Optional[OllamaClient] = None
_gemini = None


def init_ai(
    backend: str = "ollama",
    ollama_host: str = DEFAULT_HOST,
    text_model: str = "gemma3:12b",
    vision_model: str = "gemma3:12b",
    gemini_api_key: Optional[str] = None,
    gemini_model: str = "gemini-2.0-flash",
):
    """Initialise the AI backend(s). Call once at startup."""
    global _ollama, _gemini

    if backend in ("ollama", "auto"):
        _ollama = OllamaClient(
            host=ollama_host,
            text_model=text_model,
            vision_model=vision_model,
        )
        if not _ollama.is_available():
            _ollama = None
            if backend == "ollama":
                raise RuntimeError(
                    f"Ollama is not reachable at {ollama_host}. "
                    "Start Ollama with: ollama serve"
                )

    if backend in ("gemini", "auto") and (gemini_api_key or True):
        try:
            from datahoarder.ai.gemini_client import GeminiClient
            _gemini = GeminiClient(api_key=gemini_api_key, model=gemini_model)
        except (ImportError, ValueError):
            _gemini = None

    if _ollama is None and _gemini is None:
        raise RuntimeError(
            "No AI backend available. "
            "Either start Ollama (ollama serve) or set GEMINI_API_KEY."
        )


def get_client():
    """Return the active AI client (Ollama preferred)."""
    if _ollama is not None:
        return _ollama
    if _gemini is not None:
        return _gemini
    raise RuntimeError("AI backend not initialised. Call init_ai() first.")


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

"""
Abstract base class for all AI clients.

Defines the contract that every backend (Ollama, Gemini, future providers)
must implement, plus shared helpers for timeout handling, image base64, and
JSON post-processing via json_utils.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from datahoarder.ai.json_utils import generate_json_with_retry

T = TypeVar("T", bound=BaseModel)


class BaseAIClient(ABC):
    """
    Minimal contract for an AI backend.

    Subclasses must implement:
      - generate(prompt) -> str
      - generate_with_image(prompt, image...) -> str
      - is_available() -> bool
      - supports_vision() -> bool
      - max_context_tokens() -> int | None
      - is_healthy() -> bool   (circuit breaker / connectivity)
    """

    @abstractmethod
    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.2,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Text-only generation. Return the raw response string."""
        ...

    @abstractmethod
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
        """Vision / multimodal generation. Return the raw response string."""
        ...

    # ------------------------------------------------------------------
    # Shared JSON helper (uses json_utils)
    # ------------------------------------------------------------------

    def generate_json(
        self,
        prompt: str,
        model_cls: Type[BaseModel] | None = None,
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
        Structured JSON output with retry and optional Pydantic validation.

        If *model_cls* is provided, the parsed JSON is validated against it.
        If None, a loose dict schema is used.
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
        )
        return validated.model_dump()

    # ------------------------------------------------------------------
    # Shared image helper
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_images(
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        images_list: list[bytes] | None = None,
    ) -> list[str]:
        """Return a list of base64-encoded image strings from various input forms."""
        b64_images: list[str] = []
        if image_path is not None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        if image_bytes is not None:
            b64_images.append(base64.b64encode(image_bytes).decode())
        if images_list:
            for img in images_list:
                b64_images.append(base64.b64encode(img).decode())
        return b64_images

    # ------------------------------------------------------------------
    # Capability / health queries
    # ------------------------------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend server / API is reachable."""
        ...

    @abstractmethod
    def is_healthy(self) -> bool:
        """Return True if the backend is healthy enough to serve traffic."""
        ...

    @abstractmethod
    def supports_vision(self) -> bool:
        """Return True if this backend can process images."""
        ...

    @abstractmethod
    def max_context_tokens(self) -> Optional[int]:
        """Return the maximum context length, or None if unknown."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release any persistent resources (connections, sessions)."""
        ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

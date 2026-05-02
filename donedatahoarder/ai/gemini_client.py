"""
Gemini client — optional cloud AI backend (Google Gemini 2.0 Flash / Pro).

Usage requires:  pip install google-generativeai
and setting:     GEMINI_API_KEY environment variable (or passing api_key=).
"""
import os
from pathlib import Path
from typing import Any, Optional

from donedatahoarder.ai.base_client import BaseAIClient
from donedatahoarder.ai.json_utils import generate_json_with_retry

try:
    import google.generativeai as genai
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiClient(BaseAIClient):
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

    # ------------------------------------------------------------------
    # BaseAIClient interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            # Lightweight connectivity check via model info
            self._model.count_tokens("hello")
            return True
        except Exception:
            return False

    def is_healthy(self) -> bool:
        return self.is_available()

    def supports_vision(self) -> bool:
        return True  # Gemini 2.0 Flash / Pro are multimodal

    def max_context_tokens(self) -> Optional[int]:
        # Gemini 2.0 Flash: 1M tokens
        if "flash" in self.model_name.lower():
            return 1_048_576
        # Gemini 2.0 Pro: 2M tokens
        if "pro" in self.model_name.lower():
            return 2_097_152
        return None

    def close(self) -> None:
        pass  # google-generativeai uses global state; nothing to close per-instance

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
        cfg = genai.types.GenerationConfig(temperature=temperature)
        # Gemini supports response_format via generation_config for Flash models
        response_format = kwargs.get("response_format")
        if response_format is not None:
            try:
                cfg = genai.types.GenerationConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                )
            except Exception:
                pass  # fallback to plain generation if SDK too old
        resp = self._model.generate_content(prompt, generation_config=cfg)
        return resp.text.strip()

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
        mime_type: str = "image/jpeg",
        **kwargs: Any,
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
        response_format = kwargs.get("response_format")
        if response_format is not None:
            try:
                cfg = genai.types.GenerationConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                )
            except Exception:
                pass
        resp = self._model.generate_content(parts, generation_config=cfg)
        return resp.text.strip()

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
        mime_type: str = "image/jpeg",
        **kwargs: Any,
    ) -> dict:
        """
        Like generate / generate_with_image but instructs the model to return
        valid JSON and parses the result.  Uses shared json_utils with retry.
        """
        from donedatahoarder.ai.json_utils import LooseDict

        if model_cls is None:
            model_cls = LooseDict

        if image_path or image_bytes or images_list:
            generate_fn = lambda **kw: self.generate_with_image(
                kw.pop("prompt", prompt),
                image_path=image_path,
                image_bytes=image_bytes,
                images_list=images_list,
                mime_type=mime_type,
                temperature=kw.pop("temperature", temperature),
                **kw,
            )
        else:
            generate_fn = lambda **kw: self.generate(
                kw.pop("prompt", prompt),
                temperature=kw.pop("temperature", temperature),
                **kw,
            )

        validated = generate_json_with_retry(
            generate_fn=generate_fn,
            prompt=prompt,
            model_cls=model_cls,
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

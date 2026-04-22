"""
AI router — backward-compatible re-export layer.

All logic has moved to datahoarder.ai.provider (AIProvider + contextvars).
This module re-exports the same names so existing imports don't break.

New code should prefer:
    from datahoarder.ai.provider import AIProvider, get_client
"""
from datahoarder.ai.provider import (
    AIProvider,
    get_client,
    get_provider,
    generate,
    generate_json,
    generate_with_image,
    init_ai,
    set_provider,
)

__all__ = [
    "AIProvider",
    "get_client",
    "get_provider",
    "generate",
    "generate_json",
    "generate_with_image",
    "init_ai",
    "set_provider",
]

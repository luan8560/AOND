"""Gemini provider stub (SRD §VII names Gemini as the initial launch backend).

Left intentionally unimplemented: the project chose Claude for the prototype. This
file documents the seam so a Gemini (or local Blackwell) backend can drop in later
by implementing the same two methods — no engine changes required.
"""
from __future__ import annotations

from typing import Any

from ..config import Settings
from .base import LLMProvider, Message


class GeminiProvider(LLMProvider):
    def __init__(self, settings: Settings):
        self._settings = settings

    def chat(self, system: str, messages: list[Message], max_tokens: int = 256) -> str:
        raise NotImplementedError(
            "GeminiProvider is a stub. Set AIOI_LLM_PROVIDER=claude or =mock."
        )

    def extract(
        self, system: str, messages: list[Message], json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "GeminiProvider is a stub. Set AIOI_LLM_PROVIDER=claude or =mock."
        )

    def describe_media(self, items: list[tuple[bytes, str]], instruction: str,
                       max_tokens: int = 1024) -> str:
        raise NotImplementedError(
            "GeminiProvider is a stub. Set AIOI_LLM_PROVIDER=claude or =mock."
        )

    def group_media(
        self,
        items: list[tuple[bytes, str]],
        labels: list[str],
        instruction: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "GeminiProvider is a stub. Set AIOI_LLM_PROVIDER=claude or =mock."
        )

    def extract_media(
        self, items: list[tuple[bytes, str]], instruction: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "GeminiProvider is a stub. Set AIOI_LLM_PROVIDER=claude or =mock."
        )

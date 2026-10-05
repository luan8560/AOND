"""Claude (Anthropic) implementation of the LLM abstraction.

Defaults: Haiku 4.5 for fast 30-word chat, Opus 4.8 for vision/OCR-grade extraction.
"""
from __future__ import annotations

import base64
from typing import Any

from ..config import Settings
from ..imaging import downscale_image
from .base import LLMProvider, Message, ToolCall, ToolTurn


class ClaudeProvider(LLMProvider):
    def __init__(self, settings: Settings):
        import anthropic  # imported lazily so offline/mock runs need no SDK

        # Bounded timeout + one retry so a blocked/slow network fails fast in dev instead of
        # hanging on the SSL read (default SDK timeout is 10 minutes). 120s gives long vision
        # jobs (full multi-page document transcription, up to several thousand tokens) room.
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=120.0,
            max_retries=1,
        )
        self._chat_model = settings.chat_model
        self._extract_model = settings.extract_model  # cheap model for structured extraction/ops
        self._vision_model = settings.vision_model  # Opus 4.8 for vision/OCR-grade reads

    @staticmethod
    def _sys(system: str) -> list[dict[str, Any]]:
        """Wrap the system prompt as a cacheable block. The system (persona + the big project
        MEMORY block) is re-sent on every extract call and every agent-loop step; prompt caching
        makes those repeats ~10% of the input cost, with NO change to the output. Ephemeral cache
        lives ~5 min — long enough for a loop turn and back-to-back messages in one conversation."""
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def chat(self, system: str, messages: list[Message], max_tokens: int = 256) -> str:
        resp = self._client.messages.create(
            model=self._chat_model,
            max_tokens=max_tokens,
            system=self._sys(system),
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def extract(
        self, system: str, messages: list[Message], json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        # Tool-based extraction: force a single tool call whose input IS the
        # structured result. More robust and faster than output_config here.
        resp = self._client.messages.create(
            model=self._extract_model,
            max_tokens=1024,
            system=self._sys(system),
            messages=[{"role": m.role, "content": m.content} for m in messages],
            tools=[
                {
                    "name": "save_property_data",
                    "description": "Lưu các trường thông tin BĐS trích xuất được từ hội thoại.",
                    "input_schema": json_schema,
                }
            ],
            tool_choice={"type": "tool", "name": "save_property_data"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {}

    def converse(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolTurn:
        # One turn of native tool use. The engine appends the returned assistant_content and the
        # tool_results, then calls again — a manual agentic loop (fine-grained control so the
        # engine gates/verifies every tool). tools are plain Claude tool defs.
        resp = self._client.messages.create(
            model=self._chat_model,
            max_tokens=max_tokens,
            system=self._sys(system),
            messages=messages,
            tools=tools,
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input))
                 for b in resp.content if b.type == "tool_use"]  # CUSTOM tools only (server tools
        # like web_search run server-side and land in resp.content already — nothing to execute).
        return ToolTurn(text=text, tool_calls=calls, assistant_content=resp.content,
                        stop_reason=resp.stop_reason or "end_turn")

    def describe_media(self, items: list[tuple[bytes, str]], instruction: str,
                       max_tokens: int = 1024) -> str:
        # Vision/OCR read on the Opus 4.8 model. Images are downscaled first (the API
        # has its own per-image size limits); a PDF goes as a document block. Multiple
        # files are read in ONE call so a batch costs a single request. `max_tokens` is
        # raised for full-document transcription (a multi-page phân lô needs headroom).
        content: list[dict[str, Any]] = []
        for data, media_type in items:
            if media_type == "application/pdf":
                b64 = base64.standard_b64encode(data).decode("ascii")
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            else:
                small, small_type = downscale_image(data, media_type)
                b64 = base64.standard_b64encode(small).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": small_type, "data": b64},
                })
        content.append({"type": "text", "text": instruction})
        resp = self._client.messages.create(
            model=self._vision_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def group_media(
        self,
        items: list[tuple[bytes, str]],
        labels: list[str],
        instruction: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        # A text label right before each image/doc lets the model refer back to it by
        # filename in its grouping — the API has no other way to tag an image block.
        content: list[dict[str, Any]] = []
        for (data, media_type), label in zip(items, labels):
            content.append({"type": "text", "text": f"--- Tệp: {label} ---"})
            if media_type == "application/pdf":
                b64 = base64.standard_b64encode(data).decode("ascii")
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            else:
                small, small_type = downscale_image(data, media_type)
                b64 = base64.standard_b64encode(small).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": small_type, "data": b64},
                })
        content.append({"type": "text", "text": instruction})
        resp = self._client.messages.create(
            model=self._vision_model,
            max_tokens=1536,
            messages=[{"role": "user", "content": content}],
            tools=[
                {
                    "name": "nhom_tep_theo_can",
                    "description": "Nhóm các tệp ảnh/tài liệu theo từng căn hoặc theo thông tin chung dự án.",
                    "input_schema": json_schema,
                }
            ],
            tool_choice={"type": "tool", "name": "nhom_tep_theo_can"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {"nhom": []}

    def extract_media(
        self, items: list[tuple[bytes, str]], instruction: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for data, media_type in items:
            if media_type == "application/pdf":
                b64 = base64.standard_b64encode(data).decode("ascii")
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            else:
                small, small_type = downscale_image(data, media_type)
                b64 = base64.standard_b64encode(small).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": small_type, "data": b64},
                })
        content.append({"type": "text", "text": instruction})
        resp = self._client.messages.create(
            model=self._vision_model,
            max_tokens=4096,  # a price list can have many rows
            messages=[{"role": "user", "content": content}],
            tools=[{"name": "luu_du_lieu", "description": "Lưu dữ liệu trích xuất từ tài liệu.",
                    "input_schema": json_schema}],
            tool_choice={"type": "tool", "name": "luu_du_lieu"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {}

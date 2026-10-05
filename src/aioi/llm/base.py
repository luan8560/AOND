"""Provider-agnostic interface. The engine only ever talks to this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class ToolCall:
    """One tool the model asked to run this turn."""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolTurn:
    """One turn of a tool-using agent loop: the model's text so far, any tools it wants run,
    and an opaque `assistant_content` payload to echo back to continue the loop (provider-
    specific — Claude's raw content blocks). No tool_calls ⇒ the model is done (end_turn).
    `stop_reason` = "pause_turn" means a SERVER tool (e.g. web_search) needs another round-trip
    to continue — the engine re-sends without adding tool_results."""
    text: str
    tool_calls: list["ToolCall"]
    assistant_content: Any = None
    stop_reason: str = "end_turn"


class LLMProvider(ABC):
    """Minimal surface the conversation engine needs.

    Two capabilities:
      - `chat`: produce a short natural-language reply given a system prompt + history.
      - `extract`: return structured JSON matching `json_schema` from the conversation.

    Three capabilities: `chat`, `extract`, and `describe_media` (vision/OCR — read a
    photo or PDF the seller sent and say, honestly, what's in it).

    Keeping this tiny is the whole point of the abstraction — any backend (Claude,
    Gemini, or a local Blackwell model later) implements these methods.
    """

    @abstractmethod
    def chat(self, system: str, messages: list[Message], max_tokens: int = 256) -> str:
        ...

    @abstractmethod
    def extract(
        self, system: str, messages: list[Message], json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def describe_media(self, items: list[tuple[bytes, str]], instruction: str,
                       max_tokens: int = 1024) -> str:
        """Read one or more images/PDFs — each a (raw bytes, MIME type) pair — in a
        single call, and return a Vietnamese description following `instruction`.
        Used for seller-sent photos / sổ hồng; `max_tokens` is raised for full transcription."""
        ...

    @abstractmethod
    def group_media(
        self,
        items: list[tuple[bytes, str]],
        labels: list[str],
        instruction: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Vision + structured extraction in ONE call: read a BATCH of images/PDFs (each
        paired with a `label` — its filename, so the model can refer to files by name) and
        return JSON matching `json_schema` — used to group a multi-file batch (a folder,
        several links) by which listing/project each file actually belongs to."""
        ...

    @abstractmethod
    def extract_media(
        self, items: list[tuple[bytes, str]], instruction: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Vision + structured extraction: read image(s)/PDF(s) and return JSON matching
        `json_schema`. General-purpose (no per-file labels) — used e.g. to pull a whole
        PRICE-LIST / inventory table (many units × price) out of a brochure PDF."""
        ...

    def converse(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolTurn:
        """ONE turn of a tool-using agent loop (native function calling). `messages` is in the
        provider's chat format; `tools` are tool definitions. Returns a ToolTurn. The engine
        runs the loop (executes tools, echoes results) so IT keeps control of guardrails.
        Default: providers without tool support degrade to a plain text reply (no tool calls),
        so the agent loop still terminates — real tool use is overridden in ClaudeProvider."""
        msgs = [Message(m["role"], m["content"]) for m in messages if isinstance(m.get("content"), str)]
        return ToolTurn(text=self.chat(system, msgs, max_tokens), tool_calls=[])

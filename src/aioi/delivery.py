"""Message delivery pacing — split a long reply into Zalo-style message bubbles.

We never truncate the model's reply. But a long reply dumped as one giant message
reads badly on chat. Instead we split it into a sequence of shorter messages and let
the sender release them one at a time with a small "typing" delay, like a real person.

This module is pure (no I/O): `chunk_reply` decides the bubbles, `typing_delay` says
how long to pause before each. The actual sleeping/sending lives in the caller (the CLI
now, the Zalo adapter later), so the pacing is identical everywhere and easy to test.
"""
from __future__ import annotations

import re

# A comfortable single Zalo bubble. Shorter replies are sent whole; longer ones split.
DEFAULT_MAX_CHARS = 160

_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+")


def chunk_reply(reply: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split a reply into sequential message bubbles for paced delivery.

    - A short, single-line reply stays as ONE bubble.
    - Blank lines are paragraph breaks → always start a new bubble.
    - Consecutive short lines are grouped up to ~max_chars (so a bulleted listing
      doesn't explode into dozens of tiny bubbles).
    - A single line longer than max_chars is split on sentence boundaries (never
      mid-word — we don't truncate).
    """
    text = reply.strip()
    if not text:
        return []
    if len(text) <= max_chars and "\n" not in text:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n".join(buf).strip())
            buf = []
            buf_len = 0

    def add_unit(unit: str) -> None:
        nonlocal buf_len
        if buf and buf_len + len(unit) + 1 > max_chars:
            flush()
        buf.append(unit)
        buf_len += len(unit) + 1

    for raw in text.split("\n"):
        line = raw.rstrip()
        if line == "":
            flush()
            continue
        if len(line) <= max_chars:
            add_unit(line)
        else:
            # Over-long line: break into sentences and let them group by size.
            flush()
            for sentence in _SENTENCE_BREAK.split(line):
                add_unit(sentence)
            flush()
    flush()
    return [c for c in chunks if c]


def typing_delay(
    chunk: str,
    *,
    chars_per_second: float = 25.0,
    min_delay: float = 0.3,
    max_delay: float = 2.0,
) -> float:
    """Seconds to 'type' a bubble before sending it, clamped to a sane range.

    Proportional to length so short lines feel snappy and long ones get a real pause
    for the reader. Tune the bounds (or set chars_per_second high) to speed it up.
    """
    seconds = len(chunk) / chars_per_second
    return max(min_delay, min(seconds, max_delay))

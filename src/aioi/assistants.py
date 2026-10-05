"""Đại gia đình Trợ lý ảo — the virtual-assistant name family.

Names are formed from a Vietnamese initial consonant + the suffix "•ai", giving a
friendly, local-feeling roster (b•ai, c•ai, t•ai, m•ai, ...). Each assistant is
assigned deterministically so the same user/property keeps the same persona, and
two roles are reserved for the buyer-side / seller-side bot-to-bot flow.
"""
from __future__ import annotations

from dataclasses import dataclass

# Kho tên phân bổ tự động (SRD §II). Order preserved from the requirements doc.
ASSISTANT_NAMES: list[str] = [
    "B•ai", "C•ai", "D•ai", "Đ•ai", "G•ai", "Gi•ai", "H•ai", "K•ai",
    "Kh•ai", "L•ai", "M•ai", "N•ai", "Nh•ai", "Ph•ai", "Q•ai", "R•ai",
    "S•ai", "T•ai", "Tr•ai", "V•ai",
]

# Reserved roles from the architecture section (Bot-to-Bot truy vấn chéo, SRD §VII).
SELLER_ASSISTANT = "T•ai"  # phụ trách Người bán (tiếp nhận, làm giàu dữ liệu)
BUYER_ASSISTANT = "M•ai"   # phụ trách Người mua (lọc khách, hẹn lịch)


@dataclass(frozen=True)
class Assistant:
    name: str
    role: str  # "seller" | "buyer"


def assign_assistant(key: str, role: str = "seller") -> Assistant:
    """Pick a stable assistant persona for a conversation key (e.g. user id).

    The buyer side is always M•ai and the seller side defaults to T•ai, matching the
    bot-to-bot handoff in the SRD. Other personas rotate by a hash of the key so the
    roster feels varied without being random per-message.
    """
    if role == "buyer":
        return Assistant(BUYER_ASSISTANT, "buyer")

    # Seller side: stable per key, but spread across the roster.
    idx = sum(ord(c) for c in key) % len(ASSISTANT_NAMES)
    name = ASSISTANT_NAMES[idx]
    return Assistant(name, "seller")

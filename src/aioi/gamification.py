"""Hệ thống chấm điểm & trò chơi hóa (SRD §IV).

Property score = 50% data completeness + 50% response timeliness.
User score     = mean(property scores) × scale-bonus multiplier.
Tiers          = Đồng (<50) / Bạc (50–79) / Vàng (>=80).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    DONG = "Đồng"
    BAC = "Bạc"
    VANG = "Vàng"


def tier_for(score: float) -> Tier:
    if score >= 80:
        return Tier.VANG
    if score >= 50:
        return Tier.BAC
    return Tier.DONG


@dataclass(frozen=True)
class TierPerks:
    """Đặc quyền cấp bậc (SRD §IV.3)."""

    max_listings: int | None  # None = không giới hạn
    scale_bonus_active: bool
    description: str


def perks_for(tier: Tier) -> TierPerks:
    if tier is Tier.VANG:
        return TierPerks(
            max_listings=None,
            scale_bonus_active=True,
            description="NMG được M•ai ưu tiên chuyển khách nét; CCRB đẩy rổ hàng tới 20 NMG lõi.",
        )
    if tier is Tier.BAC:
        return TierPerks(
            max_listings=None,
            scale_bonus_active=True,
            description="Mở rổ hàng không giới hạn, kích hoạt Thưởng quy mô.",
        )
    return TierPerks(
        max_listings=5,
        scale_bonus_active=False,
        description="Giới hạn 5 căn rao, bị xếp cuối luồng ưu tiên.",
    )


def property_score(completeness: float, timeliness: float) -> float:
    """Điểm BĐS thành phần — 50% độ hoàn chỉnh + 50% độ kịp thời. Inputs in [0, 1].

    Returns a 0–100 score.
    """
    completeness = _clamp01(completeness)
    timeliness = _clamp01(timeliness)
    return round((0.5 * completeness + 0.5 * timeliness) * 100, 1)


def scale_bonus_multiplier(num_listings: int) -> float:
    """Thuật toán Thưởng Quy mô cho Môi giới (SRD §IV.2).

    Lũy tiến theo số căn N. We use the midpoint of each band's per-listing rate and
    cap the cumulative bonus so it stays a gentle multiplier rather than runaway.

        N <= 10      : +5%–7% / căn   (midpoint 6%)
        10 < N <= 30 : +3%–5% / căn   (midpoint 4%)
        N > 30       : +1%–2% / căn   (midpoint 1.5%)
    """
    if num_listings <= 0:
        return 1.0

    bonus = 0.0
    # Band 1: first up to 10 listings
    band1 = min(num_listings, 10)
    bonus += band1 * 0.06
    # Band 2: listings 11..30
    band2 = min(max(num_listings - 10, 0), 20)
    bonus += band2 * 0.04
    # Band 3: listings beyond 30
    band3 = max(num_listings - 30, 0)
    bonus += band3 * 0.015

    multiplier = 1.0 + bonus
    # Cap the raw multiplier generously; the real ceiling is user_score capping uy tín at 100.
    return round(min(multiplier, 3.0), 4)


def user_score(property_scores: list[float], num_listings: int | None = None) -> float:
    """Điểm Uy tín Người rao = mean(điểm BĐS) × hệ số thưởng quy mô.

    `num_listings` defaults to the number of scored properties.
    """
    if not property_scores:
        return 0.0
    n = num_listings if num_listings is not None else len(property_scores)
    base = sum(property_scores) / len(property_scores)
    score = base * scale_bonus_multiplier(n)
    return round(min(score, 100.0), 1)  # uy tín capped at 100


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

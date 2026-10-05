"""Revenue policy & seller classification (SRD §V, extended 2026-06-24).

A property in a project can be listed by several roles. The brokerage fee (collected
ONLY on a successful transaction) depends on the role, grouped owner-side vs broker-side:

- Owner-side (1% of closing price, or 3/4 of one month's rent):
    chu_nha    (homeowner — owns & resells their unit, thứ cấp)
    luot_song  (flipper — owns the HĐMB/contract, resells thứ cấp)
- Broker-side (0.5%, or 3/4 month rent):
    moi_gioi   (agent)         — must stay eligible (>=10 listings, 5%/6mo close, >=3 sao)
    san        (exchange / F1 distributor for the developer)
- Special:
    chu_dau_tu (developer) — primary sale; fee is a separate negotiated arrangement
                             (developers usually pay commission to floors/agents), NOT 1%/0.5%.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SellerRole(str, Enum):
    CHU_NHA = "chu_nha"          # chủ nhà (homeowner, secondary)
    LUOT_SONG = "luot_song"      # nhà đầu tư lướt sóng (flipper, owns HĐMB)
    MOI_GIOI = "moi_gioi"        # nhân viên môi giới (agent)
    SAN = "san"                  # sàn giao dịch (exchange / F1 distributor)
    CHU_DAU_TU = "chu_dau_tu"    # chủ đầu tư (developer — special fee)
    UNKNOWN = "unknown"


# Sale fee as a fraction of the final closing price, by role.
SALE_FEE_RATE: dict[SellerRole, float] = {
    SellerRole.CHU_NHA: 0.01,
    SellerRole.LUOT_SONG: 0.01,
    SellerRole.MOI_GIOI: 0.005,
    SellerRole.SAN: 0.005,
}
# Rental fee = 3/4 of one month's rent, for any non-special role.
RENTAL_FEE_MONTHS: float = 0.75

# Broker eligibility thresholds (SRD §V) — apply to broker-side roles (moi_gioi, san).
NMG_MIN_LISTINGS: int = 10
NMG_MIN_CLOSE_RATE: float = 0.05   # over a 6-month window
NMG_MIN_RATING: float = 3.0        # below this -> cooperation stopped

BROKER_ROLES = {SellerRole.MOI_GIOI, SellerRole.SAN}
OWNER_ROLES = {SellerRole.CHU_NHA, SellerRole.LUOT_SONG}


@dataclass
class FeeQuote:
    """A fee owed upon a successful deal. `amount` is None when undetermined/special."""

    role: SellerRole
    amount: float | None
    basis: str   # short Vietnamese description of how it was computed

    @property
    def determined(self) -> bool:
        return self.amount is not None


def quote_fee(
    role: SellerRole,
    *,
    is_rental: bool,
    closing_price: float | None = None,
    monthly_rent: float | None = None,
) -> FeeQuote:
    """Fee owed on a successful transaction (never charged before closing).

    Returns an undetermined quote when the role is unknown/special or the needed amount
    (price / rent) is missing — so callers never quote a fabricated number.
    """
    if role == SellerRole.CHU_DAU_TU:
        return FeeQuote(role, None, "Chủ đầu tư — phí theo thoả thuận riêng với dự án")
    if role not in SALE_FEE_RATE:  # UNKNOWN
        return FeeQuote(role, None, "Chưa xác định vai trò người rao")

    if is_rental:
        if monthly_rent is None or monthly_rent <= 0:
            return FeeQuote(role, None, "Cho thuê — chưa có giá thuê/tháng để tính phí")
        return FeeQuote(role, RENTAL_FEE_MONTHS * monthly_rent, "3/4 tháng tiền thuê")

    if closing_price is None or closing_price <= 0:
        return FeeQuote(role, None, "Chưa có giá chốt để tính phí")
    rate = SALE_FEE_RATE[role]
    return FeeQuote(role, rate * closing_price, f"{rate:.1%} giá chốt")


@dataclass
class NMGEligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)  # why NOT eligible, if any


def nmg_eligibility(
    num_listings: int,
    close_rate: float,
    rating: float,
) -> NMGEligibility:
    """Check whether a broker (môi giới / sàn) meets the cooperation requirements (§V)."""
    reasons: list[str] = []
    if num_listings < NMG_MIN_LISTINGS:
        reasons.append(f"Cần ≥{NMG_MIN_LISTINGS} BĐS (hiện {num_listings})")
    if close_rate < NMG_MIN_CLOSE_RATE:
        reasons.append(
            f"Tỷ lệ chốt cần ≥{NMG_MIN_CLOSE_RATE:.0%}/6 tháng (hiện {close_rate:.0%})"
        )
    if rating < NMG_MIN_RATING:
        reasons.append(f"Đánh giá <{NMG_MIN_RATING:g} sao sẽ ngưng hợp tác (hiện {rating:g})")
    return NMGEligibility(eligible=not reasons, reasons=reasons)

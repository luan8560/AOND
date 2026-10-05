from aioi.revenue import (
    NMG_MIN_LISTINGS,
    SellerRole,
    nmg_eligibility,
    quote_fee,
)


def test_owner_roles_sale_fee_is_one_percent():
    for role in (SellerRole.CHU_NHA, SellerRole.LUOT_SONG):
        q = quote_fee(role, is_rental=False, closing_price=10_000_000_000)
        assert q.amount == 100_000_000  # 1% of 10 tỷ
        assert q.determined


def test_broker_roles_sale_fee_is_half_percent():
    for role in (SellerRole.MOI_GIOI, SellerRole.SAN):
        q = quote_fee(role, is_rental=False, closing_price=10_000_000_000)
        assert q.amount == 50_000_000  # 0.5% of 10 tỷ


def test_developer_fee_is_special_not_a_percent():
    q = quote_fee(SellerRole.CHU_DAU_TU, is_rental=False, closing_price=10_000_000_000)
    assert q.amount is None  # negotiated separately, never a fabricated number
    assert "thoả thuận riêng" in q.basis


def test_rental_fee_is_three_quarter_month_regardless_of_role():
    a = quote_fee(SellerRole.CHU_NHA, is_rental=True, monthly_rent=20_000_000)
    b = quote_fee(SellerRole.MOI_GIOI, is_rental=True, monthly_rent=20_000_000)
    assert a.amount == b.amount == 15_000_000  # 3/4 of one month


def test_unknown_role_sale_is_undetermined():
    q = quote_fee(SellerRole.UNKNOWN, is_rental=False, closing_price=10_000_000_000)
    assert q.amount is None
    assert not q.determined


def test_missing_price_is_undetermined():
    q = quote_fee(SellerRole.CHU_NHA, is_rental=False, closing_price=None)
    assert q.amount is None


def test_nmg_eligible_when_all_thresholds_met():
    e = nmg_eligibility(num_listings=12, close_rate=0.06, rating=4.5)
    assert e.eligible
    assert e.reasons == []


def test_nmg_not_eligible_lists_every_reason():
    e = nmg_eligibility(num_listings=5, close_rate=0.02, rating=2.0)
    assert not e.eligible
    assert len(e.reasons) == 3  # too few listings, low close rate, low rating


def test_nmg_min_listings_boundary():
    e = nmg_eligibility(num_listings=NMG_MIN_LISTINGS, close_rate=0.05, rating=3.0)
    assert e.eligible

from aioi import gamification as g
from aioi.gamification import Tier


def test_property_score_is_half_completeness_half_timeliness():
    assert g.property_score(1.0, 1.0) == 100.0
    assert g.property_score(0.0, 0.0) == 0.0
    assert g.property_score(1.0, 0.0) == 50.0
    assert g.property_score(0.5, 0.5) == 50.0


def test_property_score_clamps_inputs():
    assert g.property_score(2.0, 2.0) == 100.0
    assert g.property_score(-1.0, -1.0) == 0.0


def test_tiers_match_srd_bands():
    assert g.tier_for(49.9) is Tier.DONG
    assert g.tier_for(50) is Tier.BAC
    assert g.tier_for(79) is Tier.BAC
    assert g.tier_for(80) is Tier.VANG


def test_scale_bonus_is_progressive():
    # More listings -> larger multiplier, but it never exceeds the 2x cap.
    assert g.scale_bonus_multiplier(0) == 1.0
    m10 = g.scale_bonus_multiplier(10)
    m30 = g.scale_bonus_multiplier(30)
    m100 = g.scale_bonus_multiplier(100)
    assert 1.0 < m10 < m30 < m100 <= 3.0


def test_perks_gate_listings_for_bronze():
    from aioi.gamification import perks_for

    assert perks_for(Tier.DONG).max_listings == 5
    assert perks_for(Tier.BAC).max_listings is None
    assert perks_for(Tier.VANG).scale_bonus_active is True


def test_user_score_capped_at_100():
    assert g.user_score([100.0, 100.0], num_listings=50) == 100.0


def test_user_score_empty():
    assert g.user_score([]) == 0.0

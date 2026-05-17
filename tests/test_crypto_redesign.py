from analysis.crypto_redesign import (
    confidence_tier,
    decision_for,
    metadata,
    planned_rr,
    rr_price_plan,
)


def test_confidence_tier_uses_opportunity_first_bands():
    assert confidence_tier(69.9) == ("probe_below_70", 0.5)
    assert confidence_tier(70.0) == ("base_70_74", 1.0)
    assert confidence_tier(75.0) == ("strong_75_84", 1.3)
    assert confidence_tier(85.0) == ("elite_85_plus", 1.6)


def test_tier_sizing_can_be_shadow_disabled_without_changing_live_size():
    d = decision_for(
        "BTCUSD",
        "btc_weekday_lob_momentum",
        88,
        enabled=False,
        shadow_only=True,
        tier_sizing_enabled=False,
        tp1_rr=0.7,
        runner_rr=2.5,
    )
    assert d.tier == "elite_85_plus"
    assert d.size_multiplier == 1.0
    assert d.shadow_only is True


def test_tier_sizing_when_live_flag_enabled():
    d = decision_for(
        "ETHUSD",
        "eth_weekday_smart_v2",
        78,
        enabled=True,
        shadow_only=False,
        tier_sizing_enabled=True,
        tp1_rr=0.7,
        runner_rr=2.2,
    )
    assert d.tier == "strong_75_84"
    assert d.size_multiplier == 1.3


def test_planned_rr_validates_long_short_geometry():
    assert planned_rr(100.0, 95.0, 115.0, "long") == 3.0
    assert planned_rr(100.0, 105.0, 85.0, "short") == 3.0
    assert planned_rr(100.0, 105.0, 115.0, "long") == 0.0
    assert planned_rr(100.0, 95.0, 85.0, "short") == 0.0


def test_rr_price_plan_uses_tp1_and_runner_rr():
    assert rr_price_plan(100.0, 95.0, "long", 0.7, 2.5) == (103.5, 112.5, 112.5)
    assert rr_price_plan(100.0, 105.0, "short", 0.7, 2.2) == (96.5, 89.0, 89.0)


def test_metadata_exposes_shadow_fields_without_order_side_effects():
    d = decision_for(
        "BTCUSD",
        "btc_weekday_lob_momentum",
        86,
        enabled=False,
        shadow_only=True,
        tier_sizing_enabled=True,
        tp1_rr=0.7,
        runner_rr=2.5,
    )
    m = metadata(d, entry=100.0, stop_loss=95.0, take_profit_1=103.5, direction="long")
    assert m["crypto_redesign_v2"] is True
    assert m["crypto_redesign_v2_shadow_only"] is True
    assert m["crypto_tier_used"] == "elite_85_plus"
    assert m["crypto_tier_size_multiplier"] == 1.6
    assert m["crypto_planned_rr_tp1"] == 0.7


def test_soft_mrd_penalty_reduces_live_tier_multiplier():
    d = decision_for(
        "BTCUSD",
        "btc_weekday_lob_momentum",
        86,
        enabled=True,
        shadow_only=False,
        tier_sizing_enabled=True,
        tp1_rr=0.7,
        runner_rr=2.5,
        soft_mrd_penalty=-0.20,
    )
    assert d.size_multiplier == 1.28
    assert d.soft_mrd_penalty == -0.20

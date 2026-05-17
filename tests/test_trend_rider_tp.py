from analysis.trend_rider_tp import (
    compute_tp_extension_multiplier,
    make_tier_runner_plan,
    apply_trend_rider,
)


def test_no_extension_when_no_overlap_no_compression():
    rs = {"overlap_tag": {"at_overlap_zone": False}}
    mult, info = compute_tp_extension_multiplier(raw_scores=rs, direction="long")
    assert mult == 1.0
    assert info["applied"] is False


def test_overlap_ride_gets_1_5x():
    rs = {"overlap_tag": {"at_overlap_zone": True, "bias_aligned": True,
                          "reversal_confirms": 3, "compression_score": 0.9}}
    mult, info = compute_tp_extension_multiplier(raw_scores=rs, direction="long")
    assert mult == 1.5
    assert info["applied"] is True


def test_compression_trend_gets_1_7x():
    rs = {"overlap_tag": {"at_overlap_zone": False, "compression_score": 0.5}}
    mult, _ = compute_tp_extension_multiplier(raw_scores=rs, direction="short")
    assert mult == 1.7


def test_overlap_plus_compression_gets_2_0x_cap():
    rs = {"overlap_tag": {"at_overlap_zone": True, "bias_aligned": True,
                          "reversal_confirms": 4, "compression_score": 0.4}}
    mult, _ = compute_tp_extension_multiplier(raw_scores=rs, direction="long")
    assert mult == 2.0


def test_bad_direction_returns_1():
    mult, info = compute_tp_extension_multiplier(raw_scores={}, direction="")
    assert mult == 1.0


def test_tier_plan_long():
    plan = make_tier_runner_plan(entry=2050, stop_loss=2048, direction="long",
                                  tp2=2053, tp3=2056)
    assert plan["valid"] is True
    assert plan["tier_1"]["price"] == 2052.0
    assert plan["tier_2"]["price"] == 2054.0
    assert plan["tier_3"]["mode"] == "trail_to_overlap_or_tp3"


def test_tier_plan_short():
    plan = make_tier_runner_plan(entry=2050, stop_loss=2052, direction="short",
                                  tp2=2046, tp3=2042)
    assert plan["tier_1"]["price"] == 2048.0
    assert plan["tier_2"]["price"] == 2046.0


def test_tier_plan_invalid_inputs():
    p = make_tier_runner_plan(entry=0, stop_loss=2048, direction="long")
    assert p["valid"] is False


def test_apply_trend_rider_extends_tp_when_overlap():
    class S: pass
    s = S()
    s.symbol = "XAUUSD"
    s.direction = "long"
    s.entry = 2050.0
    s.stop_loss = 2048.0
    s.take_profit_2 = 2052.0  # 1R from entry
    s.take_profit_3 = 2053.0  # 1.5R from entry
    s.raw_scores = {"overlap_tag": {"at_overlap_zone": True, "bias_aligned": True,
                                     "reversal_confirms": 3}}
    out = apply_trend_rider(signal=s)
    assert out["applied"] is True
    assert s.take_profit_2 > 2052.0  # extended
    assert s.take_profit_3 > 2053.0
    assert "trend_rider" in s.raw_scores
    assert "tier_runner_plan" in s.raw_scores


def test_apply_trend_rider_non_xau_no_op():
    class S: pass
    s = S()
    s.symbol = "BTCUSD"
    s.direction = "long"
    s.entry = 50000
    s.stop_loss = 49500
    s.take_profit_2 = 50500
    s.take_profit_3 = 51000
    s.raw_scores = {}
    out = apply_trend_rider(signal=s)
    assert out["applied"] is False


def test_apply_trend_rider_never_tightens_tp():
    class S: pass
    s = S()
    s.symbol = "XAUUSD"
    s.direction = "long"
    s.entry = 2050.0
    s.stop_loss = 2048.0
    s.take_profit_2 = 2055.0
    s.take_profit_3 = 2060.0
    s.raw_scores = {"overlap_tag": {"at_overlap_zone": False, "compression_score": 0.95}}
    apply_trend_rider(signal=s)
    # mult=1.0 → unchanged
    assert s.take_profit_2 == 2055.0
    assert s.take_profit_3 == 2060.0


def test_apply_trend_rider_never_raises():
    apply_trend_rider(signal=None)
    class Bad: pass
    apply_trend_rider(signal=Bad())

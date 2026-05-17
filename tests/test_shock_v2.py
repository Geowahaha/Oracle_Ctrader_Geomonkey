"""Tests for Shock V2 — detector, action tilt, and never-block guarantee."""
from analysis.shock_detector_v2 import (
    get_price_shock_score,
    get_news_shock_score,
    get_cross_asset_shock_score,
    compute_combined_shock,
    decay_score,
    auto_recover_check,
)
from analysis.shock_action_tilt import (
    get_tier,
    has_strong_opportunity,
    apply_shock_tilt,
)


def _c(o, h, l, cl):
    return {"open": o, "high": h, "low": l, "close": cl}


# ===== Detector =====

def test_price_shock_low_for_calm_market():
    candles = [_c(2050, 2050.5, 2049.5, 2050.0)] * 60
    out = get_price_shock_score(m5=candles)
    assert out["score"] < 30


def test_price_shock_high_for_volatility_spike():
    base = [_c(2050, 2050.3, 2049.7, 2050.0) for _ in range(50)]
    spike = [_c(2050, 2065, 2050, 2063)]  # huge range candle
    out = get_price_shock_score(m5=base + spike)
    assert out["score"] >= 25
    assert any("spike" in r or "elevated" in r for r in out["reasons"])


def test_price_shock_handles_short_input():
    out = get_price_shock_score(m5=[])
    assert out["score"] == 0
    out = get_price_shock_score(m5=None)
    assert out["score"] == 0


def test_news_shock_no_client_returns_zero():
    out = get_news_shock_score(news_client=None)
    assert out["score"] == 0
    assert "no_news_client" in out["reasons"]


def test_news_shock_with_mock_client():
    class MockClient:
        def get_recent_high_impact_events(self, lookback_min=90):
            return [
                {"theme": "FED_RATE", "source_quality": 0.9, "impact_score": 8.0, "age_min": 10},
                {"theme": "CPI_NFP", "source_quality": 0.85, "impact_score": 7.0, "age_min": 30},
            ]
    out = get_news_shock_score(MockClient())
    assert out["score"] > 0
    assert len(out["events"]) == 2


def test_news_shock_filters_low_quality():
    class MockClient:
        def get_recent_high_impact_events(self, lookback_min=90):
            return [
                {"theme": "FED_RATE", "source_quality": 0.5, "impact_score": 8.0, "age_min": 10},
                {"theme": "GAMING", "source_quality": 0.95, "impact_score": 9.0, "age_min": 5},
            ]
    out = get_news_shock_score(MockClient())
    assert out["score"] == 0


def test_cross_asset_no_client_returns_zero():
    out = get_cross_asset_shock_score(cross_asset_client=None)
    assert out["score"] == 0


def test_cross_asset_with_metrics():
    class MockClient:
        def get_correlation_metrics(self):
            return {"dxy_change_pct": 0.6, "vix_level": 28, "vix_change_pct": 12, "yield_change_bp": 12}
    out = get_cross_asset_shock_score(MockClient())
    assert out["score"] >= 50


def test_combine_weights_correctly():
    p = {"score": 100.0, "reasons": []}
    n = {"score": 50.0, "reasons": []}
    c = {"score": 0.0, "reasons": []}
    out = compute_combined_shock(p, n, c)
    # 100*0.4 + 50*0.4 + 0*0.2 = 60
    assert out["score"] == 60.0


def test_decay_halves_at_half_life():
    assert decay_score(100, age_minutes=30, half_life_min=30) == 50.0
    assert decay_score(100, age_minutes=60, half_life_min=30) == 25.0


def test_auto_recover_when_calm_and_normal_atr():
    assert auto_recover_check(calm_minutes=70, atr_ratio=1.0) is True
    assert auto_recover_check(calm_minutes=70, atr_ratio=2.0) is False
    assert auto_recover_check(calm_minutes=30, atr_ratio=1.0) is False


# ===== Tier table =====

def test_tier_table_boundaries():
    assert get_tier(0)["name"] == "normal"
    assert get_tier(29.9)["name"] == "normal"
    assert get_tier(30)["name"] == "elevated"
    assert get_tier(49.9)["name"] == "elevated"
    assert get_tier(50)["name"] == "active"
    assert get_tier(70)["name"] == "high"
    assert get_tier(85)["name"] == "extreme"
    assert get_tier(100)["name"] == "extreme"


def test_tier_size_mults_decreasing():
    # As score increases, size mult decreases
    mults = [get_tier(s)["size_mult"] for s in (10, 40, 60, 78, 95)]
    assert mults == sorted(mults, reverse=True)


# ===== Opportunity override =====

def test_opportunity_override_when_reversal_setup_4plus():
    rs = {
        "reversal_setup": {
            "score": 4,
            "layers": {"pa_volume_confirm": {"confirmed": True}},
        }
    }
    out = has_strong_opportunity(rs)
    assert out["override"] is True


def test_opportunity_override_when_overlap_4plus_confirms():
    rs = {"overlap_tag": {"at_overlap_zone": True, "reversal_confirms": 4}}
    out = has_strong_opportunity(rs)
    assert out["override"] is True


def test_no_override_when_low_signals():
    rs = {"reversal_setup": {"score": 2}, "overlap_tag": {"reversal_confirms": 1}}
    out = has_strong_opportunity(rs)
    assert out["override"] is False


def test_no_override_on_empty_raw():
    assert has_strong_opportunity({})["override"] is False
    assert has_strong_opportunity(None)["override"] is False


# ===== apply_shock_tilt =====

def _make_signal(direction="long", entry=2050, sl=2048, risk_override=2.0, raw=None):
    class S: pass
    s = S()
    s.symbol = "XAUUSD"
    s.direction = direction
    s.entry = entry
    s.stop_loss = sl
    s.raw_scores = dict(raw or {})
    if risk_override:
        s.raw_scores["ctrader_risk_usd_override"] = risk_override
    return s


def test_apply_shock_normal_tier_no_change():
    s = _make_signal()
    out = apply_shock_tilt(signal=s, shock_score=10.0)
    assert out["tier"] == "normal"
    assert out["size_mult"] == 1.0
    assert s.raw_scores["ctrader_risk_usd_override"] == 2.0


def test_apply_shock_high_tier_reduces_size():
    s = _make_signal(risk_override=2.0)
    out = apply_shock_tilt(signal=s, shock_score=75.0)
    assert out["tier"] == "high"
    assert out["size_mult"] == 0.3
    assert s.raw_scores["ctrader_risk_usd_override"] == round(2.0 * 0.3, 4)


def test_apply_shock_extreme_still_trades():
    s = _make_signal(risk_override=2.0)
    out = apply_shock_tilt(signal=s, shock_score=95.0)
    assert out["tier"] == "extreme"
    assert out["size_mult"] == 0.15
    # Critical: still has a positive size, never blocks
    assert s.raw_scores["ctrader_risk_usd_override"] > 0


def test_apply_shock_NEVER_blocks_at_any_score():
    # Even score=200 (clamped to 100) must produce a tradeable signal
    for score in (0, 30, 50, 70, 85, 99, 100, 200, 1000):
        s = _make_signal(risk_override=2.0)
        out = apply_shock_tilt(signal=s, shock_score=score)
        assert out["applied"] is True
        # Note: applied=True means it processed, NOT blocked. There is no
        # "block" return path by design.


def test_opportunity_override_halves_penalty():
    # High-conviction signal: should get less reduction
    raw = {"reversal_setup": {"score": 5, "layers": {"pa_volume_confirm": {"confirmed": True}}}}
    s = _make_signal(risk_override=2.0, raw=raw)
    out = apply_shock_tilt(signal=s, shock_score=75.0)
    # Without override: size=0.3, with override: (0.3+1.0)/2 = 0.65
    assert out["opportunity_override"] is True
    assert out["size_mult"] > 0.3
    assert s.raw_scores["ctrader_risk_usd_override"] > round(2.0 * 0.3, 4)


def test_apply_shock_tightens_sl():
    s = _make_signal(direction="long", entry=2050, sl=2040)  # 10 risk
    apply_shock_tilt(signal=s, shock_score=75.0)  # sl_tighten=0.5
    # New SL should be at entry - (10*0.5) = 2045
    assert abs(s.stop_loss - 2045.0) < 0.01


def test_apply_shock_short_direction_sl():
    s = _make_signal(direction="short", entry=2050, sl=2060)  # 10 risk
    apply_shock_tilt(signal=s, shock_score=75.0)  # sl_tighten=0.5
    # New SL = 2050 + 5 = 2055
    assert abs(s.stop_loss - 2055.0) < 0.01


def test_apply_shock_never_raises():
    apply_shock_tilt(signal=None, shock_score=50)
    class Bad: pass
    apply_shock_tilt(signal=Bad(), shock_score=50)
    apply_shock_tilt(signal=Bad(), shock_score="garbage")


def test_shock_v2_tag_always_written():
    s = _make_signal()
    apply_shock_tilt(signal=s, shock_score=50)
    assert "shock_v2" in s.raw_scores
    tag = s.raw_scores["shock_v2"]
    assert "tier" in tag
    assert "size_mult_applied" in tag
    assert "opportunity_override" in tag

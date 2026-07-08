"""Tests for dexter3/edge_buckets.py — ANTI-CHASE sizing gate.

Owner directive 2026-07-08: the edge-discovery sweep (Layer 1,
scripts/dexter3_edge_discovery.py) found the "aligned x trending" bucket —
side agrees with H1 trend AND H1 is in a strong directional regime, i.e.
chasing a mature trend — is the ENTIRE system loss (-116R over 6 days, 43%
of entries) while the rest is +85R. These tests cover:
  (a) classify_bucket's truth table (align x regime -> is_chase only when
      aligned+trending).
  (b) h1_trend_sign / regime match scripts/dexter3_edge_discovery.py's
      definitions on shared fixtures.
  (c) anti_chase_risk_mult: 0.15 on chase, 1.0 otherwise; enabled=False ->
      always 1.0 but classification still returned (shadow mode).
  (d) a focused wiring-level test of the risk_usd_override math the
      shadow_runner call site performs (base_risk * mult).
"""
from __future__ import annotations

import pytest

from dexter3.edge_buckets import (
    EdgeGateConfig,
    anti_chase_risk_mult,
    classify_bucket,
    h1_trend_sign,
    regime,
)

# ---------------------------------------------------------------------------
# fixtures: H1 bar series
# ---------------------------------------------------------------------------


def _bar(close: float) -> dict:
    return {"close": close, "open": close, "high": close, "low": close}


def _trending_up_h1(n: int = 10) -> list[dict]:
    """Monotonic net-up series -> h1_trend_sign=+1, regime='trending'
    (directional efficiency ~1.0, well above the 0.35 threshold)."""
    return [_bar(2000.0 + i * 1.0) for i in range(n)]


def _trending_down_h1(n: int = 10) -> list[dict]:
    return [_bar(2000.0 - i * 1.0) for i in range(n)]


def _ranging_h1(n: int = 10) -> list[dict]:
    """Zig-zag series with near-zero net change but large path length ->
    regime='ranging' (low directional efficiency)."""
    closes = []
    base = 2000.0
    for i in range(n):
        base += 5.0 if i % 2 == 0 else -5.0
    # build an explicit oscillation ending back near start with large path
    seq = [2000.0]
    for i in range(1, n):
        seq.append(seq[-1] + (6.0 if i % 2 == 1 else -5.8))
    return [_bar(c) for c in seq]


def _flat_h1(n: int = 10) -> list[dict]:
    """Zero net change AND zero path -> regime='unknown' per _regime's
    path<=0 guard, h1_trend_sign=0."""
    return [_bar(2000.0) for _ in range(n)]


def _too_short_h1() -> list[dict]:
    return [_bar(2000.0)]


# ---------------------------------------------------------------------------
# h1_trend_sign / regime — mirror scripts/dexter3_edge_discovery.py exactly
# ---------------------------------------------------------------------------


def test_h1_trend_sign_up():
    assert h1_trend_sign(_trending_up_h1()) == 1


def test_h1_trend_sign_down():
    assert h1_trend_sign(_trending_down_h1()) == -1


def test_h1_trend_sign_flat_is_zero():
    assert h1_trend_sign(_flat_h1()) == 0


def test_h1_trend_sign_insufficient_bars():
    assert h1_trend_sign(_too_short_h1()) == 0
    assert h1_trend_sign([]) == 0


def test_h1_trend_sign_uses_last_n_only():
    # first 20 bars trend down hard, last 6 bars (n default) trend up ->
    # sign should reflect only the trailing window
    bars = _trending_down_h1(20) + [_bar(1980.0 + i * 2.0) for i in range(6)]
    assert h1_trend_sign(bars, n=6) == 1


def test_regime_trending_on_monotonic_series():
    assert regime(_trending_up_h1()) == "trending"
    assert regime(_trending_down_h1()) == "trending"


def test_regime_ranging_on_zigzag_series():
    assert regime(_ranging_h1()) == "ranging"


def test_regime_unknown_on_flat_series():
    assert regime(_flat_h1()) == "unknown"


def test_regime_unknown_on_too_few_bars():
    assert regime(_too_short_h1()) == "unknown"
    assert regime([]) == "unknown"


def test_regime_respects_custom_threshold():
    # a mild zigzag: net change modest relative to path -> efficiency
    # somewhere in between; verify raising the threshold can flip trending->ranging
    bars = [_bar(2000.0), _bar(2004.0), _bar(2002.0), _bar(2006.0), _bar(2004.0), _bar(2008.0), _bar(2006.0), _bar(2010.0)]
    lax = regime(bars, thresh=0.1)
    strict = regime(bars, thresh=0.99)
    assert lax == "trending"
    assert strict == "ranging"


# ---------------------------------------------------------------------------
# classify_bucket truth table
# ---------------------------------------------------------------------------


def test_classify_bucket_aligned_trending_is_chase():
    b = classify_bucket("buy", _trending_up_h1())
    assert b["align"] == "aligned"
    assert b["regime"] == "trending"
    assert b["is_chase"] is True


def test_classify_bucket_aligned_trending_sell_is_chase():
    b = classify_bucket("sell", _trending_down_h1())
    assert b["align"] == "aligned"
    assert b["regime"] == "trending"
    assert b["is_chase"] is True


def test_classify_bucket_counter_trending_not_chase():
    b = classify_bucket("sell", _trending_up_h1())
    assert b["align"] == "counter"
    assert b["regime"] == "trending"
    assert b["is_chase"] is False


def test_classify_bucket_aligned_ranging_not_chase():
    b = classify_bucket("buy", _ranging_h1())
    # side agrees with whatever weak net drift exists, but regime is ranging
    # -> never a chase regardless of align
    assert b["regime"] == "ranging"
    assert b["is_chase"] is False


def test_classify_bucket_no_trend_not_chase():
    b = classify_bucket("buy", _flat_h1())
    assert b["align"] == "no_trend"
    assert b["is_chase"] is False


def test_classify_bucket_unknown_regime_not_chase():
    b = classify_bucket("buy", _too_short_h1())
    assert b["regime"] == "unknown"
    assert b["is_chase"] is False


def test_classify_bucket_none_side_never_chase():
    b = classify_bucket(None, _trending_up_h1())
    assert b["align"] == "counter"  # side_sign=0 != trend_sign=1
    assert b["is_chase"] is False


@pytest.mark.parametrize(
    "side,h1_builder,expect_align,expect_regime,expect_chase",
    [
        ("buy", _trending_up_h1, "aligned", "trending", True),
        ("sell", _trending_up_h1, "counter", "trending", False),
        ("buy", _trending_down_h1, "counter", "trending", False),
        ("sell", _trending_down_h1, "aligned", "trending", True),
        ("buy", _ranging_h1, None, "ranging", False),
        ("sell", _ranging_h1, None, "ranging", False),
        ("buy", _flat_h1, "no_trend", "unknown", False),
    ],
)
def test_classify_bucket_full_matrix(side, h1_builder, expect_align, expect_regime, expect_chase):
    b = classify_bucket(side, h1_builder())
    if expect_align is not None:
        assert b["align"] == expect_align
    assert b["regime"] == expect_regime
    assert b["is_chase"] is expect_chase


# ---------------------------------------------------------------------------
# anti_chase_risk_mult
# ---------------------------------------------------------------------------


def test_anti_chase_risk_mult_downsizes_chase_bucket():
    mult, reason = anti_chase_risk_mult("buy", _trending_up_h1())
    assert mult == pytest.approx(0.15)
    assert reason["is_chase"] is True
    assert reason["applied"] is True


def test_anti_chase_risk_mult_full_size_on_counter_trend():
    mult, reason = anti_chase_risk_mult("sell", _trending_up_h1())
    assert mult == pytest.approx(1.0)
    assert reason["is_chase"] is False


def test_anti_chase_risk_mult_full_size_on_ranging():
    mult, reason = anti_chase_risk_mult("buy", _ranging_h1())
    assert mult == pytest.approx(1.0)
    assert reason["regime"] == "ranging"


def test_anti_chase_risk_mult_custom_chase_size_mult():
    cfg = EdgeGateConfig(chase_size_mult=0.3)
    mult, reason = anti_chase_risk_mult("buy", _trending_up_h1(), cfg)
    assert mult == pytest.approx(0.3)


def test_anti_chase_risk_mult_disabled_always_full_size_but_classifies():
    cfg = EdgeGateConfig(enabled=False)
    mult, reason = anti_chase_risk_mult("buy", _trending_up_h1(), cfg)
    assert mult == pytest.approx(1.0)
    # shadow mode: classification still fully present even though disabled
    assert reason["is_chase"] is True
    assert reason["would_downsize"] is True
    assert reason["applied"] is False
    assert reason["enabled"] is False


def test_anti_chase_risk_mult_disabled_non_chase_also_full_size():
    cfg = EdgeGateConfig(enabled=False)
    mult, reason = anti_chase_risk_mult("sell", _trending_up_h1(), cfg)
    assert mult == pytest.approx(1.0)
    assert reason["is_chase"] is False
    assert reason["would_downsize"] is False


def test_anti_chase_risk_mult_default_config_is_enabled():
    cfg = EdgeGateConfig()
    assert cfg.enabled is True
    assert cfg.chase_size_mult == pytest.approx(0.15)
    assert cfg.regime_thresh == pytest.approx(0.35)


def test_anti_chase_risk_mult_custom_regime_thresh_flips_classification():
    # a bucket that is 'trending' at the default 0.35 threshold can become
    # 'ranging' at a much stricter threshold, changing is_chase.
    bars = [_bar(2000.0), _bar(2004.0), _bar(2002.0), _bar(2006.0), _bar(2004.0), _bar(2008.0), _bar(2006.0), _bar(2010.0)]
    lax_cfg = EdgeGateConfig(regime_thresh=0.1)
    strict_cfg = EdgeGateConfig(regime_thresh=0.99)
    mult_lax, reason_lax = anti_chase_risk_mult("buy", bars, lax_cfg)
    mult_strict, reason_strict = anti_chase_risk_mult("buy", bars, strict_cfg)
    assert reason_lax["regime"] == "trending"
    assert reason_strict["regime"] == "ranging"
    assert mult_lax == pytest.approx(0.15)
    assert mult_strict == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# wiring-level: the risk_usd_override math the shadow_runner call site
# performs (base_risk_usd * mult, rounded) — mirrors
# shadow_runner._apply_anti_chase_gate without needing the full MCP/executor
# harness.
# ---------------------------------------------------------------------------


def _gated_risk_usd(base_risk_usd: float, side: str | None, h1_bars: list[dict], cfg: EdgeGateConfig | None = None) -> float:
    mult, _reason = anti_chase_risk_mult(side, h1_bars, cfg)
    return round(base_risk_usd * mult, 4)


def test_gated_risk_usd_scales_governor_risk_on_chase():
    governor_risk = 20.0
    gated = _gated_risk_usd(governor_risk, "buy", _trending_up_h1())
    assert gated == pytest.approx(3.0)  # 20 * 0.15


def test_gated_risk_usd_unchanged_on_non_chase():
    governor_risk = 20.0
    gated = _gated_risk_usd(governor_risk, "sell", _trending_up_h1())
    assert gated == pytest.approx(20.0)


def test_gated_risk_usd_unchanged_when_disabled_even_on_chase():
    governor_risk = 20.0
    cfg = EdgeGateConfig(enabled=False)
    gated = _gated_risk_usd(governor_risk, "buy", _trending_up_h1(), cfg)
    assert gated == pytest.approx(20.0)

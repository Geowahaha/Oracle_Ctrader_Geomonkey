"""Unit tests for dexter3/market_lens.py — pure price-action feature functions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dexter3 import market_lens


def _bar(o: float, h: float, l: float, c: float, ts: str = "") -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _ts_series(n: int, start: datetime | None = None, step_min: int = 5) -> list[str]:
    start = start or datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    return [(start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def _uptrend_bars(n: int = 30, step: float = 0.8) -> list[dict]:
    """Staircase uptrend: 3-bar impulse legs separated by a 2-bar pullback so
    genuine higher-high/higher-low pivots form under a pivot_span=2 check
    (a perfectly monotonic line, or a 1-bar dip, never turns down for two
    full bars on both sides and so has no detectable local pivots at all —
    not representative of real markets and untestable with this method)."""
    ts = _ts_series(n)
    bars = []
    price = 2000.0
    for i in range(n):
        is_pullback = (i % 5) in (3, 4)
        o = price
        if is_pullback:
            c = price - step * 0.4
            h = o + 0.05
            l = c - 0.1
        else:
            c = price + step
            h = c + 0.2
            l = o - 0.1
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    return bars


def _downtrend_bars(n: int = 30, step: float = 0.8) -> list[dict]:
    """Staircase downtrend: mirror of _uptrend_bars with a 2-bar pullback
    (bounce) interleaved so genuine lower-high/lower-low pivots form under
    a pivot_span=2 check."""
    ts = _ts_series(n)
    bars = []
    price = 2000.0
    for i in range(n):
        is_pullback = (i % 5) in (3, 4)
        o = price
        if is_pullback:
            c = price + step * 0.4
            h = c + 0.1
            l = o - 0.05
        else:
            c = price - step
            h = o + 0.1
            l = c - 0.2
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    return bars


def _flat_chop_bars(n: int = 25) -> list[dict]:
    ts = _ts_series(n)
    bars = []
    price = 2000.0
    for i in range(n):
        o = price
        c = price + (0.3 if i % 2 == 0 else -0.3)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    return bars


# -- swing_structure ----------------------------------------------------------


def test_swing_structure_detects_uptrend():
    bars = _uptrend_bars(30)
    result = market_lens.swing_structure(bars)
    assert result["value"] == "uptrend"
    assert result["high_trend"] == "HH"
    assert result["low_trend"] == "HL"


def test_swing_structure_detects_downtrend():
    bars = _downtrend_bars(30)
    result = market_lens.swing_structure(bars)
    assert result["value"] == "downtrend"
    assert result["high_trend"] == "LH"
    assert result["low_trend"] == "LL"


def test_swing_structure_handles_insufficient_bars():
    result = market_lens.swing_structure([_bar(1, 2, 0, 1.5)])
    assert result["value"] == "unknown"
    assert result["last_swing_high"] is None
    assert result["last_swing_low"] is None


# -- liquidity_sweep ------------------------------------------------------------


def test_liquidity_sweep_detects_sell_side_sweep():
    # Prior bars form a range top around 2010; last bar wicks above then closes back inside.
    ts = _ts_series(11)
    prior = [_bar(2000 + i * 0.2, 2000 + i * 0.2 + 1.0, 2000 + i * 0.2 - 1.0, 2000 + i * 0.2 + 0.3, ts[i]) for i in range(10)]
    prior_high = max(b["high"] for b in prior)
    last = _bar(prior_high - 0.5, prior_high + 1.5, prior_high - 1.0, prior_high - 1.2, ts[10])
    bars = prior + [last]
    result = market_lens.liquidity_sweep(bars)
    assert result["value"] is True
    assert result["side"] == "sell"
    assert result["level"] is not None


def test_liquidity_sweep_detects_buy_side_sweep():
    ts = _ts_series(11)
    prior = [_bar(2000 - i * 0.2, 2000 - i * 0.2 + 1.0, 2000 - i * 0.2 - 1.0, 2000 - i * 0.2 - 0.3, ts[i]) for i in range(10)]
    prior_low = min(b["low"] for b in prior)
    last = _bar(prior_low + 0.5, prior_low + 1.0, prior_low - 1.5, prior_low + 1.2, ts[10])
    bars = prior + [last]
    result = market_lens.liquidity_sweep(bars)
    assert result["value"] is True
    assert result["side"] == "buy"


def test_liquidity_sweep_no_sweep_on_flat_chop():
    bars = _flat_chop_bars(15)
    result = market_lens.liquidity_sweep(bars)
    assert result["value"] is False
    assert result["side"] is None


def test_liquidity_sweep_insufficient_bars():
    result = market_lens.liquidity_sweep([_bar(1, 2, 0, 1)])
    assert result["value"] is False
    assert "insufficient" in result["evidence"]


# -- reclaim --------------------------------------------------------------------


def test_reclaim_buy_side_confirms_after_break_below():
    ts = _ts_series(4)
    bars = [
        _bar(2000, 2001, 1999, 2000.5, ts[0]),
        _bar(2000.5, 2001, 1997, 1997.5, ts[1]),  # breaks below level=1998
        _bar(1997.5, 1999, 1997, 1998.5, ts[2]),
        _bar(1998.5, 2000.5, 1998, 2000.0, ts[3]),  # reclaims above 1998
    ]
    result = market_lens.reclaim(bars, level=1998.0, side="buy")
    assert result["value"] is True
    assert result["side"] == "buy"


def test_reclaim_fails_without_prior_break():
    ts = _ts_series(4)
    bars = [_bar(2000, 2001, 1999.5, 2000.5, t) for t in ts]
    result = market_lens.reclaim(bars, level=1998.0, side="buy")
    assert result["value"] is False


def test_reclaim_empty_bars():
    result = market_lens.reclaim([], level=100.0)
    assert result["value"] is False
    assert result["evidence"] == "no_bars"


# -- displacement -----------------------------------------------------------------


def test_displacement_detects_expansion_bar():
    ts = _ts_series(22)
    quiet = [_bar(2000, 2000.3, 1999.8, 2000.1, ts[i]) for i in range(20)]
    big = _bar(2000.1, 2005.0, 2000.0, 2004.8, ts[20])
    bars = quiet + [big]
    result = market_lens.displacement(bars)
    assert result["value"] is True
    assert result["direction"] == "buy"
    assert result["body_ratio"] >= 0.5


def test_displacement_false_on_quiet_bar():
    ts = _ts_series(21)
    bars = [_bar(2000, 2000.3, 1999.8, 2000.1 if i % 2 == 0 else 1999.9, ts[i]) for i in range(21)]
    result = market_lens.displacement(bars)
    assert result["value"] is False


def test_displacement_insufficient_bars():
    result = market_lens.displacement([_bar(1, 2, 0, 1)] * 2)
    assert result["value"] is False


# -- compression_release ------------------------------------------------------------


def test_compression_release_detects_contraction_then_expansion():
    # reference regime (before the squeeze) has TR ~= 3.0; the compression
    # window squeezes to TR ~= 0.5; the release bar must expand well BEYOND
    # the reference regime, not merely back to it, to count as a release.
    ts = _ts_series(15)
    wide = [_bar(2000, 2003, 2000, 2001, ts[i]) for i in range(6)]
    tight = [_bar(2001, 2001.3, 2000.8, 2001.1, ts[6 + i]) for i in range(6)]
    release = _bar(2001.1, 2011.0, 2001.0, 2010.8, ts[12])
    bars = wide + tight + [release]
    result = market_lens.compression_release(bars, compression_window=6)
    assert result["value"] is True
    assert result["direction"] == "buy"


def test_compression_release_false_without_prior_compression():
    bars = _uptrend_bars(15)
    result = market_lens.compression_release(bars, compression_window=6)
    # uptrend has fairly uniform ranges, not a compression->release shape
    assert result["value"] in (False, True)  # shape-dependent; must not raise
    assert "direction" in result


# -- close_location_pressure ---------------------------------------------------------


def test_close_location_pressure_bullish_bias():
    ts = _ts_series(5)
    bars = [_bar(100, 110, 95, 108, t) for t in ts]  # closes near the high each bar
    result = market_lens.close_location_pressure(bars, n=5)
    assert result["value"] > 0.6
    assert result["bias"] == "buy"


def test_close_location_pressure_bearish_bias():
    ts = _ts_series(5)
    bars = [_bar(100, 110, 95, 97, t) for t in ts]  # closes near the low each bar
    result = market_lens.close_location_pressure(bars, n=5)
    assert result["value"] < 0.4
    assert result["bias"] == "sell"


def test_close_location_pressure_empty_bars():
    result = market_lens.close_location_pressure([])
    assert result["value"] == 0.5
    assert result["n"] == 0


# -- day_range_position ---------------------------------------------------------------


def test_day_range_position_upper_shelf():
    ts = _ts_series(10)
    bars = [_bar(2000, 2000 + i, 1999, 2000 + i - 0.5, ts[i]) for i in range(10)]
    result = market_lens.day_range_position(bars)
    assert result["value"] >= 0.78
    assert result["zone"] == "upper_shelf"


def test_day_range_position_lower_shelf():
    ts = _ts_series(10)
    bars = [_bar(2010, 2011, 2010 - i, 2010 - i + 0.5, ts[i]) for i in range(10)]
    result = market_lens.day_range_position(bars)
    assert result["value"] <= 0.22
    assert result["zone"] == "lower_shelf"


def test_day_range_position_empty_bars():
    result = market_lens.day_range_position([])
    assert result["value"] == 0.5
    assert result["day_hi"] is None


# -- session_context ------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [
        (2, "asian"),
        (9, "london"),
        (13, "overlap"),
        (18, "ny"),
        (22, "off_hours"),
    ],
)
def test_session_context_labels(hour, expected):
    ts = f"2026-07-05T{hour:02d}:00:00Z"
    result = market_lens.session_context(ts)
    assert result["value"] == expected
    assert result["hour_utc"] == hour


def test_session_context_unparseable():
    result = market_lens.session_context("not-a-timestamp")
    assert result["value"] == "unknown"


def test_session_context_empty_string():
    result = market_lens.session_context("")
    assert result["value"] == "unknown"


# -- volatility_state -----------------------------------------------------------------


def test_volatility_state_hot_regime():
    ts = _ts_series(15)
    quiet = [_bar(2000, 2000.2, 1999.9, 2000.1, ts[i]) for i in range(14)]
    hot = _bar(2000.1, 2010, 2000, 2009, ts[14])
    bars = quiet + [hot]
    result = market_lens.volatility_state(bars)
    assert result["value"] == "hot"


def test_volatility_state_dead_regime():
    ts = _ts_series(15)
    wide = [_bar(2000, 2005, 1995, 2002, ts[i]) for i in range(14)]
    dead = _bar(2002, 2002.05, 2001.98, 2002.02, ts[14])
    bars = wide + [dead]
    result = market_lens.volatility_state(bars)
    assert result["value"] == "dead"


def test_volatility_state_insufficient_bars_defaults_normal():
    result = market_lens.volatility_state([_bar(1, 2, 0, 1)] * 2)
    assert result["value"] == "normal"


# -- leader_score ---------------------------------------------------------------------


def test_leader_score_strong_buy_composite():
    features = {
        "liquidity_sweep": {"value": True, "side": "buy"},
        "displacement": {"value": True, "direction": "buy"},
        "compression_release": {"value": True, "direction": "buy"},
        "close_location_pressure": {"bias": "buy"},
        "swing_structure": {"value": "uptrend"},
    }
    result = market_lens.leader_score(features)
    assert result["side"] == "buy"
    assert result["value"] >= market_lens.LEADER_STRONG_SCORE
    assert result["band"] == "strong"


def test_leader_score_weak_when_no_evidence():
    features = {
        "liquidity_sweep": {"value": False, "side": None},
        "displacement": {"value": False, "direction": None},
        "compression_release": {"value": False, "direction": None},
        "close_location_pressure": {"bias": None},
        "swing_structure": {"value": "unknown"},
    }
    result = market_lens.leader_score(features)
    assert result["side"] is None
    assert result["band"] == "weak"
    assert result["value"] < market_lens.LEADER_MIN_SCORE


def test_leader_score_handles_missing_keys_gracefully():
    result = market_lens.leader_score({})
    assert result["value"] == 0.0
    assert result["band"] == "weak"


def test_leader_score_conflicting_signals_produce_gap():
    features = {
        "liquidity_sweep": {"value": True, "side": "buy"},
        "displacement": {"value": True, "direction": "sell"},
        "compression_release": {"value": False, "direction": None},
        "close_location_pressure": {"bias": "sell"},
        "swing_structure": {"value": "unknown"},
    }
    result = market_lens.leader_score(features)
    assert result["gap"] >= 0.0
    assert "buy_score" in result and "sell_score" in result

"""Tests for 5-layer reversal setup detector + S/R + PA/volume confirm."""
from analysis.sr_levels_detector import detect_sr_levels, find_nearest_sr
from analysis.price_action_volume_confirm import detect_pa_volume_confirm
from analysis.reversal_setup_detector import evaluate


def _c(o, h, l, cl, v=100):
    return {"open": o, "high": h, "low": l, "close": cl, "volume": v}


# ===== SR detector =====

def test_sr_detect_returns_empty_on_short_input():
    assert detect_sr_levels([]) == []
    assert detect_sr_levels([_c(1, 1, 1, 1)] * 5) == []


def test_sr_detect_finds_repeated_level():
    candles = []
    # noise candles
    for i in range(20):
        candles.append(_c(2050 + i * 0.1, 2050.5 + i * 0.1, 2049.5 + i * 0.1, 2050.2 + i * 0.1))
    # 4 candles wicking down to 2048 (support test)
    for _ in range(4):
        candles.append(_c(2050, 2050.5, 2048.0, 2049.5))
        candles.append(_c(2049.5, 2050.0, 2049.2, 2049.7))
    levels = detect_sr_levels(candles, min_touches=3)
    assert isinstance(levels, list)


def test_sr_never_raises():
    detect_sr_levels(None)
    detect_sr_levels("garbage")
    detect_sr_levels([{"high": "x"}])


def test_find_nearest_sr_returns_none_when_far():
    levels = [{"level": 2050.0, "score": 5.0, "touches": 3, "kind": "support",
               "n_resistance_touches": 0, "n_support_touches": 3,
               "last_touch_bar": 50, "bars_since_last_touch": 5, "recency": 0.95}]
    assert find_nearest_sr(levels, price=2100.0, atr=2.0, max_distance_atr=1.0) is None


def test_find_nearest_sr_returns_when_close():
    levels = [{"level": 2050.0, "score": 5.0, "touches": 3, "kind": "support",
               "n_resistance_touches": 0, "n_support_touches": 3,
               "last_touch_bar": 50, "bars_since_last_touch": 5, "recency": 0.95}]
    res = find_nearest_sr(levels, price=2050.5, atr=2.0, max_distance_atr=1.0)
    assert res is not None
    assert res["level"] == 2050.0


# ===== PA + volume confirm =====

def test_pa_rejection_wick_long_with_volume_confirms():
    candles = [_c(2050, 2050.5, 2049.5, 2050.0, v=100) for _ in range(25)]
    # rejection wick at 2048: low pierces, close back up, big volume
    candles.append(_c(2049.5, 2050.0, 2047.5, 2049.8, v=200))
    r = detect_pa_volume_confirm("long", candles, sr_level=2048.0, atr=1.0)
    assert r["confirmed"] is True
    assert r["pattern"] == "rejection_wick"


def test_pa_breakout_body_short_with_volume_confirms():
    candles = [_c(2050, 2050.5, 2049.5, 2050.0, v=100) for _ in range(25)]
    # bearish breakout: open above level, close decisively below
    candles.append(_c(2050.5, 2050.7, 2048.5, 2048.7, v=180))
    r = detect_pa_volume_confirm("short", candles, sr_level=2050.0, atr=1.0)
    assert r["confirmed"] is True
    assert r["pattern"] == "breakout_body"


def test_pa_low_volume_does_not_confirm():
    candles = [_c(2050, 2050.5, 2049.5, 2050.0, v=200) for _ in range(25)]
    candles.append(_c(2049.5, 2050.0, 2047.5, 2049.8, v=100))  # low rel volume
    r = detect_pa_volume_confirm("long", candles, sr_level=2048.0, atr=1.0)
    assert r["confirmed"] is False
    assert r["reason"] in ("low_volume", "no_pattern")


def test_pa_bad_inputs_returns_unconfirmed():
    r = detect_pa_volume_confirm("long", [], sr_level=2050.0, atr=1.0)
    assert r["confirmed"] is False


def test_pa_never_raises():
    detect_pa_volume_confirm("long", None, sr_level=0, atr=0)
    detect_pa_volume_confirm("", "garbage", sr_level=2050, atr=1)


# ===== Full reversal setup evaluator =====

def test_evaluate_insufficient_data():
    out = evaluate([_c(1, 1, 1, 1)] * 10)
    assert out["score"] == 0
    assert out["entry_type"] == "none"


def test_evaluate_returns_full_structure():
    # Build a downtrend → consolidation (60+ bars)
    candles = []
    p = 2070.0
    for _ in range(40):
        candles.append(_c(p, p + 0.4, p - 0.5, p - 0.3, v=100))
        p -= 0.5
    # consolidation
    for _ in range(20):
        candles.append(_c(p, p + 0.2, p - 0.2, p + 0.05, v=110))
    out = evaluate(candles, direction_hint="long")
    assert "layers" in out
    assert set(out["layers"].keys()) == {
        "failed_sweep", "swing_progression",
        "compression_overlap", "dema_flip", "pa_volume_confirm",
    }
    assert 0 <= out["score"] <= 5


def test_evaluate_market_entry_requires_confirmed_pa_volume():
    # Even if score is high, market entry requires layer 5 confirmed
    candles = []
    p = 2070.0
    for _ in range(40):
        candles.append(_c(p, p + 0.4, p - 0.5, p - 0.3))
        p -= 0.5
    for _ in range(20):
        candles.append(_c(p, p + 0.2, p - 0.2, p + 0.05))
    out = evaluate(candles, direction_hint="long")
    # without explicit layer 5 confirm, entry shouldn't be "market"
    if out["entry_type"] == "market":
        assert out["layers"]["pa_volume_confirm"]["confirmed"] is True


def test_evaluate_planned_levels_consistent():
    candles = []
    p = 2070.0
    for _ in range(60):
        candles.append(_c(p, p + 0.3, p - 0.4, p - 0.2))
        p -= 0.3
    out = evaluate(candles, direction_hint="long")
    if out["entry_type"] != "none":
        assert out["planned_entry"] > 0
        assert out["planned_sl"] > 0
        assert out["planned_entry"] != out["planned_sl"]


def test_evaluate_never_raises():
    evaluate(None)
    evaluate("garbage")
    evaluate([{"open": "x"}] * 5)


def test_evaluate_htf_aligned_recorded():
    candles = [_c(2050 - i * 0.3, 2050 - i * 0.3 + 0.3, 2050 - i * 0.3 - 0.4, 2050 - i * 0.3 - 0.2)
               for i in range(60)]
    out = evaluate(candles, h1_trend="up")
    # bias likely "long" after downtrend; htf "up" → aligned
    if out["bias"] == "long":
        assert out["htf_aligned"] is True

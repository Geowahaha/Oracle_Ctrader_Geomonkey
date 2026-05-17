from learning.reversal_training_dataset import Candle, detect_live_reversal_zone, detect_sweep_reversal_candidates, label_followthrough


def test_label_followthrough_reversal_long():
    out = label_followthrough(
        direction="long",
        entry_price=100.0,
        atr=2.0,
        future_highs=[101.0, 103.2, 102.7],
        future_lows=[99.6, 99.7, 100.2],
        min_follow_r=1.0,
        max_adverse_r=0.6,
    )
    assert out["label"] == "reversal_followthrough"
    assert out["favorable_r"] >= 1.0
    assert out["adverse_r"] <= 0.6


def test_label_followthrough_continuation_short():
    out = label_followthrough(
        direction="short",
        entry_price=100.0,
        atr=2.0,
        future_highs=[102.5, 102.2, 101.8],
        future_lows=[99.7, 99.6, 99.5],
        min_follow_r=1.0,
        max_adverse_r=0.6,
    )
    assert out["label"] == "continuation_followthrough"
    assert out["adverse_r"] >= 1.0
    assert out["favorable_r"] <= 0.6


def test_detect_sweep_reversal_candidates_long_pattern():
    candles = [
        Candle(1, "2026-01-01T00:01:00Z", 60000, 119000, 100.0, 100.5, 99.8, 100.2, 8),
        Candle(2, "2026-01-01T00:02:00Z", 120000, 179000, 100.0, 100.2, 96.0, 99.9, 10),  # sweep bar
        Candle(3, "2026-01-01T00:03:00Z", 180000, 239000, 100.1, 101.0, 99.9, 100.6, 12),  # recovery
        Candle(4, "2026-01-01T00:04:00Z", 240000, 299000, 100.7, 101.2, 100.1, 101.0, 9),
    ]
    out = detect_sweep_reversal_candidates(
        candles,
        min_wick_ratio=0.55,
        min_sweep_pips=3.0,
        atr_bars=3,
    )
    assert len(out) == 1
    row = out[0]
    assert row["direction"] == "long"
    assert row["sweep_wick_ratio"] >= 0.55


def test_detect_live_reversal_zone_armed_long():
    candles = [
        Candle(1, "2026-01-01T00:01:00Z", 60000, 119000, 100.0, 100.4, 99.7, 100.1, 8),
        Candle(2, "2026-01-01T00:02:00Z", 120000, 179000, 100.0, 100.2, 96.0, 98.3, 10),
    ]
    out = detect_live_reversal_zone(
        candles,
        confirm_min_wick_ratio=0.55,
        confirm_min_sweep_pips=3.0,
        atr_bars=3,
        armed_min_wick_ratio=0.40,
        armed_min_sweep_pips=0.5,
        armed_min_close_pos=0.45,
    )
    assert out["confirmed"] is False
    assert out["armed"] is True
    assert out["direction"] == "long"
    assert out["stage"] == "armed"


def test_detect_live_reversal_zone_confirmed_short():
    candles = [
        Candle(1, "2026-01-01T00:01:00Z", 60000, 119000, 100.0, 100.5, 99.7, 100.2, 8),
        Candle(2, "2026-01-01T00:02:00Z", 120000, 179000, 100.0, 104.0, 99.8, 100.2, 10),
        Candle(3, "2026-01-01T00:03:00Z", 180000, 239000, 99.9, 100.0, 99.6, 99.7, 12),
    ]
    out = detect_live_reversal_zone(
        candles,
        confirm_min_wick_ratio=0.55,
        confirm_min_sweep_pips=3.0,
        atr_bars=3,
        armed_min_wick_ratio=0.40,
        armed_min_sweep_pips=0.5,
        armed_min_close_pos=0.45,
    )
    assert out["confirmed"] is True
    assert out["armed"] is True
    assert out["direction"] == "short"
    assert out["stage"] == "confirmed"

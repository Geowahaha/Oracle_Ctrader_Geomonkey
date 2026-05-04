from types import SimpleNamespace

from analysis.fibo_tf_telemetry import (
    fibo_ratio_zone,
    fibo_tf_metadata,
    fibo_parent_impulse_id,
)


def test_ratio_zone_labels_golden_and_deep_retest():
    assert fibo_ratio_zone(0.501)["ratio_zone"] == "near_0.50"
    assert fibo_ratio_zone(0.618)["ratio_zone"] == "near_0.618"
    assert fibo_ratio_zone(0.665)["ratio_zone"] == "0.65_0.70"
    assert fibo_ratio_zone(0.886)["ratio_zone"] == "0.886_deep_retest"


def test_tf_metadata_keeps_source_stable_but_adds_display_source():
    meta = fibo_tf_metadata(entry_tf="M1", setup_tf="M5", parent_tf="H1", source="fibo_xauusd")
    assert meta["tf_label"] == "M1"
    assert meta["display_source"] == "fibo_M1_xauusd"
    assert meta["source_stable"] == "fibo_xauusd"
    assert meta["parent_tf"] == "H1"


def test_parent_impulse_id_is_stable_from_swing_context():
    fib = SimpleNamespace(direction="bullish", swing_start=4500.123, swing_end=4600.456, swing_start_idx=10, swing_end_idx=42)
    first = fibo_parent_impulse_id("H1", fib)
    second = fibo_parent_impulse_id("h1", fib)
    assert first == second
    assert first.startswith("H1:bullish:")

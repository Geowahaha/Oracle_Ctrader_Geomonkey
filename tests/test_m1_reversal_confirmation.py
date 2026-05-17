from analysis.m1_reversal_confirmation import detect_engulfing, shadow_log_payload


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_bullish_engulfing_confirmed_for_long():
    candles = [
        _candle(2050.0, 2050.5, 2049.0, 2049.2),
        _candle(2049.0, 2051.0, 2048.8, 2050.8),
    ]
    r = detect_engulfing("long", candles, entry_price=2050.5)
    assert r["confirmed"] is True
    assert r["pattern"] == "bullish_engulfing"
    assert r["body_ratio"] >= 1.0


def test_bearish_engulfing_confirmed_for_short():
    candles = [
        _candle(2049.0, 2050.5, 2048.8, 2050.0),
        _candle(2050.5, 2050.8, 2048.5, 2048.8),
    ]
    r = detect_engulfing("short", candles, entry_price=2049.0)
    assert r["confirmed"] is True
    assert r["pattern"] == "bearish_engulfing"


def test_no_engulfing_pattern_returns_false_not_none():
    candles = [
        _candle(2050.0, 2050.2, 2049.8, 2050.1),
        _candle(2050.1, 2050.3, 2049.9, 2050.2),
    ]
    r = detect_engulfing("long", candles, entry_price=2050.5)
    assert r["confirmed"] is False
    assert r["reason"] in ("no_pattern", "body_too_small")


def test_insufficient_data_returns_none():
    r = detect_engulfing("long", [], entry_price=2050.0)
    assert r["confirmed"] is None
    assert r["reason"] == "insufficient_data"


def test_bad_direction_returns_none():
    r = detect_engulfing("", [_candle(1, 1, 1, 1)] * 2)
    assert r["confirmed"] is None
    assert r["reason"] == "bad_direction"


def test_too_far_from_entry_marked_unconfirmed():
    candles = [
        _candle(2050.0, 2050.5, 2049.0, 2049.2),
        _candle(2049.0, 2051.0, 2048.8, 2050.8),
    ]
    r = detect_engulfing("long", candles, entry_price=2070.0, max_distance_pips=30.0)
    assert r["confirmed"] is False
    assert r["reason"] == "too_far_from_entry"


def test_shadow_log_payload_has_all_keys():
    payload = shadow_log_payload(
        "long",
        [_candle(2050.0, 2050.5, 2049.0, 2049.2), _candle(2049.0, 2051.0, 2048.8, 2050.8)],
        entry_price=2050.5,
        session="new_york",
        regime="trend",
        family="xau_scalp_pullback_limit",
    )
    expected = {
        "m1_engulfing_confirmed",
        "m1_engulfing_pattern",
        "m1_engulfing_body_ratio",
        "m1_engulfing_distance_pips",
        "m1_engulfing_reason",
        "m1_engulfing_session",
        "m1_engulfing_regime",
        "m1_engulfing_family",
    }
    assert expected.issubset(set(payload.keys()))
    assert payload["m1_engulfing_confirmed"] is True


def test_never_raises_on_garbage_input():
    detect_engulfing("long", None)
    detect_engulfing("long", "not_a_list")
    detect_engulfing("long", [{"open": "x"}])

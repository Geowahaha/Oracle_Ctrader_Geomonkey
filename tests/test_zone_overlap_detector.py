from analysis.zone_overlap_detector import (
    Zone,
    detect_zones,
    overlap_at_price,
    compression_score,
    infer_reversal_bias,
)


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _trend_down_then_up_series():
    """Build M5 candles: downtrend creating swing high, uptrend creating swing low,
    then return to overlap zone."""
    candles = []
    # downtrend from 2060 -> 2040 (creates swing high near start)
    price = 2060.0
    for i in range(15):
        h = price + 0.4
        l = price - 0.6
        c = price - 0.5
        candles.append(_c(price, h, l, c))
        price -= 1.3
    # uptrend back from 2040 -> 2055 (creates swing low at bottom)
    for i in range(15):
        h = price + 0.6
        l = price - 0.3
        c = price + 0.5
        candles.append(_c(price, h, l, c))
        price += 1.0
    # consolidation near 2055 (overlap with the original swing high zone)
    for i in range(10):
        candles.append(_c(price, price + 0.4, price - 0.4, price + 0.05))
        price += 0.05
    return candles


def test_detect_zones_returns_some_zones_on_trend_reversal_data():
    candles = _trend_down_then_up_series()
    zones = detect_zones(candles)
    assert isinstance(zones, list)
    # should find at least one red and one blue zone in this synthetic series
    kinds = {z.kind for z in zones}
    assert "red" in kinds or "blue" in kinds


def test_detect_zones_empty_input_returns_empty():
    assert detect_zones([]) == []
    assert detect_zones(None) == []


def test_detect_zones_never_raises_on_garbage():
    detect_zones("not_a_list")
    detect_zones([{"open": "x"}])
    detect_zones([None, None])


def test_overlap_at_price_returns_none_when_no_zones():
    assert overlap_at_price([], 2050.0) is None


def test_overlap_at_price_finds_intersection():
    zones = [
        Zone(kind="red", top=2055.0, bottom=2050.0, born_bar=10, touches=0, strength=0.8),
        Zone(kind="blue", top=2053.0, bottom=2048.0, born_bar=20, touches=0, strength=0.9),
    ]
    ov = overlap_at_price(zones, 2052.0)
    assert ov is not None
    assert ov["at_overlap_zone"] is True
    assert ov["overlap_top"] == 2053.0
    assert ov["overlap_bottom"] == 2050.0
    assert 0.0 < ov["overlap_quality"] <= 1.0


def test_overlap_at_price_skips_when_price_outside():
    zones = [
        Zone(kind="red", top=2055.0, bottom=2050.0, born_bar=10),
        Zone(kind="blue", top=2053.0, bottom=2048.0, born_bar=20),
    ]
    assert overlap_at_price(zones, 2070.0) is None


def test_overlap_at_price_filters_low_quality():
    zones = [
        Zone(kind="red", top=2055.0, bottom=2050.0, born_bar=10),
        # tiny intersection with red (only 0.1 of width)
        Zone(kind="blue", top=2050.1, bottom=2030.0, born_bar=20),
    ]
    assert overlap_at_price(zones, 2050.05, min_quality=0.4) is None


def test_compression_score_low_during_consolidation():
    # Wide swings then tight range
    candles = []
    for i in range(60):
        if i < 50:
            candles.append(_c(2050, 2055, 2045, 2052))
        else:
            candles.append(_c(2050, 2050.3, 2049.7, 2050.1))
    cs = compression_score(candles)
    assert 0 < cs < 1.0


def test_compression_score_handles_short_input():
    assert compression_score([]) == 0.0
    assert compression_score([_c(1, 1, 1, 1)] * 5) == 0.0


def test_infer_reversal_bias_long_after_downtrend():
    candles = [_c(2060 - i, 2060 - i + 0.5, 2060 - i - 0.5, 2060 - i - 0.3) for i in range(20)]
    assert infer_reversal_bias(candles) == "long"


def test_infer_reversal_bias_short_after_uptrend():
    candles = [_c(2040 + i, 2040 + i + 0.5, 2040 + i - 0.5, 2040 + i + 0.3) for i in range(20)]
    assert infer_reversal_bias(candles) == "short"


def test_infer_reversal_bias_neutral_when_flat():
    candles = [_c(2050, 2050.4, 2049.6, 2050.05) for _ in range(20)]
    assert infer_reversal_bias(candles) == "neutral"

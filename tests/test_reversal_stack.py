from analysis.reversal_stack import score_reversal, size_tilt_from_score


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_score_zero_when_no_inputs_aligned():
    r = score_reversal(overlap=None, direction="long")
    assert r["confirms"] == 0


def test_score_bad_direction_returns_zero():
    r = score_reversal(overlap=None, direction="")
    assert r["confirms"] == 0


def test_full_stack_long_max_score():
    candles = [
        _c(2050.0, 2050.5, 2049.0, 2049.2),
        _c(2049.0, 2051.0, 2048.8, 2050.8),
    ]
    r = score_reversal(
        overlap={"at_overlap_zone": True, "overlap_quality": 0.8},
        direction="long",
        m1_candles=candles,
        h1_dema_slope=0.2,
        delta_proxy=0.15,
        depth_imbalance=0.7,
        compression_score=0.5,
        failed_sweep_wick=True,
        overlap_top=2051.0,
        overlap_bottom=2049.0,
    )
    assert r["confirms"] == 6


def test_partial_stack_short_three_confirms():
    r = score_reversal(
        overlap={"at_overlap_zone": True, "overlap_quality": 0.5},
        direction="short",
        m1_candles=None,
        h1_dema_slope=-0.3,
        delta_proxy=-0.10,
        depth_imbalance=-0.7,
        compression_score=0.9,
        failed_sweep_wick=False,
    )
    assert r["confirms"] == 3


def test_size_tilt_never_below_one():
    assert size_tilt_from_score(0, overlap_present=False, bias_aligned=False) == 1.0
    assert size_tilt_from_score(6, overlap_present=False, bias_aligned=True) == 1.0
    assert size_tilt_from_score(6, overlap_present=True, bias_aligned=False) == 1.0


def test_size_tilt_high_conviction():
    assert size_tilt_from_score(4, overlap_present=True, bias_aligned=True) == 1.30
    assert size_tilt_from_score(5, overlap_present=True, bias_aligned=True) == 1.30


def test_size_tilt_medium_conviction():
    assert size_tilt_from_score(2, overlap_present=True, bias_aligned=True) == 1.10
    assert size_tilt_from_score(3, overlap_present=True, bias_aligned=True) == 1.10


def test_size_tilt_low_conviction_no_tilt():
    assert size_tilt_from_score(1, overlap_present=True, bias_aligned=True) == 1.0


def test_score_never_raises_on_garbage_inputs():
    score_reversal(overlap={}, direction="long", m1_candles="garbage")
    score_reversal(overlap=None, direction="short", delta_proxy="x", depth_imbalance=None)

"""Unit tests for dexter3.stop_floor — the volatility-normalized SL floor.

Pins the invariants the live path depends on: widen-only, RR preserved,
never fabricate/repair geometry, fail-open on unusable input, and the
max_sl_abs bound that keeps a widened stop from turning into an executor
refusal at the XAU 1-ounce volume floor.
"""
from __future__ import annotations

import pytest

from dexter3.stop_floor import apply_floor, median_true_range


def _bars(n: int, hi: float, lo: float, close: float) -> list[dict]:
    return [{"high": hi, "low": lo, "close": close, "open": close} for _ in range(n)]


# --- median_true_range ------------------------------------------------------

def test_mtr_constant_bars():
    # h-l = 2.0 every bar, no gaps -> TR = 2.0
    assert median_true_range(_bars(20, 101.0, 99.0, 100.0), lookback=14) == 2.0


def test_mtr_counts_gaps():
    bars = [{"high": 101.0, "low": 99.0, "close": 100.0, "open": 100.0},
            {"high": 111.0, "low": 109.0, "close": 110.0, "open": 110.0}]
    # |high - prev_close| = 11 > (high-low)=2 -> gap dominates
    assert median_true_range(bars, lookback=14) == 11.0


def test_mtr_robust_to_single_spike():
    bars = _bars(20, 101.0, 99.0, 100.0)
    bars.append({"high": 200.0, "low": 50.0, "close": 100.0, "open": 100.0})
    # median ignores the spike (a mean would not)
    assert median_true_range(bars, lookback=14) == 2.0


@pytest.mark.parametrize("bars", [[], [{"high": 1, "low": 1, "close": 1}], None])
def test_mtr_insufficient_data_returns_zero(bars):
    assert median_true_range(bars or [], lookback=14) == 0.0


def test_mtr_skips_unusable_bars():
    bars = [{"high": 0, "low": 0, "close": 0}, {"high": 0, "low": 0, "close": 0}]
    assert median_true_range(bars, lookback=14) == 0.0


# --- apply_floor: the core behaviour ----------------------------------------

def test_buy_tight_stop_is_widened_and_rr_preserved():
    # entry 100, sl 99 (dist 1.0), tp 102 (dist 2.0, RR 2.0); TR 2.0, mult 1.2
    out = apply_floor(side="buy", entry=100.0, sl=99.0, tp=102.0, tr=2.0, mult=1.2)
    assert out is not None
    assert out["sl"] == pytest.approx(97.6)          # 100 - 2.4
    assert out["tp"] == pytest.approx(104.8)         # 100 + 2.0*2.4
    rr = (out["tp"] - 100.0) / (100.0 - out["sl"])
    assert rr == pytest.approx(2.0)                  # RR preserved exactly
    assert out["meta"]["sl_over_tr_after"] == pytest.approx(1.2)
    assert out["meta"]["capped_by_max_abs"] is False


def test_sell_tight_stop_is_widened_and_rr_preserved():
    out = apply_floor(side="sell", entry=100.0, sl=101.0, tp=98.0, tr=2.0, mult=1.2)
    assert out is not None
    assert out["sl"] == pytest.approx(102.4)
    assert out["tp"] == pytest.approx(95.2)
    rr = (100.0 - out["tp"]) / (out["sl"] - 100.0)
    assert rr == pytest.approx(2.0)


def test_already_wide_enough_is_untouched():
    # sl_dist 3.0 >= floor 1.2*2.0 = 2.4
    assert apply_floor(side="buy", entry=100.0, sl=97.0, tp=106.0, tr=2.0, mult=1.2) is None


def test_widen_only_never_tightens():
    # a very small mult must NOT pull the stop closer
    assert apply_floor(side="buy", entry=100.0, sl=95.0, tp=110.0, tr=2.0, mult=0.5) is None


def test_max_abs_caps_the_floor():
    # floor would be 1.2*10=12.0 but the cap allows only 5.0
    out = apply_floor(side="buy", entry=100.0, sl=99.0, tp=102.0, tr=10.0, mult=1.2, max_sl_abs=5.0)
    assert out is not None
    assert out["meta"]["sl_dist_after"] == pytest.approx(5.0)
    assert out["meta"]["capped_by_max_abs"] is True
    assert out["sl"] == pytest.approx(95.0)


def test_max_abs_below_current_sl_leaves_untouched():
    # cap (2.0) is already <= current sl_dist (3.0) -> nothing to do, no tightening
    assert apply_floor(side="buy", entry=100.0, sl=97.0, tp=106.0, tr=10.0,
                       mult=1.2, max_sl_abs=2.0) is None


# --- fail-open / never fabricate --------------------------------------------

@pytest.mark.parametrize("kw", [
    {"tr": 0.0},            # unknown volatility
    {"mult": 0.0},          # feature disabled
    {"entry": 0.0},
    {"sl": 0.0},
    {"tp": 0.0},
    {"tr": float("nan")},
])
def test_unusable_inputs_return_none(kw):
    base = dict(side="buy", entry=100.0, sl=99.0, tp=102.0, tr=2.0, mult=1.2)
    base.update(kw)
    assert apply_floor(**base) is None


@pytest.mark.parametrize("side", ["", "long", None, "BUY "])
def test_bad_side_returns_none(side):
    if side == "BUY ":
        # whitespace/case IS normalised - this one must work
        assert apply_floor(side=side, entry=100.0, sl=99.0, tp=102.0, tr=2.0, mult=1.2) is not None
        return
    assert apply_floor(side=side, entry=100.0, sl=99.0, tp=102.0, tr=2.0, mult=1.2) is None


def test_inverted_buy_geometry_is_not_repaired():
    # sl above entry on a buy = a broken producer decision; do not touch it
    assert apply_floor(side="buy", entry=100.0, sl=101.0, tp=102.0, tr=2.0, mult=1.2) is None


def test_inverted_sell_geometry_is_not_repaired():
    assert apply_floor(side="sell", entry=100.0, sl=99.0, tp=98.0, tr=2.0, mult=1.2) is None


def test_output_keeps_sl_entry_tp_ordering():
    out = apply_floor(side="buy", entry=100.0, sl=99.5, tp=101.0, tr=3.0, mult=1.5)
    assert out is not None and out["sl"] < 100.0 < out["tp"]
    out2 = apply_floor(side="sell", entry=100.0, sl=100.5, tp=99.0, tr=3.0, mult=1.5)
    assert out2 is not None and out2["tp"] < 100.0 < out2["sl"]


def test_realistic_xau_case_from_the_audit():
    # the 2026-07-25 -9.56 fable loss: SL 2.9 pts while TR ~3.37 -> 0.86x TR.
    # At mult 1.2 the floor lifts it to ~4.04 (well under fable's $12 abs cap).
    out = apply_floor(side="buy", entry=4064.0, sl=4061.1, tp=4078.0, tr=3.37, mult=1.2)
    assert out is not None
    assert out["meta"]["sl_over_tr_before"] == pytest.approx(0.8605, abs=1e-3)
    assert out["meta"]["sl_dist_after"] == pytest.approx(4.044, abs=1e-3)
    assert out["meta"]["sl_dist_after"] < 12.0  # would not trip the abs cap

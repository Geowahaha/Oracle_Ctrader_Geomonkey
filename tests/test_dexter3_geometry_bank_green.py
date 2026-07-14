"""Tests for the "bank" exit style in scripts/dexter3_geometry_optimizer.py.

Owner directive 2026-07-14: replay the ORIGINAL Dexter3 v1.0 basket exit
(2026-07-05, commit 3ca3341, basket_manager.py close_all_in_profit) inside
the exit-geometry grid as a single-trade approximation — bank the whole
position at the first M5 CLOSE where floating R >= bank_target_r (v1.0
default 0.2R). Conservative SL-first on the same bar; timeout marks to the
last walked bar's close, same convention as ``_simulate``.

Research-only script, no live behavior touched.
"""
from __future__ import annotations

import pytest

from scripts.dexter3_geometry_optimizer import _combo_r, _simulate_bank


def _bar(o: float, h: float, l: float, c: float) -> dict:
    return {"open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# (a) banks at the first CLOSE where floating R >= target, on an up-drift
# ---------------------------------------------------------------------------


def test_banks_at_first_close_meeting_target_buy_side():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    future = [
        _bar(2000.5, 2001.0, 2000.0, 2000.5),   # R = 0.25 < 0.5
        _bar(2000.5, 2001.5, 2000.4, 2001.2),   # R = 0.60 >= 0.5 -> bank here
        _bar(2001.2, 2005.0, 2001.0, 2004.0),   # would be way past target — must NOT reach here
    ]
    outcome, r, bars_held = _simulate_bank("buy", entry, sl, future, max_hold=10, target_r=0.5)
    assert outcome == "bank"
    assert bars_held == 1
    assert r == pytest.approx((2001.2 - entry) / 2.0)


# ---------------------------------------------------------------------------
# (b) SL-first conservative check wins on a bar whose LOW breaches SL even
#     though that same bar's close would otherwise be green (buy side)
# ---------------------------------------------------------------------------


def test_sl_first_wins_even_when_same_bar_close_is_green_buy_side():
    entry, sl = 2000.0, 1998.0
    future = [
        # low wicks through SL, but close is above entry (would look "green")
        _bar(2000.0, 2001.0, 1997.5, 2000.5),
    ]
    outcome, r, bars_held = _simulate_bank("buy", entry, sl, future, max_hold=10, target_r=0.2)
    assert outcome == "loss"
    assert r == pytest.approx(-1.0)
    assert bars_held == 0


# ---------------------------------------------------------------------------
# (c) sell-side banking: close below entry by >= target_r
# ---------------------------------------------------------------------------


def test_banks_on_sell_side_when_close_drops_by_target():
    entry, sl = 2000.0, 2002.0  # risk = 2.0
    future = [
        _bar(2000.0, 2000.3, 1999.5, 1999.8),   # R = 0.10 < 0.3
        _bar(1999.8, 2000.0, 1998.5, 1999.0),   # R = 0.50 >= 0.3 -> bank
    ]
    outcome, r, bars_held = _simulate_bank("sell", entry, sl, future, max_hold=10, target_r=0.3)
    assert outcome == "bank"
    assert bars_held == 1
    assert r == pytest.approx((entry - 1999.0) / 2.0)


def test_sl_first_wins_on_sell_side_even_when_close_would_be_green():
    entry, sl = 2000.0, 2002.0
    future = [
        # high wicks through SL, but close is below entry (would look "green")
        _bar(2000.0, 2002.5, 1999.0, 1999.5),
    ]
    outcome, r, bars_held = _simulate_bank("sell", entry, sl, future, max_hold=10, target_r=0.1)
    assert outcome == "loss"
    assert r == pytest.approx(-1.0)
    assert bars_held == 0


# ---------------------------------------------------------------------------
# (d) timeout: never reaches target_r within max_hold -> exits at the last
#     WALKED bar's close R (same "win"/"loss"-by-sign convention as _simulate)
# ---------------------------------------------------------------------------


def test_timeout_exits_at_last_walked_bar_close_when_never_green_enough():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    future = [
        _bar(2000.0, 2000.4, 1999.8, 2000.2),   # R = 0.10
        _bar(2000.2, 2000.6, 2000.0, 2000.3),   # R = 0.15
        _bar(2000.3, 2000.7, 2000.1, 2000.25),  # R = 0.125 — never hits 0.5 target
    ]
    outcome, r, bars_held = _simulate_bank("buy", entry, sl, future, max_hold=3, target_r=0.5)
    # last walked bar is future[2] (max_hold=3 -> walks indices 0,1,2)
    expected_r = (2000.25 - entry) / 2.0
    assert outcome == "win"  # r > 0
    assert r == pytest.approx(expected_r)
    assert bars_held == 2


def test_timeout_reports_loss_when_last_close_is_negative_r():
    entry, sl = 2000.0, 1998.0
    future = [
        _bar(2000.0, 2000.2, 1999.9, 1999.95),  # R negative, never hits SL or target
    ]
    outcome, r, bars_held = _simulate_bank("buy", entry, sl, future, max_hold=5, target_r=0.5)
    assert outcome == "loss"  # r <= 0 but SL never touched
    assert r == pytest.approx((1999.95 - entry) / 2.0)
    assert bars_held == 0


def test_zero_risk_is_skip():
    outcome, r, bars_held = _simulate_bank("buy", 2000.0, 2000.0, [_bar(2000, 2001, 1999, 2000.5)],
                                            max_hold=5, target_r=0.2)
    assert outcome == "skip"
    assert r == 0.0
    assert bars_held == 0


# ---------------------------------------------------------------------------
# (e) style="bank" through _combo_r applies the same spread/commission cost
#     adjustment as "plain" (numeric comparison, not just "doesn't crash")
# ---------------------------------------------------------------------------


def test_combo_r_bank_style_applies_same_cost_adjustment_as_plain():
    # A trade that both a plain SL/TP replay and a bank replay resolve
    # identically-in-R on: TP is far away (irrelevant to bank), and the bank
    # target is met on the exact bar that would otherwise time out green.
    entry, sl, tp = 2000.0, 1998.0, 2100.0  # risk = 2.0, TP unreachable in this future
    future = [
        _bar(2000.0, 2000.4, 1999.8, 2000.2),   # R = 0.10
        _bar(2000.2, 2001.5, 2000.1, 2001.2),   # R = 0.60 -> bank here at target 0.5
    ]
    trade = {"side": "buy", "entry": entry, "sl": sl, "tp": tp, "future": future}
    spread_abs, commission_r = 0.12, 0.03

    bank_result = _combo_r(trade, "bank", max_hold=10, disaster=2.0, sl_mult=1.0,
                            spread_abs=spread_abs, commission_r=commission_r, bank_target_r=0.5)

    # Manually replicate _simulate_bank's raw R for this same scenario and
    # apply the identical cost formula used by _combo_r for every style.
    raw_outcome, raw_r, _ = _simulate_bank("buy", entry, sl, future, max_hold=10, target_r=0.5)
    risk = abs(entry - sl)
    expected = raw_r - (spread_abs / risk + commission_r)

    assert raw_outcome == "bank"
    assert bank_result == pytest.approx(expected)

    # And confirm bank's cost adjustment formula is IDENTICAL in shape to
    # plain's (same trade, forcing a plain SL/TP outcome that also resolves
    # in-bar for a clean apples-to-apples cost-formula check).
    plain_future = [_bar(2000.0, 2100.5, 1999.0, 2100.2)]  # TP hit in-bar
    plain_trade = {"side": "buy", "entry": entry, "sl": sl, "tp": tp, "future": plain_future}
    plain_result = _combo_r(plain_trade, "plain", max_hold=10, disaster=2.0, sl_mult=1.0,
                             spread_abs=spread_abs, commission_r=commission_r)
    plain_raw_r = (tp - entry) / risk
    plain_expected = plain_raw_r - (spread_abs / risk + commission_r)
    assert plain_result == pytest.approx(plain_expected)


def test_combo_r_bank_style_requires_bank_target_r():
    trade = {"side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2100.0,
              "future": [_bar(2000.0, 2000.4, 1999.8, 2000.2)]}
    with pytest.raises(ValueError):
        _combo_r(trade, "bank", max_hold=10, disaster=2.0, sl_mult=1.0,
                 spread_abs=0.12, commission_r=0.03)

"""Tests for scripts/dexter3_convex_exit_replay.py.

Owner directive 2026-07-16: the live DRAGON LADDER (dexter3/opening_manager.py
``ladder_floor_r``) arms on the very first small pop and gets knocked out by
the NORMAL ~0.40R dip that precedes almost every real runner -- no prior
replay in this repo ever modeled that ladder as the baseline. These tests
pin down (a) the discrete-rung ladder replay, (b) the continuous convex
trail, and (c) the pyramid second-leg overlay, all via hand-computed OHLC
bars so every asserted number traces to arithmetic in the test itself.

Pure synthetic bars, no transport. Conventions follow
tests/test_dexter3_geometry_bank_green.py (SL-first conservative, timeout ->
last walked bar's close).
"""
from __future__ import annotations

import pytest

from scripts.dexter3_convex_exit_replay import (
    _combo_r,
    _pyramid_fill,
    _simulate_convex,
    _simulate_ladder,
)

RUNGS = [(0.25, 0.02), (0.50, 0.15), (0.80, 0.40), (1.20, 0.80), (2.00, 1.45), (3.00, 2.25)]


def _bar(o: float, h: float, l: float, c: float) -> dict:
    return {"open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# (a) ladder arms on a rung and exits at the floor on retrace (hand-computed)
# ---------------------------------------------------------------------------


def test_ladder_arms_and_exits_at_floor_on_retrace():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    # peak/adverse values are kept clearly clear of the 0.80 rung boundary
    # (not exactly 0.80) to avoid a float-equality flake at the threshold.
    future = [
        # peak_r = (2001.7-2000)/2 = 0.85 -> rung (0.80, 0.40) arms, floor=0.40
        # adverse_r = (2000.9-2000)/2 = 0.45 > 0.40 -> no exit yet
        _bar(2001.3, 2001.7, 2000.9, 2001.6),
        # bar_peak_r = (2001.6-2000)/2 = 0.80 < 0.85 -> peak stays 0.85, floor stays 0.40
        # adverse_r = (2000.75-2000)/2 = 0.375 <= 0.40 -> exit AT the floor
        _bar(2001.6, 2001.6, 2000.75, 2001.0),
    ]
    outcome, r, held = _simulate_ladder("buy", entry, sl, future, RUNGS, max_hold=10)
    assert outcome == "ladder"
    assert r == pytest.approx(0.40)
    assert held == 1


def test_ladder_sell_side_arms_and_exits_at_floor():
    entry, sl = 2000.0, 2002.0  # risk = 2.0, sell side (profit = price down)
    future = [
        # peak_r (sell) = (entry-lo)/risk = (2000-1999.0)/2 = 0.50 -> rung
        # (0.50, 0.15) arms, floor=0.15. adverse_r (sell) = (entry-hi)/risk =
        # (2000-1999.6)/2 = 0.20 > 0.15 -> no exit yet.
        _bar(1999.6, 1999.6, 1999.0, 1999.3),
        # bar_peak_r = (2000-1999.4)/2 = 0.30 < 0.50 -> peak stays 0.50, floor stays 0.15
        # adverse_r = (2000-1999.7)/2 = 0.15 <= 0.15 -> exit at floor
        _bar(1999.5, 1999.7, 1999.4, 1999.6),
    ]
    outcome, r, held = _simulate_ladder("sell", entry, sl, future, RUNGS, max_hold=10)
    assert outcome == "ladder"
    assert r == pytest.approx(0.15)
    assert held == 1


# ---------------------------------------------------------------------------
# (b) ladder does NOT exit if peak never reaches the first rung (0.25)
# ---------------------------------------------------------------------------


def test_ladder_never_arms_when_peak_stays_below_first_rung():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    future = [
        _bar(2000.0, 2000.3, 1999.8, 2000.2),   # peak_r = 0.15 < 0.25
        _bar(2000.2, 2000.4, 1999.9, 2000.3),   # peak_r = 0.20 < 0.25 -- still never arms
    ]
    outcome, r, held = _simulate_ladder("buy", entry, sl, future, RUNGS, max_hold=2)
    # never armed -> no "ladder" exit possible; resolves as a timeout mark-to-close
    assert outcome != "ladder"
    assert outcome == "win"
    assert r == pytest.approx(0.15)
    assert held == 1


# ---------------------------------------------------------------------------
# (c) convex: no trail before arm_at_r, then trails at peak - giveback
# ---------------------------------------------------------------------------


def test_convex_does_not_trail_before_arm_then_trails_at_peak_minus_giveback():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    atr, arm_at_r, giveback_atr = 2.0, 1.0, 1.0   # giveback_r = 1.0*2.0/2.0 = 1.0
    future = [
        # peak_r = 0.5 < arm_at_r(1.0) -- NOT armed. If a bug armed pre-threshold,
        # a naive trail (peak 0.5 - giveback 1.0 = -0.5) would fire here, since
        # adverse_r = (1998.5-2000)/2 = -0.75 <= -0.5 -- this bar is a positive
        # control proving the arm-gate actually blocks the trail before arm_at_r.
        _bar(2000.0, 2001.0, 1998.5, 2000.9),
        # gap up (valid OHLC: open at this bar's own low). peak jumps to 3.0
        # (armed now). trail_level = 3.0 - 1.0 = 2.0 (price 2004.0).
        # adverse_r = (2004.5-2000)/2 = 2.25 > 2.0 -- no exit yet.
        _bar(2004.5, 2006.0, 2004.5, 2005.5),
        # gap down (open at this bar's own high). peak stays 3.0 (this bar's
        # high -> r=2.5 < 3.0). trail_level stays 2.0.
        # adverse_r = (2003.5-2000)/2 = 1.75 <= 2.0 -- exit at the trail level.
        _bar(2005.0, 2005.0, 2003.5, 2004.0),
    ]
    outcome, r, held = _simulate_convex("buy", entry, sl, future, arm_at_r, giveback_atr, atr, max_hold=3)
    assert outcome == "trail"
    assert r == pytest.approx(2.0)
    assert held == 2


# ---------------------------------------------------------------------------
# (d) convex SL-first on the same bar (huge favorable wick, but low breaches SL)
# ---------------------------------------------------------------------------


def test_convex_sl_first_wins_even_with_a_huge_favorable_wick_same_bar():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    future = [
        # hi=2010 -> would-be peak_r = 5.0 (armed many times over), but
        # lo=1997.0 breaches SL(1998) -- SL must win regardless.
        _bar(2000.0, 2010.0, 1997.0, 2005.0),
    ]
    outcome, r, held = _simulate_convex("buy", entry, sl, future, arm_at_r=0.1, giveback_atr=1.0,
                                         atr=2.0, max_hold=5)
    assert outcome == "loss"
    assert r == pytest.approx(-1.0)
    assert held == 0


# ---------------------------------------------------------------------------
# (e) THE core regression: a "dip then 6R run" trade -- ladder captures a
#     near-breakeven scrap (armed by the initial pop, stopped by the normal
#     pre-run dip) while convex, un-armed through that same dip, rides the
#     run and locks in a multi-R trail exit. Same bars fed to both sims.
# ---------------------------------------------------------------------------


def test_dip_then_6r_run_ladder_scraps_convex_captures_multi_r():
    entry, sl = 2000.0, 1998.0  # risk = 2.0
    atr, arm_at_r, giveback_atr = 2.0, 1.0, 1.0   # giveback_r = 1.0

    future = [
        # 0: initial pop -- peak_r = 0.30. ladder arms rung (0.25,0.02) floor=0.02.
        #    convex: 0.30 < arm_at_r(1.0) -- not armed.
        _bar(2000.5, 2000.6, 2000.5, 2000.55),
        # 1: the NORMAL pre-run dip -- adverse_r = (1999.8-2000)/2 = -0.10 <= 0.02
        #    -> ladder EXITS here at its floor (0.02R, near breakeven).
        #    convex: bar_peak_r = 0.30 (not a new peak) -- still not armed, keeps going.
        _bar(2000.55, 2000.6, 1999.8, 1999.9),
        # 2: ramp begins (convex only walks past here; ladder already exited).
        # gap-up bars below (open == that bar's own low) -- valid OHLC, values
        # that matter for the sim (high/low/close) are unchanged.
        _bar(2001.6, 2002.0, 2001.6, 2001.9),    # peak_r=1.0 -> convex arms, trail=0.0
        _bar(2003.6, 2004.0, 2003.6, 2003.9),    # peak_r=2.0, trail=1.0
        _bar(2005.6, 2006.0, 2005.6, 2005.9),    # peak_r=3.0, trail=2.0
        _bar(2007.6, 2008.0, 2007.6, 2007.9),    # peak_r=4.0, trail=3.0
        _bar(2009.6, 2010.0, 2009.6, 2009.9),    # peak_r=5.0, trail=4.0
        _bar(2011.6, 2012.0, 2011.6, 2011.9),    # peak_r=6.0 (THE peak), trail=5.0
        # 8: sharp reversal -- adverse_r = (2005.0-2000)/2 = 2.5 <= 5.0 -> exit at trail(5.0)
        _bar(2011.9, 2012.0, 2005.0, 2006.0),
    ]

    ladder_outcome, ladder_r, ladder_held = _simulate_ladder("buy", entry, sl, future, RUNGS, max_hold=10)
    convex_outcome, convex_r, convex_held = _simulate_convex("buy", entry, sl, future, arm_at_r,
                                                              giveback_atr, atr, max_hold=10)

    assert ladder_outcome == "ladder"
    assert ladder_r == pytest.approx(0.02)
    assert ladder_held == 1

    assert convex_outcome == "trail"
    assert convex_r == pytest.approx(5.0)
    assert convex_held == 8

    # THE regression: on the identical bars, convex captures multi-R while the
    # live-shaped ladder captures a near-breakeven scrap of the same run.
    assert convex_r > 3.0
    assert ladder_r < 0.5
    assert convex_r > ladder_r * 10


# ---------------------------------------------------------------------------
# (f) / (g) pyramid overlay: fills on the dip and improves combined R; does
#     not fill (and does not change the result) when the dip never comes.
# ---------------------------------------------------------------------------


def _ladder_exit_fn(side, entry, sl, future, max_hold):
    return _simulate_ladder(side, entry, sl, future, RUNGS, max_hold)


def test_pyramid_fills_on_the_dip_and_combined_r_exceeds_leg1_alone():
    entry, sl = 2000.0, 1998.0  # risk1 = 2.0, dip_r=0.4 -> dip price = 1999.2
    future = [
        # dip touches 1999.2 in bar 0 (within window) -> leg2 fills here at 1999.2
        _bar(2000.0, 2000.1, 1999.2, 1999.5),
        # rally: leg1 (entry 2000, risk 2.0) peak_r=(2002-2000)/2=1.0 -> rung
        # (0.80,0.40) arms, floor=0.40; adverse_r=(1999.4-2000)/2=-0.30 <= 0.40
        # -> leg1 exits "ladder" at r1=0.40, held=1.
        _bar(1999.5, 2002.0, 1999.4, 2001.8),
    ]

    trade = {"side": "buy", "entry": entry, "sl": sl, "tp": 2100.0, "future": future}
    combined = _combo_r(trade, _ladder_exit_fn, max_hold=10, spread_abs=0.0, commission_r=0.0,
                         pyramid=True, dip_r=0.4, window_bars=3)
    assert combined is not None
    combined_r, filled = combined
    assert filled is True

    # leg1 alone (no pyramid) for comparison
    leg1_only = _combo_r(trade, _ladder_exit_fn, max_hold=10, spread_abs=0.0, commission_r=0.0,
                          pyramid=False, dip_r=0.4, window_bars=3)
    assert leg1_only is not None
    leg1_r, leg1_filled = leg1_only
    assert leg1_filled is False

    # cross-check leg1's raw R directly against _simulate_ladder
    raw_outcome1, raw_r1, _held1 = _simulate_ladder("buy", entry, sl, future, RUNGS, max_hold=10)
    assert raw_outcome1 == "ladder"
    assert leg1_r == pytest.approx(raw_r1)

    # cross-check the pyramid fill + leg2's raw R directly
    filled2, fill_price, fill_idx = _pyramid_fill("buy", entry, sl, future, 0.4, 3)
    assert filled2 is True
    assert fill_price == pytest.approx(1999.2)
    assert fill_idx == 0
    future2 = future[fill_idx + 1:]
    raw_outcome2, raw_r2, _held2 = _simulate_ladder("buy", fill_price, sl, future2, RUNGS, max_hold=10)
    risk1 = abs(entry - sl)
    risk2 = abs(fill_price - sl)
    expected_combined = raw_r1 + raw_r2 * (risk2 / risk1)
    assert raw_outcome2 != "skip"
    assert combined_r == pytest.approx(expected_combined)

    # THE point of the overlay: combined beats leg1 alone
    assert combined_r > leg1_r


def test_pyramid_does_not_fill_when_dip_never_reached():
    entry, sl = 2000.0, 1998.0  # dip price = 1999.2 at dip_r=0.4
    future = [
        # low never drops below 1999.6 -- dip (1999.2) never touched
        _bar(2000.0, 2000.5, 1999.6, 2000.2),
        _bar(2000.2, 2000.6, 1999.7, 2000.4),
        _bar(2000.4, 2000.5, 1999.8, 2000.3),
    ]
    filled, fill_price, fill_idx = _pyramid_fill("buy", entry, sl, future, 0.4, window_bars=3)
    assert filled is False
    assert fill_price is None
    assert fill_idx is None

    trade = {"side": "buy", "entry": entry, "sl": sl, "tp": 2100.0, "future": future}
    combined = _combo_r(trade, _ladder_exit_fn, max_hold=10, spread_abs=0.0, commission_r=0.0,
                         pyramid=True, dip_r=0.4, window_bars=3)
    leg1_only = _combo_r(trade, _ladder_exit_fn, max_hold=10, spread_abs=0.0, commission_r=0.0,
                          pyramid=False, dip_r=0.4, window_bars=3)
    assert combined is not None and leg1_only is not None
    combined_r, filled_flag = combined
    leg1_r, _ = leg1_only
    assert filled_flag is False
    # unfilled pyramid must be IDENTICAL to leg1-alone, not merely "not worse"
    assert combined_r == pytest.approx(leg1_r)

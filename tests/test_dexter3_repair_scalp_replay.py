"""Tests for scripts/dexter3_repair_scalp_replay.py.

Owner hypothesis (2026-07-15): when a lane position is TRAPPED past a repair
trigger, repeatedly scalping the mirror side v1.0-style (bank at the first
M5 close >= bank_target_r, re-enter, repeat) until the parent resolves at T
should shrink -- or even reverse -- the trapped episode's total loss,
compared to letting the parent alone eat its eventual SL.

All scenarios below are pure synthetic OHLC bars (no transport / network) so
these tests run everywhere, on the PC and the VM alike.

Research-only script, no live behavior touched.
"""
from __future__ import annotations

import pytest

from scripts.dexter3_repair_scalp_replay import (
    _build_episode,
    _find_repair_trigger,
    _mirror_side,
    _run_repair_scalps,
)


def _bar(o: float, h: float, l: float, c: float) -> dict:
    return {"open": o, "high": h, "low": l, "close": c}


def _uptrend_future(bars: int = 30, step: float = 0.5, start: float = 2000.0) -> list[dict]:
    """A steady 0.5-per-bar grind up, small wicks, starting from ``start``."""
    out = []
    for k in range(1, bars + 1):
        price = start + step * k
        out.append(_bar(price - step, price + 0.05, price - step - 0.05, price))
    return out


# ---------------------------------------------------------------------------
# (a) trapped SHORT in a steady uptrend -> repair BUY scalps bank repeatedly,
#     repaired_total > baseline (the hypothesis holding true)
# ---------------------------------------------------------------------------


def test_trapped_short_in_uptrend_repair_scalps_improve_on_baseline():
    entry, sl, tp = 2000.0, 2010.0, 1900.0  # short, risk=10, tp far away/irrelevant
    future = _uptrend_future()
    trade = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": future}

    ep = _build_episode(trade, parent_max_hold=24, trigger_r=0.3, scalp_sl_frac=1.0,
                         scalp_max_hold=12, bank_target_r=0.2, spread_abs=0.12, commission_r=0.03)

    assert ep["excluded"] is None
    assert ep["T"] == 19          # parent's SL (2010) wick-touched at future index 19
    assert ep["trigger_idx"] == 5  # close first reaches -0.3R (2003) at index 5
    assert ep["baseline_r"] == pytest.approx(-1.042)
    # every repair scalp in a clean, uninterrupted uptrend should bank profitably
    assert len(ep["scalp_rs"]) == 3
    assert all(r == pytest.approx(0.158) for r in ep["scalp_rs"])
    assert ep["repaired_total"] == pytest.approx(-0.568)
    # the central hypothesis: harvesting the adverse move shrinks the total loss
    assert ep["repaired_total"] > ep["baseline_r"]


# ---------------------------------------------------------------------------
# (b) whipsaw chop -> repair scalps lose, repaired_total < baseline (the
#     engine must be able to report the hypothesis FALSE, not just confirm it)
# ---------------------------------------------------------------------------


def test_whipsaw_chop_repair_scalps_lose_worse_than_baseline():
    entry, sl, tp = 2000.0, 2010.0, 1900.0  # short, risk=10
    # rise to the trigger (closes 2001, 2002, 2003 -> trigger at -0.3R)
    future = [_bar(2000, 2001.1, 1999.9, 2001), _bar(2001, 2002.1, 2000.9, 2002),
              _bar(2002, 2003.1, 2001.9, 2003)]
    # pure 2001<->2003 oscillation for 21 bars (net-zero chop, total future = 24
    # bars = parent_max_hold -> parent times out at the last bar, never
    # touching its own SL(2010) or TP(1900))
    price, direction = 2003.0, -1
    for _ in range(21):
        newprice = price + direction * 2.0
        o, c = price, newprice
        future.append(_bar(o, max(o, c), min(o, c), c))
        price = newprice
        direction *= -1
    assert len(future) == 24
    trade = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": future}

    # small scalp risk (0.05 x parent risk = 0.5) -> every down-swing (2 pts)
    # comfortably breaches the mirror BUY scalp's own stop
    ep = _build_episode(trade, parent_max_hold=24, trigger_r=0.3, scalp_sl_frac=0.05,
                         scalp_max_hold=12, bank_target_r=0.2, spread_abs=0.12, commission_r=0.03)

    assert ep["excluded"] is None
    assert ep["T"] == 23  # parent survives the whole window -> times out at the last bar
    assert ep["trigger_idx"] == 2
    assert ep["baseline_r"] == pytest.approx(-0.142)
    assert len(ep["scalp_rs"]) == 11
    assert all(r == pytest.approx(-1.27) for r in ep["scalp_rs"])
    assert ep["repaired_total"] == pytest.approx(-14.112)
    # the engine must be willing to report the hypothesis FALSE in chop
    assert ep["repaired_total"] < ep["baseline_r"]


# ---------------------------------------------------------------------------
# (c) episode truncation at parent resolution -- bars AFTER T must never be
#     consulted, even when they would hugely change a scalp's outcome
# ---------------------------------------------------------------------------


def test_episode_truncates_at_parent_resolution_bar_t():
    entry, sl, tp = 2000.0, 2010.0, 1900.0
    future = _uptrend_future()  # same as scenario (a): T=19, trigger=5
    # append a catastrophic crash AFTER T that would hugely help (or ruin) a
    # mirror BUY scalp if it leaked into the episode
    leaked = list(future) + [_bar(2010, 2010, 900, 900) for _ in range(5)]

    trade_clean = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": future}
    trade_leaked = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": leaked}

    ep_clean = _build_episode(trade_clean, 24, 0.3, 1.0, 12, 0.2, 0.12, 0.03)
    ep_leaked = _build_episode(trade_leaked, 24, 0.3, 1.0, 12, 0.2, 0.12, 0.03)

    assert ep_clean["T"] == ep_leaked["T"] == 19
    assert ep_clean["scalp_rs"] == ep_leaked["scalp_rs"]
    assert ep_clean["repaired_total"] == pytest.approx(ep_leaked["repaired_total"])


# ---------------------------------------------------------------------------
# (d) cost adjustment applied to every scalp (numeric check vs hand-computed)
# ---------------------------------------------------------------------------


def test_scalp_cost_adjustment_matches_hand_computed_formula_on_a_bank():
    # scalp opens at 2003 (mirror BUY, scalp_risk = 1.0 x parent risk(10) = 10),
    # next bar closes at 2005.5 -> raw bank R = (2005.5-2003)/10 = 0.25,
    # bank_target_r=0.2 is met (0.25 >= 0.2)
    episode_bars = [_bar(2002.5, 2003.1, 2002.4, 2003.0), _bar(2003.0, 2006.0, 2002.9, 2005.5)]
    spread_abs, commission_r, scalp_risk = 0.12, 0.03, 10.0

    scalp_rs = _run_repair_scalps("sell", 2000.0, 2010.0, episode_bars, scalp_sl_frac=1.0,
                                   scalp_max_hold=12, bank_target_r=0.2,
                                   spread_abs=spread_abs, commission_r=commission_r)

    raw_r = (2005.5 - 2003.0) / scalp_risk
    expected = raw_r - (spread_abs / scalp_risk + commission_r)
    assert scalp_rs == [pytest.approx(expected)]
    assert scalp_rs == [pytest.approx(0.208)]

    # and on a loss (SL-stopped) scalp: cost uses the SAME formula, scaled by
    # the scalp's OWN (smaller) risk distance -- reuses scenario (b)'s numbers
    entry, sl, tp = 2000.0, 2010.0, 1900.0
    future = [_bar(2000, 2001.1, 1999.9, 2001), _bar(2001, 2002.1, 2000.9, 2002),
              _bar(2002, 2003.1, 2001.9, 2003), _bar(2003, 2003.1, 2000.9, 2001)]
    trade = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": future}
    ep = _build_episode(trade, 24, 0.3, 0.05, 12, 0.2, spread_abs, commission_r)
    scalp_risk_loss = 0.05 * 10.0
    expected_loss = -1.0 - (spread_abs / scalp_risk_loss + commission_r)
    assert ep["scalp_rs"][0] == pytest.approx(expected_loss)
    assert ep["scalp_rs"][0] == pytest.approx(-1.27)


# ---------------------------------------------------------------------------
# (e) trigger never fires -> episode excluded, not scored
# ---------------------------------------------------------------------------


def test_trigger_never_fires_episode_is_excluded():
    # steady rise straight to TP, never dips -- floating R for a BUY never
    # goes negative, so it can never breach -0.3R
    entry, sl, tp = 2000.0, 1998.0, 2010.0
    future = [_bar(2000 + i, 2001 + i, 1999.5 + i, 2001 + i) for i in range(10)]
    trade = {"side": "buy", "entry": entry, "sl": sl, "tp": tp, "future": future}

    ep = _build_episode(trade, parent_max_hold=24, trigger_r=0.3, scalp_sl_frac=1.0,
                         scalp_max_hold=12, bank_target_r=0.2, spread_abs=0.12, commission_r=0.03)

    assert ep["excluded"] == "no_trigger"
    assert ep["trigger_idx"] is None
    assert ep["scalp_rs"] == []
    assert ep["repaired_total"] is None
    # baseline is still computed for a book-keeping / reporting purposes
    assert ep["baseline_r"] is not None


def test_find_repair_trigger_returns_none_when_it_never_breaches():
    future = [_bar(2000 + i, 2001 + i, 1999.5 + i, 2001 + i) for i in range(10)]
    assert _find_repair_trigger("buy", 2000.0, 1998.0, future, trigger_r=0.3) is None


# ---------------------------------------------------------------------------
# (f) a scalp still open when only T remains marks-to-close at T (no
#     look-past-T, no phantom bank/SL fabricated beyond the data given)
# ---------------------------------------------------------------------------


def test_scalp_open_at_t_marks_to_close():
    # episode_bars = [trigger bar, T bar] -- exactly one scalp can open (at
    # the trigger bar's close); it neither hits its own SL nor its bank
    # target within the single remaining (T) bar, so it must mark to T's close
    episode_bars = [_bar(2002.5, 2003.1, 2002.4, 2003.0), _bar(2003.0, 2003.6, 2002.9, 2003.5)]
    scalp_rs = _run_repair_scalps("sell", 2000.0, 2010.0, episode_bars, scalp_sl_frac=1.0,
                                   scalp_max_hold=12, bank_target_r=0.2,
                                   spread_abs=0.12, commission_r=0.03)
    assert len(scalp_rs) == 1
    raw_r = (2003.5 - 2003.0) / 10.0  # mark-to-close at T's close, scalp_risk=10
    expected = raw_r - (0.12 / 10.0 + 0.03)
    assert scalp_rs[0] == pytest.approx(expected)
    assert scalp_rs[0] == pytest.approx(0.008)


def test_no_fresh_scalp_opens_on_ts_own_bar():
    # a 1-bar episode (trigger IS T) can never open a scalp -- there is no
    # room left to run one
    episode_bars = [_bar(2002.5, 2003.1, 2002.4, 2003.0)]
    scalp_rs = _run_repair_scalps("sell", 2000.0, 2010.0, episode_bars, scalp_sl_frac=1.0,
                                   scalp_max_hold=12, bank_target_r=0.2,
                                   spread_abs=0.12, commission_r=0.03)
    assert scalp_rs == []


# ---------------------------------------------------------------------------
# (g) "wide" parent model regression (coordinator directive 2026-07-15):
#     plain and smart both degenerate (their machinery ends the parent at the
#     first close beyond -1.0R, so trigger bar == T, 0 scalps). The wide model
#     (hard SL at entry +/- parent_sl_mult x ORIGINAL risk, no close-based
#     exit) must let a close breach -1.2R (original units) at bar k << T,
#     with T forced later by the widened SL at -2R -- and scalps must run,
#     with every reported R in ORIGINAL-risk units.
# ---------------------------------------------------------------------------


def test_wide_parent_trigger_fires_before_t_and_scalps_run_in_original_units():
    entry, sl, tp = 2000.0, 2002.0, 1990.0  # short; ORIGINAL risk = 2.0
    # widened SL (mult 2.0) = 2000 + 2*(2002-2000) = 2004
    future = [
        _bar(2000.00, 2001.00, 1999.80, 2000.80),  # close r = -0.40 (original units)
        _bar(2000.80, 2001.80, 2000.60, 2001.60),  # -0.80
        _bar(2001.60, 2002.70, 2001.40, 2002.50),  # -1.25 <= -1.2 -> TRIGGER (k=2);
                                                   # a plain parent would already be
                                                   # dead here (high 2002.7 > sl 2002)
        _bar(2002.50, 2003.20, 2002.30, 2003.00),  # scalp1 banks: (2003-2002.5)/2 = 0.25
        _bar(2003.00, 2003.60, 2002.80, 2003.40),  # scalp2 opens at this close
        _bar(2003.40, 2003.95, 2003.20, 2003.85),  # scalp2 banks: (2003.85-2003.4)/2 = 0.225
        _bar(2003.85, 2004.20, 2003.70, 2004.10),  # high 2004.2 >= widened SL 2004 -> T=6
    ]
    trade = {"side": "sell", "entry": entry, "sl": sl, "tp": tp, "future": future}
    spread_abs, commission_r = 0.12, 0.03

    ep = _build_episode(trade, parent_max_hold=36, trigger_r=1.2, scalp_sl_frac=1.0,
                         scalp_max_hold=12, bank_target_r=0.2, spread_abs=spread_abs,
                         commission_r=commission_r, parent_style="wide", parent_sl_mult=2.0)

    assert ep["excluded"] is None
    assert ep["trigger_idx"] == 2      # trigger fires at bar k=2 ...
    assert ep["T"] == 6                # ... well before the widened-SL death at T=6
    # parent death in ORIGINAL-risk units = -parent_sl_mult, plus original-risk cost
    cost = spread_abs / 2.0 + commission_r  # 0.09, denominated in ORIGINAL risk (2.0)
    assert ep["baseline_r"] == pytest.approx(-2.0 - cost)
    assert ep["baseline_r"] == pytest.approx(-2.09)
    # at least one scalp ran (the whole point of the wide model) -- exactly two
    # here, and their values prove the scalp risk = scalp_sl_frac x ORIGINAL
    # risk (2.0), not the widened distance (4.0)
    assert len(ep["scalp_rs"]) == 2
    assert ep["scalp_rs"][0] == pytest.approx((2003.00 - 2002.50) / 2.0 - cost)  # 0.16
    assert ep["scalp_rs"][1] == pytest.approx((2003.85 - 2003.40) / 2.0 - cost)  # 0.135
    assert ep["repaired_total"] == pytest.approx(-2.09 + 0.16 + 0.135)
    assert ep["repaired_total"] > ep["baseline_r"]


# ---------------------------------------------------------------------------
# misc small-surface checks
# ---------------------------------------------------------------------------


def test_mirror_side():
    assert _mirror_side("buy") == "sell"
    assert _mirror_side("sell") == "buy"


def test_build_episode_zero_risk_parent_is_excluded():
    trade = {"side": "buy", "entry": 2000.0, "sl": 2000.0, "tp": 2010.0,
             "future": [_bar(2000, 2001, 1999, 2000.5)]}
    ep = _build_episode(trade, parent_max_hold=24, trigger_r=0.3, scalp_sl_frac=1.0,
                         scalp_max_hold=12, bank_target_r=0.2, spread_abs=0.12, commission_r=0.03)
    assert ep["excluded"] == "zero_risk"
    assert ep["baseline_r"] is None

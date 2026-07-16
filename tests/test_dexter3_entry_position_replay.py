"""Tests for scripts/dexter3_entry_position_replay.py.

Owner directive 2026-07-16 ("เข้าให้ถูกทางในตำแหน่งที่เสี่ยงน้อย"): the compound
entry replay replaces the signal's market entry with a trough LIMIT at
entry -/+ dip_r * risk and filters counter-trend sides at decision time.
These tests pin the new primitives with hand-computed OHLC bars so every
asserted number traces to arithmetic in the test itself -- same conventions
as tests/test_dexter3_convex_exit.py (SL-first conservative; a fill bar that
also breaches the original SL is filled-and-stopped; the fill bar's own
favorable excursion is ignored).

Pure synthetic bars, no transport.
"""
from __future__ import annotations

import pytest

from scripts.dexter3_entry_position_replay import (
    _dir_allows,
    _limit_fill,
    _score,
    _trade_r,
)

RUNGS = [(0.25, 0.02), (0.50, 0.15), (0.80, 0.40), (1.20, 0.80), (2.00, 1.45), (3.00, 2.25)]
SPREAD = 0.12
COMM = 0.03


def _bar(o: float, h: float, l: float, c: float) -> dict:
    return {"open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# _limit_fill -- fill / same-bar-stop / miss / window, both sides
# ---------------------------------------------------------------------------


def test_limit_fill_buy_clean():
    # entry 2000, sl 1998 (risk 2), dip 0.4 -> limit 1999.2; low 1999.1 touches
    # the limit but stays above the SL -> clean fill at the limit price.
    future = [_bar(1999.8, 2000.1, 1999.1, 1999.5)]
    status, price, idx = _limit_fill("buy", 2000.0, 1998.0, future, 0.4, 6)
    assert status == "filled"
    assert price == pytest.approx(1999.2)
    assert idx == 0


def test_limit_fill_buy_same_bar_stop():
    # the touching bar's low 1997.9 also breaches the SL 1998 -> the intrabar
    # path is unknowable, so conservatively filled AND stopped on that bar.
    future = [_bar(1999.8, 2000.1, 1997.9, 1998.5)]
    status, price, idx = _limit_fill("buy", 2000.0, 1998.0, future, 0.4, 6)
    assert status == "filled_stopped"
    assert price == pytest.approx(1999.2)
    assert idx == 0


def test_limit_fill_miss_and_window():
    # lows never reach 1999.2 -> miss; and a touch AT bar index 1 with
    # window_bars=1 is outside the window -> miss.
    future = [_bar(2000.0, 2000.5, 1999.5, 2000.2), _bar(2000.2, 2000.6, 1999.1, 2000.0)]
    assert _limit_fill("buy", 2000.0, 1998.0, [future[0]], 0.4, 6)[0] == "miss"
    assert _limit_fill("buy", 2000.0, 1998.0, future, 0.4, 1)[0] == "miss"
    # same bars, window 2 -> the idx-1 bar fills
    status, _price, idx = _limit_fill("buy", 2000.0, 1998.0, future, 0.4, 2)
    assert status == "filled"
    assert idx == 1


def test_limit_fill_sell_mirror():
    # entry 2000, sl 2002 (risk 2), dip 0.4 -> limit 2000.8; high 2000.9
    # touches without breaching sl -> clean; high 2002.1 -> filled_stopped.
    clean = [_bar(2000.2, 2000.9, 2000.0, 2000.4)]
    stopped = [_bar(2000.2, 2002.1, 2000.0, 2001.5)]
    assert _limit_fill("sell", 2000.0, 2002.0, clean, 0.4, 6)[0] == "filled"
    assert _limit_fill("sell", 2000.0, 2002.0, stopped, 0.4, 6)[0] == "filled_stopped"


# ---------------------------------------------------------------------------
# _trade_r -- own-risk R math on the limit entry (hand-computed)
# ---------------------------------------------------------------------------


def _limit_trade() -> dict:
    # buy entry 2000, sl 1998 (risk1 = 2). dip 0.4 -> fill 1999.2, and the SL
    # stays at 1998, so new risk = 1999.2 - 1998 = 1.2 (= 0.6 x risk1).
    return {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [
            # fill bar: low 1999.0 touches the 1999.2 limit, stays above SL.
            # Its high 2000.5 is IGNORED (exit sim starts next bar).
            _bar(1999.8, 2000.5, 1999.0, 1999.4),
            # exit bar (new-risk units): peak_r = (2000.4-1999.2)/1.2 = 1.0
            # -> rung (0.80, 0.40) armed; adverse_r = (1999.5-1999.2)/1.2 =
            # 0.25 <= 0.40 -> ladder exit AT the 0.40 floor.
            _bar(1999.6, 2000.4, 1999.5, 2000.2),
        ],
    }


def test_trade_r_limit_ladder_hand_computed():
    status, r = _trade_r(_limit_trade(), "limit", 0.4, 6, "ladder", {"rungs": RUNGS},
                         atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert status == "taken"
    # r = 0.40 floor - costs; costs = 0.12/1.2 + 0.03 = 0.13 (NEW risk units)
    assert r == pytest.approx(0.40 - 0.13)


def test_trade_r_limit_same_bar_stop_is_minus_one_new_risk():
    t = _limit_trade()
    t["future"] = [_bar(1999.8, 2000.1, 1997.9, 1998.2)]
    status, r = _trade_r(t, "limit", 0.4, 6, "ladder", {"rungs": RUNGS},
                         atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert status == "taken"
    assert r == pytest.approx(-1.0 - 0.13)


def test_trade_r_limit_miss_returns_none():
    t = _limit_trade()
    t["future"] = [_bar(2000.0, 2000.5, 1999.5, 2000.3)]
    assert _trade_r(t, "limit", 0.4, 6, "ladder", {"rungs": RUNGS},
                    atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM) == ("miss", None)


def test_trade_r_market_uses_original_risk():
    # market entry on the same trade: risk = 2.0, so the exit bar's peak_r =
    # (2000.5-2000)/2 = 0.25 on the FILL bar (counted for market) -> rung
    # (0.25, 0.02) arms; adverse (1999.0-2000)/2 = -0.5 <= 0.02 -> floor 0.02.
    status, r = _trade_r(_limit_trade(), "market", 0.0, 0, "ladder", {"rungs": RUNGS},
                         atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert status == "taken"
    # costs in ORIGINAL risk units = 0.12/2 + 0.03 = 0.09
    assert r == pytest.approx(0.02 - 0.09)


# ---------------------------------------------------------------------------
# _dir_allows -- decision-time direction filter
# ---------------------------------------------------------------------------


def test_dir_allows_matrix():
    assert _dir_allows("none", "buy", -1)
    assert not _dir_allows("nobuy-h1down", "buy", -1)
    assert _dir_allows("nobuy-h1down", "sell", -1)      # sells never blocked
    assert _dir_allows("nobuy-h1down", "buy", 1)
    assert not _dir_allows("nocounter", "buy", -1)
    assert not _dir_allows("nocounter", "sell", 1)
    assert _dir_allows("nocounter", "sell", -1)
    assert _dir_allows("nocounter", "buy", 0)           # no-trend never blocked


# ---------------------------------------------------------------------------
# _score -- miss counterfactual is the market-entry R under the SAME exit
# ---------------------------------------------------------------------------


def test_score_counts_miss_counterfactual():
    filled = _limit_trade()
    missed = _limit_trade()
    # never dips to 1999.2 -> miss; market counterfactual runs the ladder from
    # entry 2000 risk 2: peak_r = (2000.6-2000)/2 = 0.30 -> floor 0.02;
    # adverse = (1999.5-2000)/2 = -0.25 <= 0.02 -> floor exit 0.02 - 0.09.
    missed["future"] = [_bar(2000.0, 2000.6, 1999.5, 2000.3)]
    sc = _score([filled, missed], "limit", 0.4, 6, "ladder", {"rungs": RUNGS},
                atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert sc is not None
    assert sc["n"] == 1
    assert sc["miss_n"] == 1
    assert sc["miss_net"] == pytest.approx(0.02 - 0.09)
    assert sc["fill_pct"] == pytest.approx(50.0)
    assert sc["net"] == pytest.approx(0.40 - 0.13)

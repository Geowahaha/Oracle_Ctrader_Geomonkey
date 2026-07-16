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
    _anchor_bias_fields,
    _dir_allows,
    _limit_fill,
    _m1_entry_to_m5_exit,
    _score,
    _trade_r,
    _zone_confirm_entry,
    _zone_confirm_entry_m1,
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
                    atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM) == ("miss_no_touch", None)


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
# _zone_confirm_entry -- touch + real-break check + reversal close
# (owner directive 2026-07-16: "เปลี่ยนจากวาง limit เป็นโซน ตรวจสอบเบรคจริง
#  + สัญญาณกลับตัว")
# ---------------------------------------------------------------------------


def test_zone_touch_and_same_bar_confirm():
    # buy entry 2000, sl 1998 (risk 2), dip 0.4 -> zone_top 1999.2. The bar
    # dips to 1999.1 (touch) and closes green at 1999.8 (above zone_top,
    # below entry) -> same-bar confirm, enter at the CLOSE 1999.8.
    future = [_bar(1999.3, 1999.9, 1999.1, 1999.8)]
    status, px, idx = _zone_confirm_entry("buy", 2000.0, 1998.0, future, 0.4, 6)
    assert status == "filled"
    assert px == pytest.approx(1999.8)
    assert idx == 0


def test_zone_survives_wick_through_sl_where_limit_dies():
    # THE mechanism difference. Bar 0 wicks to 1997.5 (through SL 1998) but
    # CLOSES back at 1998.6 -- no position yet, so the zone model survives;
    # the blind limit is filled AND stopped on that same bar. Bar 1 closes
    # green at 1999.4 (> zone_top 1999.2, < entry) -> zone enters at 1999.4.
    future = [
        _bar(1999.5, 1999.6, 1997.5, 1998.6),
        _bar(1998.7, 1999.5, 1998.4, 1999.4),
    ]
    l_status, _p, _i = _limit_fill("buy", 2000.0, 1998.0, future, 0.4, 6)
    assert l_status == "filled_stopped"                 # limit: -1R knife
    z_status, z_px, z_idx = _zone_confirm_entry("buy", 2000.0, 1998.0, future, 0.4, 6)
    assert z_status == "filled"
    assert z_px == pytest.approx(1999.4)
    assert z_idx == 1


def test_zone_break_on_close_beyond_sl():
    # a CLOSE at 1997.8 (< sl 1998) = confirmed break -> no trade at all.
    future = [_bar(1999.5, 1999.6, 1997.5, 1997.8)]
    assert _zone_confirm_entry("buy", 2000.0, 1998.0, future, 0.4, 6)[0] == "zone_break"


def test_zone_no_confirm_and_no_touch():
    # touched but every close stays red/below zone_top -> no_confirm;
    # never reaches the zone at all -> no_touch.
    touched_only = [_bar(1999.5, 1999.5, 1999.0, 1999.1), _bar(1999.1, 1999.15, 1998.6, 1998.7)]
    assert _zone_confirm_entry("buy", 2000.0, 1998.0, touched_only, 0.4, 6)[0] == "no_confirm"
    never = [_bar(2000.0, 2000.5, 1999.5, 2000.2)]
    assert _zone_confirm_entry("buy", 2000.0, 1998.0, never, 0.4, 6)[0] == "no_touch"


def test_zone_confirm_above_entry_rejected():
    # the reversal bar closes at 2000.4 (> signal entry 2000) -- the discount
    # is gone, chasing is exactly what this model refuses -> no_confirm.
    future = [_bar(1999.3, 1999.4, 1999.0, 1999.1), _bar(1999.1, 2000.6, 1999.0, 2000.4)]
    assert _zone_confirm_entry("buy", 2000.0, 1998.0, future, 0.4, 2)[0] == "no_confirm"


def test_zone_sell_mirror():
    # sell entry 2000, sl 2002 (risk 2), dip 0.4 -> zone_top 2000.8. Bar
    # opens 2000.5, spikes to 2000.9 (touch) and closes RED at 2000.3
    # (c < o, < zone_top, > entry) -> confirm at 2000.3.
    future = [_bar(2000.5, 2000.9, 2000.1, 2000.3)]
    status, px, _idx = _zone_confirm_entry("sell", 2000.0, 2002.0, future, 0.4, 6)
    assert status == "filled"
    assert px == pytest.approx(2000.3)


def test_trade_r_zone_ladder_hand_computed():
    # zone confirm at 1999.8 (test above), SL stays 1998 -> new risk = 1.8.
    # Exit bar: peak_r = (2001.6-1999.8)/1.8 = 1.0 -> rung (0.80, 0.40) armed;
    # adverse_r = (1999.9-1999.8)/1.8 = 0.0556 <= 0.40 -> ladder floor 0.40.
    # costs = 0.12/1.8 + 0.03 = 0.0967 (new-risk units).
    t = {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [
            _bar(1999.3, 1999.9, 1999.1, 1999.8),      # touch + confirm bar
            _bar(1999.9, 2001.6, 1999.9, 2001.0),      # runner bar, exits at floor
        ],
    }
    status, r = _trade_r(t, "zone", 0.4, 6, "ladder", {"rungs": RUNGS},
                         atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert status == "taken"
    assert r == pytest.approx(0.40 - (SPREAD / 1.8 + COMM))


def test_trade_r_plain_exit_reaches_signal_tp():
    # market entry, plain exit: TP 2004 (risk 2 -> +2R), hit on bar 0 high.
    t = {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [_bar(2000.2, 2004.5, 1999.9, 2004.0)],
    }
    status, r = _trade_r(t, "market", 0.0, 0, "plain", {},
                         atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert status == "taken"
    assert r == pytest.approx(2.0 - 0.09)


def test_score_zone_miss_classes():
    # one confirm-fill, one zone_break, one no_touch -> miss_by carries the
    # per-class counterfactuals (break cf is the knife the check refused).
    fill = {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [_bar(1999.3, 1999.9, 1999.1, 1999.8), _bar(1999.9, 2001.6, 1999.9, 2001.0)],
    }
    broke = {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [_bar(1999.5, 1999.6, 1997.5, 1997.8)],
    }
    never = {
        "side": "buy", "entry": 2000.0, "sl": 1998.0, "tp": 2004.0,
        "future": [_bar(2000.0, 2000.6, 1999.5, 2000.3)],
    }
    sc = _score([fill, broke, never], "zone", 0.4, 6, "ladder", {"rungs": RUNGS},
                atr=4.5, max_hold=48, spread_abs=SPREAD, commission_r=COMM)
    assert sc is not None
    assert sc["n"] == 1
    assert sc["miss_n"] == 2
    assert set(sc["miss_by"]) == {"break", "no_touch"}
    # break counterfactual: market entry, bar low 1997.5 <= sl -> -1R - 0.09
    n_b, cf_b = sc["miss_by"]["break"]
    assert n_b == 1
    assert cf_b == pytest.approx(-1.0 - 0.09)


# ---------------------------------------------------------------------------
# _zone_confirm_entry_m1 + _m1_entry_to_m5_exit -- M5 green light, M1 trigger
# (owner idea 2026-07-16: "เข้าจุดที่ดีที่สุดใน M1 โดย pattern ที่ M5 ไฟเขียว")
# ---------------------------------------------------------------------------


def _m1bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _epoch_of(ts: str) -> float:
    from scripts.dexter3_edge_discovery import _epoch
    return _epoch(ts)


def test_zone_m1_confirms_earlier_at_better_price():
    # buy entry 2000, sl 1998, dip 0.4 -> zone_top 1999.2. M1 stream: dip
    # touches 1999.15 at :01, the very next M1 closes green at 1999.30 --
    # confirm 4 minutes before any M5 close could, at a price much closer
    # to the zone than a typical M5 reversal close.
    m1 = [
        _m1bar("2026-07-10T00:00:00Z", 1999.6, 1999.7, 1999.4, 1999.5),
        _m1bar("2026-07-10T00:01:00Z", 1999.5, 1999.5, 1999.15, 1999.18),  # touch
        _m1bar("2026-07-10T00:02:00Z", 1999.18, 1999.35, 1999.1, 1999.30),  # green > zone_top
    ]
    status, px, e = _zone_confirm_entry_m1("buy", 2000.0, 1998.0, m1,
                                           0.4, _epoch_of("2026-07-10T00:30:00Z"))
    assert status == "filled"
    assert px == pytest.approx(1999.30)
    assert e == pytest.approx(_epoch_of("2026-07-10T00:02:00Z"))


def test_zone_m1_break_and_deadline():
    # an M1 CLOSE below the SL kills it; bars at/after the deadline are ignored.
    m1_break = [_m1bar("2026-07-10T00:01:00Z", 1999.3, 1999.3, 1997.6, 1997.9)]
    assert _zone_confirm_entry_m1("buy", 2000.0, 1998.0, m1_break, 0.4,
                                  _epoch_of("2026-07-10T00:30:00Z"))[0] == "zone_break"
    # touch happens only AFTER the deadline -> no_touch
    late = [_m1bar("2026-07-10T01:00:00Z", 1999.5, 1999.5, 1999.0, 1999.3)]
    assert _zone_confirm_entry_m1("buy", 2000.0, 1998.0, late, 0.4,
                                  _epoch_of("2026-07-10T00:30:00Z"))[0] == "no_touch"


def test_m1_bridge_checks_entry_period_wicks_then_hands_to_m5():
    # entry at :02 inside the 00:00-00:05 M5 period. A later M1 bar at :04
    # wicks to 1997.9 (<= sl) -> stopped inside the entry period.
    entry_epoch = _epoch_of("2026-07-10T00:02:00Z")
    m1 = [
        _m1bar("2026-07-10T00:02:00Z", 1999.2, 1999.35, 1999.1, 1999.30),
        _m1bar("2026-07-10T00:04:00Z", 1999.3, 1999.3, 1997.9, 1998.4),   # SL wick
    ]
    m5_future = [{"ts": "2026-07-10T00:05:00Z", "open": 1998.5, "high": 1999.0,
                  "low": 1998.2, "close": 1998.8}]
    rest, stopped = _m1_entry_to_m5_exit("buy", 1999.30, 1998.0, entry_epoch, m1, m5_future)
    assert stopped is True
    # clean period -> hands off exactly the M5 bars from the NEXT period
    m1_clean = [m1[0]]
    rest, stopped = _m1_entry_to_m5_exit("buy", 1999.30, 1998.0, entry_epoch, m1_clean, m5_future)
    assert stopped is False
    assert len(rest) == 1 and rest[0]["ts"] == "2026-07-10T00:05:00Z"


# ---------------------------------------------------------------------------
# decide_daytrend -- with-bias pullback-continuation producer (2026-07-16)
# ---------------------------------------------------------------------------


def test_daytrend_sell_day_pullback_resume():
    from scripts.dexter3_entry_position_replay import decide_daytrend

    # sell-bias day (bias -1, 3h in). Day low 3990; price bounced to 3999.4
    # (pullback 9.4 > 0.8*5.0 ATR = 4.0) and the newest bar closes RED ->
    # sell at the close, SL above the 6-bar swing high + 0.1*ATR buffer.
    prefix = [
        _tbar("2026-07-16T09:50:00Z", 4008.0, 4006.0),
        _tbar("2026-07-16T09:55:00Z", 4006.0, 4005.0),
        _tbar("2026-07-16T10:00:00Z", 4005.0, 3998.0),
        {"ts": "2026-07-16T10:05:00Z", "open": 3998.0, "high": 3999.0, "low": 3990.0, "close": 3992.0},
        {"ts": "2026-07-16T10:10:00Z", "open": 3992.0, "high": 4000.5, "low": 3991.5, "close": 4000.2},
        {"ts": "2026-07-16T10:15:00Z", "open": 4000.2, "high": 4001.0, "low": 3998.8, "close": 3999.4},
    ]
    sig = decide_daytrend(prefix, {"bias_d0": -1, "hrs_d0": 3.0}, atr=5.0,
                          swing_bars=3)
    assert sig is not None and sig["side"] == "sell"
    assert sig["entry"] == pytest.approx(3999.4)
    assert sig["sl"] == pytest.approx(4001.0 + 0.5)     # swing high + 0.1*ATR
    assert sig["tp"] == pytest.approx(3990.0)           # retest of the day low
    # green (counter) close -> no signal; no bias -> no signal
    prefix_green = prefix[:-1] + [
        {"ts": "2026-07-16T10:15:00Z", "open": 3998.8, "high": 4001.0, "low": 3998.5, "close": 4000.6}]
    assert decide_daytrend(prefix_green, {"bias_d0": -1, "hrs_d0": 3.0}, 5.0, swing_bars=3) is None
    assert decide_daytrend(prefix, {"bias_d0": 0, "hrs_d0": 3.0}, 5.0, swing_bars=3) is None
    # pullback too shallow -> no signal (extreme 3990, close 3992 -> 2.0 < 4.0)
    shallow = prefix[:5] + [
        {"ts": "2026-07-16T10:15:00Z", "open": 3993.0, "high": 3993.5, "low": 3991.0, "close": 3992.0}]
    assert decide_daytrend(shallow, {"bias_d0": -1, "hrs_d0": 3.0}, 5.0, swing_bars=3) is None


# ---------------------------------------------------------------------------
# _dir_allows -- decision-time direction filter
# ---------------------------------------------------------------------------


def _ctx(tsign: int = 0, **extra) -> dict:
    return {"trend_sign": tsign, **extra}


def test_dir_allows_matrix():
    assert _dir_allows("none", "buy", _ctx(-1))
    assert not _dir_allows("nobuy-h1down", "buy", _ctx(-1))
    assert _dir_allows("nobuy-h1down", "sell", _ctx(-1))   # sells never blocked
    assert _dir_allows("nobuy-h1down", "buy", _ctx(1))
    assert not _dir_allows("nocounter", "buy", _ctx(-1))
    assert not _dir_allows("nocounter", "sell", _ctx(1))
    assert _dir_allows("nocounter", "sell", _ctx(-1))
    assert _dir_allows("nocounter", "buy", _ctx(0))        # no-trend never blocked


def test_dir_allows_dayopen_bias():
    # ต่ำเปิด (below the day open, 2h in): sells only, buys blocked.
    below = _ctx(bias_d22=-1, hrs_d22=2.0)
    assert not _dir_allows("dayopen22-h1", "buy", below)
    assert _dir_allows("dayopen22-h1", "sell", below)
    # ยืนเปิด (above the day open): buys only.
    above = _ctx(bias_d0=1, hrs_d0=2.0)
    assert _dir_allows("dayopen0-h1", "buy", above)
    assert not _dir_allows("dayopen0-h1", "sell", above)
    # bias too YOUNG for the mode's min-hours gate -> neutral, both allowed
    # (same raw bias, mode requires 3h but only 2h have passed).
    young = _ctx(bias_d22=-1, hrs_d22=2.0)
    assert _dir_allows("dayopen22-h3", "buy", young)
    assert _dir_allows("dayopen22-h3", "sell", young)
    # session-anchored variant reads bias_sess
    sess = _ctx(bias_sess=1, hrs_sess=1.5)
    assert _dir_allows("sessopen-h1", "buy", sess)
    assert not _dir_allows("sessopen-h1", "sell", sess)


# ---------------------------------------------------------------------------
# _anchor_bias_fields -- day/session open detection on hand-built timestamps
# ---------------------------------------------------------------------------


def _tbar(ts: str, o: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": max(o, c), "low": min(o, c), "close": c}


def test_anchor_bias_day_open_and_hours():
    # Day opens at 00:00Z with open 100; by 02:00Z close is 99 -> bias_d0=-1,
    # hrs_d0=2.0 (ต่ำเปิด). The 22:00Z anchor of the PREVIOUS day anchors the
    # first bar (hrs_d22 = 2h after midnight = 26h... no: most recent 22:00 is
    # 2h before midnight, so at 02:00Z hrs_d22 = 4.0) and its open is the
    # first bar's open (100) because the series starts after that anchor.
    bars = [
        _tbar("2026-07-14T00:00:00Z", 100.0, 101.0),   # day open bar, closes up
        _tbar("2026-07-14T02:00:00Z", 101.0, 99.0),    # now below the open
    ]
    rows = _anchor_bias_fields(bars)
    assert rows[0]["bias_d0"] == 1                      # 101 > 100
    assert rows[0]["hrs_d0"] == pytest.approx(0.0)
    assert rows[1]["bias_d0"] == -1                     # 99 < 100 = ต่ำเปิด
    assert rows[1]["hrs_d0"] == pytest.approx(2.0)
    assert rows[1]["hrs_d22"] == pytest.approx(4.0)     # anchor 13th 22:00Z


def test_anchor_bias_session_reset():
    # London anchor 07:00Z re-anchors the session open: a bar at 07:00Z opens
    # 105, and at 08:00Z close 104 -> bias_sess=-1 vs the SESSION open (105)
    # even though price is still above the 00:00Z day open (100).
    bars = [
        _tbar("2026-07-14T00:00:00Z", 100.0, 102.0),
        _tbar("2026-07-14T07:00:00Z", 105.0, 106.0),
        _tbar("2026-07-14T08:00:00Z", 106.0, 104.0),
    ]
    rows = _anchor_bias_fields(bars)
    assert rows[2]["bias_d0"] == 1                      # 104 > 100 day-open bias up
    assert rows[2]["bias_sess"] == -1                   # 104 < 105 session bias down
    assert rows[2]["hrs_sess"] == pytest.approx(1.0)
    assert rows[1]["hrs_sess"] == pytest.approx(0.0)    # fresh session anchor


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

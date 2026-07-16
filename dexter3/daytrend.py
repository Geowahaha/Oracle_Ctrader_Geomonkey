"""DAYTREND producer — with-the-day pullback continuation (owner order
2026-07-16: "สร้างเลย เทรดจริงเลย").

Mechanizes the owner's own live method (3 green sells on the 2026-07-16
sell-only day: sold the bounces, rode toward the day extreme): on a day with
an established open bias (ยืนเปิด/ต่ำเปิด — the same 00Z anchor the VP gate
uses), a pullback AGAINST the bias of at least ``pull_atr`` x ATR off the
day's running extreme, whose newest bar CLOSES back in the bias direction
(reversal-resume), is a continuation entry: SL beyond the pullback swing,
TP at the day extreme (the retest target).

Evidence (scripts/dexter3_entry_position_replay.py --producer daytrend,
VM /tmp/entry_pos_daytrend_no_{6000,10000,20000}.log, --no-overlap = one
position at a time, market x plain-TP h48):
    6k  derive -7.96 (PF 0.95) / validate +34.98 (PF 1.30)
    10k derive +59.99 (PF 1.22) / validate +14.67 (PF 1.07)
    14k derive +62.77 (PF 1.15) / validate +30.55 (PF 1.11)
5/6 segment-cells positive, worst cell ~breakeven — the first producer in
repo history that survives the crash month (hunt -148..-462, VP -57..-112
on the same window). Exit for this producer is PLAIN SL/TP + time stop:
convex flipped signs under no-overlap and the ladder amputates — set
DEXTER3_OM_TRAIL_MODE=plain on this lane.

``decide_daytrend`` below is BAR-IDENTICAL to the replay's function (the
proof); tests/test_dexter3_daytrend.py pins the parity on shared fixtures.
Every knob is env-tunable but the defaults ARE the proven values — change
them only through a new replay pass.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3 import vp_lane
from dexter3.hunter_brain import Decision

DAYTREND_LABEL = "dexter3:dtr:canary"
# Family root (2026-07-15 versioned-labels design): ownership matches by
# prefix so a version bump never orphans open positions.
DAYTREND_LABEL_FAMILY = "dexter3:dtr"

ENV_PULL_ATR = "DEXTER3_DAYTREND_PULL_ATR"           # default 0.8 (proven)
ENV_SWING_BARS = "DEXTER3_DAYTREND_SWING_BARS"       # default 6
ENV_BUFFER_ATR = "DEXTER3_DAYTREND_BUFFER_ATR"       # default 0.1
ENV_MIN_HOURS = "DEXTER3_DAYTREND_MIN_HOURS"         # default 1.0
ENV_MAX_RISK_ATR = "DEXTER3_DAYTREND_MAX_RISK_ATR"   # default 2.0
ENV_ANCHOR_HOUR = "DEXTER3_DAYTREND_ANCHOR_HOUR"     # default 0 (00Z)


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _env(key: str, default: float) -> float:
    return _f(os.environ.get(key), default)


def _skip(ts_close: str, symbol: str, session: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="small",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"daytrend_skip: {reason}"], session=session, features={},
    )


def decide_daytrend(symbol: str, m5_prefix: list, spread_abs: float,
                    session: str = "unknown") -> Decision:
    """One decision per closed M5 bar. Same arithmetic as the replay proof
    (scripts.dexter3_entry_position_replay.decide_daytrend), expressed on
    live inputs: bias/hours from ``vp_lane.dayopen_bias`` (identical epoch
    math to the replay's ``_anchor_bias_fields``), ATR = mean true range of
    the trailing day (``vp_lane.mean_true_range``, the live counterpart of
    the replay's whole-series mean TR)."""
    ts_close = str(m5_prefix[-1].get("ts") or "") if m5_prefix else ""
    swing_bars = int(_env(ENV_SWING_BARS, 6))
    if len(m5_prefix) < swing_bars + 3:
        return _skip(ts_close, symbol, session, f"bars<{swing_bars + 3}")
    atr = vp_lane.mean_true_range(m5_prefix)
    if atr <= 0:
        return _skip(ts_close, symbol, session, "no_atr")
    anchor_hour = int(_env(ENV_ANCHOR_HOUR, 0))
    min_hours = _env(ENV_MIN_HOURS, 1.0)
    bias, hours = vp_lane.dayopen_bias(m5_prefix, anchor_hour)
    if bias == 0 or hours < min_hours:
        return _skip(ts_close, symbol, session, f"no_bias (bias={bias} hrs={hours:.1f})")

    pull_atr = _env(ENV_PULL_ATR, 0.8)
    buffer_atr = _env(ENV_BUFFER_ATR, 0.1)
    max_risk_atr = _env(ENV_MAX_RISK_ATR, 2.0)
    last = m5_prefix[-1]
    o = _f(last.get("open"), 0.0)
    c = _f(last.get("close"), 0.0)
    day_bars = min(len(m5_prefix), max(swing_bars + 2, int(hours * 12) + 1))
    day = m5_prefix[-day_bars:]
    features = {"daytrend": {"bias": bias, "hours": round(hours, 2),
                             "atr": round(atr, 4), "day_bars": day_bars}}

    if bias < 0:                                   # ต่ำเปิด -> sells only
        extreme = min(_f(b.get("low"), 0.0) for b in day)
        pullback = c - extreme
        if not (pullback >= pull_atr * atr and c < o):
            return _skip(ts_close, symbol, session,
                         f"no_setup (pull={pullback:.2f} need>={pull_atr * atr:.2f} red={c < o})")
        swing_hi = max(_f(b.get("high"), 0.0) for b in m5_prefix[-swing_bars:])
        sl = swing_hi + buffer_atr * atr
        risk = sl - c
        if risk <= 0 or risk > max_risk_atr * atr:
            return _skip(ts_close, symbol, session, f"risk_out_of_band ({risk:.2f})")
        return Decision(
            ts_close=ts_close, symbol=symbol, action="enter", side="sell",
            entry_type="market", entry=c, sl=sl, tp=extreme, size_class="small",
            leader_score=0.0, p_win_est=0.0, setup="daytrend_pullback_resume",
            reasons=[f"ต่ำเปิด {hours:.1f}h: pullback {pullback:.2f} off day low "
                     f"{extreme:.2f}, red resume close; sl>{swing_hi:.2f} swing"],
            session=session, features=features,
        )

    extreme = max(_f(b.get("high"), 0.0) for b in day)
    pullback = extreme - c
    if not (pullback >= pull_atr * atr and c > o):
        return _skip(ts_close, symbol, session,
                     f"no_setup (pull={pullback:.2f} need>={pull_atr * atr:.2f} green={c > o})")
    swing_lo = min(_f(b.get("low"), 0.0) for b in m5_prefix[-swing_bars:])
    sl = swing_lo - buffer_atr * atr
    risk = c - sl
    if risk <= 0 or risk > max_risk_atr * atr:
        return _skip(ts_close, symbol, session, f"risk_out_of_band ({risk:.2f})")
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side="buy",
        entry_type="market", entry=c, sl=sl, tp=extreme, size_class="small",
        leader_score=0.0, p_win_est=0.0, setup="daytrend_pullback_resume",
        reasons=[f"ยืนเปิด {hours:.1f}h: pullback {pullback:.2f} off day high "
                 f"{extreme:.2f}, green resume close; sl<{swing_lo:.2f} swing"],
        session=session, features=features,
    )

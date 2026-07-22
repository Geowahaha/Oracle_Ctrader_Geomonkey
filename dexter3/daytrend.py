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

# DPULL lane (owner sign-off 2026-07-22 "สร้างเลนใหม่เลยครับ ... deploy เทรดจริง
# ได้เลย"): the SAME decide_daytrend producer run with cap12 + deep-pullback
# bypass 3xATR, but the dtcap matrix's winning GEOMETRY instead of daytrend's
# market x plain: limit entry at signal -0.5R (TTL 6 bars) x convex trail
# arm 2.0R / giveback 3.0xATR / max age 24 bars. Evidence (VM
# /tmp/dtcap_bypass3_{6000,10000,14000}.log, row "limit -0.5R w6 x convex
# a2.0 gb3.0 h24"): derive/validate +35.67/+62.80, +19.32/+116.55,
# +12.78/+100.10 — positive BOTH segments in ALL 3 windows (the bar that
# killed BE-ratchet and the rejection-quality gates). Same-producer overlap
# with the daytrend lane on cap12-passing signals is handled by the standing
# DEXTER3_CROSS_LANE_DEDUP=downsize policy, NOT by forking the producer.
DPULL_LABEL = "dexter3:dpull:canary"
DPULL_LABEL_FAMILY = "dexter3:dpull"

ENV_PULL_ATR = "DEXTER3_DAYTREND_PULL_ATR"           # default 0.8 (proven)
ENV_SWING_BARS = "DEXTER3_DAYTREND_SWING_BARS"       # default 6
ENV_BUFFER_ATR = "DEXTER3_DAYTREND_BUFFER_ATR"       # default 0.1
ENV_MIN_HOURS = "DEXTER3_DAYTREND_MIN_HOURS"         # default 1.0
ENV_MAX_RISK_ATR = "DEXTER3_DAYTREND_MAX_RISK_ATR"   # default 2.0
ENV_ANCHOR_HOUR = "DEXTER3_DAYTREND_ANCHOR_HOUR"     # default 0 (00Z)
# G1 capitulation guard (owner 2026-07-16, after the first live day: two
# continuation sells died to the DZ rebound at ~14x ATR day range): once the
# day has traveled this far, continuation entries STOP. 0 = off.
ENV_RANGE_CAP_ATR = "DEXTER3_DAYTREND_RANGE_CAP_ATR"
# Deep-pullback bypass of G1 (owner miss 2026-07-22: a strong ยืนเปิด trend
# day ran 65-73pts > the 12xATR cap, so ALL 57 continuation evaluations
# 09-15Z were range_cap-skipped — including the textbook ~28pt pullback-to-
# DZ + resume that then ran +30pts; the guard built for capitulation-chasing
# also killed every deep-retrace resume, which is a different anatomy: the
# entry is AFTER a retrace, not AT the extreme). When > 0 and the pullback
# off the day extreme is >= this x ATR, the range cap does not skip; the
# normal pullback/resume/risk-band logic still applies. 0 = off (bitwise
# pre-2026-07-22 behavior). GATED ON REPLAY EVIDENCE before any live enable.
ENV_CAP_PULLBACK_BYPASS_ATR = "DEXTER3_DAYTREND_CAP_PULLBACK_BYPASS_ATR"
# DAYREVERSAL (owner order 2026-07-16 "โอกาสแบบนี้หายาก เปิดเลย"): the mirror
# twin — the SAME capitulation condition ARMS the reversal hunt at the day
# extreme. Rare by construction (2-9 occurrences per 10 replay weeks even at
# 8x — backtest cannot judge it; deployed LIVE at canary size on explicit
# owner order, forward numbers are the trial).
ENV_DAYREV_ENABLED = "DEXTER3_DAYREVERSAL"           # "1" -> live
ENV_DAYREV_ARM_ATR = "DEXTER3_DAYREV_ARM_ATR"        # default 12.0
ENV_DAYREV_NEAR_ATR = "DEXTER3_DAYREV_NEAR_ATR"      # default 2.0
ENV_DAYREV_RETRACE = "DEXTER3_DAYREV_RETRACE"        # default 0.5
ENV_DAYREV_SWING_K = "DEXTER3_DAYREV_SWING_K"        # default 2 (fractal k)


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
    # G1 capitulation guard: beyond the cap the extreme is a DZ/SZ, not a
    # continuation target — the reversal producer takes over from here.
    # Deep-pullback bypass (2026-07-22): a retrace >= bypass x ATR off the
    # extreme is entry-after-retrace anatomy, not capitulation-chasing — the
    # cap stops skipping it (see ENV_CAP_PULLBACK_BYPASS_ATR).
    range_cap = _env(ENV_RANGE_CAP_ATR, 0.0)
    cap_bypassed = False
    if range_cap > 0:
        d_hi = max(_f(b.get("high"), 0.0) for b in day)
        d_lo = min(_f(b.get("low"), 0.0) for b in day)
        if (d_hi - d_lo) > range_cap * atr:
            bypass_atr = _env(ENV_CAP_PULLBACK_BYPASS_ATR, 0.0)
            pullback_now = (d_hi - c) if bias > 0 else (c - d_lo)
            if bypass_atr > 0 and pullback_now >= bypass_atr * atr:
                cap_bypassed = True
            else:
                return _skip(ts_close, symbol, session,
                             f"range_cap ({(d_hi - d_lo):.1f} > {range_cap:g}x{atr:.2f}ATR)")
    features = {"daytrend": {"bias": bias, "hours": round(hours, 2),
                             "atr": round(atr, 4), "day_bars": day_bars,
                             "cap_bypassed": cap_bypassed}}

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

def dayreversal_enabled() -> bool:
    return os.environ.get(ENV_DAYREV_ENABLED, "0").strip() == "1"


def decide_dayreversal(symbol: str, m5_prefix: list, spread_abs: float,
                       session: str = "unknown") -> Decision:
    """DZ/SZ exhaustion-reversal (owner order 2026-07-16 "เปิดเลย"): the
    mirror twin of the continuation producer — the SAME capitulation
    condition that stops daytrend ARMS this hunt. BAR-IDENTICAL to the
    replay's ``decide_dayreversal`` (parity-tested):
      * day bias >= min_hours -> there IS a day extreme;
      * capitulation: day range >= arm x ATR;
      * price within near x ATR of the day extreme (the DZ/SZ);
      * classic structure flip: higher swing low (ยก low, fractal k) AND the
        newest bar CLOSES above the last swing high (ยก high);
      * SL beyond the DZ, TP at the retrace fraction of the capitulation leg.
    Rare by construction — every occurrence is journaled with full features."""
    ts_close = str(m5_prefix[-1].get("ts") or "") if m5_prefix else ""
    swing_k = int(_env(ENV_DAYREV_SWING_K, 2))
    if len(m5_prefix) < swing_k * 2 + 8:
        return _skip(ts_close, symbol, session, "dayrev_bars_short")
    atr = vp_lane.mean_true_range(m5_prefix)
    if atr <= 0:
        return _skip(ts_close, symbol, session, "dayrev_no_atr")
    anchor_hour = int(_env(ENV_ANCHOR_HOUR, 0))
    min_hours = _env(ENV_MIN_HOURS, 1.0)
    bias, hours = vp_lane.dayopen_bias(m5_prefix, anchor_hour)
    if bias == 0 or hours < min_hours:
        return _skip(ts_close, symbol, session, "dayrev_no_bias")
    arm_atr = _env(ENV_DAYREV_ARM_ATR, 12.0)
    near_atr = _env(ENV_DAYREV_NEAR_ATR, 2.0)
    retrace = _env(ENV_DAYREV_RETRACE, 0.5)
    buffer_atr = _env(ENV_BUFFER_ATR, 0.1)
    max_risk_atr = _env(ENV_MAX_RISK_ATR, 2.0)
    day_bars = min(len(m5_prefix), max(swing_k * 2 + 8, int(hours * 12) + 1))
    day = m5_prefix[-day_bars:]
    d_hi = max(_f(b.get("high"), 0.0) for b in day)
    d_lo = min(_f(b.get("low"), 0.0) for b in day)
    day_range = d_hi - d_lo
    if day_range < arm_atr * atr:
        return _skip(ts_close, symbol, session,
                     f"dayrev_not_armed (range {day_range:.1f} < {arm_atr:g}xATR)")
    c = _f(m5_prefix[-1].get("close"), 0.0)

    def _swings(vals: list, is_low: bool) -> list:
        out = []
        for j in range(swing_k, len(vals) - swing_k):
            w = vals[j - swing_k: j + swing_k + 1]
            if (is_low and vals[j] == min(w)) or ((not is_low) and vals[j] == max(w)):
                out.append((j, vals[j]))
        return out

    lows = [_f(b.get("low"), 0.0) for b in day]
    highs = [_f(b.get("high"), 0.0) for b in day]
    features = {"dayreversal": {"bias": bias, "hours": round(hours, 2),
                                "atr": round(atr, 4), "day_range": round(day_range, 2),
                                "d_hi": round(d_hi, 2), "d_lo": round(d_lo, 2)}}
    if bias < 0:                                   # sell day -> BUY the DZ flip
        if c - d_lo > near_atr * atr:
            return _skip(ts_close, symbol, session, "dayrev_not_in_zone")
        sw_lo = _swings(lows, True)
        sw_hi = _swings(highs, False)
        if len(sw_lo) < 2 or not sw_hi:
            return _skip(ts_close, symbol, session, "dayrev_no_swings")
        (_, prev_low), (last_idx, last_low) = sw_lo[-2], sw_lo[-1]
        if not (last_low > prev_low):
            return _skip(ts_close, symbol, session, "dayrev_no_higher_low")
        hi_after = [v for j, v in sw_hi if j >= last_idx - swing_k]
        ref_hi = hi_after[-1] if hi_after else sw_hi[-1][1]
        if not (c > ref_hi):
            return _skip(ts_close, symbol, session, "dayrev_no_break")
        sl = min(d_lo, last_low) - buffer_atr * atr
        risk = c - sl
        if risk <= 0 or risk > max_risk_atr * atr:
            return _skip(ts_close, symbol, session, f"dayrev_risk_band ({risk:.2f})")
        return Decision(
            ts_close=ts_close, symbol=symbol, action="enter", side="buy",
            entry_type="market", entry=c, sl=sl, tp=d_lo + retrace * day_range,
            size_class="small", leader_score=0.0, p_win_est=0.0,
            setup="dayreversal_structure_flip",
            reasons=[f"DZ flip: capitulation {day_range:.1f}pts, ยก low {prev_low:.2f}->"
                     f"{last_low:.2f}, close {c:.2f} broke swing-high {ref_hi:.2f}; "
                     f"sl<{sl:.2f} tp@{retrace:g} retrace"],
            session=session, features=features,
        )
    if d_hi - c > near_atr * atr:                  # buy day -> SELL the SZ flip
        return _skip(ts_close, symbol, session, "dayrev_not_in_zone")
    sw_hi = _swings(highs, False)
    sw_lo = _swings(lows, True)
    if len(sw_hi) < 2 or not sw_lo:
        return _skip(ts_close, symbol, session, "dayrev_no_swings")
    (_, prev_hi), (last_idx, last_hi) = sw_hi[-2], sw_hi[-1]
    if not (last_hi < prev_hi):
        return _skip(ts_close, symbol, session, "dayrev_no_lower_high")
    lo_after = [v for j, v in sw_lo if j >= last_idx - swing_k]
    ref_lo = lo_after[-1] if lo_after else sw_lo[-1][1]
    if not (c < ref_lo):
        return _skip(ts_close, symbol, session, "dayrev_no_break")
    sl = max(d_hi, last_hi) + buffer_atr * atr
    risk = sl - c
    if risk <= 0 or risk > max_risk_atr * atr:
        return _skip(ts_close, symbol, session, f"dayrev_risk_band ({risk:.2f})")
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side="sell",
        entry_type="market", entry=c, sl=sl, tp=d_hi - retrace * day_range,
        size_class="small", leader_score=0.0, p_win_est=0.0,
        setup="dayreversal_structure_flip",
        reasons=[f"SZ flip: capitulation {day_range:.1f}pts, ยก high ลง {prev_hi:.2f}->"
                 f"{last_hi:.2f}, close {c:.2f} broke swing-low {ref_lo:.2f}; "
                 f"sl>{sl:.2f} tp@{retrace:g} retrace"],
        session=session, features=features,
    )

ENV_SDZONE_ENABLED = "DEXTER3_SDZONE"                # "1" -> zone entries live


def sdzone_enabled() -> bool:
    return os.environ.get(ENV_SDZONE_ENABLED, "0").strip() == "1"


def decide_sdzone_live(symbol: str, m5_prefix: list, spread_abs: float,
                       session: str = "unknown") -> Decision:
    """Owner order 2026-07-17 ("โอกาสหายาก เปิดเลย"): zone-anchored entries
    from the SAME engine the replay proved (dexter3/sd_zones.py — single
    source of truth, parity by construction). Zones recomputed statelessly
    from the lane's 340-bar prefix each M5 close; entry when price re-enters
    a live zone and CLOSES back outside it in the zone's direction; SL
    beyond the zone; TP = RR2. The day-open bias gate applies via the
    runner's vp_entry_gate (the proven x-bias combo), and the lane's plain
    exit mode leaves broker SL/TP + the 240min cap as the only exits."""
    from dexter3.sd_zones import decide_sdzone, zones_from_prefix

    ts_close = str(m5_prefix[-1].get("ts") or "") if m5_prefix else ""
    if len(m5_prefix) < 40:
        return _skip(ts_close, symbol, session, "sdzone_bars_short")
    engine, atr = zones_from_prefix(m5_prefix)
    if atr <= 0:
        return _skip(ts_close, symbol, session, "sdzone_no_atr")
    sig = decide_sdzone(m5_prefix, len(m5_prefix) - 1, engine, atr)
    if sig is None:
        return _skip(ts_close, symbol, session,
                     f"sdzone_no_setup (zones={len(engine.zones)})")
    zinfo = [{"kind": z["kind"], "top": round(z["top"], 2),
              "bottom": round(z["bottom"], 2)} for z in engine.zones[:4]]
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=sig["side"],
        entry_type="market", entry=sig["entry"], sl=sig["sl"], tp=sig["tp"],
        size_class="small", leader_score=0.0, p_win_est=0.0,
        setup="sdzone_reentry_confirm",
        reasons=[f"SD zone re-entry confirm ({sig['side']}); zones={zinfo}"],
        session=session, features={"sdzone": {"zones": zinfo, "atr": round(atr, 3)}},
    )

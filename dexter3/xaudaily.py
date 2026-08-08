"""XAUDAILY producer — the low-frequency lane (owner order 2026-08-07:
"ทำข้อ 2 และ 3 live deploy new lane เลย ... เอาให้ชัวร์อย่าสรุปมั่วๆอีก").

WHY THIS LANE EXISTS — the measurement that produced it
-------------------------------------------------------
2026-08-07 rigorous scan (non-overlapping samples, standard errors on every
number, positive controls in every harness):
  * USTEC M5 price paths carry NO directional information: 1-bar return
    autocorrelation +0.0101, and a breakout bracket scores −0.83..−1.63
    points/trade against a no-information control at −2.0..−4.1 — i.e. it
    recovers roughly the spread and nothing more. Fades score WORSE than
    random. Eight separate entry families died on this fact.
  * XAU M5 carries no measurable information either: volume structure
    (LVN / HVN / value-area state, computed with the live vp lane's own
    build_profile), Kaufman efficiency-ratio regime, and the day-open bias
    were tested at 30-minute, 1-, 2- and 4-hour horizons. NOT ONE state
    exceeded 2 standard errors at any horizon.
  * The honest reading of that null is NOT "no edge exists": with N of
    100-340 per state the standard error is 0.24-0.43 ATR, so only effects
    larger than ~0.5 ATR are detectable, while a perfectly tradeable edge
    can be 0.1-0.2 ATR. **70 days of M5 data cannot resolve a scalping edge
    in either direction — which is precisely why this project judges on
    live broker deals and treats replay as a filter, never a proof.**

THE DESIGN — built from what survives that null, not from a prediction
----------------------------------------------------------------------
If direction is barely predictable, the only lever left is COST. Spread is
a fixed charge per trade, so the fewer and larger the trades, the smaller
its drag on whatever small edge exists. This lane is therefore the exact
opposite of the USTEC scalp fleet (7-19 trades/day, 15-minute holds):

  ONE trade per day, at the hour with the highest measured volatility
  (13:00Z on XAU derive: mean M5 range 8.61 vs 4-6 elsewhere — chosen by
  VOLATILITY, never by backtest PnL, so the hour is not a fitted parameter),
  in the direction of the day-open bias (price vs the 00:00Z open — this
  repo's own long-standing bias layer), with a WIDE stop and a FAR fixed
  target, and NO management whatsoever. The wide stop follows the 2026-07-25
  full audit's single most durable finding ("THE EDGE = SL >= 1.2xTR") and
  the unmanaged exit follows its companion finding ("81% of profit = not
  managing"; fixed broker TP produced 66.5% of all live gross profit).

EVIDENCE STATUS — read this before believing anything
------------------------------------------------------
Measured on 13,999 real XAU M5 bars (2026-05-28 -> 2026-08-07), spread 0.30
charged, per-trade PnL normalised by ATR, split 60/40 by bar index:

    SL1.5/TP3.0  derive +0.097 +-0.402 (N=30) | validate +0.573 +-0.529 (N=19)
    SL2.0/TP4.0  derive +0.347 +-0.546 (N=30) | validate +0.950 +-0.687 (N=19)
    SL2.0/TP3.0  derive +0.114 +-0.460 (N=30) | validate +0.739 +-0.569 (N=19)
    INVERTED (fade the bias, control)
                 derive -0.203 +-0.383 (N=30) | validate -0.611 +-0.433 (N=19)

All four geometries are positive in BOTH halves and the inverted control is
negative in BOTH halves, so the sign structure is at least self-consistent.
**But no cell reaches 2 standard errors (the best is 1.38 SE), N is only
30/19, and the validate half was consumed by this same run — there is no
untouched hold-out left.** THIS LANE THEREFORE CLAIMS NO BACKTESTED EDGE.
It is the best-motivated candidate available, and the only test it can still
face is forward live money. Judge it exactly like every other lane: N>=30
broker deals in DOLLARS. Negative -> kill it without ceremony, no tuning.

Off-by-default: DEXTER3_MODE=xaudaily / DEXTER3_PRODUCER=xaudaily activates.
Every function pure (env readers aside). tests/test_dexter3_xaudaily.py pins.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts

Bar = dict[str, Any]

XAUDAILY_LABEL_FAMILY = "dexter3:xaudaily"
XAUDAILY_LABEL = XAUDAILY_LABEL_FAMILY + ":canary"

ENV_ATR_LEN = "DEXTER3_XAUDAILY_ATR_LEN"        # Wilder ATR length (14)
ENV_ENTRY_HOUR = "DEXTER3_XAUDAILY_HOUR"        # UTC hour of the single entry (13)
ENV_SL_ATR = "DEXTER3_XAUDAILY_SL_ATR"          # stop distance in ATR (2.0)
ENV_TP_ATR = "DEXTER3_XAUDAILY_TP_ATR"          # target distance in ATR (4.0)
ENV_ANCHOR_HOUR = "DEXTER3_XAUDAILY_ANCHOR_HOUR"  # day-open anchor hour (0)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    return _f(os.environ.get(key), default)


def _env_int(key: str, default: int) -> int:
    return int(_env_float(key, float(default)))


def xaudaily_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "xaudaily":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "xaudaily"


def _atr_rma(bars: list[Bar], length: int) -> float:
    """Wilder RMA of true range — the same ATR the measurement used."""
    if len(bars) < length + 1:
        return 0.0
    trs: list[float] = []
    for k in range(1, len(bars)):
        hi, lo = _f(bars[k].get("high")), _f(bars[k].get("low"))
        pc = _f(bars[k - 1].get("close"))
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    atr = sum(trs[:length]) / length
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


def day_open_price(m5_bars: list[Bar], day: str, anchor_hour: int) -> float | None:
    """OPEN of the day's anchor bar, or None when the prefix does not actually
    reach it.

    2026-08-07 (caught by its own test before deployment): an earlier version
    returned the first bar of ``day`` at-or-after the anchor hour, which
    silently used a 09:00Z bar as the "day open" whenever the prefix started
    late — the same defect class as the BRK gate's 15h memory, and it would
    have inverted the bias on exactly the days the fetch ran short. The
    prefix's FIRST bar of that day must itself be the anchor hour; anything
    later means the anchor is unknown and the day must be skipped."""
    for b in m5_bars:
        ts = str(b.get("ts") or "")
        if len(ts) < 16 or ts[:10] != day:
            continue
        try:
            hour = int(ts[11:13])
        except ValueError:
            return None
        return _f(b.get("open")) if hour == anchor_hour else None
    return None


def is_entry_bar(ts: str, entry_hour: int) -> bool:
    """True only for the bar that OPENS at ``entry_hour``:00 UTC. Bars are
    labeled by open time, so the decision lands on that bar's close."""
    if len(ts) < 16:
        return False
    try:
        return int(ts[11:13]) == entry_hour and int(ts[14:16]) == 0
    except ValueError:
        return False


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"xaudaily_skip: {reason}"], features={},
    )


def decide_xaudaily(symbol: str, m5_bars: list[Bar], spread_abs: float,
                    session: str = "unknown") -> Decision:
    """One decision per day: at the entry hour, take the day-open bias with a
    wide stop and a far fixed target. Every other bar is a skip."""
    atr_len = max(2, _env_int(ENV_ATR_LEN, 14))
    entry_hour = _env_int(ENV_ENTRY_HOUR, 13)
    anchor_hour = _env_int(ENV_ANCHOR_HOUR, 0)
    sl_atr = _env_float(ENV_SL_ATR, 2.0)
    tp_atr = _env_float(ENV_TP_ATR, 4.0)

    last_ts = str(m5_bars[-1].get("ts") or "") if m5_bars else ""
    ts_close = _bar_close_ts(last_ts) if last_ts else ""
    if len(m5_bars) < atr_len + 2:
        return _skip(ts_close, symbol, f"bars<{atr_len + 2}")
    if not is_entry_bar(last_ts, entry_hour):
        return _skip(ts_close, symbol, f"not_entry_bar (window {entry_hour:02d}:00Z)")

    anchor = day_open_price(m5_bars, last_ts[:10], anchor_hour)
    if anchor is None or anchor <= 0:
        return _skip(ts_close, symbol, "no_day_open_anchor_in_prefix")

    a = _atr_rma(m5_bars, atr_len)
    if a <= 0:
        return _skip(ts_close, symbol, "atr<=0")

    entry = _f(m5_bars[-1].get("close"))
    if entry == anchor:
        return _skip(ts_close, symbol, "flat_vs_day_open")
    side = "buy" if entry > anchor else "sell"
    risk = sl_atr * a
    if risk <= 0:
        return _skip(ts_close, symbol, "zero_risk")
    sl = entry - risk if side == "buy" else entry + risk
    tp = (entry + tp_atr * a) if side == "buy" else (entry - tp_atr * a)
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(entry, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=0.40, setup="xaudaily_bias",
        reasons=[f"day-open bias {side}: close {entry:.2f} vs {anchor_hour:02d}Z open "
                 f"{anchor:.2f} ({entry - anchor:+.2f}); ATR {a:.2f}; "
                 f"SL {sl_atr}xATR TP {tp_atr}xATR, unmanaged"],
        features={"xd_atr": round(a, 4), "xd_day_open": round(anchor, 4),
                  "xd_bias_pts": round(entry - anchor, 3),
                  "xd_bias_atr": round((entry - anchor) / a, 2)},
        session=session,
    )

"""MSCALP producer — the owner's 5-minute close-based scalper (order
2026-08-05: "สร้าง incredible winner trading ทุกๆ 5 นาที จบแท่ง แล้วเปิดและปิดใน
แท่งนั้นๆ เลย ... หรือไม่ปิดได้ถ้ากำไรยังไปต่อ ... เทรดใน USTEC ตัวเดียวก่อน").

DESIGN PRINCIPLE (the week's real-chart lessons, [[feedback_live_wins_only]]):
built ONLY from components that have won on live broker deals in this repo —
  * WITH-momentum continuation, never reversion (the sniper autopsy: every
    reversion cell negative; dtr's continuation = the week's biggest live win);
  * bank small green fast on CLOSED bars (grok small-lock 0.35R streaks, the
    scalp lane's bank-green, fixed-TP = 66.5% of all live gross profit);
  * regime-gated: only trade when the market HAS a direction (channelfade's
    complement — |6h drift| >= drift_min_atr x ATR).

ENTRY (on every closed M5 bar): the bar is an IMPULSE (body > body_frac x
range AND range > range_atr x ATR14) in the direction of the 6h drift
(close - close[72 bars ago]), and |drift| >= drift_min_atr x ATR ->
MARKET at the close. SL = sl_atr x ATR (disaster wick stop). TP = far
backstop at tp_backstop_r x risk — the REAL exits are close-based:

EXIT (existing OM machinery, no new exit code):
  DEXTER3_OM_TRAIL_MODE=bank  + DEXTER3_OM_BANK_R    -> bank when a CLOSED
      bar shows >= bank_r x risk profit ("ปิดกำไรในแท่ง");
  DEXTER3_BASKET_TIME_STOP_MIN (15 = 3 bars)          -> flat if undecided;
  broker SL                                           -> the disaster case.
"ถือถ้ากำไรยังไปต่อ" emerges naturally: a bar that closes above +bank_r banks
at that close; the position is never held past 3 bars.

EVIDENCE STATUS (binding standard: live wins only): the concept passed a
POINTS-based filter on real USTEC M5 bars (2026-07-21→08-05, spread-charged,
one-position): drift 3.0 x bank 0.5 -> derive +$177 (N=105) / validate +$368
(N=85), WR 53%. THAT IS A FILTER, NOT A PROOF — the lane deploys as a canary
and is judged ONLY on broker deals.

Off-by-default: DEXTER3_MODE=mscalp / DEXTER3_PRODUCER=mscalp activates.
Every function pure (env readers aside). tests/test_dexter3_mscalp.py pins.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts

Bar = dict[str, Any]

MSCALP_LABEL_FAMILY = "dexter3:mscalp"
MSCALP_LABEL = MSCALP_LABEL_FAMILY + ":canary"

# -- MSCALP2 (owner order 2026-08-06 "clone ทำ parallel lane ... กำไร >5/10 USD
# ให้ปิดเลย และใช้ exit แบบเดียวกับต้นแบบ"): the forward-A/B twin. SAME
# producer, SAME entries, SAME bank/time/SL exits — the ONLY difference is
# the fast-tick dollar-take below (close the position the first tick its
# REAL broker floating PnL >= take_usd). Measures bank-the-tail-vs-take-fast
# on live money, the dpull/dpull-cs pattern.
#
# EVIDENCE (filter, NOT proof — day-1 mscalp autopsy 2026-08-06, N=12 real
# positions replayed against M1 MFE): $5-take ~= +$29.7 (11W/1L) vs actual
# +$50.90; $10-take ~= -$2.7 (caps the +101.4 tail, misses three losers'
# $6.7-9.5 MFE). $5 chosen as the default; judged at N>=30 broker deals.
MSCALP2_LABEL_FAMILY = "dexter3:mscalp2"
MSCALP2_LABEL = MSCALP2_LABEL_FAMILY + ":canary"

ENV_MSCALP2_TAKE_USD = "DEXTER3_MSCALP2_TAKE_USD"      # dollar take; <=0 = off

ENV_ATR_LEN = "DEXTER3_MSCALP_ATR_LEN"                # ATR length (14)
ENV_BODY_FRAC = "DEXTER3_MSCALP_IMPULSE_BODY_FRAC"    # impulse body > frac x range (0.6)
ENV_RANGE_ATR = "DEXTER3_MSCALP_RANGE_ATR"            # impulse range > k x ATR (1.0)
ENV_DRIFT_BARS = "DEXTER3_MSCALP_DRIFT_BARS"          # drift lookback in M5 bars (72 = 6h)
ENV_DRIFT_MIN_ATR = "DEXTER3_MSCALP_DRIFT_MIN_ATR"    # regime gate: |drift| >= k x ATR (3.0)
ENV_SL_ATR = "DEXTER3_MSCALP_SL_ATR"                  # disaster stop distance in ATR (1.3)
ENV_TP_BACKSTOP_R = "DEXTER3_MSCALP_TP_BACKSTOP_R"    # far TP backstop in R (3.0)


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


def mscalp_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "mscalp":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "mscalp"


def mscalp2_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "mscalp2":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "mscalp2"


def mscalp2_take_usd() -> float:
    """Dollar-take threshold for the mscalp2 twin; <= 0 disables the branch
    (byte-identical to the mscalp original, so a mis-set env can never arm
    it silently on another lane)."""
    return _env_float(ENV_MSCALP2_TAKE_USD, 0.0)


def mscalp2_take_decision(agg_pnl_usd: float, take_usd: float) -> bool:
    """Pure: should the mscalp2 lane close NOW? True the first evaluation
    where the lane's REAL broker floating PnL has reached the dollar take.
    The caller feeds broker-valued aggregate PnL (spread/commission already
    inside), so `>= take_usd` means the owner's "กำไร >$X จริง" in hand."""
    return take_usd > 0 and agg_pnl_usd >= take_usd


def _atr(bars: list[Bar], length: int) -> float:
    if len(bars) < length + 1:
        return 0.0
    trs = []
    for k in range(len(bars) - length, len(bars)):
        hi, lo = _f(bars[k].get("high")), _f(bars[k].get("low"))
        pc = _f(bars[k - 1].get("close"))
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / length


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"mscalp_skip: {reason}"], features={},
    )


def decide_mscalp(symbol: str, m5_bars: list[Bar], spread_abs: float,
                  session: str = "unknown") -> Decision:
    """Impulse-continuation scalp decision on the last CLOSED bar."""
    atr_len = max(2, _env_int(ENV_ATR_LEN, 14))
    body_frac = _env_float(ENV_BODY_FRAC, 0.6)
    range_atr = _env_float(ENV_RANGE_ATR, 1.0)
    drift_bars = max(2, _env_int(ENV_DRIFT_BARS, 72))
    drift_min = _env_float(ENV_DRIFT_MIN_ATR, 3.0)
    sl_atr = _env_float(ENV_SL_ATR, 1.3)
    tp_r = _env_float(ENV_TP_BACKSTOP_R, 3.0)

    ts_close = _bar_close_ts(str(m5_bars[-1].get("ts") or "")) if m5_bars else ""
    if len(m5_bars) < max(drift_bars + 1, atr_len + 2):
        return _skip(ts_close, symbol, f"bars<{max(drift_bars + 1, atr_len + 2)}")
    a = _atr(m5_bars, atr_len)
    if a <= 0:
        return _skip(ts_close, symbol, "atr<=0")

    last = m5_bars[-1]
    o, c = _f(last.get("open")), _f(last.get("close"))
    hi, lo = _f(last.get("high")), _f(last.get("low"))
    rng, body = hi - lo, abs(c - o)
    if rng <= 0 or body <= body_frac * rng or rng <= range_atr * a:
        return _skip(ts_close, symbol, "no_impulse")

    side = "buy" if c > o else "sell"
    drift = c - _f(m5_bars[-drift_bars - 1].get("close"))
    if (side == "buy" and drift <= 0) or (side == "sell" and drift >= 0):
        return _skip(ts_close, symbol, f"impulse_against_drift ({drift:+.2f})")
    if abs(drift) < drift_min * a:
        return _skip(ts_close, symbol,
                     f"regime_flat |drift|={abs(drift):.2f} < {drift_min}xATR({a:.2f})")

    risk = sl_atr * a
    if risk <= 0:
        return _skip(ts_close, symbol, "zero_risk")
    sl = c - risk if side == "buy" else c + risk
    tp = c + tp_r * risk if side == "buy" else c - tp_r * risk
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(c, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=0.53, setup="mscalp_impulse",
        reasons=[f"impulse {side} body/rng={body / rng:.2f} rng/ATR={rng / a:.2f} "
                 f"drift={drift:+.1f} ({abs(drift) / a:.1f}xATR); exits=bank/time/SL"],
        features={"mscalp_atr": round(a, 4), "mscalp_drift_atr": round(drift / a, 2),
                  "mscalp_body_frac": round(body / rng, 3),
                  "mscalp_range_atr": round(rng / a, 2)},
        session=session,
    )

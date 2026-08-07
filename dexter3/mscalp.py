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

# -- MSCALP-BE (owner order 2026-08-06 "ทำ twin ตัวที่สาม mscalp-be"): the
# BE-at-deadline twin. SAME producer/entries; at deadline_sec the position's
# broker SL is AMENDED to breakeven instead of being flattened — bank 0.5R
# and the 3R TP keep working, so "ถือถ้ากำไรยังไปต่อ" holds literally: the
# clock only decides when the trade must become FREE, never when to exit.
#
# EVIDENCE (filter, NOT proof — 2026-08-06 time-stop sweep, 9000 real USTEC
# M5 bars / 711 signals / spread 1.5 charged / 60-40 split): BE15 was the
# ONLY cell positive in BOTH segments — derive +236 / validate +1277 PF 1.58,
# thrown runners 0 — vs the deployed T15's derive -1252 / validate +536 with
# 61 of 257 time stops discarding a >=1R continuation. Judged at N>=30
# broker deals vs mscalp/mscalp2 on the same signals.
MSCALP_BE_LABEL_FAMILY = "dexter3:mscalp-be"   # '-be' is NOT a "-v<d>" token,
MSCALP_BE_LABEL = MSCALP_BE_LABEL_FAMILY + ":canary"  # so no family overlap

ENV_MSCALP_BE_SEC = "DEXTER3_MSCALP_BE_SEC"    # BE deadline seconds; <=0 = off

# -- MSCALP-BRK (owner observation 2026-08-07 "Buy ที่แนวต้าน Sell ที่แนวรับ"
# confirmed by data: the losing entries sat 0.06xATR from the nearest M15
# level while winners had 4+xATR of air): the blue-sky entry twin. SAME
# mscalp impulse signal, taken ONLY when the entry close has broken beyond
# EVERY M15 pivot in the lookback on its own side — no structure overhead
# for a buy, none underfoot for a sell. Binary gate, no tuned threshold.
#
# EVIDENCE (filter, NOT proof — 2026-08-07 sweep, 9000 real USTEC M5 bars /
# 689 signals / T15 exits / spread charged / 60-40 split): BRK = the ONLY
# both-segments-positive entry cell — derive +168 PF 1.10 / validate +364
# PF 1.38 (N=106/71) vs BASE derive −1018 / validate +302; graded headroom
# gates (0.5/1.0/1.5xATR) stayed derive-negative. M15 pivots are aggregated
# FROM THE M5 PREFIX (complete 900s groups, pivot k=2) so replay and live
# share the state model by construction. Judged at N>=30 broker deals vs
# the mscalp original (identical T15 exits — the A/B isolates the entry).
MSCALP_BRK_LABEL_FAMILY = "dexter3:mscalp-brk"  # '-brk' != '-v<d>': disjoint family
MSCALP_BRK_LABEL = MSCALP_BRK_LABEL_FAMILY + ":canary"

ENV_MSCALP_BRK_LOOK_M5 = "DEXTER3_MSCALP_BRK_LOOK_M5"    # M5 lookback bars (180)
ENV_MSCALP_BRK_PIVOT_K = "DEXTER3_MSCALP_BRK_PIVOT_K"    # pivot wing width (2)

# Entry selector for the EXIT twins (owner 2026-08-07 "Mscalp-BE และ Mscalp2
# ให้ใช้ entry แบบใหม่ด้วย"): DEXTER3_MSCALP_ENTRY=brk switches a twin's
# entry stream to decide_mscalp_brk so the exit A/B (T15 vs $5 vs BEW) is
# measured ON the blue-sky population, judged against dexter3-mscalp-brk as
# the T15 control. Default absent = the original decide_mscalp — the
# dexter3-mscalp control lane never sets this.
ENV_MSCALP_ENTRY = "DEXTER3_MSCALP_ENTRY"


def mscalp_entry_is_brk() -> bool:
    return os.environ.get(ENV_MSCALP_ENTRY, "").strip().lower() == "brk"

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


def mscalp_brk_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "mscalp-brk":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "mscalp-brk"


def _bar_epoch(bar: Bar) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(
            str(bar.get("ts") or "").replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


def m15_pivots_from_m5(m5_bars: list[Bar], look_m5: int, pivot_k: int) -> tuple[list[float], list[float]]:
    """(pivot_highs, pivot_lows) of M15 candles aggregated from the last
    ``look_m5`` COMPLETED M5 bars — complete 900s groups only, the trailing
    partial group dropped, classic k-wing pivots. Pure; identical to the
    2026-08-07 sweep's construction so replay and live share the state
    model by definition."""
    lo = max(0, len(m5_bars) - look_m5)
    groups: dict[int, list[int]] = {}
    for idx in range(lo, len(m5_bars)):
        e = _bar_epoch(m5_bars[idx])
        if e <= 0:
            continue
        g = int(e // 900)
        span = groups.setdefault(g, [idx, idx])
        span[1] = idx
    keys = sorted(groups)
    m15: list[tuple[float, float]] = []
    for g in keys[:-1]:  # drop the trailing (possibly partial) group
        a, b = groups[g]
        if b - a == 2:  # complete 3-bar group only
            m15.append((
                max(_f(m5_bars[k].get("high")) for k in range(a, b + 1)),
                min(_f(m5_bars[k].get("low")) for k in range(a, b + 1)),
            ))
    piv_h: list[float] = []
    piv_l: list[float] = []
    for j in range(pivot_k, len(m15) - pivot_k):
        hh, ll = m15[j]
        if all(hh > m15[j + d][0] for d in range(-pivot_k, pivot_k + 1) if d != 0):
            piv_h.append(hh)
        if all(ll < m15[j + d][1] for d in range(-pivot_k, pivot_k + 1) if d != 0):
            piv_l.append(ll)
    return piv_h, piv_l


def mscalp_brk_blue_sky(side: str, entry: float,
                        piv_h: list[float], piv_l: list[float]) -> bool:
    """Pure: True when the entry is in blue sky on its own side — a buy
    above EVERY pivot high, a sell below EVERY pivot low (an empty pivot
    list is blue sky by definition)."""
    if side == "buy":
        return not piv_h or entry > max(piv_h)
    if side == "sell":
        return not piv_l or entry < min(piv_l)
    return False


def decide_mscalp_brk(symbol: str, m5_bars: list[Bar], spread_abs: float,
                      session: str = "unknown") -> Decision:
    """The mscalp impulse decision gated by the M15 blue-sky check — an
    'enter' survives only when it has broken past every pivot on its side;
    otherwise it becomes a journaled skip (``sr_wall``) so the catalog can
    keep scoring what the gate rejects."""
    decision = decide_mscalp(symbol, m5_bars, spread_abs, session=session)
    if decision.action != "enter":
        return decision
    look_m5 = max(30, _env_int(ENV_MSCALP_BRK_LOOK_M5, 180))
    pivot_k = max(1, _env_int(ENV_MSCALP_BRK_PIVOT_K, 2))
    piv_h, piv_l = m15_pivots_from_m5(m5_bars, look_m5, pivot_k)
    entry = _f(decision.entry, 0.0)
    if mscalp_brk_blue_sky(str(decision.side), entry, piv_h, piv_l):
        return Decision(
            ts_close=decision.ts_close, symbol=decision.symbol, action="enter",
            side=decision.side, entry_type=decision.entry_type,
            entry=decision.entry, sl=decision.sl, tp=decision.tp,
            size_class=decision.size_class, leader_score=decision.leader_score,
            p_win_est=decision.p_win_est, setup="mscalp_brk_bluesky",
            reasons=decision.reasons + [
                f"blue_sky {decision.side}: entry beyond all "
                f"{len(piv_h) if decision.side == 'buy' else len(piv_l)} M15 pivots"],
            features={**(decision.features or {}),
                      "brk_piv_h": len(piv_h), "brk_piv_l": len(piv_l)},
            session=session,
        )
    if decision.side == "buy":
        wall = min((p for p in piv_h if p > entry), default=max(piv_h) if piv_h else 0.0)
    else:
        wall = max((p for p in piv_l if p < entry), default=min(piv_l) if piv_l else 0.0)
    return Decision(
        ts_close=decision.ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"mscalp_brk_skip: sr_wall ({decision.side} at {entry} "
                 f"vs M15 level {wall} — not blue sky)"],
        features={"brk_wall": wall, "brk_side": decision.side},
        session=session,
    )


def mscalp_be_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "mscalp-be":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "mscalp-be"


def mscalp_be_deadline_sec() -> float:
    """BE deadline for the mscalp-be twin; <= 0 disables the branch."""
    return _env_float(ENV_MSCALP_BE_SEC, 0.0)


def _position_open_epoch(position: dict[str, Any]) -> float:
    """Open epoch of a broker position dict (0.0 unknown) — same field
    fallbacks as basket_live._position_open_ts."""
    raw = str(
        position.get("openTimestamp")
        or position.get("open_ts")
        or position.get("createTimestamp")
        or position.get("utcLastUpdateTimestamp")
        or ""
    )
    if not raw:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        val = _f(raw, 0.0)
        if val > 1e12:
            return val / 1000.0
        return val if val > 1e9 else 0.0


def position_floating_usd(position: dict[str, Any]) -> float | None:
    """Broker floating PnL of one position dict (None when absent) — same
    field fallbacks basket_live's aggregate uses."""
    for key in ("netProfit", "profit", "grossProfit", "pnl"):
        if position.get(key) is not None:
            return _f(position.get(key), 0.0)
    return None


def mscalp_be_action(position: dict[str, Any], now_epoch: float,
                     deadline_sec: float) -> tuple[str, float] | None:
    """Pure: what the BE deadline demands for this position NOW.

    Returns ('amend', breakeven_price) for a position AT/ABOVE breakeven —
    the broker accepts an SL at entry only from the profitable side; or
    ('close', floating_usd) for an UNDERWATER position — an entry-level stop
    on the losing side would fill instantly at market anyway (and the broker
    rejects the amend outright: measured live 2026-08-06 01:39Z,
    `amend_rejected: New SL for SELL position should be >= current ASK`), so
    the honest deadline action for a loser IS the market close, exactly the
    BEW cell the corrected sweep measured (T15 −1214/+535 vs BEW0 −1133/+661
    PF 1.23, both segments better). None = nothing due (young position,
    already at BE-or-better, or unreadable).

    Idempotent: once the SL sits at breakeven or better the check returns
    None forever. A missing SL on a winner still amends (BE beats naked)."""
    if deadline_sec <= 0:
        return None
    opened = _position_open_epoch(position)
    if opened <= 0 or (now_epoch - opened) < deadline_sec:
        return None
    entry = _f(position.get("entryPrice") or position.get("entry_price"), 0.0)
    sl = _f(position.get("stopLoss") or position.get("stop_loss"), 0.0)
    side = str(position.get("tradeSide") or position.get("side") or "").lower()
    if entry <= 0 or not (side.startswith("buy") or side.startswith("sell")):
        return None
    floating = position_floating_usd(position)
    if floating is None:
        return None  # never act blind on an unreadable PnL
    if floating < 0:
        return ("close", floating)
    if side.startswith("buy") and sl >= entry:
        return None
    if side.startswith("sell") and 0 < sl <= entry:
        return None
    return ("amend", entry)


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

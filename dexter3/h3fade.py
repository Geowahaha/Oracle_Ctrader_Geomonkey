"""H3FADE producer — the M1 streak-fade lane (pre-registered spec 2026-08-05,
docs/handoff/H3_BUILD_BRIEF.md — execute VERBATIM, re-tuning voids the
pre-registration).

THE SPEC (locked as the env defaults below):
  * XAUUSD, M1 bars, UTC hours 13:00-16:59 ONLY (the trigger bar's ts label —
    bars are labeled by OPEN time, measured live 2026-07-05).
  * Trigger: 5 consecutive same-direction M1 CLOSES whose total move is
    >= 2.0 x ATR1 (RMA-14 of M1 true range) -> FADE the streak (sell after an
    up-streak / buy after a down-streak), MARKET at that M1 close.
  * SL = 2.0 x ATR1 (wick disaster stop; ~$3.3 at the 1-oz floor). Broker TP
    = far 3R backstop only — the REAL exits are the M1-close bank (first M1
    CLOSE >= +0.5R), the 4-minute time stop, then the broker SL, in that
    order (h3fade_exit_decision below + shadow_runner._run_h3fade_exit_tick).
    Touch-TP was MEASURED to kill this edge (explore +11.4 -> -5.5): the
    close-based bank is load-bearing, never replace it with a broker TP.

EVIDENCE STATUS (binding standard [[feedback_live_wins_only]]): sole survivor
of the 28-cell XAU scalp campaign on 14k real M1 bars — explore +$11.4
(N=33, WR 55%) / confirm +$18.8 (N=20) / days+ 6/10. N=53 is SMALL and with
28 cells 1-2 false survivors were statistically expected — THIS IS A FILTER,
NOT A PROOF. The lane deploys as a canary and is judged ONLY on broker deals
at N>=30 in dollars; negative -> kill without ceremony, no tuning.

Off-by-default: DEXTER3_MODE=h3fade / DEXTER3_PRODUCER=h3fade activates.
Every function pure (env readers aside). tests/test_dexter3_h3fade.py pins.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts

Bar = dict[str, Any]

H3FADE_LABEL_FAMILY = "dexter3:h3fade"
H3FADE_LABEL = H3FADE_LABEL_FAMILY + ":canary"

M1_BAR_SEC = 60
M1_FETCH_BARS = 40          # per-poll M1 window (brief: "~40 M1 bars per poll")

ENV_ATR_LEN = "DEXTER3_H3FADE_ATR_LEN"          # ATR1 = RMA-N of M1 TR (14)
ENV_STREAK = "DEXTER3_H3FADE_STREAK"            # consecutive same-dir closes (5)
ENV_MOVE_ATR = "DEXTER3_H3FADE_MOVE_ATR"        # streak total move >= k x ATR1 (2.0)
ENV_SL_ATR = "DEXTER3_H3FADE_SL_ATR"            # SL distance in ATR1 (2.0)
ENV_TP_BACKSTOP_R = "DEXTER3_H3FADE_TP_BACKSTOP_R"  # far TP backstop in R (3.0)
ENV_HOUR_START = "DEXTER3_H3FADE_HOUR_START"    # first allowed UTC hour (13)
ENV_HOUR_END = "DEXTER3_H3FADE_HOUR_END"        # last allowed UTC hour, inclusive (16)
ENV_BANK_R = "DEXTER3_H3FADE_BANK_R"            # M1-close bank threshold in R (0.5)
ENV_TIME_STOP_SEC = "DEXTER3_H3FADE_TIME_STOP_SEC"  # time stop in seconds (240)


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


def h3fade_mode_enabled() -> bool:
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "h3fade":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "h3fade"


def min_m1_bars() -> int:
    """Fewest M1 bars decide_h3fade can decide on (ATR seed + streak window)."""
    atr_len = max(2, _env_int(ENV_ATR_LEN, 14))
    streak = max(2, _env_int(ENV_STREAK, 5))
    return max(atr_len + 2, streak + 2)


def _atr_rma(bars: list[Bar], length: int) -> float:
    """Wilder RMA of true range — the spec's ATR1(14,RMA), NOT the SMA the
    M5 producers use: the 28-cell campaign measured H3 with RMA smoothing."""
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


def _ts_hour_utc(ts: str) -> int:
    """UTC hour of a bar ts label ('2026-08-05T13:07:00Z' -> 13); -1 unparseable."""
    try:
        return int(str(ts)[11:13])
    except (TypeError, ValueError):
        return -1


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"h3fade_skip: {reason}"], features={},
    )


def decide_h3fade(symbol: str, m1_bars: list[Bar], spread_abs: float,
                  session: str = "unknown") -> Decision:
    """Streak-fade decision on the last CLOSED M1 bar (the pre-registered H3)."""
    atr_len = max(2, _env_int(ENV_ATR_LEN, 14))
    streak = max(2, _env_int(ENV_STREAK, 5))
    move_atr = _env_float(ENV_MOVE_ATR, 2.0)
    sl_atr = _env_float(ENV_SL_ATR, 2.0)
    tp_r = _env_float(ENV_TP_BACKSTOP_R, 3.0)
    hour_start = _env_int(ENV_HOUR_START, 13)
    hour_end = _env_int(ENV_HOUR_END, 16)

    last_ts = str(m1_bars[-1].get("ts") or "") if m1_bars else ""
    ts_close = _bar_close_ts(last_ts, bar_sec=M1_BAR_SEC) if last_ts else ""
    need = max(atr_len + 2, streak + 2)
    if len(m1_bars) < need:
        return _skip(ts_close, symbol, f"bars<{need}")

    hour = _ts_hour_utc(last_ts)
    if not (hour_start <= hour <= hour_end):
        return _skip(ts_close, symbol,
                     f"outside_hours ({hour:02d}Z not in {hour_start:02d}-{hour_end:02d}Z)")

    a = _atr_rma(m1_bars, atr_len)
    if a <= 0:
        return _skip(ts_close, symbol, "atr<=0")

    closes = [_f(b.get("close")) for b in m1_bars[-(streak + 1):]]
    deltas = [closes[i + 1] - closes[i] for i in range(streak)]
    if all(d > 0 for d in deltas):
        streak_dir = "up"
    elif all(d < 0 for d in deltas):
        streak_dir = "down"
    else:
        return _skip(ts_close, symbol, f"no_streak (need {streak} same-dir closes)")

    move = abs(closes[-1] - closes[0])
    if move < move_atr * a:
        return _skip(ts_close, symbol,
                     f"move_too_small ({move:.2f} < {move_atr}xATR1({a:.2f}))")

    side = "sell" if streak_dir == "up" else "buy"  # FADE the streak
    entry = closes[-1]
    risk = sl_atr * a
    if risk <= 0:
        return _skip(ts_close, symbol, "zero_risk")
    sl = entry + risk if side == "sell" else entry - risk
    tp = entry - tp_r * risk if side == "sell" else entry + tp_r * risk
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(entry, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=0.55, setup="h3fade_streak",
        reasons=[f"fade {streak_dir}-streak x{streak} move={move:.2f} "
                 f"({move / a:.1f}xATR1 {a:.2f}) hour={hour:02d}Z; "
                 "exits=m1_bank/4min/SL"],
        features={"h3_atr1": round(a, 4), "h3_move_atr": round(move / a, 2),
                  "h3_streak_dir": streak_dir, "h3_hour": hour},
        session=session,
    )


# ---------------------------------------------------------------------------
# M1-close bank / time exit — the pure half of the fast-tick exit branch
# (shadow_runner._run_h3fade_exit_tick owns the I/O + close call)
# ---------------------------------------------------------------------------


def _position_open_epoch(position: dict[str, Any]) -> float:
    """Open epoch of a broker position dict (0.0 when unknown) — same field
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
        from datetime import datetime, timezone

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        # broker payloads sometimes carry epoch millis
        val = _f(raw, 0.0)
        if val > 1e12:
            return val / 1000.0
        return val if val > 1e9 else 0.0


def h3fade_exit_decision(
    lane: list[dict[str, Any]],
    m1_bars: list[Bar],
    now_epoch: float,
) -> tuple[str, dict[str, Any]] | None:
    """('h3_bank'|'h3_time', info) when the lane must close NOW, else None.

    Exit priority per the pre-registered spec: (1) first M1 CLOSE with
    PnL >= +bank_r x R -> bank ("h3_bank" — MUST be close-based, touch-TP
    kills the edge); (2) position age >= time-stop -> "h3_time"; (3) broker
    SL is the disaster case and never reaches this function. Pure — the
    caller owns the broker close. A bank is only ever taken off an M1 bar
    that CLOSED AFTER the position opened (a stale pre-entry bar can sit
    past the threshold while live PnL is ~0 — closing on it would bank
    nothing real).
    """
    if not lane:
        return None
    bank_r = _env_float(ENV_BANK_R, 0.5)
    time_stop_sec = max(0.0, _env_float(ENV_TIME_STOP_SEC, 240.0))

    oldest_open = 0.0
    for pos in lane:
        opened = _position_open_epoch(pos)
        if opened > 0 and (oldest_open <= 0 or opened < oldest_open):
            oldest_open = opened

    # (1) M1-close bank — every position with real geometry must clear bank_r.
    if m1_bars:
        last = m1_bars[-1]
        last_close = _f(last.get("close"), 0.0)
        bar_close_epoch = 0.0
        try:
            from datetime import datetime, timezone

            bar_close_epoch = datetime.fromisoformat(
                str(last.get("ts") or "").replace("Z", "+00:00")
            ).timestamp() + M1_BAR_SEC
        except (TypeError, ValueError):
            bar_close_epoch = 0.0
        closed_after_entry = oldest_open > 0 and bar_close_epoch > oldest_open
        if last_close > 0 and closed_after_entry:
            r_values: list[float] = []
            for pos in lane:
                entry = _f(pos.get("entryPrice") or pos.get("entry_price"), 0.0)
                sl = _f(pos.get("stopLoss") or pos.get("stop_loss"), 0.0)
                side = str(pos.get("tradeSide") or pos.get("side") or "").lower()
                risk = abs(entry - sl)
                # require a REAL stop (missing stopLoss -> sl 0.0 -> risk =
                # the gold price and r ~ 0 forever — the 2026-07-26 OM bank
                # audit bug; same guard here)
                if entry <= 0 or sl <= 0 or risk <= 0:
                    r_values = []
                    break
                r = ((last_close - entry) / risk if side.startswith("buy")
                     else (entry - last_close) / risk)
                r_values.append(r)
            if r_values and all(r >= bank_r for r in r_values):
                return "h3_bank", {"r_close": round(min(r_values), 4),
                                   "bank_r": bank_r,
                                   "bar_ts": str(last.get("ts") or "")}

    # (2) time stop
    if oldest_open > 0 and time_stop_sec > 0 and (now_epoch - oldest_open) >= time_stop_sec:
        return "h3_time", {"age_sec": round(now_epoch - oldest_open, 1),
                           "time_stop_sec": time_stop_sec}
    return None

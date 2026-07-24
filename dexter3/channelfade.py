"""CHANNELFADE producer — the RANGE phase of the day grammar (owner
2026-07-16/17: "ตลาด sideway ใน H1 DZ มี ch ชัดเจน เราไม่ควรเข้ากลางทาง ควรเก็บ
Sell/Buy ขอบ ch บนล่าง"; re-confirmed 2026-07-24 after an all-red chop day when
fable SOLD the range lows / BOUGHT the range highs — the exact inverse of this
edge).

WHAT IT IS: fade the EDGES of a detected sideways channel back toward the
middle — the one regime no live lane covers. Every check is closed-bar only,
pre-registered, NO mid-channel entries:
  * channel over the trailing ``window`` M5 bars: height H in
    [min_h_atr, max_h_atr] x ATR (too tight = spread noise, too wide =
    trending), directional efficiency |drift|/H <= max_eff (a real box, not a
    drift), and >= min_touches bar-touches of EACH edge band;
  * the newest bar must TOUCH an edge band AND close back INSIDE in the fade
    direction (reversal close — same M5-close confirm convention as every
    other dexter3 producer; the live lane may add M1 ENTRY_CONFIRM);
  * SL beyond the faded edge + sl_buf_atr x ATR;
  * TP = the VOLUME-PROFILE POC of the box when it sits on the fade side
    (graft the only lane that is live-positive — vp +23.58 since surgery, its
    poc_reversion is the proven mean-reversion target), else the geometric
    half-box mid (tp_frac) as the fallback. POC-target is env-gated ON.

EXIT: the replay-confirmed exit for a fade is PLAIN (broker SL/TP + basket
caps only; DEXTER3_OM_TRAIL_MODE=plain). convex/ladder measurably DESTROY it
(a fade has a fixed target and must NOT ride past it) — see
scripts/dexter3_entry_position_replay.py --producer channelfade.

VALIDATION STANCE (owner 2026-07-24): replay UNDER-RATES range edges (vp is
replay-marginal yet live +23.58), so replay is NOT the promotion gate here.
This lane exists to gather the honest arbiter — FORWARD realized PnL as a
canary — exactly like dexter3-dpull-cs. Off-by-default; only DEXTER3_MODE=
channelfade (or DEXTER3_PRODUCER=channelfade) activates it, so the fable/other
lanes are byte-identical when the CHF envs are absent (additive-only rule).

Every function is PURE (env readers aside). The replay prototype
``scripts/dexter3_entry_position_replay.decide_channelfade`` is the behavioral
ancestor; tests/test_dexter3_channelfade.py pins this module.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts
from dexter3.volume_profile import build_profile

Bar = dict[str, Any]

# Broker label for the CHANNELFADE canary lane — the peer-isolation contract
# (loops never touch foreign-labelled positions). shadow_runner forces
# executor.LABEL to this when DEXTER3_MODE=channelfade.
CHF_LABEL = "dexter3:chf:canary"
CHF_LABEL_FAMILY = "dexter3:chf"

# A mean-reversion fade wins on HIT-RATE, not payoff geometry (replay WR
# 64-76% at a half-box target) — the hunt/vp 1.2 RR floor is the WRONG gate
# here and rejects the exact fades the replay proved profitable. Own floor,
# env-tunable, default 1.0 (break-even geometry; the win-rate is the edge).
DEFAULT_MIN_RR = 1.0

# -- env knobs (single source of truth; no hardcoded magic in the body) ------
ENV_MIN_RR = "DEXTER3_CHF_MIN_RR"              # reward:risk floor (default 1.0)
ENV_WINDOW = "DEXTER3_CHF_WINDOW"              # trailing M5 bars defining the box
ENV_MIN_H_ATR = "DEXTER3_CHF_MIN_H_ATR"        # min box height in ATR
ENV_MAX_H_ATR = "DEXTER3_CHF_MAX_H_ATR"        # max box height in ATR
ENV_MAX_EFF = "DEXTER3_CHF_MAX_EFF"            # max directional efficiency |drift|/H
ENV_EDGE_FRAC = "DEXTER3_CHF_EDGE_FRAC"        # edge band width as fraction of H
ENV_MIN_TOUCHES = "DEXTER3_CHF_MIN_TOUCHES"    # min bar-touches of EACH edge
ENV_SL_BUF_ATR = "DEXTER3_CHF_SL_BUF_ATR"      # SL buffer beyond the edge, in ATR
ENV_MAX_RISK_ATR = "DEXTER3_CHF_MAX_RISK_ATR"  # reject entries whose risk > this x ATR
ENV_TP_FRAC = "DEXTER3_CHF_TP_FRAC"            # geometric TP as fraction of H from the edge
ENV_POC_TARGET = "DEXTER3_CHF_POC_TARGET"      # "1" -> use vp POC as the fade target


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


def channelfade_mode_enabled() -> bool:
    """Same trigger set as shadow_runner._channelfade_producer_enabled — kept
    in sync by tests, not imports (no circular dependency on the runner)."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "channelfade":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "channelfade"


def mean_true_range(bars: list[Bar]) -> float:
    """Mean true range over the supplied bars (live ATR proxy; the replay used
    the whole-series mean TR). Returns 0.0 when < 2 bars so callers skip."""
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = _f(bars[0].get("close"))
    for b in bars[1:]:
        hi, lo = _f(b.get("high")), _f(b.get("low"))
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        prev_close = _f(b.get("close"))
    return (sum(trs) / len(trs)) if trs else 0.0


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"chf_skip: {reason}"], features={},
    )


def _enter(ts_close: str, symbol: str, side: str, entry: float, sl: float,
           tp: float, session: str, reasons: list[str],
           features: dict[str, Any]) -> Decision | None:
    """Same RR + geometry guard as volume_profile._enter — a fade with a bad
    reward:risk or an inverted stop/target is no trade (returns None so the
    caller reports a clean skip)."""
    min_rr = _env_float(ENV_MIN_RR, DEFAULT_MIN_RR)
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or (reward / risk) < min_rr:
        return None
    if side == "buy" and not (sl < entry < tp):
        return None
    if side == "sell" and not (tp < entry < sl):
        return None
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(entry, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=0.45, setup="channelfade_edge_fade",
        reasons=reasons + [f"rr={reward / max(risk, 1e-9):.2f}>={min_rr}"],
        features=features, session=session,
    )


def _poc_target(box: list[Bar], side: str, entry: float,
                geo_mid_tp: float) -> tuple[float, str]:
    """The fade target: the volume-profile POC of the box when it sits on the
    fade side of the entry (graft vp's proven poc_reversion), else the
    geometric half-box mid fallback. POC-target is env-gated (default ON);
    disable with DEXTER3_CHF_POC_TARGET=0 to reproduce the pure-geometry
    prototype."""
    if os.environ.get(ENV_POC_TARGET, "1").strip() != "1":
        return geo_mid_tp, "geo_mid"
    profile = build_profile(box)
    if profile is None:
        return geo_mid_tp, "geo_mid(no_profile)"
    poc = _f(profile.poc_price)
    # POC must be a real reversion target on the fade side (a SELL fades DOWN,
    # so a valid POC target is BELOW entry; BUY the inverse). Otherwise the
    # box is one-sided vol -> geometric mid is the honest half-box target.
    if side == "sell" and poc < entry:
        return poc, f"poc={poc:.2f}"
    if side == "buy" and poc > entry:
        return poc, f"poc={poc:.2f}"
    return geo_mid_tp, f"geo_mid(poc_wrong_side={poc:.2f})"


def decide_channelfade(symbol: str, m5_bars: list[Bar], spread_abs: float,
                       session: str = "unknown") -> Decision:
    """Channel-edge fade decision on the last CLOSED bar of ``m5_bars``.

    Returns an ``enter`` Decision (market entry at the reversal close, SL
    beyond the faded edge, TP at the POC/mid) or a ``skip`` Decision. Never
    raises on malformed bars/missing volume (degrades to skip) — a range lane
    must fail closed, never blind-enter."""
    window = max(4, _env_int(ENV_WINDOW, 36))
    min_h_atr = _env_float(ENV_MIN_H_ATR, 1.5)
    max_h_atr = _env_float(ENV_MAX_H_ATR, 4.0)
    max_eff = _env_float(ENV_MAX_EFF, 0.35)
    edge_frac = _env_float(ENV_EDGE_FRAC, 0.2)
    min_touches = max(1, _env_int(ENV_MIN_TOUCHES, 2))
    sl_buf_atr = _env_float(ENV_SL_BUF_ATR, 0.35)
    max_risk_atr = _env_float(ENV_MAX_RISK_ATR, 2.0)
    tp_frac = _env_float(ENV_TP_FRAC, 0.5)

    ts_close = _bar_close_ts(str(m5_bars[-1].get("ts") or "")) if m5_bars else ""
    if len(m5_bars) < window + 2:
        return _skip(ts_close, symbol, f"bars<{window + 2}")
    atr = mean_true_range(m5_bars)
    if atr <= 0:
        return _skip(ts_close, symbol, "atr<=0")

    box = m5_bars[-window:]
    highs = [_f(b.get("high")) for b in box]
    lows = [_f(b.get("low")) for b in box]
    closes = [_f(b.get("close")) for b in box]
    ch_hi, ch_lo = max(highs), min(lows)
    height = ch_hi - ch_lo
    if not (min_h_atr * atr <= height <= max_h_atr * atr):
        return _skip(ts_close, symbol, f"height {height:.2f} outside [{min_h_atr}-{max_h_atr}]xATR")
    if height <= 0:
        return _skip(ts_close, symbol, "flat_box")
    if abs(closes[-1] - closes[0]) / height > max_eff:
        return _skip(ts_close, symbol, f"efficiency>{max_eff} (trending, not a box)")
    band = edge_frac * height
    if sum(1 for h in highs if h >= ch_hi - band) < min_touches:
        return _skip(ts_close, symbol, f"top edge <{min_touches} touches")
    if sum(1 for lo in lows if lo <= ch_lo + band) < min_touches:
        return _skip(ts_close, symbol, f"bottom edge <{min_touches} touches")

    last = box[-1]
    o, c = _f(last.get("open")), _f(last.get("close"))
    hi, lo = _f(last.get("high")), _f(last.get("low"))
    mid = (ch_hi + ch_lo) / 2.0
    feat_base: dict[str, Any] = {
        "chf_ch_hi": round(ch_hi, 5), "chf_ch_lo": round(ch_lo, 5),
        "chf_height_atr": round(height / atr, 3), "chf_atr": round(atr, 4),
        "chf_efficiency": round(abs(closes[-1] - closes[0]) / height, 3),
    }

    # SELL the top edge: touched the top band, closed RED back inside, and the
    # close is still in the upper half (never a mid-channel entry).
    if hi >= ch_hi - band and c < o and c > mid:
        sl = ch_hi + sl_buf_atr * atr
        risk = sl - c
        if 0 < risk <= max_risk_atr * atr:
            geo_tp = ch_hi - tp_frac * height
            tp, tp_src = _poc_target(box, "sell", c, geo_tp)
            d = _enter(ts_close, symbol, "sell", c, sl, tp, session,
                       [f"fade top edge {ch_hi:.2f} (touch {hi:.2f}, close {c:.2f}), tp {tp_src}"],
                       {**feat_base, "chf_side": "sell", "chf_tp_src": tp_src})
            if d:
                return d
    # BUY the bottom edge: mirror.
    if lo <= ch_lo + band and c > o and c < mid:
        sl = ch_lo - sl_buf_atr * atr
        risk = c - sl
        if 0 < risk <= max_risk_atr * atr:
            geo_tp = ch_lo + tp_frac * height
            tp, tp_src = _poc_target(box, "buy", c, geo_tp)
            d = _enter(ts_close, symbol, "buy", c, sl, tp, session,
                       [f"fade bottom edge {ch_lo:.2f} (touch {lo:.2f}, close {c:.2f}), tp {tp_src}"],
                       {**feat_base, "chf_side": "buy", "chf_tp_src": tp_src})
            if d:
                return d

    return _skip(ts_close, symbol, "no_edge_reversal")

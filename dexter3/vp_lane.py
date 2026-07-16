"""VP live-lane primitives — owner deploy order 2026-07-16 ("deploy เลย").

The deployed variant is the 3-window replay survivor with the largest sample
(scripts/dexter3_entry_position_replay.py, VM /tmp/entry_pos_vp_*.log):

    VP producer x day-open bias (00Z anchor, >=1h) x LIMIT entry at
    entry - 0.4R (SL unchanged at the structural level) x CONVEX trail
    exit (arms at peak 1.0R, giveback 3.0 x ATR, max hold 240min), no TP
    (a far protective TP satisfies executor geometry only).

Every function here is PURE (env readers aside) and mirrors the replay
semantics exactly — the replay tests in
tests/test_dexter3_entry_position_replay.py are the behavioral spec, and
tests/test_dexter3_vp_lane.py pins this module against the same arithmetic.
shadow_runner.py and opening_manager.py call through tiny env-gated hooks so
the fable/grok lanes are byte-identical when the VP envs are absent
(additive-only rule).

LIMIT entry is SYNTHETIC (owner-visible design decision): the lane stores an
intent and fires a MARKET order on the first fast tick (8s) whose touch-side
quote reaches the level, instead of resting a broker limit. Rationale: the
daemon supports LIMIT orders but has no cancel/expiry lifecycle wired into
the lane (pending-order reconciliation would be a whole new failure class);
the 8s poll costs at most one tick of slippage on a demo canary and the
fill-vs-level delta is journaled on every fill so the cost of the synthetic
is MEASURED, not assumed. Upgrade path to native limits only if that delta
proves material.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

# -- env keys (single source of truth) ---------------------------------------
ENV_BIAS_ENABLED = "DEXTER3_VP_DAYOPEN_BIAS"           # "1" -> gate active
ENV_BIAS_ANCHOR_HOUR = "DEXTER3_VP_BIAS_ANCHOR_HOUR"   # UTC hour, default 0
ENV_BIAS_MIN_HOURS = "DEXTER3_VP_BIAS_MIN_HOURS"       # default 1.0
ENV_NO_TRADE_UTC = "DEXTER3_VP_NO_TRADE_UTC"           # "HH:MM-HH:MM" or ""
ENV_LIMIT_DIP_R = "DEXTER3_VP_LIMIT_DIP_R"             # 0 -> market (off)
ENV_LIMIT_TTL_MIN = "DEXTER3_VP_LIMIT_TTL_MIN"         # default 30
ENV_FAR_TP_R = "DEXTER3_VP_FAR_TP_R"                   # default 12.0
ENV_TRAIL_MODE = "DEXTER3_OM_TRAIL_MODE"               # "convex" -> new exit
ENV_CONVEX_ARM_R = "DEXTER3_OM_CONVEX_ARM_R"           # default 1.0
ENV_CONVEX_GIVEBACK_ATR = "DEXTER3_OM_CONVEX_GIVEBACK_ATR"  # default 3.0
ENV_CONVEX_MAX_AGE_MIN = "DEXTER3_OM_CONVEX_MAX_AGE_MIN"    # default 240
ENV_CONVEX_ATR_PTS_DEFAULT = "DEXTER3_OM_CONVEX_ATR_PTS_DEFAULT"  # default 5.0

ATR_WINDOW_BARS = 288   # ~1 trading day of M5 — live counterpart of the
                        # replay's whole-series mean TR (~4.96 on the proof
                        # windows); journaled per intent so drift is visible.


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    return _f(os.environ.get(key), default)


def _epoch(ts: str) -> float:
    t = (ts or "").strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return 0.0


def vp_mode_enabled() -> bool:
    """Same trigger set as shadow_runner._vp_producer_enabled — kept in sync
    by tests, not imports (no circular dependency on the 4k-line runner)."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "vp":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "vp"


# ---------------------------------------------------------------------------
# Day-open bias (ยืนเปิด/ต่ำเปิด) — decision-time facts only, no lookahead.
# Mirrors scripts/dexter3_entry_position_replay._anchor_bias_fields for a
# single anchor evaluated on the newest closed bar.
# ---------------------------------------------------------------------------


def dayopen_bias(m5_prefix: list, anchor_hour: int, now_epoch: float | None = None
                 ) -> tuple[int, float]:
    """(bias, hours_since_anchor) for the NEWEST closed bar of ``m5_prefix``.
    bias = sign(last close - day open), where the day open is the OPEN of the
    first bar at/after the most recent daily ``anchor_hour``:00 UTC. bias 0
    when the prefix has no bar in the current anchor period."""
    if not m5_prefix:
        return 0, 0.0
    last = m5_prefix[-1]
    e_last = _epoch(str(last.get("ts") or ""))
    if e_last <= 0:
        return 0, 0.0
    anchor = ((e_last - anchor_hour * 3600) // 86400) * 86400 + anchor_hour * 3600
    open_px: float | None = None
    for b in m5_prefix:                       # first bar at/after the anchor
        if _epoch(str(b.get("ts") or "")) >= anchor:
            open_px = _f(b.get("open"))
            break
    if open_px is None or open_px <= 0:
        return 0, 0.0
    ref_epoch = now_epoch if now_epoch is not None else e_last
    hours = max(0.0, (ref_epoch - anchor) / 3600.0)
    diff = _f(last.get("close")) - open_px
    return (1 if diff > 0 else (-1 if diff < 0 else 0)), hours


def dayopen_bias_allows(side: str, m5_prefix: list) -> tuple[bool, dict[str, Any]]:
    """ต่ำเปิด -> sells only; ยืนเปิด -> buys only; bias younger than the
    min-hours gate -> neutral (both sides allowed). Env-gated: allow-all
    unless DEXTER3_VP_DAYOPEN_BIAS=1."""
    if os.environ.get(ENV_BIAS_ENABLED, "0").strip() != "1":
        return True, {"bias_gate": "off"}
    anchor_hour = int(_env_float(ENV_BIAS_ANCHOR_HOUR, 0))
    min_hours = _env_float(ENV_BIAS_MIN_HOURS, 1.0)
    bias, hours = dayopen_bias(m5_prefix, anchor_hour)
    info = {"bias": bias, "hours": round(hours, 2), "anchor_hour": anchor_hour,
            "min_hours": min_hours}
    if hours < min_hours or bias == 0:
        return True, {**info, "verdict": "neutral"}
    allowed = (side == "buy") == (bias > 0)
    return allowed, {**info, "verdict": "allow" if allowed else "blocked"}


# ---------------------------------------------------------------------------
# No-trade window (owner 2026-07-16: "ก่อนตลาดปิด และหลังตลาดเปิด ตลาดแกว่ง
# ไม่ควรเปิดเทรด"). Default window in the unit covers the XAU daily
# close/reopen (20:45-22:15Z) where the replay never traded anyway (no bars
# exist inside the break) and live spreads blow out around it.
# ---------------------------------------------------------------------------


def in_no_trade_window(now_iso: str, window: str | None = None) -> bool:
    spec = (window if window is not None else os.environ.get(ENV_NO_TRADE_UTC, "")).strip()
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return False
    e = _epoch(now_iso)
    if e <= 0:
        return False
    minute_of_day = int((e % 86400) // 60)
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= minute_of_day <= end
    return minute_of_day >= start or minute_of_day <= end   # crosses midnight


def vp_entry_gate(side: str, m5_prefix: list, now_iso: str) -> dict[str, Any]:
    """Combined VP-lane entry gate, same result shape as the v16 gate so the
    runner's existing blocked-status plumbing handles it unchanged."""
    if in_no_trade_window(now_iso):
        return {"allow": False, "reason": "vp_no_trade_window", "features": {}}
    allowed, info = dayopen_bias_allows(side, m5_prefix)
    if not allowed:
        return {"allow": False, "reason": "vp_dayopen_bias", "features": info}
    return {"allow": True, "reason": "vp_gate_pass", "features": info}


# ---------------------------------------------------------------------------
# Synthetic LIMIT intent lifecycle (JSON-plain dict — it persists in the
# runner's shadow state across restarts).
# ---------------------------------------------------------------------------


def limit_entry_enabled() -> bool:
    return _env_float(ENV_LIMIT_DIP_R, 0.0) > 0.0


def mean_true_range(m5_prefix: list, window: int = ATR_WINDOW_BARS) -> float:
    seg = m5_prefix[-(window + 1):]
    if len(seg) < 2:
        return _env_float(ENV_CONVEX_ATR_PTS_DEFAULT, 5.0)
    trs: list[float] = []
    prev_close = _f(seg[0].get("close"))
    for b in seg[1:]:
        hi, lo = _f(b.get("high")), _f(b.get("low"))
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        prev_close = _f(b.get("close"))
    return (sum(trs) / len(trs)) if trs else _env_float(ENV_CONVEX_ATR_PTS_DEFAULT, 5.0)


def make_limit_intent(decision: Any, m5_prefix: list, risk_usd: float,
                      now_iso: str) -> dict[str, Any]:
    """Build the pending-intent record from an ALLOWED VP enter decision.
    Level = entry -/+ dip_r * risk (risk = |entry - sl|); SL stays at the
    decision's structural level; TP is a far protective cap at
    ``far_tp_r`` x the FILLED stop distance from the level (executor geometry
    requires a TP; the convex trail is the real exit)."""
    side = str(decision.side)
    entry = _f(decision.entry)
    sl = _f(decision.sl)
    risk_pts = abs(entry - sl)
    dip_r = _env_float(ENV_LIMIT_DIP_R, 0.4)
    ttl_min = _env_float(ENV_LIMIT_TTL_MIN, 30.0)
    far_tp_r = _env_float(ENV_FAR_TP_R, 12.0)
    level = entry - dip_r * risk_pts if side == "buy" else entry + dip_r * risk_pts
    stop_pts = abs(level - sl)
    tp = level + far_tp_r * stop_pts if side == "buy" else level - far_tp_r * stop_pts
    now_e = _epoch(now_iso)
    return {
        "symbol": str(decision.symbol),
        "side": side,
        "level": round(level, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "signal_entry": round(entry, 5),
        "signal_ts": str(decision.ts_close or now_iso),
        "setup": str(getattr(decision, "setup", "") or "vp"),
        "session": str(getattr(decision, "session", "") or ""),
        "risk_usd": _f(risk_usd),
        "dip_r": dip_r,
        "created_epoch": now_e,
        "deadline_epoch": now_e + ttl_min * 60.0,
        "atr_pts": round(mean_true_range(m5_prefix), 4),
        "stop_pts": round(stop_pts, 5),
    }


def check_intent_fill(intent: dict[str, Any], bid: float, ask: float,
                      now_iso: str) -> str | None:
    """"fill" when the touch-side quote reaches the level (buy fills when the
    ASK trades at/below it — what a real buy limit needs), "expired" past the
    TTL deadline, else None (keep waiting)."""
    now_e = _epoch(now_iso)
    if now_e > _f(intent.get("deadline_epoch")):
        return "expired"
    level = _f(intent.get("level"))
    if str(intent.get("side")) == "buy":
        return "fill" if (ask > 0 and ask <= level) else None
    return "fill" if (bid > 0 and bid >= level) else None


def intent_to_decision(intent: dict[str, Any], decision_cls: Any, now_iso: str) -> Any:
    """Materialize the executor-facing Decision at FILL time: entry = the
    level (the executor computes stop pips from entry-sl, so the placed
    SL/TP land at the intent's absolute prices within one quote of drift)."""
    return decision_cls(
        ts_close=now_iso,
        symbol=str(intent.get("symbol")),
        action="enter",
        side=str(intent.get("side")),
        entry_type="market",
        entry=_f(intent.get("level")),
        sl=_f(intent.get("sl")),
        tp=_f(intent.get("tp")),
        size_class="small",
        leader_score=0.0,
        p_win_est=0.0,
        setup=str(intent.get("setup") or "vp"),
        reasons=[f"vp_limit_fill level={intent.get('level')} signal_entry={intent.get('signal_entry')}"],
        session=str(intent.get("session") or ""),
        features={"vp_limit_intent": {k: intent.get(k) for k in (
            "level", "signal_entry", "signal_ts", "dip_r", "atr_pts", "stop_pts",
            "created_epoch", "deadline_epoch")}},
    )


# ---------------------------------------------------------------------------
# Convex trail exit (the 3-window proof's exit: arm 1.0R, giveback 3.0xATR,
# max hold 240min). Replaces hard-take/ladder/stall for lanes that opt in via
# DEXTER3_OM_TRAIL_MODE=convex; the fable/grok ladder path is untouched.
# ---------------------------------------------------------------------------


def convex_trail_enabled() -> bool:
    return os.environ.get(ENV_TRAIL_MODE, "").strip().lower() == "convex"


def convex_floor_r(peak_r: float, atr_pts: float, stop_pts: float) -> float | None:
    """None until ``peak_r`` reaches the arm threshold; then a single
    continuous line ``peak_r - (giveback_atr * atr_pts / stop_pts)``. The
    floor MAY be negative by design (unlike the ladder's >=0 guarantee) —
    below the broker SL it simply never fires, which is exactly the replay's
    SL-first semantics."""
    arm_r = _env_float(ENV_CONVEX_ARM_R, 1.0)
    if peak_r < arm_r:
        return None
    if stop_pts <= 0:
        return None
    atr = atr_pts if atr_pts > 0 else _env_float(ENV_CONVEX_ATR_PTS_DEFAULT, 5.0)
    giveback_r = _env_float(ENV_CONVEX_GIVEBACK_ATR, 3.0) * atr / stop_pts
    return peak_r - giveback_r


def convex_age_exceeded(oldest_open_ts: str | None, now_iso: str | None) -> bool:
    max_age_min = _env_float(ENV_CONVEX_MAX_AGE_MIN, 240.0)
    if max_age_min <= 0 or not oldest_open_ts or not now_iso:
        return False
    e_open, e_now = _epoch(str(oldest_open_ts)), _epoch(str(now_iso))
    if e_open <= 0 or e_now <= 0:
        return False
    return (e_now - e_open) / 60.0 >= max_age_min

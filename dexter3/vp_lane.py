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
ENV_ENTRY_CONFIRM = "DEXTER3_ENTRY_CONFIRM"            # off|m1|m5: reversal-confirm entries
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
# dpull-cs vol-gated close-stop (2026-07-23 investigation): the initial hard
# stop is CLOSE-based (holds through intrabar noise wicks) unless the
# breaching M5 bar's range exceeds VOL_GATE x ATR (a crash bar -> cut now).
# The entry places the broker SL at BACKSTOP x soft-risk beyond the soft SL
# so a gap/spike is still bounded while the OM software owns the -1R stop.
ENV_CONVEX_CLOSE_STOP = "DEXTER3_OM_CONVEX_CLOSE_STOP"          # "1" -> enable
ENV_CONVEX_CLOSE_STOP_VOL_GATE = "DEXTER3_OM_CONVEX_CLOSE_STOP_VOL_GATE"  # ATR mult, 0=off
ENV_CONVEX_CLOSE_STOP_BACKSTOP = "DEXTER3_OM_CONVEX_CLOSE_STOP_BACKSTOP"  # broker-SL widen mult (default 2.5)
ENV_BANK_R = "DEXTER3_OM_BANK_R"                       # bank mode: close-based take (default 0.4)
ENV_CONFIRM_PREM_CAP = "DEXTER3_CONFIRM_PREM_CAP"     # reject confirms paying > cap x stop above the level (0=off)

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
                      now_iso: str, *, dip_r: float | None = None,
                      ttl_min: float | None = None,
                      far_tp_r: float | None = None,
                      prefer_signal_tp: bool = False) -> dict[str, Any]:
    """Build the pending-intent record from an ALLOWED enter decision.
    Level = entry -/+ dip_r * risk (risk = |entry - sl|); SL stays at the
    decision's structural level; TP is a far protective cap at
    ``far_tp_r`` x the FILLED stop distance from the level (executor geometry
    requires a TP). Producer-agnostic (owner 2026-07-16: the same trough-limit
    layer is two-window proven on the HUNT producer too): the keyword
    overrides let a hunt lane pass its own env-derived knobs while the VP
    lane keeps reading the VP envs by default. For a hunt lane the signal's
    ORIGINAL tp is kept when it is farther than the protective cap would be
    (hunt TPs are structural; never bring a TP closer)."""
    side = str(decision.side)
    entry = _f(decision.entry)
    sl = _f(decision.sl)
    risk_pts = abs(entry - sl)
    dip_r = _env_float(ENV_LIMIT_DIP_R, 0.4) if dip_r is None else float(dip_r)
    ttl_min = _env_float(ENV_LIMIT_TTL_MIN, 30.0) if ttl_min is None else float(ttl_min)
    far_tp_r = _env_float(ENV_FAR_TP_R, 12.0) if far_tp_r is None else float(far_tp_r)
    level = entry - dip_r * risk_pts if side == "buy" else entry + dip_r * risk_pts
    stop_pts = abs(level - sl)
    signal_tp = _f(getattr(decision, "tp", None), 0.0)
    tp_valid = (signal_tp > level) if side == "buy" else (0.0 < signal_tp < level)
    if prefer_signal_tp and tp_valid:
        # hunt lanes keep their structural TP unchanged — the limit layer
        # moves only the ENTRY; exits (ladder + broker TP) stay exactly the
        # live config the two-window proof measured against.
        tp = signal_tp
    else:
        # VP lane: far protective cap only (executor geometry needs a TP;
        # the convex trail is the real exit and must be free to ride).
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
        # replay parity (caught live 2026-07-16 16:10Z on the VP lane's first
        # fill): the confirm state machine must only see bars CLOSED AFTER
        # the intent exists — the replay's zone entry walks future bars only.
        # Seeding confirm_last_ts with the signal close ts makes
        # advance_confirm_intent skip every earlier bar.
        "confirm_last_ts": str(decision.ts_close or now_iso),
    }


def confirm_mode() -> str | None:
    """off (None) | "m1" | "m5" — owner order 2026-07-16 ("กรองต่อ M5 M1
    เบรคและกลับตัวเท่านั้น ไม่รับมีด"): when set, a pending intent NEVER fills
    on a bare touch (the blind limit catches every falling knife by
    construction); it fills only on a CONFIRMED reversal close on the
    confirm timeframe, and a close beyond the structural SL KILLS the setup
    with no trade at all. Live twin of the replay's zone-confirm /
    zone-M1 entries (3-window survivors on the VP producer)."""
    raw = os.environ.get(ENV_ENTRY_CONFIRM, "").strip().lower()
    return raw if raw in ("m1", "m5") else None


def advance_confirm_intent(intent: dict[str, Any], closed_bars: list
                           ) -> tuple[str | None, float | None]:
    """Drive one pending intent through the zone-confirm state machine using
    newly CLOSED confirm-TF bars (ascending). Bar-for-bar identical to the
    replay's ``_zone_confirm_entry`` / ``_zone_confirm_entry_m1``:
      * touch: bar range reaches the level;
      * REAL-BREAK KILL: a bar CLOSE beyond the structural SL -> ("killed",
        None) — the knife is refused outright; a WICK beyond the SL without
        such a close is survived (no position exists yet);
      * REVERSAL FILL: at/after the touch, the first bar closing back in the
        entry direction beyond the level while the discount vs the signal
        entry is still intact -> ("fill", that close).
    Mutates ``intent`` (confirm_touched / confirm_last_ts) so restarts and
    repeated ticks never re-process a bar. Returns (None, None) to keep
    waiting."""
    side = str(intent.get("side"))
    level = _f(intent.get("level"))
    sl = _f(intent.get("sl"))
    signal_entry = _f(intent.get("signal_entry"))
    touched = bool(intent.get("confirm_touched"))
    last_ts = str(intent.get("confirm_last_ts") or "")
    for b in closed_bars:
        ts = str(b.get("ts") or "")
        if not ts or ts <= last_ts:
            continue
        intent["confirm_last_ts"] = ts
        o = _f(b.get("open"))
        c = _f(b.get("close"))
        hi = _f(b.get("high"))
        lo = _f(b.get("low"))
        prem_cap = _env_float(ENV_CONFIRM_PREM_CAP, 0.0)
        stop_pts = abs(level - sl)
        if side == "buy":
            if c <= sl:
                return "killed", None
            if not touched and lo <= level:
                touched = True
                intent["confirm_touched"] = True
            if touched and c > o and c > level and c < signal_entry:
                if prem_cap > 0 and (c - level) > prem_cap * stop_pts:
                    continue   # confirm too far above the zone (2026-07-17
                               # surgery: buys paid 8-45% of the stop in
                               # premium; cap improved 5/6 replay cells,
                               # net -40.4R -> -10.7R) -- wait for a closer bar
                return "fill", c
        else:
            if c >= sl:
                return "killed", None
            if not touched and hi >= level:
                touched = True
                intent["confirm_touched"] = True
            if touched and c < o and c < level and c > signal_entry:
                if prem_cap > 0 and (level - c) > prem_cap * stop_pts:
                    continue
                return "fill", c
    return None, None


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


def intent_to_decision(intent: dict[str, Any], decision_cls: Any, now_iso: str,
                       entry_px: float | None = None) -> Any:
    """Materialize the executor-facing Decision at FILL time: entry = the
    level (touch fills) or the CONFIRM close (reversal-confirmed fills, via
    ``entry_px``) — the executor computes stop pips from entry-sl, so the
    placed SL/TP land at the filled geometry within one quote of drift."""
    return decision_cls(
        ts_close=now_iso,
        symbol=str(intent.get("symbol")),
        action="enter",
        side=str(intent.get("side")),
        entry_type="market",
        entry=_f(entry_px) if entry_px is not None else _f(intent.get("level")),
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


def trail_mode() -> str | None:
    """None (classic ladder) | "convex" (VP lane) | "plain" (daytrend lane —
    broker SL/TP + basket caps ONLY; the OM must not profit-exit at all:
    the daytrend proof's exit is plain TP at the day extreme, and both the
    ladder and convex measurably destroy it)."""
    raw = os.environ.get(ENV_TRAIL_MODE, "").strip().lower()
    return raw if raw in ("convex", "plain", "bank") else None


def convex_trail_enabled() -> bool:
    return trail_mode() == "convex"


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


def convex_close_stop_enabled() -> bool:
    return os.environ.get(ENV_CONVEX_CLOSE_STOP, "0").strip() == "1"


def convex_close_stop_vol_gate() -> float:
    return _env_float(ENV_CONVEX_CLOSE_STOP_VOL_GATE, 0.0)


def convex_close_stop_backstop_mult() -> float:
    return _env_float(ENV_CONVEX_CLOSE_STOP_BACKSTOP, 2.5)


def convex_close_stop_hit(side: str, entry: float, stop_pts: float, atr_pts: float,
                          last_bar: dict) -> bool:
    """dpull-cs vol-gated close-based hard stop, evaluated on the LATEST CLOSED
    M5 bar (mirror of scripts/dexter3_convex_exit_replay._simulate_convex's
    close_stop + vol_gate legs). The soft SL is ``entry ∓ stop_pts`` (the
    structural -1R level; the broker SL sits further out as a backstop):
      * a VOLATILE bar (range > vol_gate x ATR) that BREACHES the soft SL
        intrabar -> cut now (crash bar, matches the replay's intrabar -1R cut);
      * else a bar that CLOSES beyond the soft SL -> cut now (close-stop);
      * else (a calm bar that only WICKED past the soft SL and closed back) ->
        hold (the wick-immunity that captured the ranging runners).
    Pure: no I/O, no env reads beyond the two gate getters passed as values by
    the caller."""
    hi = _f(last_bar.get("high"), 0.0)
    lo = _f(last_bar.get("low"), 0.0)
    cl = _f(last_bar.get("close"), 0.0)
    if stop_pts <= 0 or entry <= 0 or cl <= 0:
        return False
    vg = convex_close_stop_vol_gate()
    volatile = vg > 0.0 and atr_pts > 0.0 and (hi - lo) > vg * atr_pts
    if str(side).lower().startswith("buy"):
        soft_sl = entry - stop_pts
        return (volatile and lo <= soft_sl) or (cl <= soft_sl)
    soft_sl = entry + stop_pts
    return (volatile and hi >= soft_sl) or (cl >= soft_sl)


def convex_age_exceeded(oldest_open_ts: str | None, now_iso: str | None) -> bool:
    max_age_min = _env_float(ENV_CONVEX_MAX_AGE_MIN, 240.0)
    if max_age_min <= 0 or not oldest_open_ts or not now_iso:
        return False
    e_open, e_now = _epoch(str(oldest_open_ts)), _epoch(str(now_iso))
    if e_open <= 0 or e_now <= 0:
        return False
    return (e_now - e_open) / 60.0 >= max_age_min

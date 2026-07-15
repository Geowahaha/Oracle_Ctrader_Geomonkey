"""Dexter3 basket LIVE glue — real broker position snapshots -> basket decisions.

``dexter3.basket_manager.BasketManager`` is the pure state machine that
already enforces the hard caps in Phase-1 shadow/paper mode (see
``dexter3/shadow_runner.py::PaperBasket``). This module is the REAL-position
counterpart: it takes whatever ``get_positions()`` returns from the broker
(the shapes documented in ``dexter3/executor.py``'s ``position_*_of()``
helpers — ``dexter3.executor`` is intentionally NOT imported here to avoid
any accidental live/mutating coupling; the field-parsing logic is duplicated
on purpose, same "additive-only, no cross-imports of live internals" posture
``market_lens.py`` documents for itself) and turns it into the same kind of
decision ``BasketManager`` would make, EXCEPT this module never mutates
anything — every function here is pure. The runner is expected to call
``decide_basket_action`` on every M5 close when lane positions exist, then
route the returned action to ``dexter3.executor`` (close/repair) itself.

HARD CAPS are enforced through exactly one choke-point function,
``enforce_caps``, mirroring ``basket_manager.BasketManager._enforce_caps``'s
philosophy: every action that could grow the basket (add a leg) or let it
keep living (time/daily-loss) must pass through it first. Cap NUMBERS are
imported from ``basket_manager.BasketConfig`` so they live in exactly one
place in the codebase (blueprint "HARD CAPS ... unit-tested", non-negotiable
#1 additive-only — no re-deriving cap values here).
"""
from __future__ import annotations

from typing import Any

from dexter3.basket_manager import (
    ACTION_CLOSE_ALL_CAP_STOP,
    ACTION_CLOSE_ALL_IN_PROFIT,
    ACTION_NONE,
    BasketConfig,
)

Position = dict[str, Any]

# FAMILY root (2026-07-15 versioned-labels design), not a full versioned
# label — callers pass a family (e.g. dexter3.executor.LABEL_FAMILY /
# shadow_runner._active_label_family()) so this filter spans every past AND
# future version of a lane's label; lane_positions' own `.startswith()` below
# already implements the family-prefix match, unchanged.
DEFAULT_LABEL_PREFIX = "dexter3:fable"

# repair-leg action tokens (mirrors basket_manager.ACTION_ADD_REPAIR_LEG /
# ACTION_HEDGE_LOCK but expressed as the two distinct repair *sides* this
# module must choose between — see decide_basket_action's docstring).
REPAIR_MODE_SAME_SIDE = "same_side"
REPAIR_MODE_HEDGE_LOCK = "hedge_lock"


# ---------------------------------------------------------------------------
# small local helpers (pure; deliberately duplicated field parsing — see
# module docstring re: no cross-import of executor.py internals)
# ---------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_label(position: Position) -> str:
    return str(position.get("label") or position.get("comment") or "").strip()


def _position_symbol(position: Position) -> str:
    return str(position.get("symbolName") or position.get("symbol") or "").strip().upper()


def _position_side(position: Position) -> str:
    raw = str(position.get("tradeSide") or position.get("side") or "").strip().lower()
    if raw.startswith("buy"):
        return "buy"
    if raw.startswith("sell"):
        return "sell"
    return raw


def _position_volume(position: Position) -> float:
    for key in ("volumeInUnits", "volume", "volumeUnits"):
        try:
            value = float(position.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _position_entry(position: Position) -> float:
    return _f(position.get("entryPrice", position.get("price", 0.0)))


def _position_id(position: Position) -> int:
    try:
        return int(position.get("positionId") or position.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _position_open_ts(position: Position) -> str:
    # ``openTime`` is the field the live cTrader MCP actually returns
    # (e.g. "2026-07-07T19:11:30.337Z") — verified against get_positions on
    # 2026-07-08. It MUST be listed first: without it this returned "" for
    # every real position, which made aggregate_lane report
    # oldest_open_ts=None on every tick. That, in turn, (1) made the OM
    # peak-R ratchet reset to live_r every tick (the trail could never bank
    # a winner — a position that peaked +0.82R rode all the way back to a
    # full -1R stop) and (2) permanently disabled the time_stop_min cap
    # (basket age was always None). The remaining keys are kept for
    # forward/test compatibility with other envelopes.
    return str(
        position.get("openTime")
        or position.get("openTimestamp")
        or position.get("open_ts")
        or position.get("openedAt")
        or position.get("ts")
        or ""
    ).strip()


def _position_pnl(position: Position) -> float | None:
    """Read PnL with fallbacks; returns None (never raises) when unreadable."""
    for key in ("netProfit", "profit", "grossProfit", "pnl"):
        raw = position.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# lane_positions
# ---------------------------------------------------------------------------


def lane_positions(
    all_positions: list[Position] | None, label_prefix: str = DEFAULT_LABEL_PREFIX
) -> list[Position]:
    """Filter ``all_positions`` down to our Dexter3 lane.

    Robust to key variants: a position's label may live under ``label`` or
    ``comment`` (mirrors ``dexter3.executor.position_label_of`` /
    ``scripts/btc_scalp_monitor.py``'s own label-vs-comment fallback). Any
    position whose resolved label string does not start with
    ``label_prefix`` is excluded (peer isolation — foreign-label positions
    from the live scalp loops or other Dexter3 setups running a different
    version tag must never be swept into this basket's aggregate). Never
    raises on malformed entries; a position dict missing expected keys is
    simply excluded rather than crashing the caller's M5 loop.
    """
    if not all_positions:
        return []
    out: list[Position] = []
    for position in all_positions:
        if not isinstance(position, dict):
            continue
        try:
            label = _position_label(position)
        except Exception:  # noqa: BLE001 - never let one malformed row crash the lane scan
            continue
        if label.startswith(label_prefix):
            out.append(position)
    return out


# ---------------------------------------------------------------------------
# aggregate_lane
# ---------------------------------------------------------------------------


def aggregate_lane(positions: list[Position], base_risk_usd: float) -> dict[str, Any]:
    """Aggregate PnL/side/volume stats for a lane's open positions.

    Never raises on missing fields — a position with an unreadable PnL
    degrades the WHOLE aggregate to ``unreliable=True`` (never trade blind
    on a partial read: ``decide_basket_action`` treats ``unreliable`` as an
    unconditional ``hold``, see its docstring).
    """
    legs = len(positions)
    if legs == 0:
        return {
            "legs": 0,
            "aggregate_pnl_usd": 0.0,
            "aggregate_r": 0.0,
            "sides": {"buy": 0, "sell": 0},
            "volume_net": 0.0,
            "oldest_open_ts": None,
            "weighted_entry": None,
            "unreliable": False,
            "pnl_sources": {},
            "unreliable_pnl_position_ids": [],
        }

    sides = {"buy": 0, "sell": 0}
    volume_net = 0.0
    total_pnl = 0.0
    unreliable = False
    pnl_sources: dict[str, int] = {}
    unreliable_pnl_position_ids: list[int] = []
    open_ts_list: list[str] = []
    weighted_entry_num = 0.0
    weighted_entry_den = 0.0

    for position in positions:
        side = _position_side(position)
        if side in sides:
            sides[side] += 1
        volume = _position_volume(position)
        signed_volume = volume if side == "buy" else (-volume if side == "sell" else 0.0)
        volume_net += signed_volume

        pnl = _position_pnl(position)
        if pnl is None:
            unreliable = True
            try:
                unreliable_pnl_position_ids.append(int(position.get("positionId") or position.get("id") or 0))
            except (TypeError, ValueError):
                unreliable_pnl_position_ids.append(0)
        else:
            total_pnl += pnl
        source = str(position.get("pnl_source") or ("broker_or_legacy" if pnl is not None else "unavailable"))
        pnl_sources[source] = pnl_sources.get(source, 0) + 1

        entry = _position_entry(position)
        if entry > 0 and volume > 0:
            weighted_entry_num += entry * volume
            weighted_entry_den += volume

        open_ts = _position_open_ts(position)
        if open_ts:
            open_ts_list.append(open_ts)

    oldest_open_ts = min(open_ts_list) if open_ts_list else None
    weighted_entry = (weighted_entry_num / weighted_entry_den) if weighted_entry_den > 0 else None
    aggregate_r = (total_pnl / base_risk_usd) if base_risk_usd and base_risk_usd > 0 else 0.0
    if base_risk_usd is None or base_risk_usd <= 0:
        # No usable risk denominator -> aggregate_r is meaningless; treat the
        # whole snapshot as unreliable rather than silently reporting 0.0R
        # (which would read as "flat", not "unknown").
        unreliable = True

    return {
        "legs": legs,
        "aggregate_pnl_usd": round(total_pnl, 4),
        "aggregate_r": round(aggregate_r, 4),
        "sides": sides,
        "volume_net": round(volume_net, 8),
        "oldest_open_ts": oldest_open_ts,
        "weighted_entry": round(weighted_entry, 5) if weighted_entry is not None else None,
        "unreliable": unreliable,
        # Observability only.  OM still makes exactly the same fail-closed
        # decision, but an event can now distinguish a normal broker PnL
        # from a stale/missing OpenAPI enrichment on a live session.
        "pnl_sources": pnl_sources,
        "unreliable_pnl_position_ids": unreliable_pnl_position_ids,
    }


# ---------------------------------------------------------------------------
# structure_evidence
# ---------------------------------------------------------------------------


def _dominant_basket_side(sides: dict[str, int]) -> str | None:
    buy, sell = sides.get("buy", 0), sides.get("sell", 0)
    if buy > sell:
        return "buy"
    if sell > buy:
        return "sell"
    return None


def structure_evidence(
    lens: dict[str, Any] | None,
    m5_bars: list[dict[str, Any]] | None,
    basket_side: str,
    weighted_entry: float | None,
) -> dict[str, Any]:
    """Determine whether the basket's defended structural level has been lost.

    ``level`` = nearest defended swing behind ``weighted_entry`` for
    ``basket_side`` (the swing low behind a long, the swing high behind a
    short) — read from ``lens['swing_structure']`` exactly like
    ``hunt_mode._nearest_opposing_swing`` reads it for fresh entries.
    ``level_lost`` = an M5 close in the recently-supplied ``m5_bars`` crossed
    beyond that level (adverse side). ``m5_close_beyond`` = the LAST close in
    ``m5_bars`` remains beyond it (i.e. the loss is still confirmed as of the
    most recent bar, not just a transient wick that has since reclaimed).
    Never raises: any missing/malformed input degrades to
    ``level=None, level_lost=False, m5_close_beyond=False``.
    """
    m5 = list(m5_bars or [])
    swing_raw = (lens or {}).get("swing_structure") if isinstance(lens, dict) else None
    swing = swing_raw if isinstance(swing_raw, dict) else {}

    level: float | None = None
    try:
        if basket_side == "buy":
            swing_low = swing.get("last_swing_low")
            candidate = swing_low.get("price") if isinstance(swing_low, dict) else None
            if candidate is not None:
                level = float(candidate)
        elif basket_side == "sell":
            swing_high = swing.get("last_swing_high")
            candidate = swing_high.get("price") if isinstance(swing_high, dict) else None
            if candidate is not None:
                level = float(candidate)
    except (TypeError, ValueError, AttributeError):
        level = None

    if level is None or not m5:
        return {
            "level_lost": False,
            "m5_close_beyond": False,
            "level": level,
            "evidence": "no_defended_level_or_no_bars",
        }

    def _close(bar: dict[str, Any]) -> float:
        try:
            return float(bar.get("close") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    if basket_side == "buy":
        crossed = [b for b in m5 if _close(b) < level]
        last_beyond = _close(m5[-1]) < level
        direction_txt = "below"
    else:
        crossed = [b for b in m5 if _close(b) > level]
        last_beyond = _close(m5[-1]) > level
        direction_txt = "above"

    level_lost = bool(crossed)
    evidence = (
        f"basket_side={basket_side} level={level:.5f} "
        f"crossed_bars={len(crossed)} last_close={_close(m5[-1]):.5f} {direction_txt} level={level_lost}, "
        f"still_beyond_on_last_close={last_beyond}"
    )
    return {
        "level_lost": level_lost,
        "m5_close_beyond": bool(last_beyond),
        "level": level,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# enforce_caps — THE single choke-point (mirrors BasketManager._enforce_caps)
# ---------------------------------------------------------------------------


def enforce_caps(
    cfg: BasketConfig,
    *,
    agg: dict[str, Any],
    now_utc_iso: str,
    daily_state: dict[str, Any] | None,
    proposed_repair_risk_usd: float = 0.0,
) -> dict[str, Any] | None:
    """Return a cap-driven override action dict, or None if the caller may proceed.

    This is the ONLY place hard caps are checked in this module. Every path
    in ``decide_basket_action`` that could grow the basket (repair leg) or
    that lets the basket keep living (time stop / daily loss stop) MUST call
    this first and obey a non-None result unconditionally, exactly mirroring
    ``BasketManager._enforce_caps``'s role for the paper/simulated path.
    Never raises: malformed ``daily_state``/``agg`` inputs degrade to "no
    cap fired" rather than crashing the caller's M5 loop (the caller's own
    ``unreliable`` check in ``decide_basket_action`` already guards the
    blind-trading case before this is ever reached).
    """
    daily = daily_state or {}
    legs = int(agg.get("legs", 0) or 0)

    # Daily loss cap: once reached, the open basket must resolve at best
    # available and may never add another leg.
    daily_loss_baskets = int(daily.get("daily_loss_baskets", 0) or 0)
    if daily_loss_baskets >= cfg.daily_loss_baskets:
        return {
            "action": "close_all_cap_stop",
            "cap": "daily_loss_baskets",
            "reason": (
                f"daily_loss_baskets={daily_loss_baskets} >= cap={cfg.daily_loss_baskets} "
                "— no new repair legs, resolve at best available"
            ),
        }

    if legs <= 0:
        return None  # nothing else to cap-check before a basket exists

    # Time stop — force resolve regardless of any other consideration.
    oldest_open_ts = agg.get("oldest_open_ts")
    age_min = _age_minutes(oldest_open_ts, now_utc_iso)
    if age_min is not None and age_min >= cfg.time_stop_min:
        return {
            "action": "close_all_cap_stop",
            "cap": "time_stop_min",
            "reason": f"basket age={age_min:.1f}min >= time_stop_min={cfg.time_stop_min}min",
        }

    # Max legs — never allow another leg past the cap.
    if proposed_repair_risk_usd > 0 and legs >= cfg.max_legs:
        return {
            "action": "hold",
            "cap": "max_legs",
            "reason": f"legs={legs} >= max_legs={cfg.max_legs} — repair leg refused",
        }

    # Max basket risk (worst-case, all legs to stop) — never allow a repair
    # leg whose addition would breach the multiplier cap.
    if proposed_repair_risk_usd > 0:
        base_risk_usd = _f(agg.get("base_risk_usd"), 0.0)
        current_risk_usd = _f(agg.get("current_basket_risk_usd"), 0.0)
        if base_risk_usd > 0:
            projected = current_risk_usd + proposed_repair_risk_usd
            max_risk = base_risk_usd * cfg.max_basket_risk_mult
            if projected > max_risk:
                return {
                    "action": "hold",
                    "cap": "max_basket_risk_mult",
                    "reason": (
                        f"projected worst-case risk={projected:.2f} > cap={max_risk:.2f} "
                        f"({cfg.max_basket_risk_mult}x base={base_risk_usd:.2f})"
                    ),
                }

    return None


def _age_minutes(oldest_open_ts: Any, now_utc_iso: str) -> float | None:
    if not oldest_open_ts or not now_utc_iso:
        return None
    opened = _parse_iso_epoch(str(oldest_open_ts))
    now = _parse_iso_epoch(str(now_utc_iso))
    if opened is None or now is None:
        return None
    return max(0.0, (now - opened) / 60.0)


def _parse_iso_epoch(ts: str) -> float | None:
    try:
        from datetime import datetime, timezone

        text = ts.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError, AttributeError):
        try:
            from datetime import datetime, timezone

            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# decide_basket_action
# ---------------------------------------------------------------------------


def _resolve_profit_action(
    cfg: BasketConfig,
    aggregate_r: float,
    basket_runtime: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """FIX 1 (2026-07-07) — peak-R basket trailing. Returns a
    ``close_all_in_profit`` action dict, or None if the basket should keep
    running (no resolve-in-profit condition met this bar).

    ``basket_runtime`` is None (backward-compat default, byte-identical to
    the pre-fix behavior all existing tests assert): resolve fires the
    instant ``aggregate_r >= cfg.resolve_target_r`` — the flat +0.2R bank
    this fix retires when the caller opts in.

    ``basket_runtime`` is a dict (peak-R trailing ACTIVE): the caller
    (``shadow_runner._manage_lane_basket``) is expected to track and pass
    ``{'peak_r': <float, best aggregate_r ever seen this basket>}`` across
    M5 closes (see FIX 2). This function is PURE — it does not mutate
    ``basket_runtime`` or persist anything; the caller owns state, this
    function only reads ``peak_r`` (already updated by the caller with
    THIS bar's aggregate_r, i.e. peak_r = max(previous_peak_r, aggregate_r)
    before calling here) and decides the action:

      1. Hard take: ``aggregate_r >= cfg.take_r`` -> close_all_in_profit
         unconditionally (this is the ceiling; never trail past it).
      2. Armed trail: once ``peak_r >= cfg.arm_trail_r``, the basket must
         give back no more than ``(1 - cfg.trail_keep_frac)`` of its peak —
         close_all_in_profit fires when
         ``aggregate_r <= peak_r * cfg.trail_keep_frac``, i.e. we bank
         ``trail_keep_frac`` of the best R the basket ever reached. The
         floor is never below ``cfg.resolve_target_r`` (repurposed as the
         MINIMUM profit ever worth closing for — a basket must never be
         "closed in profit" below this bar even if trail math would permit
         a lower number for a very small peak).
      3. Not yet armed (``peak_r < cfg.arm_trail_r``): resolve-in-profit
         does NOT fire from this function at all — a green-but-not-yet-
         armed basket is left running (no premature banking at the old
         flat +0.2R). Other paths (structure repair, hard caps) still
         apply as before; this function only ever returns a
         close_all_in_profit action or None.
    """
    if basket_runtime is None:
        # Pre-fix behavior, preserved exactly for backward compatibility.
        if aggregate_r >= cfg.resolve_target_r:
            return {
                "action": "close_all_in_profit",
                "reason": f"aggregate_r={aggregate_r:.4f} >= resolve_target_r={cfg.resolve_target_r}",
            }
        return None

    peak_r = _f(basket_runtime.get("peak_r"), aggregate_r)
    # Defensive: peak_r can never be less than this bar's own aggregate_r —
    # a caller that forgot to update peak_r before calling us must not let
    # a stale (lower) peak understate how far the basket has actually run.
    peak_r = max(peak_r, aggregate_r)

    trail_info = {
        "peak_r": round(peak_r, 4),
        "arm_trail_r": cfg.arm_trail_r,
        "trail_keep_frac": cfg.trail_keep_frac,
        "take_r": cfg.take_r,
        "resolve_target_r_floor": cfg.resolve_target_r,
    }

    if aggregate_r >= cfg.take_r:
        trail_info["close_r"] = round(aggregate_r, 4)
        trail_info["trigger"] = "take_r"
        return {
            "action": "close_all_in_profit",
            "reason": f"aggregate_r={aggregate_r:.4f} >= take_r={cfg.take_r} (hard take)",
            "trail": trail_info,
        }

    if peak_r >= cfg.arm_trail_r:
        trail_stop_r = max(peak_r * cfg.trail_keep_frac, cfg.resolve_target_r)
        if aggregate_r <= trail_stop_r:
            trail_info["close_r"] = round(aggregate_r, 4)
            trail_info["trail_stop_r"] = round(trail_stop_r, 4)
            trail_info["trigger"] = "trail_stop"
            return {
                "action": "close_all_in_profit",
                "reason": (
                    f"aggregate_r={aggregate_r:.4f} retraced to trail_stop_r={trail_stop_r:.4f} "
                    f"(peak_r={peak_r:.4f} * trail_keep_frac={cfg.trail_keep_frac}, "
                    f"floor=resolve_target_r={cfg.resolve_target_r})"
                ),
                "trail": trail_info,
            }
        return None

    # Not yet armed — let the basket run; no resolve-in-profit this bar.
    return None


def decide_basket_action(
    cfg: BasketConfig,
    agg: dict[str, Any],
    evidence: dict[str, Any],
    now_utc_iso: str,
    daily_state: dict[str, Any] | None = None,
    basket_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide what to do with a live lane basket on this M5 close.

    Returns exactly one of:
      - ``{'action': 'close_all_in_profit', 'trail': {...}}``
        See ``_resolve_profit_action`` — either the pre-fix flat
        ``resolve_target_r`` bar (``basket_runtime is None``, backward
        compatible) or the FIX 1 peak-R trail / hard take_r (
        ``basket_runtime`` supplied). The ``trail`` key is present only in
        the peak-R-trail path so callers/tests can always see WHY it fired
        (peak_r, close_r, take_r, trigger).
      - ``{'action': 'add_repair_leg', 'side': ..., 'note': ...}``
        ONLY when ``evidence.level_lost`` AND ``evidence.m5_close_beyond``
        AND ``legs < cfg.max_legs`` AND the worst-case total risk after
        adding the leg would stay within ``cfg.max_basket_risk_mult`` of
        base risk. Repair side is chosen from the evidence + lens sweep
        state:
          * SAME direction (``REPAIR_MODE_SAME_SIDE``) when the level was
            merely swept (a liquidity-sweep wick beyond it that has NOT yet
            closed back inside on this read, or generic structural pressure
            without a confirmed sweep-and-hold) — i.e. price probed through
            structure but the read does not yet confirm a durable break, so
            we re-enter the SAME side at the better price the sweep
            offered.
          * OPPOSITE direction, ``hedge_lock`` (``REPAIR_MODE_HEDGE_LOCK``),
            when the level was truly lost — an M5 close beyond it AND no
            lens ``liquidity_sweep`` reclaim fired in the basket's own
            direction on this read, meaning the break is confirmed rather
            than a wick-and-reclaim.
      - ``{'action': 'close_all_cap_stop', 'cap': ...}``
        when ``enforce_caps`` fires for ANY reason (time stop, basket
        worst-case breach, or ``daily_state`` says the daily-loss-baskets
        cap is reached) — see ``enforce_caps`` for the exact conditions.
      - ``{'action': 'hold'}``
        otherwise, OR whenever ``agg['unreliable']`` is True (an unreliable
        PnL snapshot must never drive a trade decision — see module
        docstring "never trade blind").
    """
    if bool(agg.get("unreliable")):
        return {"action": "hold", "reason": "unreliable_pnl_snapshot"}

    legs = int(agg.get("legs", 0) or 0)
    if legs <= 0:
        return {"action": "hold", "reason": "no_open_lane_legs"}

    aggregate_r = _f(agg.get("aggregate_r"), 0.0)

    # Resolve-in-profit target takes priority over repair/cap evaluation,
    # mirroring BasketManager.on_m5_close's ordering. FIX 1: routed through
    # _resolve_profit_action so basket_runtime=None stays byte-identical to
    # the old flat-bar behavior, while a supplied basket_runtime activates
    # peak-R trailing (see that function's docstring for the full contract).
    resolve_action = _resolve_profit_action(cfg, aggregate_r, basket_runtime)
    if resolve_action is not None:
        return resolve_action

    # Cap checks (time stop / daily loss / would-be max-legs / would-be
    # max-risk with a placeholder repair-sized probe) BEFORE evaluating
    # whether repair evidence exists — a capped-out basket must resolve
    # regardless of what the structure evidence says.
    lifecycle_cap = enforce_caps(cfg, agg=agg, now_utc_iso=now_utc_iso, daily_state=daily_state)
    if lifecycle_cap is not None and lifecycle_cap["action"] == "close_all_cap_stop":
        return lifecycle_cap

    level_lost = bool(evidence.get("level_lost"))
    m5_close_beyond = bool(evidence.get("m5_close_beyond"))
    has_repair_evidence = level_lost and m5_close_beyond

    if has_repair_evidence:
        basket_side = _dominant_basket_side(agg.get("sides") or {})
        if basket_side is None:
            return {"action": "hold", "reason": "repair_evidence_present_but_basket_side_indeterminate"}

        repair_risk_probe = _f(agg.get("base_risk_usd"), 0.0) or 1.0
        cap_decision = enforce_caps(
            cfg,
            agg=agg,
            now_utc_iso=now_utc_iso,
            daily_state=daily_state,
            proposed_repair_risk_usd=repair_risk_probe,
        )
        if cap_decision is not None:
            # hold or close_all_cap_stop — either way, no repair leg fires.
            return cap_decision

        confirmed_break = _is_confirmed_break(evidence, agg)
        if confirmed_break:
            repair_side = _opposite(basket_side)
            mode = REPAIR_MODE_HEDGE_LOCK
            note = (
                f"level truly lost (confirmed break, no reclaim in basket direction) — "
                f"hedge_lock opposite side={repair_side}"
            )
        else:
            repair_side = basket_side
            mode = REPAIR_MODE_SAME_SIDE
            note = (
                f"structure merely swept (not a confirmed durable break) — "
                f"re-enter same side={repair_side} at improved price"
            )
        return {
            "action": "add_repair_leg",
            "side": repair_side,
            "mode": mode,
            "note": note,
        }

    if level_lost or m5_close_beyond:
        # Partial evidence only — explicitly refuse to repair, mirrors
        # BasketManager.on_m5_close's "price distance alone is never
        # sufficient" refusal.
        return {
            "action": "hold",
            "reason": (
                f"partial structure evidence only (level_lost={level_lost}, "
                f"m5_close_beyond={m5_close_beyond}) — both required"
            ),
        }

    return {
        "action": "hold",
        "reason": f"holding: aggregate_r={aggregate_r:.4f}, no repair evidence, target not reached",
    }


def _opposite(side: str) -> str:
    return "sell" if side == "buy" else "buy"


def _is_confirmed_break(evidence: dict[str, Any], agg: dict[str, Any]) -> bool:
    """True when the structural break is durable (hedge_lock), False when it
    reads as a sweep-and-hold-through that a same-side repair can exploit.

    Uses the evidence's own ``evidence`` text plus any lens sweep state the
    caller folded into ``agg`` (optional ``agg['lens_liquidity_sweep']`` —
    callers that have the lens handy may pass it through; when absent we
    fall back to evidence alone: a level that was crossed AND remains
    crossed on the last close with no sweep info available reads as a
    confirmed break, the more conservative (hedge_lock) assumption when the
    caller cannot supply sweep detail).
    """
    sweep = agg.get("lens_liquidity_sweep") or {}
    basket_side = _dominant_basket_side(agg.get("sides") or {})
    if sweep.get("value") and sweep.get("side") == basket_side:
        # A liquidity_sweep fired back in the basket's OWN direction — i.e.
        # price swept through and reclaimed in our favor — that is a sweep,
        # not a durable break against us.
        return False
    # No reclaim-in-our-favor sweep signal -> treat as a confirmed break
    # (conservative default; hedge_lock is the safer failure mode when
    # sweep detail is unavailable, since it caps directional exposure
    # rather than doubling down on the same side blind).
    return True

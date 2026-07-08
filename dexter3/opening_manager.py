"""Dexter3 OPENING MANAGER (OM) — fast intrabar defense (owner directive 2026-07-07).

**The bug OM fixes** (see ``docs/DEXTER3_M5_HUNTER_BLUEPRINT.md``, "OPENING
MANAGER (OM)"): basket management previously ran ONLY on a NEW M5 close —
``dexter3.shadow_runner.run_symbol_cycle`` gates ``_manage_lane_basket``
behind ``is_newest`` inside the ``pending_m5_closes`` loop, and the loop
itself sleeps ``poll_sec`` (default 20s) between calls to ``run_once``. Between
M5 bars, an open basket was completely unmonitored: an intrabar spike to
+2R that fell back to -1R before the next M5 close was invisible, because
``peak_r`` (see ``basket_live._resolve_profit_action`` / ``FIX 1/2`` in
``shadow_runner``) was only ever updated once per 5 minutes. Owner's words:
"ได้กำไรมากแล้วไม่ปิด รอจนโครงสร้างเปลี่ยนติดลบถึงปิด...เทรดด้วยความกลัว
ไม่ใช่ความหิวกระหายกำไร" (fear-driven trading, not profit-hunting) and "ออกไม้
แก้ต้องวัดแนวโน้มและความได้เปรียบก่อน ถ้าฝั่งตรงข้ามจะชนะควรเปิดซ้ำ + มี opening
manager คอย monitoring" (a repair leg must measure current edge before firing,
and if the opposite side would win, open there instead — with an Opening
Manager continuously monitoring).

This module is the CONTINUOUS, tick-resolution counterpart:
``OpeningManager.evaluate()`` is called every fast tick (default ~4s, see
``dexter3.shadow_runner``'s fast-tick loop) rather than only on M5 closes, so
the ratcheting trail floor and the repair decision are both measured against
the TRUE running peak, not a 5-minute-sampled one.

Design constraints (mirrors every other ``dexter3/`` module):
  - ``evaluate()`` is PURE — no I/O, no MCP calls, no mutation of broker
    state. It reads ``lane_positions``/bars/``state`` and returns an action
    dict; the caller (``shadow_runner``) executes it via
    ``dexter3.executor.Dexter3Executor`` and persists any state updates.
  - Never trades blind: an ``unreliable`` aggregate (see
    ``basket_live.aggregate_lane``) always degrades to ``{'action': 'hold'}``.
  - Hard caps (max legs, max basket risk multiplier, time stop, daily loss
    baskets) are enforced through the EXISTING single choke-point,
    ``dexter3.basket_live.enforce_caps`` — this module never re-derives cap
    math, it only calls that function before returning any action that could
    grow the basket or let a capped-out basket keep living.
  - The peak-R runtime dict is owned by the CALLER (same pattern as
    ``shadow_runner._basket_runtime_for``); ``evaluate()`` receives it via
    ``state['basket_runtime'][symbol]`` and returns the UPDATED dict inside
    its result so the caller can persist it without this module reaching into
    ``shadow_runner`` internals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dexter3 import basket_live, hunt_mode
from dexter3.basket_manager import BasketConfig

Bar = dict[str, Any]
Position = dict[str, Any]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# OMConfig — every constant documented + env-overridable (see shadow_runner's
# ``_om_config_from_env`` builder for the env var names).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OMConfig:
    # -- Profit Hunter (ratcheting trail) — LEGACY flat arm/keep, retained for
    # env-override backward-compat only. The DRAGON LADDER (see
    # ``ladder_floor_r`` / ``ladder_points`` below) is the DEFAULT profit exit
    # as of the 2026-07-08 owner directive (docs/DEXTER3_M5_HUNTER_BLUEPRINT.md
    # "DRAGON LADDER"); these two fields no longer drive the default close
    # path but remain live knobs (``DEXTER3_OM_ARM_R`` / ``DEXTER3_OM_TRAIL_KEEP``
    # still parsed in shadow_runner._om_config_from_env for anyone who wants
    # to A/B the old flat trail against the ladder). ------------------------
    arm_trail_r: float = 0.4
    trail_keep_frac: float = 0.70

    # Hard take: close all unconditionally once live aggregate_r reaches this
    # — the ceiling above which we never keep trailing hoping for more. Fires
    # ABOVE the ladder (checked first every tick).
    take_r: float = 1.2

    # Spike capture: an instant news/liquidity burst straight past the normal
    # take level is banked immediately, no ratchet math needed. Fires ABOVE
    # the ladder too.
    spike_take_r: float = 2.5

    # -- DRAGON LADDER (owner directive 2026-07-08) — concave giveback -------
    # The gap the ladder closes: the flat trail above gave NO protection
    # below ``arm_trail_r`` (0.4R default), so a small winner (peak 0.3R)
    # could ride all the way back to a full -1R stop — "ที่กำไรแล้วปล่อยให้ SL
    # ขัดแย้งกับ mindset ล่ากำไร" (owner). The ladder floor is ALWAYS present
    # once peak_r crosses the first breakpoint, and only ever rises with
    # peak_r (see ``ladder_floor_r``). Points are ``(peak_threshold, floor)``
    # pairs, strictly increasing in both peak and floor, interpolated
    # piecewise-linearly BETWEEN points (which — because each segment's slope
    # is < 1 and shrinks as peak grows — produces the concave "tight near the
    # start, generous further out" shape the blueprint table specifies).
    # Below the first threshold: no floor yet (``ladder_floor_r`` -> None).
    # Beyond the last point: floor = ``ladder_tail_keep_frac`` * peak_r.
    ladder_points: tuple[tuple[float, float], ...] = (
        (0.15, 0.00),
        (0.30, 0.18),
        (0.50, 0.32),
        (0.80, 0.55),
        (1.20, 0.85),
        (2.00, 1.45),
        (3.00, 2.25),
    )
    # Slope applied to peak_r beyond the last ladder point (keeps ~75% of
    # peak for a dragon run past 3R — matches the 2.25/3.00 = 0.75 ratio of
    # the final table row so the curve has no kink at the boundary).
    ladder_tail_keep_frac: float = 0.75

    # -- Stall-take (hungry scalp) --------------------------------------------
    # If a SMALL winner (peak_r below this) has not made a new peak for
    # ``stall_ticks`` consecutive fast ticks AND live_r has decayed to/below
    # ``stall_decay_frac`` of its peak, bank it now rather than wait for the
    # (much looser, near-breakeven) ladder floor — small edges decay fast and
    # the owner wants the hunt hungrier than the floor alone would be.
    stall_max_peak_r: float = 0.5
    stall_ticks: int = 12
    stall_decay_frac: float = 0.6

    # -- Opportunity add — pyramid the dragon (owner directive 2026-07-08) ---
    # Master kill switch: pyramid path is fully disabled when False (ladder +
    # stall-take stay on regardless) — see DEXTER3_OM_PYRAMID_ENABLED.
    pyramid_enabled: bool = True
    # Minimum live_r required before an add is even considered — never add to
    # a basket that is not already comfortably green.
    pyramid_min_live_r: float = 0.3
    # Minimum hunt-committee conviction for the SAME-side continuation read
    # that justifies adding risk to a winner.
    pyramid_min_conv: float = 0.4
    # Peak must have advanced by at least this many R since the last pyramid
    # add before another one is permitted (one add per tier crossed).
    pyramid_tier_step: float = 0.5

    # -- Basket Doctor (edge-measured repair) --------------------------------
    # Only consider a repair leg once the basket is this far underwater (in
    # aggregate R). Never fires on a green or barely-red basket.
    repair_trigger_r: float = -0.4

    # Minimum hunt-committee conviction required before a repair leg fires,
    # in EITHER direction (opposite-side counter_trend_recovery or same-side
    # add). Below this, the edge read is too weak to justify adding risk —
    # hold and let the existing hard caps (time stop, etc.) resolve it.
    repair_min_conviction: float = 0.35


# ---------------------------------------------------------------------------
# DRAGON LADDER — pure concave giveback floor (owner directive 2026-07-08)
# ---------------------------------------------------------------------------


def ladder_floor_r(peak_r: float, cfg: OMConfig) -> float | None:
    """Return the current close-floor for ``peak_r``, or ``None`` if the
    peak has not yet crossed the first breakpoint (too small to protect).

    Piecewise-linear interpolation between ``cfg.ladder_points`` (each a
    ``(peak_threshold, floor)`` pair, ascending). MONOTONIC non-decreasing in
    ``peak_r`` by construction: every segment has slope in ``[0, 1)`` (a
    floor point is always <= its peak point, and each successive floor point
    is >= the previous one — see the table in the module docstring / OMConfig
    comment), so interpolating between two such points can never produce a
    floor that falls as peak rises, and the tail slope
    (``ladder_tail_keep_frac < 1``) continues that property indefinitely.

    Guarantee this function exists to enforce: once ``peak_r >= 0.15`` (the
    first breakpoint), the floor is always ``>= 0.0`` — a real winner can
    never be laddered down to a full loss; the only residual risk is a price
    GAP past the floor between ticks, which is broker-SL territory, not a
    ladder bug.
    """
    points = cfg.ladder_points
    if not points:
        return None
    first_peak, first_floor = points[0]
    if peak_r < first_peak:
        return None

    # Walk the ascending breakpoints; interpolate within the bracketing pair.
    prev_peak, prev_floor = points[0]
    for peak_pt, floor_pt in points[1:]:
        if peak_r <= peak_pt:
            if peak_pt == prev_peak:  # degenerate (duplicate) point — avoid /0
                return floor_pt
            frac = (peak_r - prev_peak) / (peak_pt - prev_peak)
            return prev_floor + frac * (floor_pt - prev_floor)
        prev_peak, prev_floor = peak_pt, floor_pt

    # Beyond the last point: tail slope keeps the curve concave with no kink
    # (continuous at the boundary would require exactly
    # last_floor == last_peak * tail_keep_frac; we use the tail formula
    # directly so the curve is defined purely by peak_r beyond that point).
    last_peak, last_floor = points[-1]
    tail_floor = peak_r * cfg.ladder_tail_keep_frac
    # Never let the tail formula produce a floor below the last table point
    # (monotonic guarantee) — clamp up to last_floor if the tail slope would
    # otherwise dip under it (cannot happen with the shipped defaults, but a
    # custom ladder_points/tail_keep_frac override must stay safe).
    return max(tail_floor, last_floor)


# ---------------------------------------------------------------------------
# per-basket peak-R runtime (state ownership mirrors shadow_runner's
# ``_basket_runtime_for`` — this module only reads/returns, never persists)
# ---------------------------------------------------------------------------


def _update_peak_r(
    basket_runtime: dict[str, Any] | None, oldest_open_ts: str | None, live_r: float
) -> dict[str, Any]:
    """Return an updated basket_runtime dict: peak_r = max(prev, live_r).

    Resets to a fresh peak (== live_r) whenever ``oldest_open_ts`` differs
    from the runtime's tracked value (a brand-new basket opened) or is
    missing — mirrors ``shadow_runner._basket_runtime_for`` exactly so the
    fast-tick path and the M5 path can never disagree about when a peak
    should reset.

    Also tracks ``ticks_since_peak`` (DRAGON LADDER part 2, stall-take): reset
    to 0 whenever this tick's ``live_r`` sets a NEW peak (strictly greater
    than the previous one), otherwise incremented by 1 — so the caller can
    tell "no new peak for N ticks" without owning a wall-clock. Carries
    ``last_pyramid_peak`` through unchanged (DRAGON LADDER part 3 owns that
    field; this function never sets or clears it except on a fresh basket).
    """
    existing = basket_runtime or {}
    if not oldest_open_ts or existing.get("oldest_open_ts") != oldest_open_ts:
        return {
            "oldest_open_ts": oldest_open_ts,
            "peak_r": live_r,
            "ticks_since_peak": 0,
            "last_pyramid_peak": None,
        }
    runtime = dict(existing)
    prev_peak = _f(existing.get("peak_r"), live_r)
    if live_r > prev_peak:
        runtime["peak_r"] = live_r
        runtime["ticks_since_peak"] = 0
    else:
        runtime["peak_r"] = prev_peak
        runtime["ticks_since_peak"] = int(_f(existing.get("ticks_since_peak"), 0)) + 1
    runtime.setdefault("last_pyramid_peak", None)
    return runtime


class OpeningManager:
    """Fast intrabar defense brain — Profit Hunter + Basket Doctor.

    ``executor``/``journal``/``config`` are accepted for the constructor's
    convenience (the runner may want a single OM instance carrying its
    executor/journal handles), but ``evaluate()`` itself never touches them —
    it is pure. Only ``config`` (an ``OMConfig``) is actually read by
    ``evaluate()``; ``executor``/``journal`` are stored for callers that want
    a thin ``run_tick()``-style wrapper later without changing this file, but
    THIS module does not add one (the runner owns execution per the
    blueprint's "runner executes the returned action" contract).
    """

    def __init__(
        self,
        executor: Any = None,
        journal: Any = None,
        config: OMConfig | None = None,
    ) -> None:
        self.executor = executor
        self.journal = journal
        self.config = config or OMConfig()

    # -- main entry point -----------------------------------------------

    def evaluate(
        self,
        symbol: str,
        lane_positions: list[Position] | None,
        spot: dict[str, Any] | None,
        m5_bars: list[Bar] | None,
        m15_bars: list[Bar] | None,
        h1_bars: list[Bar] | None,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """The per-tick brain. PURE decision — no I/O, no mutation.

        ``state`` is read-only here EXCEPT for the returned
        ``basket_runtime`` key, which the caller is expected to persist back
        into its own state dict (same contract as
        ``shadow_runner._basket_runtime_for`` / ``_clear_basket_runtime``).

        ``state`` may optionally carry the following caller-supplied context
        (all have safe defaults so a minimal ``state={}`` still works in
        tests): ``basket_runtime`` (dict | None, previous tick's runtime),
        ``now_utc_iso`` (str, for cap age math), ``daily_state`` (dict, for
        the daily-loss-baskets cap), ``basket_cfg`` (``BasketConfig``, so the
        SAME env-derived caps/trail config the M5 path uses also governs the
        fast tick — falls back to ``BasketConfig()`` defaults if absent),
        ``base_risk_usd`` (float, for R-denominator and repair sizing —
        falls back to ``self.executor.config.risk_usd`` or 0.5), and
        ``spread_abs`` (float, for the Basket Doctor's hunt_mode call —
        falls back to 0.0, which only affects hunt_mode's own spread-based
        hard veto, not this module's decisions).

        Returns one of:
          - ``{'action': 'hold', 'reason': ...}``
          - ``{'action': 'hold', 'reason': 'unreliable_pnl'}`` (never trade
            blind — see ``basket_live.aggregate_lane``'s ``unreliable`` flag)
          - ``{'action': 'close_all', 'reason':
            'spike'|'take'|'ladder_floor'|'stall_take'|'cap_stop',
            'peak_r':.., 'floor_r':.., 'live_r':.., 'basket_runtime': {...}}``
          - ``{'action': 'add_repair_leg', 'side':.., 'note':.., 'basket_runtime': {...}}``

        Priority order every tick (highest first, DRAGON LADDER owner
        directive 2026-07-08): cap-stop > spike/hard-take > ladder-floor
        close > stall-take > pyramid-add > basket-doctor repair > hold. An
        ``unreliable`` aggregate always degrades to hold before any of this
        is evaluated.
        """
        cfg = self.config
        positions = list(lane_positions or [])
        st = state if isinstance(state, dict) else {}

        # -- step a: aggregate the lane (reuse basket_live.aggregate_lane) --
        base_risk_usd = (
            _f(st["base_risk_usd"]) if st.get("base_risk_usd") is not None else self._base_risk_usd()
        )
        agg = basket_live.aggregate_lane(positions, base_risk_usd=base_risk_usd)

        if agg.get("legs", 0) <= 0:
            return {"action": "hold", "reason": "no_open_lane_legs"}

        # -- step g: unreliable aggregate -> always hold (never trade blind) --
        if bool(agg.get("unreliable")):
            return {"action": "hold", "reason": "unreliable_pnl"}

        live_r = _f(agg.get("aggregate_r"), 0.0)
        oldest_open_ts = agg.get("oldest_open_ts")

        # -- step b: update CONTINUOUS peak_r + ticks_since_peak (the actual
        # bug fix — this is called every fast tick, not once per M5 close) --
        basket_runtime = _update_peak_r(st.get("basket_runtime"), oldest_open_ts, live_r)
        peak_r = _f(basket_runtime.get("peak_r"), live_r)

        # -- step e: time-stop / cap-stop (hard caps take priority over
        # profit-hunting so a capped-out basket always resolves) ------------
        basket_cfg = st.get("basket_cfg") or self._basket_config()
        now_utc_iso = str(st.get("now_utc_iso") or "")
        daily_state = st.get("daily_state")
        cap_action = basket_live.enforce_caps(
            basket_cfg, agg=agg, now_utc_iso=now_utc_iso, daily_state=daily_state
        )
        if cap_action is not None and cap_action.get("action") == "close_all_cap_stop":
            return {
                "action": "close_all",
                "reason": "cap_stop",
                "cap": cap_action.get("cap"),
                "cap_reason": cap_action.get("reason"),
                "peak_r": round(peak_r, 4),
                "live_r": round(live_r, 4),
                "basket_runtime": basket_runtime,
            }

        # -- step c: spike / hard-take (fire ABOVE the ladder, unconditional
        # ceiling captures — never wait on giveback math once the move is
        # this big) ------------------------------------------------------
        hard_take_action = self._hard_take(live_r, peak_r, cfg)
        if hard_take_action is not None:
            hard_take_action["basket_runtime"] = basket_runtime
            return hard_take_action

        # -- step c2: DRAGON LADDER floor close (the new default profit
        # exit — see ``ladder_floor_r``) -----------------------------------
        floor_r = ladder_floor_r(peak_r, cfg)
        if floor_r is not None and live_r <= floor_r:
            return {
                "action": "close_all",
                "reason": "ladder_floor",
                "peak_r": round(peak_r, 4),
                "floor_r": round(floor_r, 4),
                "live_r": round(live_r, 4),
                "basket_runtime": basket_runtime,
            }

        # -- step c3: stall-take (hungry scalp) — a small winner that has
        # stopped making new peaks and is decaying back toward the (much
        # looser) ladder floor gets banked now instead of waiting. Only
        # applies below ``stall_max_peak_r`` — above that tier the ladder
        # itself is already tight enough to ride.
        if 0.0 < peak_r < cfg.stall_max_peak_r:
            ticks_since_peak = int(_f(basket_runtime.get("ticks_since_peak"), 0))
            if ticks_since_peak >= cfg.stall_ticks and live_r <= peak_r * cfg.stall_decay_frac:
                return {
                    "action": "close_all",
                    "reason": "stall_take",
                    "peak_r": round(peak_r, 4),
                    "floor_r": floor_r if floor_r is None else round(floor_r, 4),
                    "live_r": round(live_r, 4),
                    "ticks_since_peak": ticks_since_peak,
                    "basket_runtime": basket_runtime,
                }

        # -- step c4: opportunity add — pyramid the dragon (winners-only,
        # capped, one add per peak-tier crossed) -----------------------------
        if cfg.pyramid_enabled:
            pyramid_action = self._pyramid_add(
                symbol,
                agg,
                live_r,
                peak_r,
                floor_r,
                basket_runtime,
                m5_bars,
                m15_bars,
                h1_bars,
                basket_cfg,
                cfg,
                now_utc_iso,
                daily_state,
                _f(st.get("spread_abs"), 0.0),
            )
            if pyramid_action is not None:
                pyramid_action["basket_runtime"] = basket_runtime
                return pyramid_action

        # -- step d: Basket Doctor (edge-measured repair) --------------------
        if live_r <= cfg.repair_trigger_r:
            evidence = self._structure_evidence(agg, positions, m5_bars)
            if bool(evidence.get("level_lost")) and bool(evidence.get("m5_close_beyond")):
                spread_abs = _f(st.get("spread_abs"), 0.0)
                repair_action = self._basket_doctor(
                    symbol,
                    agg,
                    positions,
                    m5_bars,
                    m15_bars,
                    h1_bars,
                    basket_cfg,
                    cfg,
                    now_utc_iso,
                    daily_state,
                    spread_abs,
                )
                if repair_action is not None:
                    repair_action["basket_runtime"] = basket_runtime
                    return repair_action

        return {
            "action": "hold",
            "reason": "no_condition_met",
            "live_r": round(live_r, 4),
            "peak_r": round(peak_r, 4),
            "floor_r": round(floor_r, 4) if floor_r is not None else None,
            "basket_runtime": basket_runtime,
        }

    # -- Profit Hunter: hard ceilings (spike / take) -----------------------

    def _hard_take(self, live_r: float, peak_r: float, cfg: OMConfig) -> dict[str, Any] | None:
        """Instant unconditional captures ABOVE the DRAGON LADDER.

        Priority order (highest first): spike_take_r (instant burst capture)
        > take_r (hard ceiling). These fire regardless of the ladder floor —
        a genuine burst or a move past the configured ceiling is banked
        immediately, no giveback math needed. The ratcheting FLOOR (formerly
        this method's third branch) is now owned by ``ladder_floor_r`` /
        ``evaluate()``'s step c2 — see the DRAGON LADDER section of
        ``docs/DEXTER3_M5_HUNTER_BLUEPRINT.md`` for why the flat arm/keep
        trail was retired as the default profit exit.
        """
        if live_r >= cfg.spike_take_r:
            return {
                "action": "close_all",
                "reason": "spike",
                "peak_r": round(peak_r, 4),
                "floor_r": None,
                "live_r": round(live_r, 4),
            }
        if live_r >= cfg.take_r:
            return {
                "action": "close_all",
                "reason": "take",
                "peak_r": round(peak_r, 4),
                "floor_r": None,
                "live_r": round(live_r, 4),
            }
        return None

    def _profit_hunter(self, live_r: float, peak_r: float, cfg: OMConfig) -> dict[str, Any] | None:
        """LEGACY flat arm/keep ratcheting trail — retained for callers that
        explicitly want the pre-ladder behavior (e.g. an A/B harness or a
        future env-flagged fallback). NOT called from ``evaluate()`` anymore;
        ``_hard_take`` + ``ladder_floor_r`` are the default path as of the
        2026-07-08 DRAGON LADDER directive. Priority order (highest first):
        spike_take_r > take_r > armed flat trail floor.
        """
        hard = self._hard_take(live_r, peak_r, cfg)
        if hard is not None:
            return hard
        if peak_r >= cfg.arm_trail_r:
            floor_r = peak_r * cfg.trail_keep_frac
            if live_r <= floor_r:
                return {
                    "action": "close_all",
                    "reason": "trail",
                    "peak_r": round(peak_r, 4),
                    "floor_r": round(floor_r, 4),
                    "live_r": round(live_r, 4),
                }
        return None

    # -- Opportunity add — pyramid the dragon (owner directive 2026-07-08) --

    def _pyramid_add(
        self,
        symbol: str,
        agg: dict[str, Any],
        live_r: float,
        peak_r: float,
        floor_r: float | None,
        basket_runtime: dict[str, Any],
        m5_bars: list[Bar] | None,
        m15_bars: list[Bar] | None,
        h1_bars: list[Bar] | None,
        basket_cfg: BasketConfig,
        cfg: OMConfig,
        now_utc_iso: str,
        daily_state: dict[str, Any] | None,
        spread_abs: float,
    ) -> dict[str, Any] | None:
        """Add ONE leg in the basket's own direction while holding a winner —
        "hunt more while holding" (owner directive 2026-07-08). Fires ONLY
        when ALL of the following hold (winners-only, never martingale):

          1. ``live_r >= cfg.pyramid_min_live_r`` — the basket is already
             comfortably green, not just barely positive.
          2. The ladder floor is already established AND >= breakeven
             (``floor_r is not None and floor_r >= 0``) — adding can never
             turn a green basket red past breakeven, because the existing
             legs are already floor-protected.
          3. The current bar's continuation read (``hunt_mode.decide_hunt``)
             gives the SAME side as the basket with conviction >=
             ``cfg.pyramid_min_conv`` — never add against a basket that has
             lost its own edge.
          4. ``basket_live.enforce_caps`` allows another leg (the single
             choke-point — reused, never bypassed; the SAME probe pattern
             ``_basket_doctor`` already uses).
          5. At most ONE add per peak-tier crossed: ``peak_r`` must have
             advanced by >= ``cfg.pyramid_tier_step`` since
             ``basket_runtime['last_pyramid_peak']`` (None the first time, so
             the very first add only needs the peak to have reached the
             normal gates above).

        Returns ``{'action': 'add_repair_leg', 'side': basket_side, 'note':
        'pyramid_add', 'conviction': ...}`` on fire (mirrors the Basket
        Doctor's repair-leg shape so the existing runner add-leg execution
        path — ``shadow_runner.run_om_tick``'s ``add_repair_leg`` branch —
        places it with no changes needed), or ``None`` to fall through to the
        next step (basket doctor / hold). On fire, ALSO stamps
        ``basket_runtime['last_pyramid_peak'] = peak_r`` in-place so the
        caller persists the tier as claimed (mirrors how this module always
        mutates+returns the SAME runtime dict object across a tick).
        """
        if live_r < cfg.pyramid_min_live_r:
            return None
        if floor_r is None or floor_r < 0:
            return None

        sides = agg.get("sides", {}) or {}
        basket_side = "buy" if int(sides.get("buy", 0)) >= int(sides.get("sell", 0)) else "sell"

        last_pyramid_peak = basket_runtime.get("last_pyramid_peak")
        if last_pyramid_peak is not None:
            if (peak_r - _f(last_pyramid_peak, 0.0)) < cfg.pyramid_tier_step:
                return None

        bars5 = list(m5_bars or [])
        if not bars5:
            return None
        hunt_decision = hunt_mode.decide_hunt(
            symbol, bars5, list(m15_bars or []), list(h1_bars or []), None, spread_abs
        )
        if hunt_decision.action != "enter" or hunt_decision.side is None:
            return None
        if str(hunt_decision.side) != basket_side:
            return None
        conviction = _f(hunt_decision.leader_score, 0.0)
        if conviction < cfg.pyramid_min_conv:
            return None

        repair_risk_probe = _f(agg.get("base_risk_usd"), 0.0) or self._base_risk_usd() or 1.0
        cap_decision = basket_live.enforce_caps(
            basket_cfg,
            agg=agg,
            now_utc_iso=now_utc_iso,
            daily_state=daily_state,
            proposed_repair_risk_usd=repair_risk_probe,
        )
        if cap_decision is not None:
            # max_legs / max_basket_risk_mult refusal, or a cap_stop already
            # caught by the earlier enforce_caps call in evaluate() — either
            # way, caps are the single choke-point and win here too.
            return None

        basket_runtime["last_pyramid_peak"] = peak_r
        return {
            "action": "add_repair_leg",
            "side": basket_side,
            "conviction": round(conviction, 4),
            "note": "pyramid_add",
        }

    # -- Basket Doctor ------------------------------------------------------

    def _structure_evidence(
        self, agg: dict[str, Any], positions: list[Position], m5_bars: list[Bar] | None
    ) -> dict[str, Any]:
        sides = agg.get("sides", {}) or {}
        basket_side = "buy" if int(sides.get("buy", 0)) >= int(sides.get("sell", 0)) else "sell"
        # lens computation is deferred to the caller in the real pipeline
        # (shadow_runner already computes it once per bar for the M5 path);
        # here we compute a lightweight lens from m5_bars directly via
        # hunt_mode's own safe lens builder so this module stays self-
        # contained and testable without requiring the caller to pass a lens.
        lens = hunt_mode._safe_run_lens(list(m5_bars or []), "") if m5_bars else {}
        return basket_live.structure_evidence(lens, m5_bars, basket_side, agg.get("weighted_entry"))

    def _basket_doctor(
        self,
        symbol: str,
        agg: dict[str, Any],
        positions: list[Position],
        m5_bars: list[Bar] | None,
        m15_bars: list[Bar] | None,
        h1_bars: list[Bar] | None,
        basket_cfg: BasketConfig,
        cfg: OMConfig,
        now_utc_iso: str,
        daily_state: dict[str, Any] | None,
        spread_abs: float = 0.0,
    ) -> dict[str, Any] | None:
        """Edge-measured repair: call hunt_mode.decide_hunt to find the side
        with edge NOW, then decide same-side vs. opposite-side (counter-trend
        recovery) repair — never blind averaging.

        Respects hard caps via ``basket_live.enforce_caps`` (the single
        choke-point) BEFORE returning any repair action — a capped-out
        basket (max_legs / max_basket_risk_mult / daily_loss_baskets) may
        never receive a repair leg from this path.
        """
        sides = agg.get("sides", {}) or {}
        basket_side = "buy" if int(sides.get("buy", 0)) >= int(sides.get("sell", 0)) else "sell"

        bars5 = list(m5_bars or [])
        if not bars5:
            return None
        hunt_decision = hunt_mode.decide_hunt(symbol, bars5, list(m15_bars or []), list(h1_bars or []), None, spread_abs)
        if hunt_decision.action != "enter" or hunt_decision.side is None:
            return None
        edge_side = str(hunt_decision.side)
        conviction = _f(hunt_decision.leader_score, 0.0)

        if conviction < cfg.repair_min_conviction:
            return None

        # Hard-cap check BEFORE deciding the repair side — a proposed repair
        # always risks base_risk_usd worth of exposure (the same sizing the
        # runner's _repair_geometry / executor.execute_repair_leg path uses).
        repair_risk_probe = _f(agg.get("base_risk_usd"), 0.0) or self._base_risk_usd() or 1.0
        cap_decision = basket_live.enforce_caps(
            basket_cfg,
            agg=agg,
            now_utc_iso=now_utc_iso,
            daily_state=daily_state,
            proposed_repair_risk_usd=repair_risk_probe,
        )
        if cap_decision is not None:
            # hold (max_legs/max_basket_risk_mult refusal) or a cap_stop the
            # caller's own enforce_caps call above would already have caught
            # — either way, no repair leg fires from the Basket Doctor.
            return None

        if edge_side != basket_side:
            # The WINNING direction is opposite our losing leg AND conviction
            # clears the floor -> drag the aggregate back toward positive by
            # opening in the direction that currently has edge.
            return {
                "action": "add_repair_leg",
                "side": edge_side,
                "conviction": round(conviction, 4),
                "note": "counter_trend_recovery",
            }

        # edge_side == basket_side: the losing side itself still holds edge
        # (adverse noise, not a genuine reversal) -> same-side add at a
        # better price, still subject to the same cap check above.
        return {
            "action": "add_repair_leg",
            "side": basket_side,
            "conviction": round(conviction, 4),
            "note": "same_side_add_better_price",
        }

    # -- small config helpers ---------------------------------------------

    def _base_risk_usd(self) -> float:
        cfg = getattr(self.executor, "config", None)
        risk = getattr(cfg, "risk_usd", None)
        return _f(risk, 0.5)

    def _basket_config(self) -> BasketConfig:
        return BasketConfig()

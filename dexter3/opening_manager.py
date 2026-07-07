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
    # -- Profit Hunter (ratcheting trail) ------------------------------------
    # Trail arms once the basket's CONTINUOUS peak_r (true running max, not
    # 5-min sampled) reaches this level. Below this, a green-but-modest
    # basket is left running untouched (no premature banking).
    arm_trail_r: float = 0.4

    # Once armed, the locked floor = peak_r * trail_keep_frac. The floor only
    # ever RATCHETS UP as peak_r grows (never down) — this is the fix for
    # "ได้กำไรมากแล้วไม่ปิด": once we've banked a floor, retracement below it
    # closes the basket immediately rather than waiting for the next M5 close
    # or for the position to go all the way back to breakeven/negative.
    trail_keep_frac: float = 0.70

    # Hard take: close all unconditionally once live aggregate_r reaches this
    # — the ceiling above which we never keep trailing hoping for more.
    take_r: float = 1.2

    # Spike capture: an instant news/liquidity burst straight past the normal
    # take level is banked immediately, no ratchet math needed.
    spike_take_r: float = 2.5

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
    """
    existing = basket_runtime or {}
    if not oldest_open_ts or existing.get("oldest_open_ts") != oldest_open_ts:
        return {"oldest_open_ts": oldest_open_ts, "peak_r": live_r}
    runtime = dict(existing)
    runtime["peak_r"] = max(_f(existing.get("peak_r"), live_r), live_r)
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
          - ``{'action': 'close_all', 'reason': 'trail'|'take'|'spike'|'cap_stop',
            'peak_r':.., 'floor_r':.., 'live_r':.., 'basket_runtime': {...}}``
          - ``{'action': 'add_repair_leg', 'side':.., 'note':.., 'basket_runtime': {...}}``
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

        # -- step b: update CONTINUOUS peak_r (the actual bug fix — this is
        # called every fast tick, not once per M5 close) --------------------
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

        # -- step c: Profit Hunter (ratcheting trail) ------------------------
        profit_action = self._profit_hunter(live_r, peak_r, cfg)
        if profit_action is not None:
            profit_action["basket_runtime"] = basket_runtime
            return profit_action

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
            "floor_r": round(peak_r * cfg.trail_keep_frac, 4) if peak_r >= cfg.arm_trail_r else None,
            "basket_runtime": basket_runtime,
        }

    # -- Profit Hunter ----------------------------------------------------

    def _profit_hunter(self, live_r: float, peak_r: float, cfg: OMConfig) -> dict[str, Any] | None:
        """Ratcheting trail / hard take / spike capture.

        Priority order (highest first): spike_take_r (instant burst capture)
        > take_r (hard ceiling) > armed trail floor. Spike is checked first
        because it is meant to fire even faster than the ordinary take_r
        ceiling on a genuine burst — in practice ``spike_take_r > take_r`` so
        this ordering only matters for readability, not behavior.
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

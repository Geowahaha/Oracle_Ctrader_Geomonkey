"""Fibo MTF live trade-planning intelligence.

This module deliberately separates structural Fibonacci telemetry from a broker-
executable trade plan.  The planner does not make the old mistake of converting
``nearest_level_price`` + HTF swing-start into live entry/SL/TP.  It classifies a
candidate into an opportunity-first route and only emits a trade plan when local
execution evidence can produce bounded broker geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


GOLDEN_RATIO_ZONES = {"0.618", "0.65_0.70", "0.650_0.700", "0.786", "0.886", "0.886_deep_retest"}
IMPULSE_ACTIVE_STATES = {"birth", "restart", "impulse_birth", "impulse_restart", "active", "mature"}
LOWER_EXECUTION_TFS = {"m1", "m5", "m15", "m30", "h1"}
HIGH_CONTEXT_TFS = {"h4", "d1", "w1", "mn1"}


@dataclass(frozen=True)
class FiboMtfTakeProfit:
    price: float
    fraction: float
    label: str


@dataclass(frozen=True)
class FiboMtfTradePlan:
    route: str
    entry_type: str
    entry_price: float
    stop_loss: float
    thesis_stop_loss: float
    size_multiplier: float
    runner_enabled: bool
    tp_plan: list[FiboMtfTakeProfit]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FiboMtfRouteDecision:
    route: str
    intent: str
    reasons: list[str]
    trade_plan: FiboMtfTradePlan | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FiboMtfPlannerInput:
    symbol: str
    direction: str
    timeframe: str
    current_price: float
    nearest_level_price: float
    raw_stop_loss: float
    atr: float
    ratio_zone: str
    impulse_state: str
    correction_end_confirmed: bool
    confidence: float
    reclaim_confirmed: bool = False
    sweep_confirmed: bool = False
    impulse_birth_confirmed: bool = False
    execution_swing_low: float | None = None
    execution_swing_high: float | None = None
    regime: str = "transition"
    winner_basket_aligned: bool = False
    broker_min_stop_distance: float = 0.0


class FiboMtfTradePlanner:
    """Route and plan Fibo MTF candidates without broad opportunity blocking."""

    def __init__(
        self,
        *,
        sl_atr_min_multiplier: float = 1.2,
        sl_atr_max_multiplier: float = 4.0,
        sl_atr_cap_multiplier: float = 8.0,
        probe_size_multiplier: float = 0.30,
        base_live_min_confidence: float = 35.0,
    ) -> None:
        self.sl_atr_min_multiplier = float(sl_atr_min_multiplier)
        self.sl_atr_max_multiplier = float(sl_atr_max_multiplier)
        self.sl_atr_cap_multiplier = float(sl_atr_cap_multiplier)
        self.probe_size_multiplier = float(probe_size_multiplier)
        self.base_live_min_confidence = float(base_live_min_confidence)

    def plan(self, candidate: FiboMtfPlannerInput) -> FiboMtfRouteDecision:
        side = str(candidate.direction or "").strip().lower()
        if side not in {"long", "buy", "short", "sell"}:
            return self._observe(["invalid_direction"], intent="learn_context")
        side = "long" if side in {"long", "buy"} else "short"
        atr = max(float(candidate.atr or 0.0), 0.01)
        entry = self._entry_price(candidate)
        raw_sl_distance = abs(float(candidate.raw_stop_loss or entry) - entry)
        cap_distance = self.sl_atr_cap_multiplier * atr
        tf = str(candidate.timeframe or "").strip().lower()
        ratio_zone = str(candidate.ratio_zone or "").strip().lower()
        impulse_state = str(candidate.impulse_state or "").strip().lower()
        has_trigger = bool(
            candidate.reclaim_confirmed
            or candidate.sweep_confirmed
            or candidate.impulse_birth_confirmed
        )
        impulse_active = impulse_state in IMPULSE_ACTIVE_STATES
        ratio_quality = ratio_zone in GOLDEN_RATIO_ZONES
        reasons: list[str] = []

        if ratio_zone == "other" and impulse_state == "idle" and not has_trigger:
            observe_reasons = ["idle_no_trigger", "ratio_other"]
            if raw_sl_distance > cap_distance and not self._has_execution_anchor(candidate, side):
                observe_reasons.append("sl_distance_over_cap")
            return self._observe(observe_reasons, intent="learn_context", metadata={
                "raw_sl_distance": round(raw_sl_distance, 4),
                "cap_distance": round(cap_distance, 4),
                "timeframe": tf,
            })

        if raw_sl_distance > cap_distance and not self._has_execution_anchor(candidate, side):
            return self._observe(["sl_distance_over_cap"], intent="learn_context", metadata={
                "raw_sl_distance": round(raw_sl_distance, 4),
                "cap_distance": round(cap_distance, 4),
                "timeframe": tf,
            })

        if candidate.winner_basket_aligned and impulse_state == "mature" and has_trigger:
            route = "runner_add"
        elif ratio_quality and impulse_active and candidate.correction_end_confirmed and has_trigger and candidate.confidence >= self.base_live_min_confidence:
            route = "base_live"
        elif has_trigger or candidate.impulse_birth_confirmed:
            route = "probe"
            if ratio_zone == "other":
                reasons.append("other_zone_triggered_probe")
            if impulse_state == "idle":
                reasons.append("idle_with_trigger_probe")
        else:
            return self._observe(["await_execution_trigger"], intent="learn_context")

        plan = self._build_trade_plan(candidate, side=side, route=route, entry=entry, atr=atr)
        if plan is None:
            return self._observe(["execution_sl_unavailable"], intent="learn_context")
        return FiboMtfRouteDecision(
            route=route,
            intent="execute_tactical_plan",
            reasons=reasons or [f"{route}_criteria_met"],
            trade_plan=plan,
            metadata={"ratio_zone": ratio_zone, "impulse_state": impulse_state, "timeframe": tf},
        )

    def _build_trade_plan(self, candidate: FiboMtfPlannerInput, *, side: str, route: str, entry: float, atr: float) -> FiboMtfTradePlan | None:
        stop = self._execution_stop(candidate, side=side, entry=entry, atr=atr)
        if stop is None:
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        if route == "probe":
            size = self.probe_size_multiplier
            runner = False
            multiples = [(0.8, 0.70, "tp1_probe"), (1.2, 0.30, "tp2_probe")]
        elif route == "runner_add":
            size = 0.50
            runner = True
            multiples = [(2.0, 1.00, "runner_trail")]
        else:
            size = 1.0
            trend = str(candidate.regime or "").strip().lower() == "trend"
            runner = trend
            multiples = [(1.0, 0.50, "tp1_bank"), (2.0, 0.30, "tp2_bank"), (3.0, 0.20, "runner")] if trend else [(0.8, 0.50, "tp1_chop"), (1.5, 0.50, "tp2_chop")]
        tp_plan = [
            FiboMtfTakeProfit(
                price=round(entry + (risk * mult if side == "long" else -risk * mult), 5),
                fraction=frac,
                label=label,
            )
            for mult, frac, label in multiples
        ]
        return FiboMtfTradePlan(
            route=route,
            entry_type="market",
            entry_price=round(entry, 5),
            stop_loss=round(stop, 5),
            thesis_stop_loss=round(float(candidate.raw_stop_loss or stop), 5),
            size_multiplier=size,
            runner_enabled=runner,
            tp_plan=tp_plan,
            metadata={
                "atr": round(atr, 5),
                "sl_distance_atr": round(abs(entry - stop) / atr, 4),
                "raw_sl_distance_atr": round(abs(float(candidate.raw_stop_loss or entry) - entry) / atr, 4),
            },
        )

    def _execution_stop(self, candidate: FiboMtfPlannerInput, *, side: str, entry: float, atr: float) -> float | None:
        min_dist = max(self.sl_atr_min_multiplier * atr, float(candidate.broker_min_stop_distance or 0.0))
        max_dist = self.sl_atr_max_multiplier * atr
        buffer = max(0.25 * atr, 0.01)
        if side == "long":
            anchors = [x for x in (candidate.execution_swing_low, candidate.raw_stop_loss) if x is not None and float(x) < entry]
            if not anchors:
                return None
            stop = min(float(max(anchors)) - buffer, entry - min_dist)
            if entry - stop > max_dist:
                stop = entry - max_dist
            return stop
        anchors = [x for x in (candidate.execution_swing_high, candidate.raw_stop_loss) if x is not None and float(x) > entry]
        if not anchors:
            return None
        stop = max(float(min(anchors)) + buffer, entry + min_dist)
        if stop - entry > max_dist:
            stop = entry + max_dist
        return stop

    @staticmethod
    def _entry_price(candidate: FiboMtfPlannerInput) -> float:
        current = float(candidate.current_price or 0.0)
        nearest = float(candidate.nearest_level_price or current)
        if current <= 0:
            return nearest
        # Live plan starts from current executable price.  A nearby Fib level may
        # nudge limit geometry only when it is effectively at market.
        if abs(nearest - current) <= max(0.1 * float(candidate.atr or 0.0), 0.01):
            return nearest
        return current

    @staticmethod
    def _has_execution_anchor(candidate: FiboMtfPlannerInput, side: str) -> bool:
        if side == "long":
            return candidate.execution_swing_low is not None
        return candidate.execution_swing_high is not None

    @staticmethod
    def _observe(reasons: list[str], *, intent: str, metadata: dict[str, Any] | None = None) -> FiboMtfRouteDecision:
        return FiboMtfRouteDecision(
            route="observe_only",
            intent=intent,
            reasons=reasons,
            trade_plan=None,
            metadata=dict(metadata or {}),
        )

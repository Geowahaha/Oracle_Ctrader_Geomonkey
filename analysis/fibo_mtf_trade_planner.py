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
    fibo_reclaim_setup: str = ""
    fibo_reclaim_score: float = 0.0
    fibo_cluster_count: int = 0
    dema_reclaim_confirmed: bool = False


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
        cap_distance = self.sl_atr_cap_multiplier * atr
        raw_stop = float(candidate.raw_stop_loss or 0.0)
        raw_sl_missing = raw_stop <= 0.0
        raw_sl_distance = abs(raw_stop - entry) if not raw_sl_missing else cap_distance + 0.0001
        tf = str(candidate.timeframe or "").strip().lower()
        ratio_zone = str(candidate.ratio_zone or "").strip().lower()
        impulse_state = str(candidate.impulse_state or "").strip().lower()
        has_trigger = bool(
            candidate.reclaim_confirmed
            or candidate.sweep_confirmed
            or candidate.impulse_birth_confirmed
        )
        fibo_reclaim_setup = str(candidate.fibo_reclaim_setup or "").strip().lower()
        fibo_reclaim_score = float(candidate.fibo_reclaim_score or 0.0)
        fibo_reclaim_trigger = bool(
            fibo_reclaim_score >= 70.0
            and int(candidate.fibo_cluster_count or 0) >= 2
            and candidate.dema_reclaim_confirmed
            and fibo_reclaim_setup in {"fibo_reclaim_long", "fibo_reclaim_short"}
        )
        has_trigger = bool(has_trigger or fibo_reclaim_trigger)
        impulse_active = impulse_state in IMPULSE_ACTIVE_STATES
        ratio_quality = ratio_zone in GOLDEN_RATIO_ZONES
        reasons: list[str] = []

        if raw_sl_missing and not self._has_execution_anchor(candidate, side):
            return self._observe(["raw_stop_loss_missing", "execution_anchor_missing"], intent="learn_context", metadata={
                "raw_sl_distance": round(raw_sl_distance, 4),
                "cap_distance": round(cap_distance, 4),
                "timeframe": tf,
            })

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
            # Winner-aligned mature impulse is treated as an add-on, not a fresh full-size base entry.
            route = "runner_add"
        elif ratio_quality and impulse_active and candidate.correction_end_confirmed and has_trigger and candidate.confidence >= self.base_live_min_confidence:
            route = "base_live"
        elif has_trigger:
            route = "probe"
            if fibo_reclaim_trigger:
                reasons.append("fibo_reclaim_confluence_probe")
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
            metadata={
                "ratio_zone": ratio_zone,
                "impulse_state": impulse_state,
                "timeframe": tf,
                "fibo_reclaim_setup": fibo_reclaim_setup,
                "fibo_reclaim_score": round(fibo_reclaim_score, 2),
                "fibo_cluster_count": int(candidate.fibo_cluster_count or 0),
            },
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "confirmed", "pass", "passed"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _first_float(raw: dict[str, Any], keys: tuple[str, ...], default: float | None = None) -> float | None:
    for key in keys:
        if key in raw and raw.get(key) is not None:
            try:
                return float(raw.get(key))
            except Exception:
                continue
    return default


def _decision_to_metadata(decision: FiboMtfRouteDecision) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "route": decision.route,
        "intent": decision.intent,
        "reasons": list(decision.reasons or []),
        "metadata": dict(decision.metadata or {}),
        "live_enabled": False,
    }
    plan = decision.trade_plan
    if plan is not None:
        meta["live_enabled"] = True
        meta["trade_plan"] = {
            "route": plan.route,
            "entry_type": plan.entry_type,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "thesis_stop_loss": plan.thesis_stop_loss,
            "size_multiplier": plan.size_multiplier,
            "runner_enabled": plan.runner_enabled,
            "tp_plan": [
                {"price": tp.price, "fraction": tp.fraction, "label": tp.label}
                for tp in list(plan.tp_plan or [])
            ],
            "metadata": dict(plan.metadata or {}),
        }
    return meta


def planner_input_from_signal(signal: Any) -> FiboMtfPlannerInput:
    """Build a planner input from an existing Fibo MTF shadow TradeSignal.

    This adapter intentionally treats existing entry/SL/TP as structural scanner
    telemetry.  The planner may reuse only bounded execution geometry; high-TF
    swing-start SL stays thesis metadata, not broker-live permission.
    """
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    direction = str(getattr(signal, "direction", "") or raw.get("direction") or "").strip().lower()
    entry = _safe_float(getattr(signal, "entry", 0.0) or raw.get("entry"), 0.0)
    current = _safe_float(raw.get("current_price"), entry) or entry
    atr = _safe_float(getattr(signal, "atr", 0.0) or raw.get("atr"), 0.0)
    nearest = _safe_float(raw.get("nearest_level_price"), entry) or entry
    return FiboMtfPlannerInput(
        symbol=str(getattr(signal, "symbol", "XAUUSD") or "XAUUSD"),
        direction=direction,
        timeframe=str(raw.get("tf_label") or getattr(signal, "timeframe", "") or ""),
        current_price=current,
        nearest_level_price=nearest,
        raw_stop_loss=_safe_float(getattr(signal, "stop_loss", 0.0) or raw.get("stop_loss"), current),
        atr=atr,
        ratio_zone=str(raw.get("ratio_zone") or raw.get("nearest_ratio_zone") or ""),
        impulse_state=str(raw.get("impulse_state_name") or raw.get("impulse_state") or ""),
        correction_end_confirmed=_truthy(raw.get("correction_end_confirmed")),
        confidence=_safe_float(getattr(signal, "confidence", 0.0), 0.0),
        reclaim_confirmed=_truthy(raw.get("reclaim_confirmed") or raw.get("reclaim_trigger") or raw.get("flow_reclaim_confirmed")),
        sweep_confirmed=_truthy(raw.get("sweep_confirmed") or raw.get("liquidity_sweep_confirmed") or raw.get("sweep_trigger")),
        impulse_birth_confirmed=_truthy(raw.get("impulse_birth_confirmed") or raw.get("impulse_restart_confirmed")),
        execution_swing_low=_first_float(raw, ("execution_swing_low", "local_swing_low", "recent_swing_low")),
        execution_swing_high=_first_float(raw, ("execution_swing_high", "local_swing_high", "recent_swing_high")),
        regime=str(raw.get("regime") or raw.get("market_regime") or raw.get("trend_regime") or "transition"),
        winner_basket_aligned=_truthy(raw.get("winner_basket_aligned") or raw.get("basket_trend_aligned")),
        broker_min_stop_distance=_safe_float(raw.get("broker_min_stop_distance"), 0.0),
        fibo_reclaim_setup=str(raw.get("fibo_reclaim_setup") or ""),
        fibo_reclaim_score=_safe_float(raw.get("fibo_reclaim_score"), 0.0),
        fibo_cluster_count=int(_safe_float(raw.get("fibo_cluster_count"), 0.0)),
        dema_reclaim_confirmed=_truthy(raw.get("dema_reclaim_confirmed") or raw.get("fibo_reclaim_confirmed")),
    )


def annotate_signal_with_fibo_mtf_plan(signal: Any, planner: FiboMtfTradePlanner | None = None) -> FiboMtfRouteDecision:
    """Attach planner route metadata to a Fibo MTF shadow signal.

    The signal remains shadow telemetry by default.  This function writes
    ``raw_scores.fibo_mtf_trade_planner`` and keeps ``fibo_mtf_live_enabled``
    false unless a later, reviewed live-promotion adapter explicitly clones a
    non-shadow signal.
    """
    planner = planner or FiboMtfTradePlanner()
    decision = planner.plan(planner_input_from_signal(signal))
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    raw["fibo_mtf_trade_planner"] = _decision_to_metadata(decision)
    raw["fibo_mtf_route"] = decision.route
    raw["fibo_mtf_route_intent"] = decision.intent
    raw["fibo_mtf_route_reasons"] = list(decision.reasons or [])
    raw["fibo_mtf_live_enabled"] = False
    raw["fibo_mtf_planner_shadow_only"] = True
    setattr(signal, "raw_scores", raw)
    return decision

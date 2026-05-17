"""XAU execution route classifier.

Pure helper for the Opus 4.7 fake-smart-confidence-stacking fix.
Default integration remains feature-flagged OFF; this module has no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


RETEST_ENTRY = "retest_entry"
BREAKDOWN_CONTINUATION = "breakdown_continuation"
HARVEST_ZONE_PM_ONLY = "harvest_zone_pm_only"
WAIT_RETEST_PLAN = "wait_retest_plan"


@dataclass(frozen=True)
class RouteDecision:
    route: str
    allowed_entry_types: tuple[str, ...]
    leg_count: int
    max_legs: int
    action: str
    size_mult: float = 1.0
    invalidation_policy: str = "normal"
    reasons: tuple[str, ...] = ()


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _as_float(features: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = features.get(key, default)
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _missing_or_idle_impulse(state: Any) -> bool:
    text = str(state or "").strip().lower()
    if not text:
        return True
    return text in {"missing", "missing_candles", "idle", "unknown", "none", "no_data"}


def classify_xau_route(features: Mapping[str, Any]) -> RouteDecision:
    """Classify an XAU opportunity into an execution route.

    The classifier intentionally does not block by side. It converts weak live
    context into safer routes: PM-only or plan-only, while preserving valid short
    retest/continuation opportunities.
    """
    reasons: list[str] = []
    source = str(features.get("source") or "")
    flow_confirmed = _as_bool(features.get("flow_confirmed"))
    impulse_missing = _missing_or_idle_impulse(features.get("impulse_state"))
    near_target = _as_bool(features.get("price_near_liquidity_target"))
    retest_rejection = _as_bool(features.get("retest_rejection"))
    rasg_active = _as_bool(features.get("rasg_active"))
    existing_position = _as_bool(features.get("existing_position"))
    equal_lows_distance_atr = _as_float(features, "equal_lows_distance_atr", 99.0)
    extended_move_atr = _as_float(features, "extended_move_atr", 0.0)

    if rasg_active:
        reasons.append("rasg_active_route_actuator")

    harvest_pressure = (
        near_target
        or equal_lows_distance_atr <= 0.25
        or extended_move_atr >= 1.25
    )
    weak_live_execution = (not flow_confirmed) or impulse_missing

    if harvest_pressure and weak_live_execution:
        reasons.append("harvest_zone_weak_live_execution")
        if existing_position:
            reasons.append("existing_position_pm_only")
        return RouteDecision(
            route=HARVEST_ZONE_PM_ONLY,
            allowed_entry_types=(),
            leg_count=0,
            max_legs=0 if not rasg_active else 1,
            action="pm_only",
            size_mult=0.0,
            invalidation_policy="protect_runner",
            reasons=tuple(reasons),
        )

    if rasg_active:
        # RASG should route safer without killing the directional opportunity.
        if retest_rejection or flow_confirmed:
            route = RETEST_ENTRY
            action = "allow_entry"
            allowed = ("limit",)
            reasons.append("rasg_forced_single_retest_leg")
            invalidation = "tight"
        else:
            route = WAIT_RETEST_PLAN
            action = "plan_only"
            allowed = ()
            reasons.append("rasg_wait_for_retest")
            invalidation = "retest_only"
        return RouteDecision(
            route=route,
            allowed_entry_types=allowed,
            leg_count=1,
            max_legs=1,
            action=action,
            size_mult=0.3,
            invalidation_policy=invalidation,
            reasons=tuple(reasons),
        )

    if weak_live_execution:
        reasons.append("wait_for_live_execution_evidence")
        return RouteDecision(
            route=WAIT_RETEST_PLAN,
            allowed_entry_types=(),
            leg_count=0,
            max_legs=0,
            action="plan_only",
            size_mult=0.0,
            invalidation_policy="retest_only",
            reasons=tuple(reasons),
        )

    if retest_rejection or str(features.get("entry_type") or "").lower() == "limit" or "winner" in source:
        reasons.append("valid_retest_entry")
        return RouteDecision(
            route=RETEST_ENTRY,
            allowed_entry_types=("limit",),
            leg_count=1,
            max_legs=1,
            action="allow_entry",
            invalidation_policy="tight",
            reasons=tuple(reasons),
        )

    reasons.append("fresh_breakdown_continuation")
    return RouteDecision(
        route=BREAKDOWN_CONTINUATION,
        allowed_entry_types=("market", "stop"),
        leg_count=1,
        max_legs=2,
        action="allow_entry",
        invalidation_policy="normal",
        reasons=tuple(reasons),
    )

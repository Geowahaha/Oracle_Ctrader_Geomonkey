"""Confidence composer for XAU route-quality gating.

Pure helper: no broker side effects. It prevents correlated narrative features
from pretending to be independent execution evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import tanh
from typing import Any, Mapping


@dataclass(frozen=True)
class ComposedConfidence:
    final_confidence: float
    base_confidence: float
    additive_total: float
    penalty_total: float
    correlation_penalty: float
    missing_evidence_penalty: float
    proximity_penalty: float
    soft_cap_applied: bool
    passed_execution_gate: bool
    reasons: tuple[str, ...] = ()


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_missing_impulse(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return text in {"idle", "missing", "missing_candles", "unknown", "none", "no_data"}


def _capped_component_sum(components: Mapping[str, Any], cap: float = 3.0) -> float:
    total = 0.0
    for value in components.values():
        v = _as_float(value)
        if v > cap:
            v = cap
        elif v < -cap:
            v = -cap
        total += v
    return total


def _correlation_penalty(components: Mapping[str, Any], features: Mapping[str, Any]) -> tuple[float, list[str]]:
    groups = features.get("correlated_component_groups") or []
    penalty = 0.0
    reasons: list[str] = []
    for group in groups:
        active = [name for name in group if _as_float(components.get(name), 0.0) > 0]
        if len(active) >= 3:
            # First two can be useful; additional correlated narrative items pay penalty.
            penalty += min(4.0, 1.5 + 0.75 * (len(active) - 3))
            reasons.append("correlated_narrative_stack")
    return penalty, reasons


def _missing_evidence_penalty(features: Mapping[str, Any]) -> tuple[float, list[str], bool]:
    penalty = 0.0
    reasons: list[str] = []
    missing_flow = not _as_bool(features.get("flow_confirmed"))
    weak_delta = _as_float(features.get("delta_proxy"), 1.0) <= 0.0
    weak_volume = _as_float(features.get("bar_volume_proxy"), 1.0) <= 0.0
    missing_impulse = _is_missing_impulse(features.get("impulse_state"))
    missing_sharpness = features.get("sharpness_has_data") is False

    if missing_flow or (weak_delta and weak_volume):
        penalty += 3.0
        reasons.append("missing_flow_evidence")
    if missing_impulse:
        penalty += 3.0
        reasons.append("missing_impulse_evidence")
    if missing_sharpness:
        penalty += 1.0
        reasons.append("missing_sharpness_evidence")

    execution_gate = not (missing_flow or missing_impulse)
    return penalty, reasons, execution_gate


def _proximity_penalty(features: Mapping[str, Any]) -> tuple[float, list[str]]:
    if _as_bool(features.get("price_near_liquidity_target")):
        return 3.0, ["near_liquidity_target"]
    distance = _as_float(features.get("equal_lows_distance_atr"), 99.0)
    if distance <= 0.25:
        return 2.0, ["near_equal_lows_highs"]
    return 0.0, []


def _soft_cap(score: float) -> tuple[float, bool]:
    if score <= 80.0:
        return score, False
    # Above 80, compress aggressively so stacked narrative needs live evidence.
    return 80.0 + 10.0 * tanh((score - 80.0) / 20.0), True


def compose_confidence(
    base_confidence: float,
    components: Mapping[str, Any] | None = None,
    features: Mapping[str, Any] | None = None,
) -> ComposedConfidence:
    components = components or {}
    features = features or {}
    reasons: list[str] = []

    base = _as_float(base_confidence)
    additive_total = _capped_component_sum(components)
    corr_penalty, corr_reasons = _correlation_penalty(components, features)
    missing_penalty, missing_reasons, execution_gate = _missing_evidence_penalty(features)
    prox_penalty, prox_reasons = _proximity_penalty(features)
    penalty_total = corr_penalty + missing_penalty + prox_penalty

    reasons.extend(corr_reasons)
    reasons.extend(missing_reasons)
    reasons.extend(prox_reasons)

    raw = base + additive_total - penalty_total
    final, soft_cap_applied = _soft_cap(raw)
    final = max(0.0, min(99.9, round(final, 4)))

    if final >= 80.0 and not execution_gate:
        # High confidence with missing live evidence is exactly the fake-smart case.
        execution_gate = False

    return ComposedConfidence(
        final_confidence=final,
        base_confidence=base,
        additive_total=round(additive_total, 4),
        penalty_total=round(penalty_total, 4),
        correlation_penalty=round(corr_penalty, 4),
        missing_evidence_penalty=round(missing_penalty, 4),
        proximity_penalty=round(prox_penalty, 4),
        soft_cap_applied=soft_cap_applied,
        passed_execution_gate=execution_gate,
        reasons=tuple(reasons),
    )

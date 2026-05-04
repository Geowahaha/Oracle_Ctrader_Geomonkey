"""Feature-flagged XAU impulse guard.

This is the first live consumer of the impulse shadow payload. It is a veto
only: it can suppress a counter-impulse XAU candidate/order dispatch, but it
must not resize, reverse, place, cancel, or modify SL/TP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from analysis.impulse_shadow_log import annotate_xau_impulse_shadow


@dataclass(frozen=True)
class XauImpulseGuardDecision:
    blocked: bool
    reason: str
    payload: dict


def evaluate_xau_impulse_guard(
    signal,
    *,
    config,
    source: str = "",
    stage: str = "candidate",
    logger=None,
    annotate_fn: Callable | None = None,
) -> XauImpulseGuardDecision:
    """Return whether the XAU impulse guard should veto ``signal``.

    The guard is intentionally conservative for the first demo rollout:
    default-off in code, XAU-only, status=ok only, impulse_run only unless env
    widens states, and confidence must meet the configured threshold.
    """
    if not bool(getattr(config, "XAU_IMPULSE_GUARD_ENABLED", False)):
        return XauImpulseGuardDecision(False, "disabled", {})

    symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
    if symbol != "XAUUSD":
        return XauImpulseGuardDecision(False, "not_xau", {})

    raw = _safe_raw(signal)
    payload = raw.get("xau_impulse_shadow")
    if not isinstance(payload, Mapping) or not payload:
        annotate = annotate_fn or annotate_xau_impulse_shadow
        try:
            payload = annotate(signal, source=source, stage=stage, logger=logger)
        except Exception as exc:
            return XauImpulseGuardDecision(False, f"annotation_error:{exc}", {})
        raw = _safe_raw(signal)
    payload = dict(payload or {})

    signal_direction = _norm_direction(getattr(signal, "direction", "") or payload.get("signal_direction", ""))
    status = str(payload.get("status") or "").strip().lower()
    state = str(payload.get("state") or "").strip().lower()
    block_direction = _norm_direction(payload.get("block_direction") or "")
    confidence = _float(payload.get("confidence"), 0.0)
    min_conf = _float(getattr(config, "XAU_IMPULSE_GUARD_MIN_CONFIDENCE", 0.70), 0.70)
    block_states = _state_set(getattr(config, "XAU_IMPULSE_GUARD_BLOCK_STATES", "impulse_run"))

    if status != "ok":
        return XauImpulseGuardDecision(False, f"status:{status or 'unknown'}", payload)
    if state not in block_states:
        return XauImpulseGuardDecision(False, f"state_not_blocked:{state or 'unknown'}", payload)
    if not signal_direction or block_direction != signal_direction:
        return XauImpulseGuardDecision(False, f"direction_ok:block={block_direction or '-'}:signal={signal_direction or '-'}", payload)
    if confidence < min_conf:
        return XauImpulseGuardDecision(False, f"confidence_below:{confidence:.2f}<{min_conf:.2f}", payload)

    reason = (
        f"xau_impulse_guard:state={state}:dir={payload.get('direction','')}:"
        f"block={block_direction}:conf={confidence:.2f}:min={min_conf:.2f}"
    )
    _mark_block(signal, reason, payload)
    _log_block(logger, signal, source, stage, reason, payload)
    return XauImpulseGuardDecision(True, reason, payload)


def _safe_raw(signal) -> dict:
    try:
        return dict(getattr(signal, "raw_scores", {}) or {})
    except Exception:
        return {}


def _mark_block(signal, reason: str, payload: Mapping[str, object]) -> None:
    raw = _safe_raw(signal)
    raw["xau_impulse_guard_blocked"] = True
    raw["xau_impulse_guard_reason"] = reason
    raw["xau_impulse_guard_payload"] = dict(payload or {})
    try:
        signal.raw_scores = raw
    except Exception:
        pass


def _state_set(value: object) -> set[str]:
    if isinstance(value, str):
        parts = value.replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        parts = list(value)
    else:
        parts = ["impulse_run"]
    states = {str(p or "").strip().lower() for p in parts if str(p or "").strip()}
    return states or {"impulse_run"}


def _norm_direction(value: object) -> str:
    token = str(value or "").strip().lower()
    if token in {"buy", "bull", "bullish", "up", "long"}:
        return "long"
    if token in {"sell", "bear", "bearish", "down", "short"}:
        return "short"
    return ""


def _float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _log_block(logger, signal, source: str, stage: str, reason: str, payload: Mapping[str, object]) -> None:
    if logger is None:
        return
    try:
        logger.info(
            "[XAUImpulseGuard] blocked stage=%s source=%s symbol=%s signal=%s state=%s impulse_dir=%s conf=%.2f reason=%s reasons=%s",
            str(stage or ""),
            str(source or ""),
            str(getattr(signal, "symbol", "") or ""),
            str(getattr(signal, "direction", "") or ""),
            str(payload.get("state", "") or ""),
            str(payload.get("direction", "") or ""),
            _float(payload.get("confidence"), 0.0),
            reason,
            ",".join(list(payload.get("reasons", ()) or ())),
        )
    except Exception:
        pass

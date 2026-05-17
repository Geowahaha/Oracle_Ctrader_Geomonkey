"""Shadow logging adapter for the XAU impulse state reader.

This module is deliberately observability-only.  It annotates signal.raw_scores
and emits a compact log line, but it must not block, resize, cancel, place, or
otherwise change trading decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

from analysis.impulse_state import ImpulseStateName, compute_impulse_state


_CANDLE_KEYS = (
    "xau_impulse_candles",
    "impulse_state_candles",
    "recent_candles",
    "xau_recent_candles",
    "candles",
    "bars",
    "ohlc",
)


def annotate_xau_impulse_shadow(
    signal,
    *,
    source: str = "",
    stage: str = "candidate",
    logger=None,
    now_iso: str = "",
) -> dict:
    """Attach XAU impulse-state telemetry to ``signal.raw_scores``.

    The return value is the exact payload written to ``raw_scores`` for XAU
    signals.  For non-XAU symbols, the function is a no-op and does not mutate
    the signal.
    """
    symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
    if symbol != "XAUUSD":
        return {"enabled": False, "reason": "not_xau", "symbol": symbol}

    raw = _safe_raw_scores(signal)
    candles = _extract_candles(raw)
    signal_direction = _norm_direction(getattr(signal, "direction", "") or raw.get("direction") or "")
    family = str(raw.get("strategy_family") or raw.get("family") or "").strip().lower()
    ts = str(now_iso or datetime.now(timezone.utc).isoformat())

    if len(candles) < 8:
        payload = {
            "enabled": True,
            "status": "missing_candles",
            "timestamp": ts,
            "symbol": symbol,
            "source": str(source or ""),
            "family": family,
            "stage": str(stage or "candidate"),
            "signal_direction": signal_direction,
            "signal_confidence": _float(getattr(signal, "confidence", raw.get("confidence", 0.0))),
            "state": ImpulseStateName.IDLE.value,
            "direction": "",
            "confidence": 0.0,
            "reasons": ("missing_candles",),
            "retracement_depth": 0.0,
            "impulse_score": 0,
            "block_direction": "",
            "candles": len(candles),
        }
        _attach(signal, raw, payload)
        _log_payload(logger, payload)
        return payload

    state = compute_impulse_state(
        candles,
        current_direction=signal_direction,
        entry_sharpness=_entry_sharpness(raw),
        higher_tf_bias=_higher_tf_bias(raw),
        prior_impulse_direction=str(raw.get("prior_impulse_direction") or raw.get("xau_prior_impulse_direction") or ""),
        prior_impulse_start=_optional_float(raw.get("prior_impulse_start") or raw.get("xau_prior_impulse_start")),
        prior_impulse_end=_optional_float(raw.get("prior_impulse_end") or raw.get("xau_prior_impulse_end")),
        structure_break_direction=str(raw.get("structure_break_direction") or raw.get("xau_structure_break_direction") or ""),
        retest_rejection_direction=str(raw.get("retest_rejection_direction") or raw.get("xau_retest_rejection_direction") or ""),
    )
    block_direction = ""
    for candidate in ("long", "short"):
        if state.blocks_direction(candidate):
            block_direction = candidate
            break
    payload = {
        "enabled": True,
        "status": "ok",
        "timestamp": ts,
        "symbol": symbol,
        "source": str(source or ""),
        "family": family,
        "stage": str(stage or "candidate"),
        "signal_direction": signal_direction,
        "signal_confidence": _float(getattr(signal, "confidence", raw.get("confidence", 0.0))),
        "state": state.name.value,
        "direction": state.direction,
        "confidence": round(float(state.confidence), 4),
        "reasons": tuple(state.reasons),
        "retracement_depth": round(float(state.retracement_depth), 4),
        "impulse_score": int(state.impulse_score),
        "block_direction": block_direction,
        "candles": len(candles),
    }
    _attach(signal, raw, payload)
    _log_payload(logger, payload)
    return payload


def _safe_raw_scores(signal) -> dict:
    try:
        return dict(getattr(signal, "raw_scores", {}) or {})
    except Exception:
        return {}


def _attach(signal, raw: dict, payload: dict) -> None:
    raw = dict(raw or {})
    raw["xau_impulse_shadow"] = payload
    try:
        signal.raw_scores = raw
    except Exception:
        pass


def _extract_candles(raw: Mapping[str, object]) -> list[dict]:
    for key in _CANDLE_KEYS:
        value = raw.get(key)
        candles = _coerce_candles(value)
        if candles:
            return candles
    nested = raw.get("xau_multi_tf_snapshot")
    if isinstance(nested, Mapping):
        for key in _CANDLE_KEYS:
            candles = _coerce_candles(nested.get(key))
            if candles:
                return candles
    return []


def _coerce_candles(value) -> list[dict]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, Mapping):
            row = dict(item)
            if any(k in row for k in ("open", "high", "low", "close")):
                out.append(row)
    return out


def _entry_sharpness(raw: Mapping[str, object]) -> float:
    for key in ("entry_sharpness_score", "entry_sharpness", "entry_sharpness_composite"):
        if key in raw:
            return _float(raw.get(key))
    return 0.0


def _higher_tf_bias(raw: Mapping[str, object]) -> str:
    mtf = raw.get("xau_multi_tf_snapshot")
    if isinstance(mtf, Mapping):
        side = str(mtf.get("strict_aligned_side") or mtf.get("aligned_side") or "").strip().lower()
        if side in {"long", "short"}:
            return side
    for key in ("higher_tf_bias", "signal_h4_trend", "signal_h1_trend", "trend", "trend_bias"):
        token = str(raw.get(key) or "").strip().lower()
        if token in {"bullish", "long", "up"}:
            return "long"
        if token in {"bearish", "short", "down"}:
            return "short"
    return "neutral"


def _norm_direction(value: object) -> str:
    token = str(value or "").strip().lower()
    if token in {"buy", "bull", "bullish", "up"}:
        return "long"
    if token in {"sell", "bear", "bearish", "down"}:
        return "short"
    if token in {"long", "short"}:
        return token
    return ""


def _float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _optional_float(value: object):
    if value is None or value == "":
        return None
    return _float(value)


def _log_payload(logger, payload: dict) -> None:
    if logger is None:
        return
    try:
        logger.info(
            "[XAUImpulseShadow] stage=%s source=%s family=%s signal=%s state=%s dir=%s conf=%.2f block=%s reasons=%s candles=%s",
            payload.get("stage", ""),
            payload.get("source", ""),
            payload.get("family", ""),
            payload.get("signal_direction", ""),
            payload.get("state", ""),
            payload.get("direction", ""),
            float(payload.get("confidence", 0.0) or 0.0),
            payload.get("block_direction", ""),
            ",".join(list(payload.get("reasons", ()) or ())),
            payload.get("candles", 0),
        )
    except Exception:
        pass

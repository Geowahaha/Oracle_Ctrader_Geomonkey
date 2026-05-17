"""Reversal confirmation stack — multi-signal scoring on top of overlap.

Pure function. Counts how many independent confirmations align with the
overlap's reversal_bias. Output is a score (0-6) the scheduler uses for
size-tilt only — never for blocking.

Per project rules:
- Additive only: layered on top of overlap detector + existing flow features.
- Demo-account safe: never blocks, never raises.
"""
from __future__ import annotations

from typing import Any, Optional

from analysis.m1_reversal_confirmation import detect_engulfing


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def score_reversal(
    *,
    overlap: Optional[dict],
    direction: str,
    m1_candles: Any = None,
    h1_dema_slope: Optional[float] = None,
    delta_proxy: Optional[float] = None,
    depth_imbalance: Optional[float] = None,
    compression_score: Optional[float] = None,
    failed_sweep_wick: Optional[bool] = None,
    overlap_top: Optional[float] = None,
    overlap_bottom: Optional[float] = None,
) -> dict:
    """Return {confirms: int 0-6, details: {...}}.

    confirmation layers:
      1. M1 engulfing in direction (inside overlap)
      2. compression_score < 0.7 (energy compressed)
      3. delta_proxy aligned with direction
      4. depth_imbalance >= 0.6 in direction
      5. h1_dema_slope NOT against direction
      6. failed_sweep_wick = True
    """
    direction = str(direction or "").strip().lower()
    is_long = direction in ("long", "buy")
    is_short = direction in ("short", "sell")
    if not (is_long or is_short):
        return {"confirms": 0, "details": {"reason": "bad_direction"}}

    details: dict = {}
    confirms = 0

    # 1. M1 engulfing
    try:
        entry_mid = None
        if overlap_top is not None and overlap_bottom is not None:
            entry_mid = (overlap_top + overlap_bottom) / 2
        eng = detect_engulfing(direction, m1_candles or [], entry_price=entry_mid)
        details["m1_engulfing"] = eng
        if eng.get("confirmed") is True:
            confirms += 1
    except Exception:
        details["m1_engulfing"] = {"confirmed": None, "reason": "error"}

    # 2. compression
    if compression_score is not None:
        cs = _f(compression_score, 1.0)
        details["compression_score"] = cs
        if 0 < cs < 0.7:
            confirms += 1

    # 3. delta proxy
    if delta_proxy is not None:
        dp = _f(delta_proxy, 0.0)
        details["delta_proxy"] = dp
        if (is_long and dp > 0.05) or (is_short and dp < -0.05):
            confirms += 1

    # 4. depth imbalance
    if depth_imbalance is not None:
        di = _f(depth_imbalance, 0.0)
        details["depth_imbalance"] = di
        # convention: positive = bid-heavy (bullish), negative = ask-heavy
        if (is_long and di >= 0.6) or (is_short and di <= -0.6):
            confirms += 1

    # 5. h1 dema slope not against
    if h1_dema_slope is not None:
        sl = _f(h1_dema_slope, 0.0)
        details["h1_dema_slope"] = sl
        if (is_long and sl >= -0.1) or (is_short and sl <= 0.1):
            confirms += 1

    # 6. failed sweep wick
    if failed_sweep_wick is not None:
        details["failed_sweep_wick"] = bool(failed_sweep_wick)
        if bool(failed_sweep_wick):
            confirms += 1

    details["overlap_present"] = bool(overlap and overlap.get("at_overlap_zone"))
    if overlap:
        details["overlap_quality"] = overlap.get("overlap_quality", 0.0)

    return {"confirms": int(min(6, max(0, confirms))), "details": details}


def size_tilt_from_score(
    confirms: int,
    *,
    overlap_present: bool,
    bias_aligned: bool,
) -> float:
    """Return multiplier in [1.0, 1.30]. Never decreases size."""
    if not overlap_present or not bias_aligned:
        return 1.0
    if confirms >= 4:
        return 1.30
    if confirms >= 2:
        return 1.10
    return 1.0

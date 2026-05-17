"""M1 reversal confirmation — SHADOW logger only, never blocks.

Detects bullish/bearish engulfing on M1 at the entry zone, alongside a few
context tags (distance-to-entry, range filter, body dominance). Result is
attached to signal.raw_scores so we can A/B compare expR(confirmed) vs
expR(not_confirmed) without ever gating a trade.

Per project rules:
- Demo account: must let strategies trade. This module NEVER returns a block.
- Additive-only: this is a *measurement* layered on top; existing entry logic
  is untouched.
"""
from __future__ import annotations

from typing import Any, Optional


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_candle(candle: Any) -> Optional[dict]:
    if candle is None:
        return None
    if isinstance(candle, dict):
        o = _f(candle.get("open", candle.get("o")))
        h = _f(candle.get("high", candle.get("h")))
        l = _f(candle.get("low", candle.get("l")))
        c = _f(candle.get("close", candle.get("c")))
    else:
        o = _f(getattr(candle, "open", None))
        h = _f(getattr(candle, "high", None))
        l = _f(getattr(candle, "low", None))
        c = _f(getattr(candle, "close", None))
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return None
    return {"open": o, "high": h, "low": l, "close": c}


def detect_engulfing(
    direction: str,
    m1_candles: Any,
    *,
    entry_price: Optional[float] = None,
    min_body_ratio: float = 1.0,
    max_distance_pips: float = 30.0,
    pip_size: float = 0.1,
) -> dict:
    """Return shadow-log dict — never raises, never blocks.

    Output keys:
        confirmed: bool | None  (None = insufficient data)
        pattern: "bullish_engulfing" | "bearish_engulfing" | ""
        body_ratio: float       (curr body / prev body)
        distance_pips: float    (|close - entry| / pip_size, -1 if N/A)
        reason: str             (short tag for logs)
    """
    out = {
        "confirmed": None,
        "pattern": "",
        "body_ratio": 0.0,
        "distance_pips": -1.0,
        "reason": "",
    }
    direction = str(direction or "").strip().lower()
    if direction not in ("long", "short", "buy", "sell"):
        out["reason"] = "bad_direction"
        return out
    is_long = direction in ("long", "buy")

    candles: list[dict] = []
    try:
        for c in (m1_candles or [])[-5:]:
            n = _normalize_candle(c)
            if n is not None:
                candles.append(n)
    except TypeError:
        out["reason"] = "candles_not_iterable"
        return out

    if len(candles) < 2:
        out["reason"] = "insufficient_data"
        return out

    prev, curr = candles[-2], candles[-1]
    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(curr["close"] - curr["open"])
    body_ratio = (curr_body / prev_body) if prev_body > 0 else 0.0
    out["body_ratio"] = round(body_ratio, 3)

    if entry_price is not None and entry_price > 0 and pip_size > 0:
        out["distance_pips"] = round(abs(curr["close"] - float(entry_price)) / pip_size, 2)

    prev_bear = prev["close"] < prev["open"]
    prev_bull = prev["close"] > prev["open"]
    curr_bull = curr["close"] > curr["open"]
    curr_bear = curr["close"] < curr["open"]

    body_ok = body_ratio >= min_body_ratio and prev_body > 0 and curr_body > 0
    distance_ok = (
        out["distance_pips"] < 0
        or out["distance_pips"] <= max_distance_pips
    )

    if is_long:
        engulf = (
            prev_bear and curr_bull
            and curr["close"] >= prev["open"]
            and curr["open"] <= prev["close"]
        )
        if engulf and body_ok and distance_ok:
            out["confirmed"] = True
            out["pattern"] = "bullish_engulfing"
            out["reason"] = "ok"
        else:
            out["confirmed"] = False
            out["pattern"] = "bullish_engulfing" if engulf else ""
            out["reason"] = (
                "no_pattern" if not engulf
                else ("body_too_small" if not body_ok else "too_far_from_entry")
            )
    else:
        engulf = (
            prev_bull and curr_bear
            and curr["close"] <= prev["open"]
            and curr["open"] >= prev["close"]
        )
        if engulf and body_ok and distance_ok:
            out["confirmed"] = True
            out["pattern"] = "bearish_engulfing"
            out["reason"] = "ok"
        else:
            out["confirmed"] = False
            out["pattern"] = "bearish_engulfing" if engulf else ""
            out["reason"] = (
                "no_pattern" if not engulf
                else ("body_too_small" if not body_ok else "too_far_from_entry")
            )

    return out


def shadow_log_payload(
    direction: str,
    m1_candles: Any,
    *,
    entry_price: Optional[float] = None,
    session: str = "",
    regime: str = "",
    family: str = "",
) -> dict:
    """Wrap detect_engulfing with context tags for journal/raw_scores."""
    result = detect_engulfing(direction, m1_candles, entry_price=entry_price)
    return {
        "m1_engulfing_confirmed": result["confirmed"],
        "m1_engulfing_pattern": result["pattern"],
        "m1_engulfing_body_ratio": result["body_ratio"],
        "m1_engulfing_distance_pips": result["distance_pips"],
        "m1_engulfing_reason": result["reason"],
        "m1_engulfing_session": str(session or "").strip().lower(),
        "m1_engulfing_regime": str(regime or "").strip().lower(),
        "m1_engulfing_family": str(family or "").strip().lower(),
    }

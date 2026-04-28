"""Price action + volume confirmation at significant S/R.

Detects three high-confidence patterns at a known S/R level:
- rejection_wick: wick pierces level, body closes back inside (with volume)
- breakout_body: bar body engulfs the level (with volume)
- absorption_pin: tight range AT level + volume spike (compression burst)

Used as Layer 5 of reversal_setup_detector. When confirmed alongside
≥3 other layers + HTF align → live market entry trigger.

Pure function. Fail-silent.
"""
from __future__ import annotations

from typing import Any


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _norm(c: Any) -> dict | None:
    if c is None:
        return None
    if isinstance(c, dict):
        o = _f(c.get("open", c.get("o")))
        h = _f(c.get("high", c.get("h")))
        l = _f(c.get("low", c.get("l")))
        cl = _f(c.get("close", c.get("c")))
        v = _f(c.get("volume", c.get("v", 0.0)))
    else:
        o = _f(getattr(c, "open", None))
        h = _f(getattr(c, "high", None))
        l = _f(getattr(c, "low", None))
        cl = _f(getattr(c, "close", None))
        v = _f(getattr(c, "volume", 0.0))
    if h <= 0 or l <= 0 or h < l:
        return None
    return {"open": o, "high": h, "low": l, "close": cl, "volume": v}


def detect_pa_volume_confirm(
    direction: str,
    candles: Any,
    *,
    sr_level: float,
    atr: float,
    volume_lookback: int = 20,
    min_volume_ratio: float = 1.3,
    min_wick_atr_ratio: float = 0.4,
) -> dict:
    """Confirm price action + volume at the given S/R level.

    direction: trade direction we want to enter (long → expect support hold,
               short → expect resistance reject)
    """
    out = {
        "confirmed": False,
        "pattern": "",
        "level": float(sr_level),
        "volume_ratio": 0.0,
        "wick_atr_ratio": 0.0,
        "reason": "no_pattern",
    }
    direction = str(direction or "").strip().lower()
    is_long = direction in ("long", "buy")
    is_short = direction in ("short", "sell")
    if not (is_long or is_short):
        out["reason"] = "bad_direction"
        return out
    if sr_level <= 0 or atr <= 0:
        out["reason"] = "bad_inputs"
        return out

    norm: list[dict] = []
    try:
        for c in (candles or [])[-(volume_lookback + 5):]:
            n = _norm(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        out["reason"] = "candles_not_iterable"
        return out
    if len(norm) < volume_lookback:
        out["reason"] = "insufficient_data"
        return out

    # Volume baseline
    prev = norm[-(volume_lookback + 1):-1] if len(norm) > volume_lookback else norm[:-1]
    avg_vol = sum(c["volume"] for c in prev) / max(len(prev), 1) if prev else 0.0
    cur = norm[-1]
    cur_vol = cur["volume"]
    vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 0.0
    out["volume_ratio"] = round(vol_ratio, 3)

    body_top = max(cur["open"], cur["close"])
    body_bot = min(cur["open"], cur["close"])
    upper_wick = cur["high"] - body_top
    lower_wick = body_bot - cur["low"]

    # Pattern 1: rejection wick
    if is_long:
        # Support test: wick pierces below level, close back above
        wick_pierce = cur["low"] < sr_level and cur["close"] > sr_level
        wick_size = max(0.0, lower_wick)
    else:
        wick_pierce = cur["high"] > sr_level and cur["close"] < sr_level
        wick_size = max(0.0, upper_wick)
    wick_ratio = wick_size / atr if atr > 0 else 0.0
    out["wick_atr_ratio"] = round(wick_ratio, 3)

    if wick_pierce and wick_ratio >= min_wick_atr_ratio:
        if vol_ratio >= min_volume_ratio:
            out["confirmed"] = True
            out["pattern"] = "rejection_wick"
            out["reason"] = "ok"
            return out
        else:
            out["pattern"] = "rejection_wick"
            out["reason"] = "low_volume"

    # Pattern 2: breakout body — body engulfs level
    if is_long:
        # Bullish breakout: body opens below level and closes above
        body_engulf = cur["open"] <= sr_level and cur["close"] > sr_level
    else:
        body_engulf = cur["open"] >= sr_level and cur["close"] < sr_level
    body_size = abs(cur["close"] - cur["open"])
    body_ratio = body_size / atr if atr > 0 else 0.0
    if body_engulf and body_ratio >= 0.6:  # decent body, not doji
        if vol_ratio >= min_volume_ratio:
            out["confirmed"] = True
            out["pattern"] = "breakout_body"
            out["reason"] = "ok"
            out["body_atr_ratio"] = round(body_ratio, 3)
            return out
        else:
            out["pattern"] = "breakout_body"
            out["reason"] = "low_volume"
            out["body_atr_ratio"] = round(body_ratio, 3)

    # Pattern 3: absorption pin — small body AT level with volume spike
    near_level = abs(cur["close"] - sr_level) <= 0.3 * atr
    small_body = body_ratio < 0.3 if atr > 0 else False
    if near_level and small_body and vol_ratio >= max(min_volume_ratio, 1.5):
        out["confirmed"] = True
        out["pattern"] = "absorption_pin"
        out["reason"] = "ok"
        return out

    return out

"""Zone overlap detector — supply/demand polarity flip detection.

Pure functions, no broker/IO. Caller passes M5 candles; detector returns
zones + active overlaps. Designed for both live tagging and historical BT
seeding.

Per project rules:
- Additive only: this is an observation/measurement layer.
- Never blocks: detector returns None or empty when uncertain, never raises.
- Size tilts only UP (handled by scheduler chokepoint, not here).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Zone:
    kind: str           # "red" (supply) | "blue" (demand)
    top: float
    bottom: float
    born_bar: int       # index in input candles
    touches: int = 0
    strength: float = 1.0   # 0..1, decays with touches/age

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(candle: Any) -> Optional[dict]:
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
    if h <= 0 or l <= 0 or h < l:
        return None
    return {"open": o, "high": h, "low": l, "close": c}


def _atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_c = candles[i - 1]["close"]
        h = candles[i]["high"]
        l = candles[i]["low"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def _is_pivot_high(candles: list[dict], i: int, left: int, right: int) -> bool:
    if i - left < 0 or i + right >= len(candles):
        return False
    p = candles[i]["high"]
    for k in range(1, left + 1):
        if candles[i - k]["high"] >= p:
            return False
    for k in range(1, right + 1):
        if candles[i + k]["high"] >= p:
            return False
    return True


def _is_pivot_low(candles: list[dict], i: int, left: int, right: int) -> bool:
    if i - left < 0 or i + right >= len(candles):
        return False
    p = candles[i]["low"]
    for k in range(1, left + 1):
        if candles[i - k]["low"] <= p:
            return False
    for k in range(1, right + 1):
        if candles[i + k]["low"] <= p:
            return False
    return True


def detect_zones(
    candles: Any,
    *,
    pivot_left: int = 2,
    pivot_right: int = 2,
    atr_period: int = 14,
    width_atr_mult: float = 0.5,
    max_age_bars: int = 200,
    max_touches: int = 3,
) -> list[Zone]:
    """Detect active red/blue zones from M5 candles. Returns most-recent first."""
    norm: list[dict] = []
    try:
        for c in candles or []:
            n = _normalize(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        return []
    if len(norm) < (pivot_left + pivot_right + 2):
        return []

    atr = _atr(norm, period=atr_period)
    if atr <= 0:
        return []
    pad = width_atr_mult * atr
    n_bars = len(norm)
    last_idx = n_bars - 1
    zones: list[Zone] = []

    for i in range(pivot_left, n_bars - pivot_right):
        age = last_idx - i
        if age > max_age_bars:
            continue
        if _is_pivot_high(norm, i, pivot_left, pivot_right):
            top = norm[i]["high"]
            lo_window = [norm[j]["low"] for j in range(max(0, i - 5), i)]
            structural_bot = min(lo_window) if lo_window else top - pad
            bottom = max(structural_bot, top - pad)
            z = Zone(kind="red", top=top, bottom=bottom, born_bar=i)
            _score_touches_and_strength(z, norm, last_idx, max_age_bars)
            if z.touches < max_touches:
                # broken if any close beyond top + atr
                broken = any(norm[j]["close"] > top + atr for j in range(i + 1, n_bars))
                if not broken:
                    zones.append(z)
        if _is_pivot_low(norm, i, pivot_left, pivot_right):
            bottom = norm[i]["low"]
            hi_window = [norm[j]["high"] for j in range(max(0, i - 5), i)]
            structural_top = max(hi_window) if hi_window else bottom + pad
            top = min(structural_top, bottom + pad)
            z = Zone(kind="blue", top=top, bottom=bottom, born_bar=i)
            _score_touches_and_strength(z, norm, last_idx, max_age_bars)
            if z.touches < max_touches:
                broken = any(norm[j]["close"] < bottom - atr for j in range(i + 1, n_bars))
                if not broken:
                    zones.append(z)

    zones.sort(key=lambda z: z.born_bar, reverse=True)
    return zones


def _score_touches_and_strength(z: Zone, candles: list[dict], last_idx: int, max_age: int) -> None:
    touches = 0
    for j in range(z.born_bar + 1, len(candles)):
        c = candles[j]
        if c["low"] <= z.top and c["high"] >= z.bottom:
            touches += 1
    z.touches = touches
    age = last_idx - z.born_bar
    age_factor = max(0.0, 1.0 - (age / max_age))
    touch_factor = max(0.0, 1.0 - (touches / 4.0))
    z.strength = round(age_factor * touch_factor, 3)


def overlap_at_price(
    zones: list[Zone],
    price: float,
    *,
    min_quality: float = 0.4,
) -> Optional[dict]:
    """Find best active overlap that contains price. Returns None if none."""
    if not zones or price <= 0:
        return None
    reds = [z for z in zones if z.kind == "red"]
    blues = [z for z in zones if z.kind == "blue"]
    best: Optional[dict] = None

    for r in reds:
        for b in blues:
            lo = max(r.bottom, b.bottom)
            hi = min(r.top, b.top)
            if hi <= lo:
                continue
            if not (lo <= price <= hi):
                continue
            min_h = min(r.height, b.height)
            quality = (hi - lo) / min_h if min_h > 0 else 0.0
            if quality < min_quality:
                continue
            score = quality * 0.5 + (r.strength + b.strength) * 0.25
            payload = {
                "at_overlap_zone": True,
                "overlap_quality": round(quality, 3),
                "overlap_top": round(hi, 5),
                "overlap_bottom": round(lo, 5),
                "overlap_mid": round((hi + lo) / 2, 5),
                "red_zone_age_bars": max(r.born_bar, 0),  # caller can re-derive
                "red_zone_touches": r.touches,
                "red_zone_strength": r.strength,
                "blue_zone_age_bars": max(b.born_bar, 0),
                "blue_zone_touches": b.touches,
                "blue_zone_strength": b.strength,
                "_score": score,
            }
            if best is None or score > best["_score"]:
                best = payload
    if best is not None:
        best.pop("_score", None)
    return best


def compression_score(candles: Any, *, short: int = 10, long: int = 50) -> float:
    """ATR(short) / ATR(long). <0.7 = compressed (energy building)."""
    norm: list[dict] = []
    try:
        for c in candles or []:
            n = _normalize(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        return 0.0
    if len(norm) < long + 2:
        return 0.0
    atr_s = _atr(norm[-(short + 1):], period=short)
    atr_l = _atr(norm, period=long)
    if atr_l <= 0:
        return 0.0
    return round(atr_s / atr_l, 3)


def infer_reversal_bias(candles: Any, lookback: int = 20) -> str:
    """Coarse trend label from recent close drift."""
    norm: list[dict] = []
    try:
        for c in (candles or [])[-lookback:]:
            n = _normalize(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        return "neutral"
    if len(norm) < 5:
        return "neutral"
    drift = norm[-1]["close"] - norm[0]["close"]
    rng = max(c["high"] for c in norm) - min(c["low"] for c in norm)
    if rng <= 0:
        return "neutral"
    ratio = drift / rng
    if ratio < -0.4:
        return "long"   # downtrend → expect reversal up
    if ratio > 0.4:
        return "short"
    return "neutral"

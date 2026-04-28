"""Significant S/R level detector — finds horizontal levels touched ≥N times.

Clusters wicks at similar prices (within tolerance) to identify levels that
the market has respected multiple times. Used by reversal setup detector
(Layer 5) and price-action confirm.

Pure function. Fail-silent. Returns sorted-by-relevance list of levels.
"""
from __future__ import annotations

from typing import Any


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _normalize(c: Any) -> dict | None:
    if c is None:
        return None
    if isinstance(c, dict):
        h = _f(c.get("high", c.get("h")))
        l = _f(c.get("low", c.get("l")))
        cl = _f(c.get("close", c.get("c")))
        o = _f(c.get("open", c.get("o")))
    else:
        h = _f(getattr(c, "high", None))
        l = _f(getattr(c, "low", None))
        cl = _f(getattr(c, "close", None))
        o = _f(getattr(c, "open", None))
    if h <= 0 or l <= 0 or h < l:
        return None
    return {"high": h, "low": l, "close": cl, "open": o}


def _atr_simple(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    w = trs[-period:] if len(trs) >= period else trs
    return sum(w) / len(w) if w else 0.0


def detect_sr_levels(
    candles: Any,
    *,
    lookback: int = 100,
    min_touches: int = 3,
    cluster_atr_mult: float = 0.3,
) -> list[dict]:
    """Return sorted list of significant levels.

    Each entry: {level, touches, last_touch_bar, kind: 'support'|'resistance'|'both', score}
    """
    norm: list[dict] = []
    try:
        for c in (candles or [])[-lookback:]:
            n = _normalize(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        return []
    if len(norm) < 20:
        return []

    atr = _atr_simple(norm)
    if atr <= 0:
        return []
    tol = cluster_atr_mult * atr

    # Collect candidate price points: every wick high (resistance) and wick low (support)
    points: list[tuple[float, int, str]] = []  # (price, bar_index, kind)
    for i, c in enumerate(norm):
        body_top = max(c["open"], c["close"])
        body_bot = min(c["open"], c["close"])
        # upper wick = resistance candidate if wick > 0.3 * body_range
        if c["high"] - body_top > tol * 0.3:
            points.append((c["high"], i, "resistance"))
        if body_bot - c["low"] > tol * 0.3:
            points.append((c["low"], i, "support"))

    if not points:
        return []

    # Cluster by price within tol
    points.sort(key=lambda p: p[0])
    clusters: list[dict] = []
    cur = {"prices": [points[0][0]], "bars": [points[0][1]], "kinds": [points[0][2]]}
    for p, b, k in points[1:]:
        if p - cur["prices"][-1] <= tol:
            cur["prices"].append(p)
            cur["bars"].append(b)
            cur["kinds"].append(k)
        else:
            if len(cur["prices"]) >= min_touches:
                clusters.append(cur)
            cur = {"prices": [p], "bars": [b], "kinds": [k]}
    if len(cur["prices"]) >= min_touches:
        clusters.append(cur)

    last_idx = len(norm) - 1
    out: list[dict] = []
    for cl in clusters:
        level = sum(cl["prices"]) / len(cl["prices"])
        touches = len(cl["prices"])
        last_touch_bar = max(cl["bars"])
        recency = max(0.0, 1.0 - (last_idx - last_touch_bar) / max(lookback, 1))
        n_res = sum(1 for k in cl["kinds"] if k == "resistance")
        n_sup = sum(1 for k in cl["kinds"] if k == "support")
        if n_res > 0 and n_sup > 0:
            kind = "both"  # polarity flip — strongest
            kind_score = 1.5
        elif n_res > 0:
            kind = "resistance"
            kind_score = 1.0
        else:
            kind = "support"
            kind_score = 1.0
        score = touches * recency * kind_score
        out.append({
            "level": round(level, 5),
            "touches": touches,
            "n_resistance_touches": n_res,
            "n_support_touches": n_sup,
            "last_touch_bar": last_touch_bar,
            "bars_since_last_touch": last_idx - last_touch_bar,
            "kind": kind,
            "recency": round(recency, 3),
            "score": round(score, 3),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def find_nearest_sr(
    levels: list[dict],
    price: float,
    *,
    max_distance_atr: float = 1.0,
    atr: float = 0.0,
) -> dict | None:
    """Return the highest-score S/R within max_distance_atr from price."""
    if not levels or price <= 0:
        return None
    if atr <= 0:
        return None
    threshold = max_distance_atr * atr
    candidates = [lvl for lvl in levels if abs(lvl["level"] - price) <= threshold]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["score"], reverse=True)
    out = dict(candidates[0])
    out["distance_to_price"] = round(price - out["level"], 5)
    return out

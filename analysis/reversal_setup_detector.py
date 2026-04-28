"""Reversal setup detector — 5-layer multi-bar pattern scoring.

Detects "reversal forming" before the impulse, using:
  Layer 1: Failed sweep sequence
  Layer 2: Higher-low / lower-high progression
  Layer 3: Compression at overlap zone
  Layer 4: DEMA slope flip
  Layer 5: Price action + volume at significant S/R

Score 0-5. Decision tree:
  score ≥4 + Layer 5 confirmed + HTF aligned → live market entry, size×1.3
  score ≥3 + at overlap zone               → limit entry, size×1.15
  score == 2                              → shadow log only
  score ≤1                                → ignore

Pure function. Fail-silent. Additive — doesn't replace any scanner.
"""
from __future__ import annotations

from typing import Any

from analysis.zone_overlap_detector import (
    detect_zones,
    overlap_at_price,
    compression_score,
    infer_reversal_bias,
)
from analysis.sr_levels_detector import detect_sr_levels, find_nearest_sr
from analysis.price_action_volume_confirm import detect_pa_volume_confirm


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _norm(c: Any) -> dict | None:
    if c is None:
        return None
    if isinstance(c, dict):
        return {
            "open": _f(c.get("open", c.get("o"))),
            "high": _f(c.get("high", c.get("h"))),
            "low": _f(c.get("low", c.get("l"))),
            "close": _f(c.get("close", c.get("c"))),
            "volume": _f(c.get("volume", c.get("v", 0.0))),
        }
    return {
        "open": _f(getattr(c, "open", None)),
        "high": _f(getattr(c, "high", None)),
        "low": _f(getattr(c, "low", None)),
        "close": _f(getattr(c, "close", None)),
        "volume": _f(getattr(c, "volume", 0.0)),
    }


def _atr(c: list[dict], p: int = 14) -> float:
    if len(c) < 2:
        return 0.0
    trs = []
    for i in range(1, len(c)):
        h, l = c[i]["high"], c[i]["low"]
        pc = c[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    w = trs[-p:] if len(trs) >= p else trs
    return sum(w) / len(w) if w else 0.0


def _layer1_failed_sweeps(candles: list[dict], atr: float, direction: str) -> dict:
    """Count failed sweeps in last N bars."""
    if len(candles) < 20 or atr <= 0:
        return {"hits": 0, "score_pts": 0}
    is_long = direction in ("long", "buy")
    window = candles[-30:]
    # for long: failed sweep = wick low < recent low but close above
    # for short: wick high > recent high but close below
    hits = 0
    for i in range(5, len(window)):
        cur = window[i]
        prior = window[max(0, i - 10):i]
        if not prior:
            continue
        if is_long:
            prior_low = min(c["low"] for c in prior)
            if cur["low"] < prior_low and cur["close"] > prior_low:
                if (cur["close"] - cur["low"]) >= 0.4 * atr:
                    hits += 1
        else:
            prior_high = max(c["high"] for c in prior)
            if cur["high"] > prior_high and cur["close"] < prior_high:
                if (cur["high"] - cur["close"]) >= 0.4 * atr:
                    hits += 1
    return {"hits": hits, "score_pts": 1 if hits >= 2 else 0}


def _layer2_swing_progression(candles: list[dict], direction: str) -> dict:
    """Higher-lows for long, lower-highs for short."""
    if len(candles) < 30:
        return {"score_pts": 0, "points": []}
    is_long = direction in ("long", "buy")
    # 3-bar fractals
    pivots: list[tuple[int, float]] = []
    for i in range(2, len(candles) - 2):
        if is_long:
            if (candles[i]["low"] < candles[i - 1]["low"] and
                candles[i]["low"] < candles[i - 2]["low"] and
                candles[i]["low"] < candles[i + 1]["low"] and
                candles[i]["low"] < candles[i + 2]["low"]):
                pivots.append((i, candles[i]["low"]))
        else:
            if (candles[i]["high"] > candles[i - 1]["high"] and
                candles[i]["high"] > candles[i - 2]["high"] and
                candles[i]["high"] > candles[i + 1]["high"] and
                candles[i]["high"] > candles[i + 2]["high"]):
                pivots.append((i, candles[i]["high"]))
    if len(pivots) < 3:
        return {"score_pts": 0, "points": [p[1] for p in pivots]}
    last3 = pivots[-3:]
    if is_long:
        ascending = last3[0][1] < last3[1][1] < last3[2][1]
        return {"score_pts": 1 if ascending else 0, "points": [p[1] for p in last3]}
    descending = last3[0][1] > last3[1][1] > last3[2][1]
    return {"score_pts": 1 if descending else 0, "points": [p[1] for p in last3]}


def _layer3_compression_overlap(candles: list[dict], price: float) -> dict:
    if len(candles) < 60:
        return {"score_pts": 0, "compression": 0.0, "at_overlap": False}
    comp = compression_score(candles)
    zones = detect_zones(candles, width_atr_mult=1.0, max_age_bars=300)
    ov = overlap_at_price(zones, price, min_quality=0.2) if zones else None
    at_ov = ov is not None
    pts = 1 if (0 < comp < 0.7 and at_ov) else 0
    return {
        "score_pts": pts,
        "compression": comp,
        "at_overlap": at_ov,
        "overlap": ov or {},
    }


def _dema(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period * 2:
        return []
    k = 2 / (period + 1)
    e1 = values[0]
    ema1 = [e1]
    for v in values[1:]:
        e1 = v * k + e1 * (1 - k)
        ema1.append(e1)
    e2 = ema1[0]
    ema2 = [e2]
    for v in ema1[1:]:
        e2 = v * k + e2 * (1 - k)
        ema2.append(e2)
    return [2 * a - b for a, b in zip(ema1, ema2)]


def _layer4_dema_flip(candles: list[dict], direction: str) -> dict:
    if len(candles) < 30:
        return {"score_pts": 0, "old_slope": 0.0, "new_slope": 0.0}
    closes = [c["close"] for c in candles]
    dm = _dema(closes, period=14)
    if len(dm) < 8:
        return {"score_pts": 0, "old_slope": 0.0, "new_slope": 0.0}
    old = dm[-6] - dm[-9]
    new = dm[-1] - dm[-4]
    is_long = direction in ("long", "buy")
    flipped = (old < 0 and new > 0) if is_long else (old > 0 and new < 0)
    return {
        "score_pts": 1 if flipped else 0,
        "old_slope": round(old, 5),
        "new_slope": round(new, 5),
    }


def _layer5_pa_volume(candles: list[dict], atr: float, direction: str,
                       sr_levels: list[dict], price: float) -> dict:
    nearest = find_nearest_sr(sr_levels, price, max_distance_atr=1.0, atr=atr)
    if not nearest:
        return {"score_pts": 0, "confirmed": False, "reason": "no_nearby_sr"}
    pa = detect_pa_volume_confirm(
        direction=direction,
        candles=candles,
        sr_level=nearest["level"],
        atr=atr,
    )
    pts = 1 if pa.get("confirmed") else 0
    return {
        "score_pts": pts,
        "confirmed": pa.get("confirmed", False),
        "pattern": pa.get("pattern", ""),
        "level": nearest["level"],
        "level_touches": nearest["touches"],
        "level_kind": nearest["kind"],
        "volume_ratio": pa.get("volume_ratio", 0.0),
        "wick_atr_ratio": pa.get("wick_atr_ratio", 0.0),
        "reason": pa.get("reason", ""),
    }


def evaluate(
    candles: Any,
    *,
    h1_trend: str = "",
    direction_hint: str | None = None,
) -> dict:
    """Run all 5 layers, return scored evaluation + entry plan."""
    out = {
        "score": 0,
        "max_score": 5,
        "bias": "neutral",
        "htf_aligned": None,
        "layers": {},
        "live_entry": False,
        "entry_type": "none",
        "planned_entry": 0.0,
        "planned_sl": 0.0,
        "planned_tp_1r": 0.0,
        "planned_tp_2r": 0.0,
        "decision_reason": "no_data",
    }
    norm: list[dict] = []
    try:
        for c in candles or []:
            n = _norm(c)
            if n is not None and n["high"] > 0:
                norm.append(n)
    except TypeError:
        return out
    if len(norm) < 60:
        out["decision_reason"] = "insufficient_data"
        return out

    bias = direction_hint or infer_reversal_bias(norm)
    if bias not in ("long", "short"):
        out["decision_reason"] = "neutral_bias"
        return out
    out["bias"] = bias

    atr = _atr(norm)
    if atr <= 0:
        out["decision_reason"] = "atr_zero"
        return out
    cur_price = norm[-1]["close"]

    sr_levels = detect_sr_levels(norm, lookback=100, min_touches=3, cluster_atr_mult=0.3)

    l1 = _layer1_failed_sweeps(norm, atr, bias)
    l2 = _layer2_swing_progression(norm, bias)
    l3 = _layer3_compression_overlap(norm, cur_price)
    l4 = _layer4_dema_flip(norm, bias)
    l5 = _layer5_pa_volume(norm, atr, bias, sr_levels, cur_price)

    out["layers"] = {
        "failed_sweep": l1,
        "swing_progression": l2,
        "compression_overlap": l3,
        "dema_flip": l4,
        "pa_volume_confirm": l5,
    }
    score = sum(layer["score_pts"] for layer in out["layers"].values())
    out["score"] = score
    out["sr_levels_top3"] = sr_levels[:3]

    htf = str(h1_trend or "").strip().lower()
    if htf:
        if bias == "long" and htf in ("up", "long", "bull", "bullish"):
            out["htf_aligned"] = True
        elif bias == "short" and htf in ("down", "short", "bear", "bearish"):
            out["htf_aligned"] = True
        else:
            out["htf_aligned"] = False

    # Entry plan
    sr_used = l5.get("level") or 0.0
    is_long = bias == "long"
    if score >= 4 and l5.get("confirmed") and out.get("htf_aligned") is not False:
        out["live_entry"] = True
        out["entry_type"] = "market"
        out["decision_reason"] = "high_conviction_market"
    elif score >= 3 and l3.get("at_overlap"):
        out["entry_type"] = "limit"
        out["decision_reason"] = "medium_conviction_limit"
    elif score >= 2:
        out["entry_type"] = "shadow"
        out["decision_reason"] = "tentative_shadow"
    else:
        out["entry_type"] = "none"
        out["decision_reason"] = f"score_too_low:{score}"

    if out["entry_type"] in ("market", "limit", "shadow"):
        # Plan the trade
        recent_lows = [c["low"] for c in norm[-15:]]
        recent_highs = [c["high"] for c in norm[-15:]]
        if is_long:
            sl = min(recent_lows) - 0.5 * atr
            entry = cur_price
            risk = entry - sl
            tp1 = entry + risk
            tp2 = entry + 2.0 * risk
        else:
            sl = max(recent_highs) + 0.5 * atr
            entry = cur_price
            risk = sl - entry
            tp1 = entry - risk
            tp2 = entry - 2.0 * risk
        out["planned_entry"] = round(entry, 5)
        out["planned_sl"] = round(sl, 5)
        out["planned_tp_1r"] = round(tp1, 5)
        out["planned_tp_2r"] = round(tp2, 5)

    return out

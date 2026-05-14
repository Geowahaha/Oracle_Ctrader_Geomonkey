"""Fibonacci confluence + DEMA reclaim scoring for XAU tactical opportunities.

Pure, side-effect-free helpers.  This is execution-grade telemetry, not a direct
live-dispatch switch: callers can use the score/reasons to route shadow probes or
amplify existing lanes while preserving existing safety controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class FiboConfluenceReclaimDecision:
    setup: str
    score: float
    cluster_count: int
    nearest_cluster_distance: float
    dema_aligned: bool
    reclaim_confirmed: bool
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _sf(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _side(direction: Any) -> str:
    raw = str(direction or "").strip().lower()
    if raw in {"long", "buy", "bull", "bullish"}:
        return "long"
    if raw in {"short", "sell", "bear", "bearish"}:
        return "short"
    return ""


def _bar_value(bar: Any, key: str, default: float = 0.0) -> float:
    if isinstance(bar, dict):
        return _sf(bar.get(key), default)
    return _sf(getattr(bar, key, default), default)


def _cluster_stats(current_price: float, atr: float, fib_level_prices: Iterable[Any]) -> tuple[int, float, list[float]]:
    levels = sorted({_sf(x) for x in list(fib_level_prices or []) if _sf(x) > 0.0})
    if current_price <= 0 or atr <= 0 or not levels:
        return 0, 999999.0, []
    window = max(1.25 * atr, current_price * 0.0015, 0.01)
    nearby = [x for x in levels if abs(x - current_price) <= window]
    if not nearby:
        return 0, min(abs(x - current_price) for x in levels), []
    return len(nearby), min(abs(x - current_price) for x in nearby), nearby


def _dema_aligned(side: str, current_price: float, dema: float, dema_previous: float, atr: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if current_price <= 0 or dema <= 0 or side not in {"long", "short"}:
        return False, ["dema_missing"]
    proximity = abs(current_price - dema) <= max(0.35 * atr, current_price * 0.001, 0.01)
    slope_up = dema_previous <= 0 or dema >= dema_previous
    slope_down = dema_previous <= 0 or dema <= dema_previous
    if side == "long":
        if current_price >= dema - max(0.35 * atr, 0.01) and (slope_up or proximity):
            reasons.append("dema_reclaim")
            return True, reasons
        return False, ["below_dema"]
    if current_price <= dema + max(0.35 * atr, 0.01) and (slope_down or proximity):
        reasons.append("dema_reclaim")
        return True, reasons
    return False, ["above_dema"]


def _reclaim_confirmed(side: str, current_price: float, dema: float, recent_bars: Iterable[Any]) -> tuple[bool, list[str], list[str]]:
    bars = list(recent_bars or [])
    if not bars or side not in {"long", "short"}:
        return False, [], ["bars_missing"]
    last = bars[-1]
    close = _bar_value(last, "close", current_price)
    open_ = _bar_value(last, "open", close)
    high = _bar_value(last, "high", close)
    low = _bar_value(last, "low", close)
    prev_close = _bar_value(bars[-2], "close", close) if len(bars) >= 2 else close
    rng = max(high - low, 0.0001)
    lower_wick = max(min(open_, close) - low, 0.0)
    upper_wick = max(high - max(open_, close), 0.0)
    reasons: list[str] = []
    risks: list[str] = []
    if side == "long":
        prior_reclaimed = any(_bar_value(b, "close", 0.0) >= dema for b in bars[:-1]) if dema > 0 else False
        retest_hold = prior_reclaimed and low <= dema and close >= low + 0.18 * rng and close >= dema - max(0.35 * rng, 0.01)
        reclaimed = close >= dema or close > prev_close or retest_hold
        rejection = lower_wick / rng >= 0.30 or close > open_ or retest_hold
        if reclaimed and rejection:
            reasons.append("structure_reclaim")
            if retest_hold:
                reasons.append("retest_hold")
            return True, reasons, risks
        risks.append("no_long_reclaim")
        return False, reasons, risks
    reclaimed = close <= dema or close < prev_close
    rejection = upper_wick / rng >= 0.30 or close < open_
    if reclaimed and rejection:
        reasons.append("structure_reclaim")
        return True, reasons, risks
    risks.append("no_short_reclaim")
    return False, reasons, risks


def evaluate_fibo_confluence_reclaim(
    *,
    direction: Any,
    current_price: float,
    atr: float,
    fib_level_prices: Iterable[Any],
    dema: float = 0.0,
    dema_previous: float = 0.0,
    recent_bars: Iterable[Any] = (),
) -> FiboConfluenceReclaimDecision:
    side = _side(direction)
    price = _sf(current_price)
    atr_v = max(_sf(atr), 0.01)
    cluster_count, nearest_dist, cluster_levels = _cluster_stats(price, atr_v, fib_level_prices)
    dema_ok, dema_notes = _dema_aligned(side, price, _sf(dema), _sf(dema_previous), atr_v)
    reclaim_ok, reclaim_reasons, reclaim_risks = _reclaim_confirmed(side, price, _sf(dema), recent_bars)

    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0
    if side:
        score += 10.0
    else:
        risks.append("invalid_direction")
    if cluster_count >= 3:
        score += 35.0
        reasons.append("fib_cluster")
    elif cluster_count >= 2:
        score += 25.0
        reasons.append("thin_fib_cluster")
    elif cluster_count == 1:
        score += 12.0
        reasons.append("single_fib_level")
    else:
        risks.append("fib_cluster_missing")
    if dema_ok:
        score += 25.0
        reasons.extend(dema_notes)
    else:
        risks.extend(dema_notes)
    if reclaim_ok:
        score += 25.0
        reasons.extend(reclaim_reasons)
    else:
        risks.extend(reclaim_risks)
    if nearest_dist <= 0.25 * atr_v:
        score += 5.0
        reasons.append("at_cluster_center")

    if score >= 70 and dema_ok and reclaim_ok and cluster_count >= 2:
        setup = f"fibo_reclaim_{side}"
    elif cluster_count >= 2 and not (dema_ok and reclaim_ok):
        setup = "failed_reclaim"
    else:
        setup = "observe_only"
    return FiboConfluenceReclaimDecision(
        setup=setup,
        score=round(min(100.0, max(0.0, score)), 2),
        cluster_count=cluster_count,
        nearest_cluster_distance=round(nearest_dist, 5),
        dema_aligned=bool(dema_ok),
        reclaim_confirmed=bool(reclaim_ok),
        reasons=list(dict.fromkeys(reasons)),
        risks=list(dict.fromkeys(risks)),
        metadata={
            "cluster_levels": [round(x, 5) for x in cluster_levels],
            "dema": round(_sf(dema), 5),
            "dema_previous": round(_sf(dema_previous), 5),
            "atr": round(atr_v, 5),
        },
    )

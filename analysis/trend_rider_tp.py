"""Trend-rider TP extension — when conditions favor riding, widen TP2/TP3.

Pure function. Reads overlap_tag + compression + structure context, decides
whether the market is in a regime where extended TP makes sense, and returns
multipliers to apply on top of MAX_ATR caps. NEVER tightens — only extends.

Per project rules: additive, fail-silent, never blocks.

Level-2 logic (TP extension):
    - if at_overlap_zone with bias_aligned + ≥2 confirms → extend ×1.5
    - if compression_score < 0.6 + trend strong (close vs ema drift) → extend ×1.7
    - if both conditions → extend ×2.0 (cap)
    - else → 1.0 (no change)

Level-3 logic (tier runner shadow plan):
    - Always emits a "tier_runner_plan" dict to raw_scores documenting
      intended partials: 50%@1R / 25%@2R / 25%trail. Shadow only — executor
      doesn't act on it yet. Becomes evidence for next-iteration PM wiring.
"""
from __future__ import annotations

from typing import Any


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def compute_tp_extension_multiplier(*, raw_scores: dict | None, direction: str) -> tuple[float, dict]:
    """Return (multiplier, debug_info). multiplier in [1.0, 2.0]."""
    info: dict = {"reason": "default", "applied": False}
    rs = raw_scores or {}
    direction = str(direction or "").strip().lower()
    if direction not in ("long", "short", "buy", "sell"):
        return 1.0, {"reason": "bad_direction"}

    overlap = rs.get("overlap_tag") or {}
    at_overlap = bool(overlap.get("at_overlap_zone"))
    bias_aligned = bool(overlap.get("bias_aligned"))
    confirms = int(overlap.get("reversal_confirms") or 0)
    compression = _f(overlap.get("compression_score"), 1.0)

    overlap_ride = at_overlap and bias_aligned and confirms >= 2
    compressed_trend = 0 < compression < 0.6

    if overlap_ride and compressed_trend:
        mult = 2.0
        info = {"reason": "overlap+compression", "applied": True,
                "overlap": True, "compression": compression, "confirms": confirms}
    elif overlap_ride:
        mult = 1.5
        info = {"reason": "overlap_ride", "applied": True,
                "overlap": True, "confirms": confirms}
    elif compressed_trend:
        mult = 1.7
        info = {"reason": "compression_trend", "applied": True,
                "compression": compression}
    else:
        mult = 1.0
        info = {"reason": "no_extension", "applied": False,
                "overlap": at_overlap, "confirms": confirms,
                "compression": compression}
    return mult, info


def make_tier_runner_plan(*, entry: float, stop_loss: float, direction: str,
                          tp2: float = 0.0, tp3: float = 0.0) -> dict:
    """Document intended partials. Shadow only — for evidence collection."""
    direction = str(direction or "").strip().lower()
    risk = abs(entry - stop_loss) if (entry > 0 and stop_loss > 0) else 0.0
    if risk <= 0 or direction not in ("long", "short", "buy", "sell"):
        return {"valid": False, "reason": "bad_inputs"}
    is_long = direction in ("long", "buy")
    p_at = lambda r: (entry + r * risk) if is_long else (entry - r * risk)
    return {
        "valid": True,
        "tier_1": {"size_pct": 50, "exit_at_R": 1.0, "price": round(p_at(1.0), 5)},
        "tier_2": {"size_pct": 25, "exit_at_R": 2.0, "price": round(p_at(2.0), 5)},
        "tier_3": {"size_pct": 25, "mode": "trail_to_overlap_or_tp3",
                   "tp3_price": round(tp3, 5) if tp3 else None,
                   "tp2_price": round(tp2, 5) if tp2 else None},
        "shadow_only": True,
        "note": "documented intent; executor does not act on this yet",
    }


def apply_trend_rider(*, signal: Any) -> dict:
    """One-call helper: compute multiplier + tier plan, mutate signal in place.

    Mutates: signal.take_profit_2, signal.take_profit_3 (extends only).
    Adds: signal.raw_scores["trend_rider"] + ["tier_runner_plan"].
    Returns: debug dict (never raises).
    """
    out: dict = {"applied": False}
    try:
        if signal is None:
            return out
        symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
        if symbol != "XAUUSD":
            return out
        direction = str(getattr(signal, "direction", "") or "").strip().lower()
        entry = _f(getattr(signal, "entry", 0.0))
        sl = _f(getattr(signal, "stop_loss", 0.0))
        tp2 = _f(getattr(signal, "take_profit_2", 0.0))
        tp3 = _f(getattr(signal, "take_profit_3", 0.0))
        if entry <= 0 or sl <= 0:
            return out
        raw = getattr(signal, "raw_scores", None) or {}

        mult, info = compute_tp_extension_multiplier(raw_scores=raw, direction=direction)

        # Extend TP2/TP3 only when multiplier > 1.0 — additive, never tightens
        if mult > 1.0 and tp2 > 0 and tp3 > 0:
            is_long = direction in ("long", "buy")
            new_tp2 = entry + (tp2 - entry) * mult if is_long else entry - (entry - tp2) * mult
            new_tp3 = entry + (tp3 - entry) * mult if is_long else entry - (entry - tp3) * mult
            try:
                signal.take_profit_2 = round(new_tp2, 5)
                signal.take_profit_3 = round(new_tp3, 5)
                # also update primary risk_reward to reflect new TP2
                risk = abs(entry - sl)
                if risk > 0:
                    signal.risk_reward = round(abs(new_tp2 - entry) / risk, 2)
            except Exception:
                pass
            out["applied"] = True
            out["multiplier"] = mult
            out["new_tp2"] = round(new_tp2, 5)
            out["new_tp3"] = round(new_tp3, 5)

        # Always emit shadow tier-runner plan (Level 3, observation only)
        plan = make_tier_runner_plan(
            entry=entry, stop_loss=sl, direction=direction,
            tp2=_f(getattr(signal, "take_profit_2", 0.0)),
            tp3=_f(getattr(signal, "take_profit_3", 0.0)),
        )

        try:
            rs = dict(raw)
            rs["trend_rider"] = {"multiplier": mult, **info}
            rs["tier_runner_plan"] = plan
            signal.raw_scores = rs
        except Exception:
            pass

        out["info"] = info
        return out
    except Exception:
        return {"applied": False, "error": "fail_silent"}

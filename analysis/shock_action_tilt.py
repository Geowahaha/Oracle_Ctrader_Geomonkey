"""Shock action tilt — NEVER blocks, only adjusts size + SL distance.

Tier table:
   0-30  normal    size×1.0   sl_tighten×1.0   no entry filter
  30-50  elevated  size×1.0   sl_tighten×0.7   no entry filter
  50-70  active    size×0.5   sl_tighten×0.6   require rejection wick
  70-85  high      size×0.3   sl_tighten×0.5   counter-momentum only
  85-100 extreme   size×0.15  sl_tighten×0.4   rejection wick + ≥2 confirms

OPPORTUNITY OVERRIDE (per user rule "โอกาสมาก่อน"):
  When signal carries strong setup evidence (reversal_setup score ≥4
  OR overlap confirmed with reversal_confirms ≥4), the shock penalty
  is HALVED — high-conviction trades pass through with much less
  size reduction. Real edge beats false-positive shock.

Never returns block. Always returns a tilt + tag.
"""
from __future__ import annotations

from typing import Any


_TIERS = [
    {"max": 30,  "name": "normal",   "size_mult": 1.00, "sl_tighten": 1.00, "filter": "none"},
    {"max": 50,  "name": "elevated", "size_mult": 1.00, "sl_tighten": 0.70, "filter": "none"},
    {"max": 70,  "name": "active",   "size_mult": 0.50, "sl_tighten": 0.60, "filter": "rejection_wick"},
    {"max": 85,  "name": "high",     "size_mult": 0.30, "sl_tighten": 0.50, "filter": "counter_momentum"},
    {"max": 101, "name": "extreme",  "size_mult": 0.15, "sl_tighten": 0.40, "filter": "rejection+confirms"},
]


def get_tier(score: float) -> dict:
    """Return the tier dict for a given score."""
    s = max(0.0, min(100.0, float(score)))
    for t in _TIERS:
        if s < t["max"]:
            return dict(t)
    return dict(_TIERS[-1])


def has_strong_opportunity(raw_scores: dict | None) -> dict:
    """Detect high-conviction setup that justifies overriding shock penalty.

    Returns {override: bool, reasons: []}.
    """
    out = {"override": False, "reasons": []}
    if not raw_scores:
        return out
    rs = dict(raw_scores or {})

    # Reversal setup: score ≥4 of 5 + L5 confirmed
    rsetup = rs.get("reversal_setup") or {}
    rs_score = int(rsetup.get("score", 0) or 0)
    layers = rsetup.get("layers", {}) or {}
    pa = layers.get("pa_volume_confirm", {}) if isinstance(layers, dict) else {}
    if rs_score >= 4 and bool(pa.get("confirmed")):
        out["override"] = True
        out["reasons"].append(f"reversal_setup_{rs_score}_pa_confirmed")

    # Overlap with strong reversal stack (≥4 confirms)
    overlap = rs.get("overlap_tag") or {}
    if bool(overlap.get("at_overlap_zone")) and int(overlap.get("reversal_confirms", 0) or 0) >= 4:
        out["override"] = True
        out["reasons"].append("overlap_4plus_confirms")

    # Trend rider with full multiplier (means overlap + compression)
    trider = rs.get("trend_rider") or {}
    if float(trider.get("multiplier", 1.0) or 1.0) >= 2.0:
        out["override"] = True
        out["reasons"].append("trend_rider_2x")

    return out


def apply_shock_tilt(*, signal: Any, shock_score: float, layers: dict | None = None,
                     reasons: list | None = None) -> dict:
    """Mutate signal.raw_scores with shock_v2 tag + apply size/SL tilts.

    NEVER returns a block. Always returns the applied tilt info.
    """
    out = {"applied": False, "tier": "normal", "size_mult": 1.0, "sl_tighten": 1.0,
           "opportunity_override": False, "reasons": list(reasons or [])}
    try:
        if signal is None:
            return out
        raw = getattr(signal, "raw_scores", None)
        if raw is None:
            raw = {}
            try:
                signal.raw_scores = raw
            except Exception:
                return out

        score = max(0.0, min(100.0, float(shock_score or 0.0)))
        tier = get_tier(score)

        opp = has_strong_opportunity(raw)
        if opp["override"]:
            # Halve the penalty: move tilts toward 1.0
            size_mult = (tier["size_mult"] + 1.0) / 2.0
            sl_tighten = (tier["sl_tighten"] + 1.0) / 2.0
            out["opportunity_override"] = True
            out["reasons"].extend([f"opp_override:{r}" for r in opp["reasons"]])
        else:
            size_mult = tier["size_mult"]
            sl_tighten = tier["sl_tighten"]

        out["tier"] = tier["name"]
        out["size_mult"] = round(size_mult, 3)
        out["sl_tighten"] = round(sl_tighten, 3)
        out["applied"] = True

        # Tag raw_scores
        rs = dict(raw)
        rs["shock_v2"] = {
            "score": round(score, 1),
            "tier": tier["name"],
            "filter": tier["filter"],
            "size_mult_applied": out["size_mult"],
            "sl_tighten_applied": out["sl_tighten"],
            "opportunity_override": out["opportunity_override"],
            "layers": dict(layers or {}),
            "reasons": out["reasons"][:10],
        }

        # Apply size mult to existing risk override (only multiply, never add)
        if size_mult < 1.0:
            existing = float(rs.get("ctrader_risk_usd_override", 0.0) or 0.0)
            if existing > 0:
                # Use 0.05 floor to avoid going below feasibility
                rs["ctrader_risk_usd_override"] = round(max(0.05, existing * size_mult), 4)

        # SL tighten: bring SL closer to entry (only when tighten<1.0)
        if sl_tighten < 1.0:
            try:
                entry = float(getattr(signal, "entry", 0.0))
                sl = float(getattr(signal, "stop_loss", 0.0))
                direction = str(getattr(signal, "direction", "") or "").lower()
                if entry > 0 and sl > 0:
                    risk = abs(entry - sl)
                    new_risk = risk * sl_tighten
                    if direction in ("long", "buy"):
                        new_sl = entry - new_risk
                    elif direction in ("short", "sell"):
                        new_sl = entry + new_risk
                    else:
                        new_sl = sl
                    signal.stop_loss = round(new_sl, 5)
            except Exception:
                pass

        signal.raw_scores = rs
        return out
    except Exception:
        return out

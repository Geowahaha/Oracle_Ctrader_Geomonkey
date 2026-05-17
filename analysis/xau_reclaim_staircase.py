"""XAU Behavior V3 reclaim/staircase helpers.

Pure, feature-flag friendly helpers for the post-capitulation red-box -> blue-box
opportunity: flush, base, reclaim, then DEMA-following staircase continuation.

The module is intentionally deterministic and scheduler-independent.  It only
returns decisions/metadata; live order changes must be gated by config flags in
callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any


XAU_RECLAIM_SOURCES = ("xauusd_scheduled", "scalp_xauusd")


@dataclass(frozen=True)
class ReclaimDecision:
    active: bool
    enabled: bool
    shadow_only: bool
    source: str
    direction: str
    phase: str = "none"
    score: float = 0.0
    reason: str = "not_detected"
    bypass_conf_below: bool = False
    winner_partial_override: bool = False
    confidence_bonus: float = 0.0
    risk_mult: float = 1.0
    planned_rr: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": bool(self.active),
            "enabled": bool(self.enabled),
            "shadow_only": bool(self.shadow_only),
            "source": str(self.source),
            "direction": str(self.direction),
            "phase": str(self.phase),
            "score": round(float(self.score), 3),
            "reason": str(self.reason),
            "bypass_conf_below": bool(self.bypass_conf_below),
            "winner_partial_override": bool(self.winner_partial_override),
            "confidence_bonus": round(float(self.confidence_bonus), 3),
            "risk_mult": round(float(self.risk_mult), 4),
            "planned_rr": round(float(self.planned_rr), 4),
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _bool(raw: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "on", "y"}:
                return True
            if value.strip().lower() in {"0", "false", "no", "off", "n"}:
                continue
        if bool(value):
            return True
    return False


def is_xau_reclaim_source(source: str) -> bool:
    src = _norm(source)
    if not src or "fibo" in src:
        return False
    return src in XAU_RECLAIM_SOURCES or src.startswith("xauusd_scheduled:") or src.startswith("scalp_xauusd:")


def planned_rr(entry: float, stop_loss: float, take_profit: float, direction: str) -> float:
    entry_f = _safe_float(entry, 0.0)
    sl_f = _safe_float(stop_loss, 0.0)
    tp_f = _safe_float(take_profit, 0.0)
    side = _norm(direction)
    if entry_f <= 0.0 or sl_f <= 0.0 or tp_f <= 0.0:
        return 0.0
    if side == "long":
        risk = entry_f - sl_f
        reward = tp_f - entry_f
    elif side == "short":
        risk = sl_f - entry_f
        reward = entry_f - tp_f
    else:
        return 0.0
    if risk <= 0.0 or reward <= 0.0:
        return 0.0
    return round(reward / risk, 4)


def _directional_flow_score(raw: Mapping[str, Any], direction: str) -> tuple[float, list[str]]:
    side = _norm(direction)
    reasons: list[str] = []
    delta = _safe_float(raw.get("delta_proxy"), 0.0)
    depth = _safe_float(raw.get("depth_imbalance"), 0.0)
    drift = _safe_float(raw.get("mid_drift_pct"), 0.0)
    tick_up = _safe_float(raw.get("tick_up_ratio"), 0.5)
    vol = _safe_float(raw.get("bar_volume_proxy"), 0.0)
    score = 0.0
    if side == "long":
        if delta >= 0.08:
            score += 12.0; reasons.append("delta_flip_long")
        if depth >= 0.03:
            score += 8.0; reasons.append("depth_support_long")
        if drift >= 0.002:
            score += 8.0; reasons.append("mid_drift_long")
        if tick_up >= 0.58:
            score += 8.0; reasons.append("tick_up_long")
    elif side == "short":
        if delta <= -0.08:
            score += 12.0; reasons.append("delta_flip_short")
        if depth <= -0.03:
            score += 8.0; reasons.append("depth_support_short")
        if drift <= -0.002:
            score += 8.0; reasons.append("mid_drift_short")
        if tick_up <= 0.42:
            score += 8.0; reasons.append("tick_down_short")
    if vol >= 0.38:
        score += 8.0; reasons.append("volume_confirm")
    return score, reasons


def score_reclaim_features(
    raw_scores: Mapping[str, Any] | None,
    *,
    direction: str,
    min_base_bars: int = 4,
    base_compress_ratio: float = 0.70,
) -> tuple[float, str, list[str]]:
    """Return (score, phase, reasons) for post-flush reclaim/staircase.

    Supports explicit scanner tags when present and otherwise builds a conservative
    score from existing Dexter microstructure fields.
    """
    raw = dict(raw_scores or {})
    side = _norm(direction)
    reasons: list[str] = []
    if side not in {"long", "short"}:
        return 0.0, "none", ["direction_missing"]

    explicit_score = raw.get("xau_reclaim_v3_score")
    explicit_phase = _norm(raw.get("xau_reclaim_v3_phase"))
    if explicit_score is not None and bool(raw.get("xau_reclaim_v3_explicit_trusted")):
        score = max(0.0, min(100.0, _safe_float(explicit_score, 0.0)))
        phase = str(explicit_phase or "explicit")
        return score, phase, ["explicit_score_trusted"]

    prior_dir = _norm(raw.get("prior_impulse_direction") or raw.get("prior_trend_direction") or raw.get("previous_trend"))
    structure_dir = _norm(raw.get("structure_break_direction") or raw.get("breakout_direction") or raw.get("reclaim_direction"))
    retest_dir = _norm(raw.get("retest_rejection_direction") or raw.get("retest_direction"))
    impulse_state = _norm(raw.get("impulse_state") or raw.get("xau_impulse_state") or raw.get("wave_state"))

    score = 0.0
    phase = "none"

    if prior_dir and prior_dir != side:
        score += 12.0; reasons.append("prior_opposite_impulse")
    if structure_dir == side:
        score += 16.0; reasons.append("structure_break_reclaim")
        phase = "base_reclaim"
    if retest_dir == side:
        score += 10.0; reasons.append("retest_rejection")
        phase = "staircase" if phase == "base_reclaim" else "base_reclaim"
    if impulse_state in {"reversal", "resume", "impulse_start", "impulse_run"}:
        score += 8.0; reasons.append(f"impulse_state:{impulse_state}")
        if impulse_state == "reversal":
            phase = "base_reclaim"
        elif impulse_state in {"resume", "impulse_run", "impulse_start"} and phase != "base_reclaim":
            phase = "staircase"

    base_bars = int(_safe_float(raw.get("base_bars") or raw.get("xau_reclaim_base_bars"), 0.0))
    compress = _safe_float(raw.get("base_compression_ratio") or raw.get("atr5_atr14_ratio"), 999.0)
    if base_bars >= max(1, int(min_base_bars)):
        score += 8.0; reasons.append("base_min_bars")
    if compress <= float(base_compress_ratio):
        score += 10.0; reasons.append("base_compression")

    if _bool(raw, "dema_reclaim", "dema14_reclaim", "close_above_dema14", "xau_reclaim_dema_reclaim"):
        score += 12.0; reasons.append("dema_reclaim")
        phase = "base_reclaim" if phase == "none" else phase
    if _bool(raw, "dema_hold", "dema14_hold", "xau_reclaim_dema_hold"):
        score += 8.0; reasons.append("dema_hold")
        if phase == "none":
            phase = "staircase"

    if _bool(raw, "higher_high_higher_low", "hh_hl", "staircase_leg", "xau_reclaim_staircase_leg"):
        score += 12.0; reasons.append("hh_hl_staircase")
        phase = "staircase"

    flow_score, flow_reasons = _directional_flow_score(raw, side)
    score += flow_score
    reasons.extend(flow_reasons)

    rejection = _safe_float(raw.get("rejection_ratio"), 0.0)
    if 0.0 < rejection <= 0.25:
        score += 4.0; reasons.append("controlled_rejection")

    if phase == "none" and score >= 50.0:
        phase = "base_reclaim"
    return max(0.0, min(100.0, score)), phase, reasons or ["no_reclaim_features"]


def tf_alignment_multiplier(
    raw_scores: Mapping[str, Any] | None,
    *,
    max_mult: float = 1.75,
) -> float:
    raw = dict(raw_scores or {})
    mult = 1.0
    if _bool(raw, "m15_trend_agree", "xau_m15_trend_agree"):
        mult *= 1.20
    if _bool(raw, "h1_trend_agree", "xau_h1_trend_agree"):
        mult *= 1.25
    if _bool(raw, "h4_not_opposing", "xau_h4_not_opposing"):
        mult *= 1.15
    if _bool(raw, "dema_hold", "dema14_hold", "xau_reclaim_dema_hold"):
        mult *= 1.10
    return round(max(1.0, min(float(max_mult or 1.0), mult)), 4)


def decision_from_signal(
    signal: object,
    *,
    source: str,
    enabled: bool = False,
    shadow_only: bool = True,
    min_score: float = 62.0,
    confidence_bonus: float = 2.5,
    max_risk_mult: float = 1.75,
    min_rr: float = 3.0,
    winner_override: bool = True,
    base_compress_ratio: float = 0.70,
    base_min_bars: int = 4,
) -> ReclaimDecision:
    src = _norm(source)
    if not is_xau_reclaim_source(src):
        return ReclaimDecision(False, bool(enabled), bool(shadow_only), src, "", reason="source_not_in_scope")
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    direction = _norm(getattr(signal, "direction", "") or raw.get("direction"))
    if direction not in {"long", "short"}:
        return ReclaimDecision(False, bool(enabled), bool(shadow_only), src, direction, reason="direction_missing")

    score, phase, reasons = score_reclaim_features(
        raw,
        direction=direction,
        min_base_bars=base_min_bars,
        base_compress_ratio=base_compress_ratio,
    )
    active = score >= float(min_score or 0.0) and phase in {"base_reclaim", "staircase", "reversal"}
    rr = planned_rr(
        _safe_float(getattr(signal, "entry", raw.get("entry", 0.0)), 0.0),
        _safe_float(getattr(signal, "stop_loss", raw.get("stop_loss", 0.0)), 0.0),
        _safe_float(getattr(signal, "take_profit_1", raw.get("take_profit_1", raw.get("take_profit", 0.0))), 0.0),
        direction,
    )
    rr_ok = rr <= 0.0 or rr >= float(min_rr or 0.0)
    live_allowed = bool(enabled) and not bool(shadow_only) and active and rr_ok
    base = src.split(":", 1)[0]
    bypass = bool(live_allowed and base == "xauusd_scheduled" and phase in {"base_reclaim", "reversal", "staircase"})
    winner = bool(
        live_allowed
        and bool(winner_override)
        and src == "scalp_xauusd:winner"
        and phase == "staircase"
        and (
            "dema_hold" in reasons
            or "hh_hl_staircase" in reasons
            or _bool(raw, "dema_hold", "dema14_hold", "xau_reclaim_dema_hold", "higher_high_higher_low", "hh_hl", "staircase_leg")
        )
        and (
            any(r.startswith("delta_flip") or r.startswith("tick_") for r in reasons)
            or (direction == "long" and (_safe_float(raw.get("delta_proxy"), 0.0) >= 0.08 or _safe_float(raw.get("tick_up_ratio"), 0.5) >= 0.58))
            or (direction == "short" and (_safe_float(raw.get("delta_proxy"), 0.0) <= -0.08 or _safe_float(raw.get("tick_up_ratio"), 0.5) <= 0.42))
        )
    )
    bonus = float(confidence_bonus or 0.0) if live_allowed and base == "scalp_xauusd" else 0.0
    risk_mult = tf_alignment_multiplier(raw, max_mult=max_risk_mult) if live_allowed else 1.0
    reason = ";".join(reasons[:8])
    if active and not rr_ok:
        reason = f"rr_below_min:{rr:.2f}<{float(min_rr or 0.0):.2f};{reason}"
    elif active and bool(shadow_only):
        reason = f"shadow_only;{reason}"
    elif active and not bool(enabled):
        reason = f"disabled;{reason}"
    return ReclaimDecision(
        active=active,
        enabled=bool(enabled),
        shadow_only=bool(shadow_only),
        source=src,
        direction=direction,
        phase=phase,
        score=score,
        reason=reason,
        bypass_conf_below=bypass,
        winner_partial_override=winner,
        confidence_bonus=bonus,
        risk_mult=risk_mult,
        planned_rr=rr,
    )


def apply_confidence_bonus(signal: object, decision: ReclaimDecision, *, cap: float = 85.0) -> float:
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    current = _safe_float(getattr(signal, "confidence", 0.0), 0.0)
    if bool(raw.get("xau_reclaim_v3_conf_applied")):
        return current
    bonus = max(0.0, float(decision.confidence_bonus or 0.0))
    if bonus <= 0.0:
        return current
    updated = min(float(cap or current), current + bonus)
    try:
        setattr(signal, "confidence", updated)
    except Exception:
        pass
    raw["xau_reclaim_v3_conf_applied"] = True
    raw["xau_reclaim_v3_conf_before"] = round(current, 3)
    raw["xau_reclaim_v3_conf_after"] = round(updated, 3)
    try:
        setattr(signal, "raw_scores", raw)
    except Exception:
        pass
    return updated


def apply_risk_multiplier(signal: object, decision: ReclaimDecision, *, default_risk_usd: float | None = None) -> dict[str, Any]:
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    if bool(raw.get("xau_reclaim_v3_risk_applied")):
        return raw
    if "ctrader_risk_usd_override" not in raw:
        raw["xau_reclaim_v3_risk_skipped"] = "missing_family_risk_override"
        try:
            setattr(signal, "raw_scores", raw)
        except Exception:
            pass
        return raw
    mult = max(1.0, float(decision.risk_mult or 1.0))
    before = _safe_float(raw.get("ctrader_risk_usd_override"), _safe_float(default_risk_usd, 0.0))
    if before <= 0.0:
        raw["xau_reclaim_v3_risk_skipped"] = "invalid_family_risk_override"
        try:
            setattr(signal, "raw_scores", raw)
        except Exception:
            pass
        return raw
    after = round(before * mult, 4)
    raw["ctrader_risk_usd_override"] = after
    raw["xau_reclaim_v3_risk_applied"] = True
    raw["xau_reclaim_v3_risk_before_usd"] = round(before, 4)
    raw["xau_reclaim_v3_risk_after_usd"] = after
    raw["xau_reclaim_v3_risk_mult"] = round(mult, 4)
    try:
        setattr(signal, "raw_scores", raw)
    except Exception:
        pass
    return raw

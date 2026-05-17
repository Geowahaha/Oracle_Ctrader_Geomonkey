"""Pure helpers for BTC/ETH crypto redesign v2.

The module is intentionally side-effect free so scheduler integration can stay
feature-flagged and isolated from XAU/Fibo behavior.
"""
from __future__ import annotations

from dataclasses import dataclass

CRYPTO_SYMBOLS = {"BTCUSD", "ETHUSD"}
BTC_FAMILIES = {"btc_weekday_lob_momentum", "btc_scalp_flow_short_sidecar", "btc_scalp_flow_long_sidecar", "btc_scalp_range_repair"}
ETH_FAMILIES = {"eth_weekday_overlap_probe", "eth_weekday_smart_v2"}


@dataclass(frozen=True)
class CryptoRedesignDecision:
    symbol: str
    family: str
    lane: str
    enabled: bool
    shadow_only: bool
    tier: str
    size_multiplier: float
    tp1_rr: float
    runner_rr: float
    soft_mrd_penalty: float = 0.0


def is_crypto_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in CRYPTO_SYMBOLS


def is_crypto_family(symbol: str, family: str) -> bool:
    sym = str(symbol or "").strip().upper()
    fam = str(family or "").strip().lower()
    if sym == "BTCUSD":
        return fam in BTC_FAMILIES or fam.startswith("btc_")
    if sym == "ETHUSD":
        return fam in ETH_FAMILIES or fam.startswith("eth_")
    return False


def confidence_tier(confidence: float) -> tuple[str, float]:
    """Return opportunity-first confidence tier and sizing multiplier."""
    try:
        conf = float(confidence)
    except Exception:
        conf = 0.0
    if conf >= 85.0:
        return "elite_85_plus", 1.6
    if conf >= 75.0:
        return "strong_75_84", 1.3
    if conf >= 70.0:
        return "base_70_74", 1.0
    return "probe_below_70", 0.5


def planned_rr(entry: float, stop_loss: float, take_profit: float, direction: str) -> float:
    try:
        entry_f = float(entry)
        sl_f = float(stop_loss)
        tp_f = float(take_profit)
    except Exception:
        return 0.0
    side = str(direction or "").strip().lower()
    risk = abs(entry_f - sl_f)
    if entry_f <= 0 or sl_f <= 0 or tp_f <= 0 or risk <= 0:
        return 0.0
    if side == "long":
        if not (sl_f < entry_f < tp_f):
            return 0.0
        return max(0.0, (tp_f - entry_f) / risk)
    if side == "short":
        if not (sl_f > entry_f > tp_f):
            return 0.0
        return max(0.0, (entry_f - tp_f) / risk)
    return 0.0


def rr_price_plan(entry: float, stop_loss: float, direction: str, tp1_rr: float, runner_rr: float, tp3_rr: float | None = None) -> tuple[float, float, float]:
    """Compute TP1/runner/TP3 prices from explicit RR values."""
    entry_f = float(entry)
    sl_f = float(stop_loss)
    risk = abs(entry_f - sl_f)
    if risk <= 0:
        return 0.0, 0.0, 0.0
    side = str(direction or "").strip().lower()
    sign = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
    if sign == 0.0:
        return 0.0, 0.0, 0.0
    rr3 = float(tp3_rr if tp3_rr is not None else max(float(runner_rr), float(tp1_rr) + 0.3))
    return (
        entry_f + sign * risk * float(tp1_rr),
        entry_f + sign * risk * float(runner_rr),
        entry_f + sign * risk * rr3,
    )


def decision_for(symbol: str, family: str, confidence: float, *, enabled: bool, shadow_only: bool, tier_sizing_enabled: bool, tp1_rr: float, runner_rr: float, soft_mrd_penalty: float = 0.0) -> CryptoRedesignDecision:
    sym = str(symbol or "").strip().upper()
    fam = str(family or "").strip().lower()
    tier, mult = confidence_tier(float(confidence or 0.0))
    if not tier_sizing_enabled:
        mult = 1.0
    penalty = float(soft_mrd_penalty or 0.0)
    if penalty < 0:
        mult = round(max(0.1, float(mult) * max(0.1, 1.0 + penalty)), 4)
    return CryptoRedesignDecision(
        symbol=sym,
        family=fam,
        lane=f"{sym.lower()}:{fam}:v2",
        enabled=bool(enabled),
        shadow_only=bool(shadow_only),
        tier=tier,
        size_multiplier=float(mult),
        tp1_rr=float(tp1_rr),
        runner_rr=float(runner_rr),
        soft_mrd_penalty=float(soft_mrd_penalty or 0.0),
    )


def metadata(decision: CryptoRedesignDecision, *, entry: float, stop_loss: float, take_profit_1: float, direction: str, blocked_by: str = "") -> dict:
    rr = planned_rr(entry, stop_loss, take_profit_1, direction)
    return {
        "crypto_redesign_v2": True,
        "crypto_redesign_v2_lane": decision.lane,
        "crypto_redesign_v2_enabled": bool(decision.enabled),
        "crypto_redesign_v2_shadow_only": bool(decision.shadow_only),
        "crypto_tier_used": decision.tier,
        "crypto_tier_size_multiplier": round(float(decision.size_multiplier), 4),
        "crypto_tp1_rr_policy": round(float(decision.tp1_rr), 4),
        "crypto_runner_rr_policy": round(float(decision.runner_rr), 4),
        "crypto_planned_rr_tp1": round(float(rr), 4),
        "crypto_blocked_by": str(blocked_by or ""),
        "crypto_soft_mrd_penalty": round(float(decision.soft_mrd_penalty), 4),
    }

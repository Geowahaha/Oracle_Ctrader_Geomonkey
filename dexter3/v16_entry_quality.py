"""V1.6 Fable entry-quality pro-pack (2026-07-09 live audit).

Layers (all V1.6-only; Grok bypasses entirely via caller):
  1. MCP health pause — no new entries while consecutive MCP errors high
  2. Min leader_score — cut coin-flip HUNT participation
  3. Chase hard-block — size-down is not enough for proven-loss bucket
  4. Weak-setup hard-skip when score still weak
  5. Smart same-side cool-down after *noise* exits only

Critical owner rule: **A+ / high-quality setups MUST bypass cool-down**.
Cool-down is only a serial-noise filter, never a dumb timer that blocks
elite score / winner+pullback / exceptional setups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Default winner / weak sets mirror shadow_runner V16_* defaults (kept local
# so this module stays importable without circular deps).
DEFAULT_WINNER_SETUPS: frozenset[str] = frozenset(
    {
        "hunt_h1_context",
        "hunt_swing_structure",
        "basket_repair",
        "opening_manager_repair",
    }
)
DEFAULT_WEAK_SETUPS: frozenset[str] = frozenset(
    {
        "hunt_m15_drift",
        "hunt_day_range_tilt",
        "hunt_sweep_reclaim",
    }
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        raw = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class V16EntryQualityConfig:
    """Env-overridable via ``shadow_runner._v16_entry_quality_config_from_env``."""

    enabled: bool = True

    # -- hard quality floors -------------------------------------------------
    min_leader_score: float = 0.18
    chase_hard_block: bool = True
    # V1.7: the measured pullback/winner edge must not be killed by the
    # coarse chase classifier. Anti-chase sizing already scouts the bucket.
    a_plus_bypasses_chase: bool = True
    # Diagnostic/optional hardening. Exact replay on 2026-07-09 showed this
    # stricter mode underperformed current V1.7, so the live default stays off.
    block_chase_bypass_on_aligned_trending: bool = False
    # Chase may pass only if score is exceptional AND (optional) pullback.
    chase_bypass_score: float = 0.35
    chase_bypass_requires_pullback: bool = True

    weak_hard_skip: bool = True
    weak_min_score: float = 0.20
    weak_setups: frozenset[str] = field(default_factory=lambda: DEFAULT_WEAK_SETUPS)
    winner_setups: frozenset[str] = field(default_factory=lambda: DEFAULT_WINNER_SETUPS)

    # -- smart cool-down (noise serial re-entry only) ------------------------
    # Default OFF per committed V1.7 spec (ops/dexter3_xau_v16_loop.ps1 sets
    # DEXTER3_V16_COOLDOWN_ENABLED=0) so bare launches match the launcher.
    cooldown_enabled: bool = False
    cooldown_sec: int = 600  # 10 min after a noise same-side exit
    # Treat exit as "noise" when stall_take (or live_r below this) / loss.
    cooldown_noise_live_r: float = 0.08
    cooldown_on_loss: bool = True
    cooldown_on_stall: bool = True

    # -- A+ cool-down BYPASS (owner: never block good opportunities) ---------
    # Tier 1: elite leader score (non-chase preferred but pullback also ok).
    bypass_score: float = 0.28
    # Tier 2: named winner setup + pullback + solid score.
    bypass_winner_score: float = 0.22
    # Tier 3: exceptional score + pullback even if chase classified.
    bypass_exceptional_score: float = 0.35

    # -- MCP pause -----------------------------------------------------------
    mcp_max_consec_errors: int = 2


def classify_a_plus(
    *,
    leader_score: float,
    setup: str,
    is_chase: bool,
    is_pullback: bool,
    cfg: V16EntryQualityConfig | None = None,
) -> tuple[bool, str]:
    """Return (is_a_plus, reason). A+ bypasses cool-down only — not MCP pause."""
    cfg = cfg or V16EntryQualityConfig()
    ls = float(leader_score)
    setup_s = str(setup or "")

    # Tier 3 first: exceptional + pullback (covers rare high-conviction chase)
    if ls >= cfg.bypass_exceptional_score and is_pullback:
        return True, "exceptional_pullback"

    # Tier 1: elite score, not a pure chase into mature trend
    if ls >= cfg.bypass_score and not is_chase:
        return True, "elite_score"

    # Tier 1b: elite score on a pullback even if chase flag is noisy
    if ls >= cfg.bypass_score and is_pullback:
        return True, "elite_score_pullback"

    # Tier 2: winner family + pullback + solid score
    if setup_s in cfg.winner_setups and is_pullback and ls >= cfg.bypass_winner_score:
        return True, "winner_pullback"

    return False, ""


def is_noise_exit(
    *,
    reason: str,
    live_r: float,
    peak_r: float,
    cfg: V16EntryQualityConfig | None = None,
) -> bool:
    """Whether a close should arm same-side cool-down (serial noise filter)."""
    cfg = cfg or V16EntryQualityConfig()
    rsn = str(reason or "").lower()
    live = float(live_r)
    if cfg.cooldown_on_loss and live < 0.0:
        return True
    if cfg.cooldown_on_stall and "stall" in rsn and live <= cfg.cooldown_noise_live_r:
        return True
    # Micro peak that never became a real edge
    if "stall" in rsn and float(peak_r) < 0.20 and live <= cfg.cooldown_noise_live_r:
        return True
    return False


def record_noise_close(
    state: dict[str, Any],
    *,
    symbol: str,
    side: str,
    reason: str,
    peak_r: float,
    live_r: float,
    now_iso: str,
    cfg: V16EntryQualityConfig | None = None,
) -> dict[str, Any] | None:
    """Persist cool-down stamp when exit is noise. Returns stamp or None."""
    cfg = cfg or V16EntryQualityConfig()
    if not cfg.cooldown_enabled:
        return None
    if not is_noise_exit(reason=reason, live_r=live_r, peak_r=peak_r, cfg=cfg):
        return None
    stamp = {
        "symbol": str(symbol or "").upper(),
        "side": str(side or "").lower(),
        "reason": str(reason or ""),
        "peak_r": round(float(peak_r), 4),
        "live_r": round(float(live_r), 4),
        "ts": now_iso,
    }
    state["v16_entry_cooldown"] = stamp
    return stamp


def evaluate_v16_entry_gate(
    *,
    decision: Any,
    state: dict[str, Any],
    mcp_consec_errors: int = 0,
    now_iso: str | None = None,
    cfg: V16EntryQualityConfig | None = None,
) -> dict[str, Any]:
    """Decide whether a V1.6 live entry is allowed.

    Returns dict:
      allow: bool
      reason: str (machine token)
      a_plus: bool
      a_plus_reason: str
      cooldown_bypassed: bool
      features: dict (for journaling)
    """
    cfg = cfg or V16EntryQualityConfig()
    features: dict[str, Any] = {"enabled": cfg.enabled}
    if not cfg.enabled:
        return {
            "allow": True,
            "reason": "gate_disabled",
            "a_plus": False,
            "a_plus_reason": "",
            "cooldown_bypassed": False,
            "features": features,
        }

    ls = _f(getattr(decision, "leader_score", 0.0), 0.0)
    setup = str(getattr(decision, "setup", "") or "")
    side = str(getattr(decision, "side", "") or "").lower()
    symbol = str(getattr(decision, "symbol", "") or "").upper()
    feat = getattr(decision, "features", None) or {}
    if not isinstance(feat, dict):
        feat = {}
    anti_chase = feat.get("anti_chase") or {}
    if not isinstance(anti_chase, dict):
        anti_chase = {}
    is_chase = bool(anti_chase.get("is_chase", False))
    anti_align = str(anti_chase.get("align") or "")
    anti_regime = str(anti_chase.get("regime") or "")
    is_aligned_trending_chase = is_chase and (
        (anti_align == "aligned" and anti_regime == "trending")
        or (not anti_align and not anti_regime)
    )
    is_pullback = bool((feat.get("pullback_gate") or {}).get("is_pullback", False))

    a_plus, a_plus_reason = classify_a_plus(
        leader_score=ls,
        setup=setup,
        is_chase=is_chase,
        is_pullback=is_pullback,
        cfg=cfg,
    )
    features.update(
        {
            "leader_score": ls,
            "setup": setup,
            "side": side,
            "is_chase": is_chase,
            "anti_chase_align": anti_align,
            "anti_chase_regime": anti_regime,
            "is_pullback": is_pullback,
            "is_aligned_trending_chase": is_aligned_trending_chase,
            "a_plus": a_plus,
            "a_plus_reason": a_plus_reason,
        }
    )

    # 1) MCP pause — never bypassed (infra safety)
    if int(mcp_consec_errors) >= int(cfg.mcp_max_consec_errors):
        features["mcp_consec_errors"] = int(mcp_consec_errors)
        return {
            "allow": False,
            "reason": "mcp_unhealthy",
            "a_plus": a_plus,
            "a_plus_reason": a_plus_reason,
            "cooldown_bypassed": False,
            "features": features,
        }

    # 2) Min leader score
    if ls < cfg.min_leader_score:
        return {
            "allow": False,
            "reason": "min_leader_score",
            "a_plus": a_plus,
            "a_plus_reason": a_plus_reason,
            "cooldown_bypassed": False,
            "features": features,
        }

    # 3) Chase hard-block. V1.7 lets A+ pullback/winner setups pass because
    # the live audit showed winner_pullback was being classified as A+ and
    # still blocked here. Size remains controlled upstream by anti-chase.
    if cfg.chase_hard_block and is_chase:
        bucket_bypass_blocked = bool(
            cfg.block_chase_bypass_on_aligned_trending and is_aligned_trending_chase
        )
        if bucket_bypass_blocked:
            features["chase_bypass_blocked_by_bucket"] = "aligned_trending"
        a_plus_chase_ok = bool(cfg.a_plus_bypasses_chase and a_plus and not bucket_bypass_blocked)
        score_chase_ok = ls >= cfg.chase_bypass_score and (
            is_pullback if cfg.chase_bypass_requires_pullback else True
        ) and not bucket_bypass_blocked
        chase_ok = a_plus_chase_ok or score_chase_ok
        if not chase_ok:
            return {
                "allow": False,
                "reason": "chase_hard_block",
                "a_plus": a_plus,
                "a_plus_reason": a_plus_reason,
                "cooldown_bypassed": False,
                "features": features,
            }
        if a_plus_chase_ok and not score_chase_ok:
            features["chase_bypass"] = a_plus_reason

    # 4) Weak setup hard-skip when still weak-score (A+ rare path still allowed)
    if cfg.weak_hard_skip and setup in cfg.weak_setups and ls < cfg.weak_min_score and not a_plus:
        return {
            "allow": False,
            "reason": "weak_setup_hard_skip",
            "a_plus": a_plus,
            "a_plus_reason": a_plus_reason,
            "cooldown_bypassed": False,
            "features": features,
        }

    # 5) Smart same-side cool-down — A+ ALWAYS bypasses
    cooldown_bypassed = False
    if cfg.cooldown_enabled:
        stamp = state.get("v16_entry_cooldown") or {}
        if isinstance(stamp, dict) and stamp:
            stamp_side = str(stamp.get("side") or "").lower()
            stamp_sym = str(stamp.get("symbol") or "").upper()
            stamp_ts = _parse_iso(str(stamp.get("ts") or ""))
            now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
            age_ok = False
            if stamp_ts and now_dt:
                age_ok = (now_dt - stamp_ts).total_seconds() < float(cfg.cooldown_sec)
            same = stamp_side == side and (not stamp_sym or stamp_sym == symbol)
            if same and age_ok:
                features["cooldown_stamp"] = stamp
                if a_plus:
                    cooldown_bypassed = True
                    features["cooldown_bypass"] = a_plus_reason
                else:
                    return {
                        "allow": False,
                        "reason": "same_side_noise_cooldown",
                        "a_plus": a_plus,
                        "a_plus_reason": a_plus_reason,
                        "cooldown_bypassed": False,
                        "features": features,
                    }

    return {
        "allow": True,
        "reason": (
            "pass_a_plus_chase_bypass"
            if features.get("chase_bypass") and not cooldown_bypassed
            else "pass_a_plus_cooldown_bypass"
            if cooldown_bypassed
            else "pass"
        ),
        "a_plus": a_plus,
        "a_plus_reason": a_plus_reason,
        "cooldown_bypassed": cooldown_bypassed,
        "features": features,
    }

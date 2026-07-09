"""Grok_v1.0 — Independent parallel scalping profit lock.

Copy + develop best of:
- V1.0: aggressive flat small-profit lock (resolve early at +0.25 to +0.45R band)
  so profits do not evaporate. "ปิดไว เอากำไรชัวร์"
- V1.6 strengths: peak awareness (only lock after real small peak achieved),
  respects participation-first, anti-chase/pullback sizing already applied upstream,
  uses structure from decision features.

Design rules:
- Parallel scalping mode: close quickly and secure small profits reliably (+0.25 to +0.45R).
- leader_score: ไม่จำเป็นต้องต่ำ — ยิ่งสูงยิ่งดี (stronger signals still benefit from fast secure close).
- Can apply broadly (high-score entries for reliable scalps, plus chase/non-pb as needed).
- COMPLETELY independent from main V1.6 Dragon Ladder / stall_take / hard_take.
- When disabled or not qualifying for scalping path → zero effect on V1.6.
- Separate close reason "grok_v10_small_lock".
- Can be toggled / A/B / run in parallel without touching V1.6 code.

This module can be called from OpeningManager (conditional) or future separate runner.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any

Bar = dict[str, Any]


@dataclass(frozen=True)
class GrokV10Config:
    """Config for Grok_v1.0 micro lock. Separate from OMConfig / BasketConfig."""

    # Core target band for small scalps (user goal: เก็บ +0.25 ถึง +0.45R บ่อย ๆ)
    small_lock_r: float = 0.35

    # Minimum peak that must be seen before we consider banking the small profit.
    # Prevents locking noise before any positive development (V1.6 peak awareness).
    min_peak_to_arm: float = 0.25

    # If live has reached the band, lock it even if it is retracing a bit
    # (prevents the "ให้หายจนเหลือน้อยมาก" problem).
    lock_on_reach_or_better: bool = True

    # Optional: also lock if it had a decent small peak and now live is still >= min_lock_floor
    # (a light safety net so it doesn't go back near zero).
    min_lock_floor: float = 0.22

    # Qualification for Grok_v1.0 parallel scalping path.
    # leader_score: higher is better (stronger signals still get fast secure close).
    # No upper limit on score — we want to scalp small reliable wins on good entries too.
    use_on_high_score: bool = False
    use_on_chase: bool = True
    use_on_non_pullback: bool = True


def is_grok_scalp_candidate(
    leader_score: float,
    is_chase: bool,
    is_pullback: bool,
    cfg: GrokV10Config,
) -> bool:
    """Decide if this entry should use the Grok_v1.0 parallel scalping lock.

    - Higher leader_score is good (ยิ่งสูงยิ่งดี).
    - We deliberately run parallel scalping: close fast for sure small profits
      (+0.25 to +0.45R band) even on strong signals.
    - Still respects chase / non-pullback for additional scalping opportunities.
    - Returns True → use fast Grok lock instead of full V1.6 ladder.
    """
    if cfg.use_on_high_score:
        return True  # Apply scalping lock broadly; higher score = better probability of quick secure win
    if cfg.use_on_chase and is_chase:
        return True
    if cfg.use_on_non_pullback and not is_pullback:
        return True
    return False


def should_grok_v10_small_lock(
    peak_r: float,
    live_r: float,
    cfg: GrokV10Config,
) -> bool:
    """Core lock rule for Grok_v1.0.

    - Must have achieved at least min_peak_to_arm (we saw real positive development).
    - Then bank aggressively once live_r reaches the small target band.
    - Safety: if it had the peak but is evaporating, still lock above min_lock_floor.
    Goal: เก็บ +0.25 ถึง +0.45R ให้บ่อย ๆ โดยไม่ให้หาย
    """
    if peak_r < cfg.min_peak_to_arm:
        return False

    if cfg.lock_on_reach_or_better and live_r >= cfg.small_lock_r:
        return True

    # Light V1.0-style safety: don't let a small winner that touched the band evaporate below floor.
    if live_r >= cfg.min_lock_floor and peak_r >= cfg.min_peak_to_arm:
        # Only trigger if we previously touched the band (simple proxy: peak was meaningfully higher)
        if peak_r >= cfg.small_lock_r * 0.9:  # touched near or above target
            return True

    return False


def grok_v10_close_action(
    peak_r: float,
    live_r: float,
    cfg: GrokV10Config | None = None,
) -> dict[str, Any] | None:
    """Return a close_all action using Grok_v1.0 small-profit rule, or None.

    Independent from V1.6:
    - Does not call ladder_floor, stall_take, or hard_take.
    - Returns its own reason so logs/journals clearly separate the two systems.
    """
    if cfg is None:
        cfg = GrokV10Config()

    if not should_grok_v10_small_lock(peak_r, live_r, cfg):
        return None

    return {
        "action": "close_all",
        "reason": "grok_v10_small_lock",
        "peak_r": round(peak_r, 4),
        "live_r": round(live_r, 4),
        "target_band": f"{cfg.min_peak_to_arm:.2f}-{cfg.small_lock_r:.2f}R",
        "grok_v10": True,
    }


def get_grok_v10_config_from_env() -> GrokV10Config:
    """Parse optional env overrides. Safe — falls back to defaults if invalid."""
    import os

    kw: dict[str, Any] = {}
    for env, field in (
        ("DEXTER3_GROK_V10_SMALL_LOCK_R", "small_lock_r"),
        ("DEXTER3_GROK_V10_MIN_PEAK_ARM", "min_peak_to_arm"),
        ("DEXTER3_GROK_V10_MIN_LOCK_FLOOR", "min_lock_floor"),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                kw[field] = float(raw)
            except ValueError:
                pass  # ignore bad value, keep default

    # Boolean flags for scalping path
    raw_use_high = os.environ.get("DEXTER3_GROK_V10_USE_ON_HIGH_SCORE")
    if raw_use_high is not None:
        kw["use_on_high_score"] = raw_use_high.strip().lower() not in ("0", "false", "no")

    raw_use_chase = os.environ.get("DEXTER3_GROK_V10_USE_ON_CHASE")
    if raw_use_chase is not None:
        kw["use_on_chase"] = raw_use_chase.strip().lower() not in ("0", "false", "no")

    raw_use_non_pb = os.environ.get("DEXTER3_GROK_V10_USE_ON_NON_PULLBACK")
    if raw_use_non_pb is not None:
        kw["use_on_non_pullback"] = raw_use_non_pb.strip().lower() not in ("0", "false", "no")

    return GrokV10Config(**kw)


# =============================================================================
# 100% Independent Grok v1.0 components for parallel operation
# =============================================================================

GROK_LABEL = "dexter3:grok-v1.0:scalper"


class GrokV10OpeningManager:
    """Completely separate OM instance for Grok_v1.0 scalping.

    - Only Grok small-profit lock logic.
    - V1.6 Dragon Ladder, stall_take, hard_take, pyramid are never used.
    - Can be paired with its own state keys (grok_v10_basket_runtime, etc.)
      so monitoring (peak tracking, etc.) is 100% independent from V1.6.
    - General safety (caps, unreliable, smart loss if enabled) still applies.
    """

    def __init__(self, executor=None, journal=None, config=None) -> None:
        from dexter3.opening_manager import OpeningManager

        self._om = OpeningManager(executor, journal, config)

    def evaluate(self, symbol, lane_positions, spot, m5_bars, m15_bars, h1_bars, state):
        state = dict(state) if isinstance(state, dict) else {}
        # V1.7: respect the entry-time classifier. Grok should scalp the
        # lower-quality/chase/non-pullback lane, but not force every high
        # quality pullback winner into a tiny profit lock.
        state.setdefault("is_grok_scalp", True)
        return self._om.evaluate(symbol, lane_positions, spot, m5_bars, m15_bars, h1_bars, state)

"""
WinnerProtectionPolicy — state machine that protects in-flight winners from
premature exit while keeping a hard backstop against giveback decay.

Design principle (revised)
--------------------------
The first cut of this module gated protection at HIGH R (arm=2, lock=3,
trail=5). In live XAU/swarm behavior that proved too late: most winning
trades never lived long enough to cross 2R because the legacy active-defense
path closed them on routine adverse flow first. The real problem we are
solving is "stop good trades from being killed before they have time to
become large winners," not "protect rare 5R+ runners."

This revision lowers the gates so a normal winning XAU trade reaches ARMED
quickly, LOCKED at modest profit, and TRAILING_STRUCT once decisively in
profit. Active defense is still primary at LOW R — RUNNING and ARMED behave
identically (the policy only labels them; no SL move, no extra friction).
This honours the project's "do not BE too early" rule:
  - ARMED is a label only.
  - LOCKED structural floor sits slightly above breakeven (+0.4R), only
    after the trade has already proven itself by reaching +1.5R.
  - TRAILING_STRUCT defers to the executor's existing structural trail
    (ATR / structural pivots) and merely disables active-defense early
    exits so a strong run is not killed by routine micro-adverse flow.

States
------
RUNNING          r_now < arm_r              no override; active defense runs
                                            normally
ARMED            arm_r <= r_now < lock_r    label only; r_peak tracked
                 (or r_peak crossed arm_r)
LOCKED           r_now >= lock_r            SL floor applied; active defense
                 (or r_peak crossed lock_r) close needs higher score (caller
                                            consults locked_close_score_bonus)
TRAILING_STRUCT  r_now >= trail_r           active defense disabled; structural
                 (or r_peak crossed trail_r trail floor at 50% of peak
                 and r_now still >= lock_r)
EMERGENCY        r_peak >= lock_r AND       force close — once a trade earned
                 r_now < emergency_threshold its way past lock_r, do not let
                                            it decay below the threshold
                 emergency_threshold =
                   max(lock_floor_r,
                       r_peak * giveback_ratio)

The `max(lock_floor_r, r_peak * giveback_ratio)` formula is a strict
tightening over a pure-ratio threshold. For small peaks (peak just past
lock_r) the ratio*peak term may underprotect; the lock_floor_r term enforces
a minimum acceptable exit. For large peaks the ratio*peak term dominates
and scales with how far the trade ran.

Asymmetry between TRAILING floor (peak * 0.5) and emergency threshold
(peak * giveback_ratio, default 0.45) is intentional: the SL floor sits a
hair above the emergency threshold so the trade closes via structural SL
on routine pullbacks, with emergency reserved as a "SL didn't get adjusted
in time" backstop.

The caller is expected to respect `allow_active_defense_close` and
`force_close`. `structural_sl_floor_r`, when set, is the minimum R at which
SL can sit (caller ratchets SL to no worse than this R).

No state lives inside the policy object. r_peak is tracked by the caller
(executor) and passed in on every evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class WinnerState(str, Enum):
    RUNNING = "running"
    ARMED = "armed"
    LOCKED = "locked"
    TRAILING_STRUCT = "trailing_struct"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class WinnerProtectionConfig:
    """Tunable thresholds — all in R (risk multiples from entry).

    Defaults are calibrated for live XAU scalp/swarm behavior:
    ARM early so r_peak tracking starts on every meaningful winner,
    LOCK at modest profit (1.5R) with a near-breakeven floor (+0.4R),
    TRAIL at decisive profit (3R) where the executor's structural trail
    takes over.
    """

    arm_r: float = 0.8
    lock_r: float = 1.5
    trail_r: float = 3.0
    lock_floor_r: float = 0.4
    giveback_emergency_ratio: float = 0.45
    locked_active_defense_score_bonus: int = 2

    def __post_init__(self) -> None:
        if not (0.0 < self.arm_r < self.lock_r < self.trail_r):
            raise ValueError(
                f"arm_r ({self.arm_r}) < lock_r ({self.lock_r}) < trail_r "
                f"({self.trail_r}) must hold",
            )
        if not (0.0 <= self.lock_floor_r < self.lock_r):
            raise ValueError(
                f"lock_floor_r ({self.lock_floor_r}) must be < lock_r "
                f"({self.lock_r})",
            )
        if not (0.0 < self.giveback_emergency_ratio < 1.0):
            raise ValueError(
                f"giveback_emergency_ratio ({self.giveback_emergency_ratio}) "
                "must be in (0, 1)",
            )
        if self.locked_active_defense_score_bonus < 0:
            raise ValueError("locked_active_defense_score_bonus must be >= 0")


@dataclass(frozen=True)
class WinnerProtectionInput:
    r_now: float
    r_peak: float
    regime_label: str = ""
    regime_confidence: float = 0.0


@dataclass(frozen=True)
class WinnerProtectionDecision:
    state: WinnerState
    allow_active_defense_close: bool
    allow_active_defense_tighten: bool
    structural_sl_floor_r: Optional[float]
    force_close: bool
    reason: str


def _emergency_threshold(cfg: WinnerProtectionConfig, r_peak: float) -> float:
    """Min R below which a proven winner is force-closed.

    max(lock_floor_r, r_peak * giveback_ratio): the ratio term scales with
    how far the trade ran; the lock_floor_r term backstops cases where the
    ratio would underprotect a barely-locked peak.
    """
    return max(cfg.lock_floor_r, r_peak * cfg.giveback_emergency_ratio)


class WinnerProtectionPolicy:
    """Stateless state-machine evaluator."""

    def __init__(self, cfg: Optional[WinnerProtectionConfig] = None) -> None:
        self._cfg = cfg or WinnerProtectionConfig()

    @property
    def config(self) -> WinnerProtectionConfig:
        return self._cfg

    def locked_close_score_bonus(self) -> int:
        return int(self._cfg.locked_active_defense_score_bonus)

    def decide(self, inp: WinnerProtectionInput) -> WinnerProtectionDecision:
        cfg = self._cfg
        try:
            r_now = float(inp.r_now)
        except (TypeError, ValueError):
            r_now = 0.0
        try:
            r_peak = float(inp.r_peak)
        except (TypeError, ValueError):
            r_peak = 0.0
        if r_peak < r_now:
            r_peak = r_now

        if r_peak >= cfg.lock_r:
            emerg_thr = _emergency_threshold(cfg, r_peak)
            if r_now < emerg_thr:
                return WinnerProtectionDecision(
                    state=WinnerState.EMERGENCY,
                    allow_active_defense_close=True,
                    allow_active_defense_tighten=True,
                    structural_sl_floor_r=None,
                    force_close=True,
                    reason=(
                        f"giveback_emergency:r_peak={r_peak:.2f},"
                        f"r_now={r_now:.2f},thr={emerg_thr:.2f}"
                        f"(ratio={cfg.giveback_emergency_ratio:.2f},"
                        f"floor={cfg.lock_floor_r:.2f})"
                    ),
                )

        if r_now >= cfg.trail_r or (r_peak >= cfg.trail_r and r_now >= cfg.lock_r):
            floor = max(cfg.lock_floor_r, r_peak * 0.5)
            return WinnerProtectionDecision(
                state=WinnerState.TRAILING_STRUCT,
                allow_active_defense_close=False,
                allow_active_defense_tighten=False,
                structural_sl_floor_r=float(floor),
                force_close=False,
                reason=f"trailing_struct:r_now={r_now:.2f},r_peak={r_peak:.2f}",
            )

        if r_now >= cfg.lock_r or r_peak >= cfg.lock_r:
            return WinnerProtectionDecision(
                state=WinnerState.LOCKED,
                allow_active_defense_close=True,
                allow_active_defense_tighten=True,
                structural_sl_floor_r=float(cfg.lock_floor_r),
                force_close=False,
                reason=f"locked:r_now={r_now:.2f},r_peak={r_peak:.2f}",
            )

        if r_now >= cfg.arm_r or r_peak >= cfg.arm_r:
            return WinnerProtectionDecision(
                state=WinnerState.ARMED,
                allow_active_defense_close=True,
                allow_active_defense_tighten=True,
                structural_sl_floor_r=None,
                force_close=False,
                reason=f"armed:r_now={r_now:.2f},r_peak={r_peak:.2f}",
            )

        return WinnerProtectionDecision(
            state=WinnerState.RUNNING,
            allow_active_defense_close=True,
            allow_active_defense_tighten=True,
            structural_sl_floor_r=None,
            force_close=False,
            reason=f"running:r_now={r_now:.2f}",
        )

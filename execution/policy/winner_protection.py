"""
WinnerProtectionPolicy — state machine that protects winning trades from
premature exit while preserving drawdown control.

Design principle
----------------
Active defense is the PRIMARY smart protection at low R. WinnerProtection
only overrides at HIGH R, when the trade has proven itself. This matches
the project's hard-earned lesson: "Do not set flat BE too early; use active
defense as primary smart protection."

States
------
RUNNING          r_now < arm_r                 no override; active defense runs normally
ARMED            arm_r <= r_now < lock_r       recording r_peak; active defense unchanged
LOCKED           r_now >= lock_r               SL floor applied; active defense close needs
                                               higher score (caller consults
                                               locked_close_score_bonus)
TRAILING_STRUCT  r_now >= trail_r              active defense disabled; structural trail only
EMERGENCY        r_peak >= lock_r AND          force close — a proven winner must not
                 r_now < r_peak * giveback     become a small win or a loss
                 emergency_ratio

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
    """Tunable thresholds — all in R (risk multiples from entry)."""

    arm_r: float = 2.0
    lock_r: float = 3.0
    trail_r: float = 5.0
    lock_floor_r: float = 1.5
    giveback_emergency_ratio: float = 0.33
    locked_active_defense_score_bonus: int = 3

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

        if r_peak >= cfg.lock_r and r_now < r_peak * cfg.giveback_emergency_ratio:
            return WinnerProtectionDecision(
                state=WinnerState.EMERGENCY,
                allow_active_defense_close=True,
                allow_active_defense_tighten=True,
                structural_sl_floor_r=None,
                force_close=True,
                reason=(
                    f"giveback_emergency:r_peak={r_peak:.2f},r_now={r_now:.2f},"
                    f"ratio={cfg.giveback_emergency_ratio:.2f}"
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

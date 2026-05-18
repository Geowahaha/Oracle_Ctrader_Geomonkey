"""MFE Progressive Trail implementation.

The engine maintains *per-position* best-seen MFE so a single excursion
wick locks the floor. The caller provides ``mfe_r_observed`` (the running
peak in R-multiples) on every tick; the engine returns ``None`` when the
trail shouldn't move and an :class:`MFETrailDirective` when it should.

Algorithm:
  1. If ``mfe_r_observed < min_mfe_r`` (default 1.0) → no directive.
  2. Pick the highest tier where ``mfe_r_observed >= tier.mfe_r``.
  3. Compute proposed SL:
        risk_distance = |entry - original_sl|
        locked_pts    = risk_distance * mfe_r_observed * tier.lock_fraction
        for LONG:  proposed_sl = entry + locked_pts
        for SHORT: proposed_sl = entry - locked_pts
  4. If proposed_sl is closer to the *current* SL toward profit than the
     existing SL, emit the directive. Otherwise no-op (we never widen).

Per-position cooldown stops us from spamming amend calls on every tick;
once we tighten, we wait ``cooldown_seconds`` before tightening again.

Concurrency: the per-position state lives in a dict guarded by a lock.
The engine is intended to be called from the scheduler tick on a single
thread, but the lock makes multi-thread use safe.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class MFETrailTier:
    mfe_r: float        # minimum MFE in R to activate this tier
    lock_fraction: float  # fraction of MFE to lock (0..1)


# Default tiers; ordered ascending by mfe_r.
DEFAULT_TIERS: tuple[MFETrailTier, ...] = (
    MFETrailTier(mfe_r=1.0, lock_fraction=0.30),
    MFETrailTier(mfe_r=2.0, lock_fraction=0.55),
    MFETrailTier(mfe_r=3.0, lock_fraction=0.70),
    MFETrailTier(mfe_r=4.0, lock_fraction=0.85),
)


@dataclass(frozen=True)
class MFETrailInputs:
    position_id: int
    direction: str
    entry_price: float
    current_stop_loss: float
    original_stop_loss: float  # used to compute risk_distance
    mfe_r_observed: float       # the running peak MFE in R-multiples


@dataclass(frozen=True)
class MFETrailDirective:
    position_id: int
    new_stop_loss: float
    locked_r: float            # R-multiples now locked (mfe_r * lock_fraction)
    tier_mfe_r: float          # threshold of activated tier
    tier_lock_fraction: float  # fraction of MFE locked
    reason: str
    issued_utc: datetime


@dataclass
class MFEProgressiveTrailConfig:
    enabled: bool = False
    min_mfe_r: float = 1.0
    tiers: tuple[MFETrailTier, ...] = DEFAULT_TIERS
    cooldown_seconds: float = 30.0
    # Optional source restriction (csv). Empty == "all sources".
    allowed_sources_csv: str = ""


def _allowed_source(source: str, csv: str) -> bool:
    csv = (csv or "").strip()
    if not csv:
        return True
    allowed = {s.strip().lower() for s in csv.split(",") if s.strip()}
    return str(source or "").strip().lower() in allowed


def _select_tier(mfe_r: float, tiers: tuple[MFETrailTier, ...]) -> Optional[MFETrailTier]:
    """Pick the highest tier whose threshold is <= mfe_r."""
    best: Optional[MFETrailTier] = None
    for t in tiers:
        if mfe_r >= t.mfe_r:
            if best is None or t.mfe_r > best.mfe_r:
                best = t
    return best


class MFEProgressiveTrail:
    """Stateful — keeps per-position peak MFE and last-tightened timestamp."""

    def __init__(
        self,
        *,
        config: Optional[MFEProgressiveTrailConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or MFEProgressiveTrailConfig()
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._peak_mfe: dict[int, float] = {}
        self._last_amend: dict[int, datetime] = {}

    def evaluate(
        self,
        *,
        inputs: MFETrailInputs,
        source: str = "",
    ) -> Optional[MFETrailDirective]:
        cfg = self.config
        if not cfg.enabled:
            return None
        if not _allowed_source(source, cfg.allowed_sources_csv):
            return None
        direction = _norm_dir(inputs.direction)
        if direction not in {"long", "short"}:
            return None
        risk_distance = abs(float(inputs.entry_price) - float(inputs.original_stop_loss))
        if risk_distance <= 1e-9:
            return None

        pid = int(inputs.position_id)
        with self._lock:
            # Update peak MFE so a single wick locks the floor permanently.
            peak = max(self._peak_mfe.get(pid, 0.0), float(inputs.mfe_r_observed))
            self._peak_mfe[pid] = peak

            if peak < cfg.min_mfe_r:
                return None

            tier = _select_tier(peak, cfg.tiers)
            if tier is None:
                return None

            locked_r = peak * tier.lock_fraction
            locked_pts = risk_distance * locked_r
            if direction == "long":
                proposed_sl = float(inputs.entry_price) + locked_pts
                # Only tighten — never move SL back from a more-protective level.
                if proposed_sl <= float(inputs.current_stop_loss):
                    return None
            else:
                proposed_sl = float(inputs.entry_price) - locked_pts
                if proposed_sl >= float(inputs.current_stop_loss):
                    return None

            now = self._clock()
            last = self._last_amend.get(pid)
            if last is not None and (now - last).total_seconds() < cfg.cooldown_seconds:
                return None
            self._last_amend[pid] = now

        reason = (
            f"mfe_trail:peak_r={peak:.2f},tier={tier.mfe_r:.1f}R,"
            f"lock={tier.lock_fraction:.0%},locked_r={locked_r:.2f}"
        )
        return MFETrailDirective(
            position_id=pid,
            new_stop_loss=round(proposed_sl, 6),
            locked_r=round(locked_r, 4),
            tier_mfe_r=tier.mfe_r,
            tier_lock_fraction=tier.lock_fraction,
            reason=reason,
            issued_utc=now,
        )

    def reset(self, position_id: Optional[int] = None) -> None:
        with self._lock:
            if position_id is None:
                self._peak_mfe.clear()
                self._last_amend.clear()
            else:
                self._peak_mfe.pop(int(position_id), None)
                self._last_amend.pop(int(position_id), None)

    def peak_mfe(self, position_id: int) -> float:
        with self._lock:
            return self._peak_mfe.get(int(position_id), 0.0)


__all__ = [
    "MFEProgressiveTrail",
    "MFEProgressiveTrailConfig",
    "MFETrailInputs",
    "MFETrailDirective",
    "MFETrailTier",
    "DEFAULT_TIERS",
]

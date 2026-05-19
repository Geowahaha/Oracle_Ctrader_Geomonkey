"""Breathing Room filter — gate premature close_position directives.

The filter sits between the XAU_PROFIT_GUARDIAN's directive output and
the executor. The guardian still generates ``close_position`` calls when
its tier_state turns adverse, but this filter consults each position's
*context* (age, MFE, MAE, current vs original SL) before letting the
close fire.

Three independent "give it room" conditions; ANY positive vote lets the
close through, ALL three positive blocks the close (the trade hasn't
been given a fair chance yet):

    age_proven   = position_age >= min_breathing_minutes
    mae_proven   = abs(mae_r) >= require_mae_r  (definitively losing)
    mfe_proven   = mfe_r <= -allow_mfe_r        (no longer winning)

When the filter blocks a close, it records a `BlockReason` for telemetry.
When all checks confirm the trade is genuinely losing AND old, the close
flows through unchanged.

The filter is pure / stateless except for an in-memory peak-MFE cache so
"one wick into profit" counts as MFE proof.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        if "T" not in v and " " in v:
            v = v.replace(" ", "T", 1)
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class PositionRoomState:
    """Snapshot of what we need to know about a position."""

    position_id: int
    direction: str
    entry_price: float
    current_price: float
    original_stop_loss: float
    current_stop_loss: float
    first_seen_utc: str  # ISO timestamp


@dataclass(frozen=True)
class FilterDecision:
    blocked: bool
    reason: str
    age_minutes: float
    mfe_r: float
    mae_r: float


@dataclass
class BreathingRoomConfig:
    enabled: bool = False
    min_breathing_minutes: float = 10.0  # don't close trades younger than this
    require_mae_r: float = 0.6           # need at least this much against us before any forced close
    allow_mfe_r: float = 0.2             # peak MFE must be at or below this to consider closing
    # Allow forced closes only when broker actually hit original SL/TP. Set False
    # to also allow the guardian's tactical closes even on young trades.
    only_block_tactical_closes: bool = True


class BreathingRoomFilter:
    """Filters guardian directives so young/undecided trades get room."""

    def __init__(
        self,
        *,
        config: Optional[BreathingRoomConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or BreathingRoomConfig()
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._peak_mfe_r: dict[int, float] = {}

    def evaluate_position(self, state: PositionRoomState) -> FilterDecision:
        """Compute the would-block decision for a single position."""
        cfg = self.config
        if not cfg.enabled:
            return FilterDecision(False, "disabled", 0.0, 0.0, 0.0)
        direction = _norm_dir(state.direction)
        if direction not in {"long", "short"}:
            return FilterDecision(False, "bad_direction", 0.0, 0.0, 0.0)
        risk = abs(float(state.entry_price) - float(state.original_stop_loss))
        if risk <= 1e-9:
            return FilterDecision(False, "risk_zero", 0.0, 0.0, 0.0)

        # MFE / MAE in R-multiples using the current price snapshot.
        if direction == "short":
            mfe_now = (float(state.entry_price) - float(state.current_price)) / risk
            mae_now = (float(state.current_price) - float(state.entry_price)) / risk
        else:
            mfe_now = (float(state.current_price) - float(state.entry_price)) / risk
            mae_now = (float(state.entry_price) - float(state.current_price)) / risk

        with self._lock:
            peak = max(self._peak_mfe_r.get(int(state.position_id), 0.0), mfe_now)
            self._peak_mfe_r[int(state.position_id)] = peak

        age_dt = _parse_iso(state.first_seen_utc)
        if age_dt is None:
            age_minutes = 0.0
        else:
            if age_dt.tzinfo is None:
                age_dt = age_dt.replace(tzinfo=timezone.utc)
            age_minutes = max(0.0, (self._clock() - age_dt).total_seconds() / 60.0)

        age_proven = age_minutes >= cfg.min_breathing_minutes
        mae_proven = mae_now >= cfg.require_mae_r
        mfe_proven = peak <= cfg.allow_mfe_r

        # ALL three must be true to allow a tactical close. If any single
        # condition is false, the trade still has room to prove itself.
        all_proven = age_proven and mae_proven and mfe_proven
        if all_proven:
            return FilterDecision(False, "all_proven_allow_close", age_minutes, peak, mae_now)

        # Otherwise BLOCK — but tell the caller why.
        votes = {
            "age_proven": age_proven,
            "mae_proven": mae_proven,
            "mfe_proven": mfe_proven,
        }
        reason = "give_room:" + ",".join(
            f"{k}={'Y' if v else 'N'}" for k, v in votes.items()
        )
        return FilterDecision(True, reason, age_minutes, peak, mae_now)

    def filter_directives(
        self,
        *,
        directives: list[Any],
        position_states: dict[int, PositionRoomState],
        protective_actions: tuple[str, ...] = (
            "hold_position", "partial_close", "amend_position", "cancel_order",
        ),
    ) -> tuple[list[Any], list[FilterDecision]]:
        """Drop close_position directives that don't pass breathing room.

        Returns (kept_directives, blocked_decisions). Protective actions
        (hold/partial/amend/cancel) always pass through unfiltered.
        """
        if not self.config.enabled:
            return list(directives), []

        kept: list[Any] = []
        blocks: list[FilterDecision] = []
        for d in directives:
            action = str(getattr(d, "action", "") or "").lower()
            pid = int(getattr(d, "position_id", 0) or 0)
            if action in protective_actions or pid <= 0:
                kept.append(d)
                continue
            # Only tactical close_position actions get filtered.
            if self.config.only_block_tactical_closes and action != "close_position":
                kept.append(d)
                continue
            state = position_states.get(pid)
            if state is None:
                kept.append(d)
                continue
            decision = self.evaluate_position(state)
            if decision.blocked:
                blocks.append(decision)
                continue
            kept.append(d)
        return kept, blocks

    def reset_position(self, position_id: int) -> None:
        with self._lock:
            self._peak_mfe_r.pop(int(position_id), None)


__all__ = [
    "BreathingRoomFilter",
    "BreathingRoomConfig",
    "FilterDecision",
    "PositionRoomState",
]

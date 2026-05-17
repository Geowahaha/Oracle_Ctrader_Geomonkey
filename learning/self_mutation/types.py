"""Shared dataclasses for the Self-Mutation Loop.

All types are frozen and serialisable; the ledger persists them via field-by-field
column writes (not pickle), so future schema migration stays explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


KIND_OK = "ok"
KIND_NEEDS_PTS = "needs_pts"
KIND_INSUFFICIENT_DATA = "insufficient_data"
KIND_ERROR = "error"


@dataclass(frozen=True)
class LossEvent:
    """A realised loss observed in the broker journal that may trigger a mutation cycle."""

    position_id: int
    source: str
    symbol: str
    direction: str
    pnl_usd: float
    closed_utc: str
    signal_run_id: str = ""
    raw_meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Mutation:
    """A single config delta candidate."""

    mutation_id: str
    knob: str
    baseline_value: float
    proposed_value: float
    direction: str  # "increase" | "decrease"
    rationale: str
    loss_event_position_id: int
    created_utc: str


@dataclass(frozen=True)
class BacktestOutcome:
    """Counterfactual outcome over a lookback window for a single config state."""

    kind: str  # KIND_OK / KIND_NEEDS_PTS / KIND_INSUFFICIENT_DATA / KIND_ERROR
    n_trades: int
    total_pnl_usd: float
    win_count: int
    loss_count: int
    win_rate: float
    max_drawdown_usd: float
    avg_pnl_per_trade: float
    pnl_std: float
    reason: str = ""

    @classmethod
    def empty(cls, kind: str = KIND_INSUFFICIENT_DATA, reason: str = "") -> "BacktestOutcome":
        return cls(
            kind=kind,
            n_trades=0,
            total_pnl_usd=0.0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            max_drawdown_usd=0.0,
            avg_pnl_per_trade=0.0,
            pnl_std=0.0,
            reason=reason,
        )


@dataclass(frozen=True)
class Verdict:
    """Comparison of a mutation outcome against baseline."""

    mutation_id: str
    passed: bool
    pnl_delta_usd: float
    win_rate_delta: float
    maxdd_delta_usd: float
    t_score: float
    n_trades: int
    baseline_pnl_usd: float
    mutation_pnl_usd: float
    reasons: tuple[str, ...]
    decided_utc: str


@dataclass(frozen=True)
class Promotion:
    """Canary or main promotion event."""

    promotion_id: str
    mutation_id: str
    knob: str
    new_value: float
    previous_value: float
    stage: str  # "canary" | "main"
    promoted_utc: str
    expires_utc: Optional[str] = None  # canary only
    reason: str = ""


@dataclass(frozen=True)
class Rollback:
    """Auto-revert event after main promotion underperforms."""

    rollback_id: str
    mutation_id: str
    knob: str
    reverted_value: float
    reverted_utc: str
    reason: str
    cooldown_until_utc: str

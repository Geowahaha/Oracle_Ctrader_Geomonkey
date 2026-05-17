"""Verdict engine — decides which mutation, if any, beats the baseline.

The verdict combines four signals, each with an explicit threshold:
1. PnL delta — the mutation must improve total PnL by at least `min_pnl_delta`.
2. Drawdown delta — the mutation must not increase max drawdown by more than
   `min_maxdd_tolerance` (small regressions allowed).
3. T-score — a light-weight Welch-style score over per-trade PnL distributions
   ensures the improvement is not statistical noise.
4. Sample size — at least `min_n_trades` to avoid promoting on a fluke.

If no mutation passes, the verdict is recorded as failed (with reasons) and
no promotion is created. Ties (rare) are broken by PnL delta then by
mutation_id (deterministic).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from learning.self_mutation.types import (
    KIND_OK,
    BacktestOutcome,
    Mutation,
    Verdict,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class VerdictThresholds:
    """Configurable pass thresholds for the verdict engine."""

    min_pnl_delta_usd: float = 30.0
    min_maxdd_tolerance_usd: float = 20.0
    min_t_score: float = 1.0
    min_n_trades: int = 5


def _welch_t_score(
    a_mean: float, a_std: float, a_n: int,
    b_mean: float, b_std: float, b_n: int,
) -> float:
    """Welch's t-statistic for two independent samples.

    Returns 0.0 when sample sizes or variances are degenerate. We intentionally
    do NOT convert to a p-value — we use the raw t-statistic as a comparable
    signal-to-noise number, which is all this engine needs.
    """
    if a_n < 2 or b_n < 2:
        return 0.0
    sa = (a_std ** 2) / a_n
    sb = (b_std ** 2) / b_n
    denom = sa + sb
    if denom <= 0:
        return 0.0
    return (b_mean - a_mean) / math.sqrt(denom)


def _evaluate_single(
    *,
    mutation: Mutation,
    baseline: BacktestOutcome,
    candidate: BacktestOutcome,
    thresholds: VerdictThresholds,
) -> Verdict:
    """Compute a Verdict for one candidate vs the baseline."""
    reasons: list[str] = []
    decided_utc = _utc_now_iso()

    if candidate.kind != KIND_OK:
        reasons.append(f"candidate_kind:{candidate.kind}")
        if candidate.reason:
            reasons.append(candidate.reason)
        return Verdict(
            mutation_id=mutation.mutation_id,
            passed=False,
            pnl_delta_usd=0.0,
            win_rate_delta=0.0,
            maxdd_delta_usd=0.0,
            t_score=0.0,
            n_trades=int(candidate.n_trades),
            baseline_pnl_usd=float(baseline.total_pnl_usd),
            mutation_pnl_usd=float(candidate.total_pnl_usd),
            reasons=tuple(reasons),
            decided_utc=decided_utc,
        )

    pnl_delta = candidate.total_pnl_usd - baseline.total_pnl_usd
    wr_delta = candidate.win_rate - baseline.win_rate
    maxdd_delta = baseline.max_drawdown_usd - candidate.max_drawdown_usd
    t_score = _welch_t_score(
        baseline.avg_pnl_per_trade, baseline.pnl_std, baseline.n_trades,
        candidate.avg_pnl_per_trade, candidate.pnl_std, candidate.n_trades,
    )

    if candidate.n_trades < thresholds.min_n_trades:
        reasons.append(f"insufficient_trades:{candidate.n_trades}<{thresholds.min_n_trades}")
    if pnl_delta < thresholds.min_pnl_delta_usd:
        reasons.append(
            f"pnl_delta_too_small:{pnl_delta:.2f}<{thresholds.min_pnl_delta_usd:.2f}"
        )
    if maxdd_delta < -thresholds.min_maxdd_tolerance_usd:
        reasons.append(
            f"drawdown_worsened:{-maxdd_delta:.2f}>{thresholds.min_maxdd_tolerance_usd:.2f}"
        )
    if t_score < thresholds.min_t_score:
        reasons.append(f"t_score_too_low:{t_score:.2f}<{thresholds.min_t_score:.2f}")

    passed = not reasons
    if passed:
        reasons = [
            f"pnl_delta:+${pnl_delta:.2f}",
            f"wr_delta:+{wr_delta:.2%}" if wr_delta >= 0 else f"wr_delta:{wr_delta:.2%}",
            f"maxdd_delta:+${maxdd_delta:.2f}" if maxdd_delta >= 0 else f"maxdd_delta:-${-maxdd_delta:.2f}",
            f"t_score:{t_score:.2f}",
        ]

    return Verdict(
        mutation_id=mutation.mutation_id,
        passed=passed,
        pnl_delta_usd=round(pnl_delta, 4),
        win_rate_delta=round(wr_delta, 4),
        maxdd_delta_usd=round(maxdd_delta, 4),
        t_score=round(t_score, 4),
        n_trades=int(candidate.n_trades),
        baseline_pnl_usd=round(baseline.total_pnl_usd, 4),
        mutation_pnl_usd=round(candidate.total_pnl_usd, 4),
        reasons=tuple(reasons),
        decided_utc=decided_utc,
    )


class VerdictEngine:
    """Stateless verdict evaluator. Reads thresholds at construction."""

    def __init__(self, thresholds: Optional[VerdictThresholds] = None):
        self.thresholds = thresholds or VerdictThresholds()

    def evaluate(
        self,
        *,
        mutation: Mutation,
        baseline: BacktestOutcome,
        candidate: BacktestOutcome,
    ) -> Verdict:
        return _evaluate_single(
            mutation=mutation,
            baseline=baseline,
            candidate=candidate,
            thresholds=self.thresholds,
        )

    def pick_winner(
        self,
        verdicts: list[Verdict],
    ) -> Optional[Verdict]:
        """Return the top passing verdict; None if none pass.

        Tie-break: higher `pnl_delta_usd`, then lexicographic `mutation_id` for
        determinism.
        """
        passing = [v for v in verdicts if v.passed]
        if not passing:
            return None
        passing.sort(key=lambda v: (-v.pnl_delta_usd, v.mutation_id))
        return passing[0]


__all__ = ["VerdictEngine", "VerdictThresholds"]

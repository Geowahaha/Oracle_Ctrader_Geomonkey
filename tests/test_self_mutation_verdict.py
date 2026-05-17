"""Tests for the Verdict engine."""
from __future__ import annotations

from learning.self_mutation.types import KIND_NEEDS_PTS, KIND_OK, BacktestOutcome, Mutation
from learning.self_mutation.verdict import VerdictEngine, VerdictThresholds


def _outcome(*, pnl: float, n: int = 10, win_rate: float = 0.5, maxdd: float = 30.0, avg: float = None, std: float = 4.0) -> BacktestOutcome:
    return BacktestOutcome(
        kind=KIND_OK,
        n_trades=n,
        total_pnl_usd=pnl,
        win_count=int(n * win_rate),
        loss_count=n - int(n * win_rate),
        win_rate=win_rate,
        max_drawdown_usd=maxdd,
        avg_pnl_per_trade=(pnl / n) if avg is None else avg,
        pnl_std=std,
    )


def _mutation() -> Mutation:
    return Mutation(
        mutation_id="mut_a",
        knob="RISK_PER_TRADE",
        baseline_value=0.01,
        proposed_value=0.012,
        direction="increase",
        rationale="test",
        loss_event_position_id=1,
        created_utc="2026-05-17T01:00:00Z",
    )


def test_verdict_passes_when_all_thresholds_met():
    engine = VerdictEngine(VerdictThresholds(
        min_pnl_delta_usd=30.0, min_maxdd_tolerance_usd=20.0,
        min_t_score=1.0, min_n_trades=5,
    ))
    baseline = _outcome(pnl=-50.0, avg=-5.0, std=4.0)
    candidate = _outcome(pnl=20.0, avg=2.0, std=4.0, maxdd=15.0)
    v = engine.evaluate(mutation=_mutation(), baseline=baseline, candidate=candidate)
    assert v.passed
    assert v.pnl_delta_usd == 70.0


def test_verdict_fails_when_pnl_delta_too_small():
    engine = VerdictEngine(VerdictThresholds(min_pnl_delta_usd=100.0))
    baseline = _outcome(pnl=-10.0)
    candidate = _outcome(pnl=20.0)  # delta = 30 < 100
    v = engine.evaluate(mutation=_mutation(), baseline=baseline, candidate=candidate)
    assert not v.passed
    assert any("pnl_delta_too_small" in r for r in v.reasons)


def test_verdict_fails_when_drawdown_worsens_too_much():
    engine = VerdictEngine(VerdictThresholds(min_pnl_delta_usd=10.0, min_maxdd_tolerance_usd=5.0))
    baseline = _outcome(pnl=10.0, maxdd=20.0)
    candidate = _outcome(pnl=30.0, maxdd=40.0)  # drawdown worsened by 20 > 5
    v = engine.evaluate(mutation=_mutation(), baseline=baseline, candidate=candidate)
    assert not v.passed
    assert any("drawdown_worsened" in r for r in v.reasons)


def test_verdict_fails_when_insufficient_trades():
    engine = VerdictEngine(VerdictThresholds(min_n_trades=10))
    baseline = _outcome(pnl=10.0, n=10)
    candidate = _outcome(pnl=50.0, n=3)  # too few trades
    v = engine.evaluate(mutation=_mutation(), baseline=baseline, candidate=candidate)
    assert not v.passed
    assert any("insufficient_trades" in r for r in v.reasons)


def test_verdict_needs_pts_kind_fails_quickly():
    engine = VerdictEngine()
    baseline = _outcome(pnl=0.0)
    candidate = BacktestOutcome.empty(KIND_NEEDS_PTS, reason="knob_requires_pts:X")
    v = engine.evaluate(mutation=_mutation(), baseline=baseline, candidate=candidate)
    assert not v.passed
    assert any("candidate_kind:needs_pts" in r for r in v.reasons)


def test_verdict_pick_winner_returns_top_passing_only():
    engine = VerdictEngine(VerdictThresholds(min_pnl_delta_usd=10.0, min_t_score=0.0, min_n_trades=3))
    # Three verdicts: two pass, one fails. Winner is the one with highest pnl_delta.
    from learning.self_mutation.types import Verdict
    verdicts = [
        Verdict(mutation_id="m1", passed=True, pnl_delta_usd=15.0, win_rate_delta=0.0,
                maxdd_delta_usd=0.0, t_score=1.0, n_trades=10, baseline_pnl_usd=0.0,
                mutation_pnl_usd=15.0, reasons=(), decided_utc="t"),
        Verdict(mutation_id="m2", passed=False, pnl_delta_usd=50.0, win_rate_delta=0.0,
                maxdd_delta_usd=0.0, t_score=0.5, n_trades=10, baseline_pnl_usd=0.0,
                mutation_pnl_usd=50.0, reasons=("t_score_too_low",), decided_utc="t"),
        Verdict(mutation_id="m3", passed=True, pnl_delta_usd=40.0, win_rate_delta=0.0,
                maxdd_delta_usd=0.0, t_score=1.2, n_trades=10, baseline_pnl_usd=0.0,
                mutation_pnl_usd=40.0, reasons=(), decided_utc="t"),
    ]
    winner = engine.pick_winner(verdicts)
    assert winner is not None
    assert winner.mutation_id == "m3"  # highest pnl_delta of the two passing


def test_verdict_pick_winner_returns_none_when_no_pass():
    engine = VerdictEngine()
    from learning.self_mutation.types import Verdict
    verdicts = [
        Verdict(mutation_id="m1", passed=False, pnl_delta_usd=0, win_rate_delta=0,
                maxdd_delta_usd=0, t_score=0, n_trades=0, baseline_pnl_usd=0,
                mutation_pnl_usd=0, reasons=(), decided_utc="t"),
    ]
    assert engine.pick_winner(verdicts) is None

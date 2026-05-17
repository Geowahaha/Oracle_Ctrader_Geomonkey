"""Tests for the Self-Mutation ledger (SQLite audit trail)."""
from __future__ import annotations

from pathlib import Path

from learning.self_mutation.ledger import Ledger
from learning.self_mutation.types import (
    LossEvent,
    Mutation,
    Promotion,
    Rollback,
    Verdict,
)


def _mk_loss(position_id: int = 1) -> LossEvent:
    return LossEvent(
        position_id=position_id,
        source="scalp_xauusd:winner",
        symbol="XAUUSD",
        direction="long",
        pnl_usd=-25.0,
        closed_utc="2026-05-17T01:00:00Z",
        signal_run_id=f"run-{position_id}",
    )


def _mk_mutation(mutation_id: str = "mut_001", knob: str = "RISK_PER_TRADE", baseline: float = 0.01, loss_position_id: int = 1) -> Mutation:
    return Mutation(
        mutation_id=mutation_id,
        knob=knob,
        baseline_value=baseline,
        proposed_value=baseline * 0.8,
        direction="decrease",
        rationale="test",
        loss_event_position_id=loss_position_id,
        created_utc="2026-05-17T01:01:00Z",
    )


def test_ledger_records_loss_event_idempotent(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    event = _mk_loss(position_id=42)
    assert ledger.record_loss_event(event) is True
    # Second insert must be a no-op (idempotent on position_id).
    assert ledger.record_loss_event(event) is False
    assert ledger.has_loss_event(42) is True
    assert ledger.has_loss_event(43) is False


def test_ledger_mutation_and_verdict_roundtrip(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_loss_event(_mk_loss())
    mut = _mk_mutation()
    ledger.record_mutation(mut)
    verdict = Verdict(
        mutation_id=mut.mutation_id,
        passed=True,
        pnl_delta_usd=42.5,
        win_rate_delta=0.05,
        maxdd_delta_usd=10.0,
        t_score=1.4,
        n_trades=12,
        baseline_pnl_usd=-30.0,
        mutation_pnl_usd=12.5,
        reasons=("pnl_delta:+$42.50",),
        decided_utc="2026-05-17T01:02:00Z",
    )
    ledger.record_verdict(verdict)
    rows = ledger.recent_mutations(limit=10)
    assert len(rows) == 1
    assert rows[0]["mutation_id"] == "mut_001"
    assert rows[0]["passed"] == 1
    assert ledger.last_mutation_utc_for_knob(mut.knob) == mut.created_utc


def test_ledger_canary_lifecycle(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_loss_event(_mk_loss())
    mut = _mk_mutation()
    ledger.record_mutation(mut)
    canary = Promotion(
        promotion_id="prom_canary_a",
        mutation_id=mut.mutation_id,
        knob=mut.knob,
        new_value=mut.proposed_value,
        previous_value=mut.baseline_value,
        stage="canary",
        promoted_utc="2026-05-17T01:00:00Z",
        expires_utc="2026-05-17T02:00:00Z",
    )
    ledger.record_promotion(canary)

    # Before expiry, canary is active.
    assert len(ledger.active_canaries(now_utc="2026-05-17T01:30:00Z")) == 1
    assert ledger.expired_canaries(now_utc="2026-05-17T01:30:00Z") == []
    # After expiry, canary is in the "expired pending follow-up" set.
    expired = ledger.expired_canaries(now_utc="2026-05-17T03:00:00Z")
    assert len(expired) == 1
    # Add a main promotion → canary disappears from expired list.
    main = Promotion(
        promotion_id="prom_main_a",
        mutation_id=mut.mutation_id,
        knob=mut.knob,
        new_value=mut.proposed_value,
        previous_value=mut.baseline_value,
        stage="main",
        promoted_utc="2026-05-17T03:01:00Z",
        expires_utc=None,
    )
    ledger.record_promotion(main)
    assert ledger.expired_canaries(now_utc="2026-05-17T03:30:00Z") == []
    assert len(ledger.active_main_promotions()) == 1


def test_ledger_rollback_removes_from_active_mains(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_loss_event(_mk_loss())
    mut = _mk_mutation()
    ledger.record_mutation(mut)
    ledger.record_promotion(Promotion(
        promotion_id="prom_main_x",
        mutation_id=mut.mutation_id,
        knob=mut.knob,
        new_value=mut.proposed_value,
        previous_value=mut.baseline_value,
        stage="main",
        promoted_utc="2026-05-10T00:00:00Z",
        expires_utc=None,
    ))
    assert len(ledger.active_main_promotions()) == 1
    ledger.record_rollback(Rollback(
        rollback_id="rb_main_x",
        mutation_id=mut.mutation_id,
        knob=mut.knob,
        reverted_value=mut.baseline_value,
        reverted_utc="2026-05-17T00:00:00Z",
        reason="post_promotion_degradation",
        cooldown_until_utc="2026-05-24T00:00:00Z",
    ))
    assert ledger.active_main_promotions() == []
    assert ledger.cooldown_until_for_knob(mut.knob) == "2026-05-24T00:00:00Z"


def test_ledger_survives_restart(tmp_path: Path):
    db = tmp_path / "ledger.db"
    a = Ledger(db)
    a.record_loss_event(_mk_loss(position_id=7))
    a.record_mutation(_mk_mutation(mutation_id="mut_persist", loss_position_id=7))
    del a
    b = Ledger(db)
    assert b.has_loss_event(7)
    assert b.last_mutation_utc_for_knob("RISK_PER_TRADE") == "2026-05-17T01:01:00Z"

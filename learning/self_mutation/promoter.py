"""Promoter — moves passing mutations through canary then main.

State machine:

    [verdict.passed=True]
            │
            ▼
       ┌─────────┐    canary_hours elapsed     ┌──────────┐
       │ canary  │ ─────────────────────────►  │ post-can │
       └─────────┘                             │ verdict  │
                                               └────┬─────┘
                                       passes       │       fails
                                       ▼            │           ▼
                                  ┌────────┐        │      ┌──────────┐
                                  │  main  │        │      │ canary   │
                                  └────────┘        │      │ revert   │
                                                    │      └──────────┘
                                                    │
                                                  (no-op)

Every transition is persisted to the ledger and reflected in the runtime
overrides file. Every transition emits a Telegram message.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from learning.self_mutation.ledger import Ledger
from learning.self_mutation.notify import Notifier, compose_promotion_message, compose_rollback_message
from learning.self_mutation.overrides import OverrideEntry, OverrideStore
from learning.self_mutation.runner import CounterfactualRunner
from learning.self_mutation.types import (
    KIND_OK,
    Mutation,
    Promotion,
    Rollback,
    Verdict,
)
from learning.self_mutation.verdict import VerdictEngine, VerdictThresholds


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _add_hours_iso(hours: float, *, base_utc: Optional[str] = None) -> str:
    if base_utc:
        try:
            v = base_utc
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            base = datetime.fromisoformat(v)
        except (TypeError, ValueError):
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    return (base + timedelta(hours=float(hours))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _short_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


class Promoter:
    """Drives canary and main promotions."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        overrides: OverrideStore,
        runner: CounterfactualRunner,
        verdict_engine: VerdictEngine,
        notifier: Notifier,
        canary_hours: float = 24.0,
        canary_pass_margin_usd: float = 15.0,
        dry_run: bool = False,
    ) -> None:
        self.ledger = ledger
        self.overrides = overrides
        self.runner = runner
        self.verdict_engine = verdict_engine
        self.notifier = notifier
        self.canary_hours = float(canary_hours)
        self.canary_pass_margin_usd = float(canary_pass_margin_usd)
        self.dry_run = bool(dry_run)

    def promote_to_canary(self, *, mutation: Mutation, verdict: Verdict) -> Optional[Promotion]:
        """Move a passing mutation to canary stage."""
        if not verdict.passed:
            return None
        previous = self.overrides.value_for(mutation.knob, mutation.baseline_value)
        promoted_utc = _utc_now_iso()
        expires_utc = _add_hours_iso(self.canary_hours, base_utc=promoted_utc)
        promotion = Promotion(
            promotion_id=f"prom_canary_{_short_id(mutation.mutation_id + promoted_utc)}",
            mutation_id=mutation.mutation_id,
            knob=mutation.knob,
            new_value=mutation.proposed_value,
            previous_value=previous,
            stage="canary",
            promoted_utc=promoted_utc,
            expires_utc=expires_utc,
            reason="verdict_passed",
        )
        self.ledger.record_promotion(promotion)
        if not self.dry_run:
            entry = OverrideEntry(
                knob=mutation.knob,
                value=mutation.proposed_value,
                applied_utc=promoted_utc,
                mutation_id=mutation.mutation_id,
                expires_utc=expires_utc,
            )
            self.overrides.set_canary(entry)
        self.notifier(
            compose_promotion_message(
                knob=mutation.knob,
                old=previous,
                new=mutation.proposed_value,
                stage="canary",
                mutation_id=mutation.mutation_id,
            )
        )
        return promotion

    def evaluate_expired_canary(
        self,
        canary_row: dict,
        *,
        mutation_lookup: Callable[[str], Optional[Mutation]],
    ) -> tuple[Optional[Promotion], Optional[Rollback]]:
        """Decide whether an expired canary becomes main or reverts.

        Re-runs the counterfactual over the canary window only (≈last
        `canary_hours`). If the mutation still beats the baseline by at least
        `canary_pass_margin_usd`, promote to main; else revert.
        """
        mutation = mutation_lookup(canary_row["mutation_id"])
        if mutation is None:
            return None, None

        post_baseline = self.runner.run_baseline()
        post_candidate = self.runner.run_mutation(
            mutation.knob, mutation.baseline_value, mutation.proposed_value
        )

        if post_candidate.kind != KIND_OK or post_baseline.kind != KIND_OK:
            # Cannot judge — leave the canary in place by reverting (safer than
            # promoting on no data).
            return None, self._revert_canary(
                canary_row=canary_row, mutation=mutation, reason="post_canary_data_insufficient"
            )

        post_pnl_delta = post_candidate.total_pnl_usd - post_baseline.total_pnl_usd
        if post_pnl_delta >= self.canary_pass_margin_usd:
            return self._promote_main(canary_row=canary_row, mutation=mutation, reason="canary_window_passed"), None
        return None, self._revert_canary(
            canary_row=canary_row, mutation=mutation,
            reason=f"canary_pnl_delta_below_margin:{post_pnl_delta:.2f}<{self.canary_pass_margin_usd:.2f}",
        )

    def _promote_main(self, *, canary_row: dict, mutation: Mutation, reason: str) -> Promotion:
        previous = float(canary_row.get("previous_value") or mutation.baseline_value)
        promoted_utc = _utc_now_iso()
        promotion = Promotion(
            promotion_id=f"prom_main_{_short_id(mutation.mutation_id + promoted_utc)}",
            mutation_id=mutation.mutation_id,
            knob=mutation.knob,
            new_value=mutation.proposed_value,
            previous_value=previous,
            stage="main",
            promoted_utc=promoted_utc,
            expires_utc=None,
            reason=reason,
        )
        self.ledger.record_promotion(promotion)
        if not self.dry_run:
            entry = OverrideEntry(
                knob=mutation.knob,
                value=mutation.proposed_value,
                applied_utc=promoted_utc,
                mutation_id=mutation.mutation_id,
                expires_utc=None,
            )
            self.overrides.set_main(entry)
        self.notifier(
            compose_promotion_message(
                knob=mutation.knob,
                old=previous,
                new=mutation.proposed_value,
                stage="main",
                mutation_id=mutation.mutation_id,
            )
        )
        return promotion

    def _revert_canary(self, *, canary_row: dict, mutation: Mutation, reason: str) -> Rollback:
        reverted_value = float(canary_row.get("previous_value") or mutation.baseline_value)
        reverted_utc = _utc_now_iso()
        cooldown_until = _add_hours_iso(24.0, base_utc=reverted_utc)
        rollback = Rollback(
            rollback_id=f"rb_canary_{_short_id(mutation.mutation_id + reverted_utc)}",
            mutation_id=mutation.mutation_id,
            knob=mutation.knob,
            reverted_value=reverted_value,
            reverted_utc=reverted_utc,
            reason=reason,
            cooldown_until_utc=cooldown_until,
        )
        self.ledger.record_rollback(rollback)
        if not self.dry_run:
            self.overrides.remove_canary(mutation.knob)
        self.notifier(
            compose_rollback_message(
                knob=mutation.knob,
                reverted_to=reverted_value,
                reason=reason,
                mutation_id=mutation.mutation_id,
            )
        )
        return rollback


__all__ = ["Promoter"]

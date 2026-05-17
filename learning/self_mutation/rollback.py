"""Rollback engine — auto-reverts main promotions that have degraded.

Once a mutation lives in `main`, the rollback engine periodically re-evaluates
its counterfactual against the baseline using the configured window (default
7 days post-promotion). If the mutation now underperforms the baseline by more
than `rollback_pnl_delta_usd`, it is reverted and the knob enters cooldown.

This is the safety net that lets the loop run autonomously: even if a canary
window happened to favour a bad mutation, the rollback engine catches it once
enough live data accumulates.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from learning.self_mutation.ledger import Ledger
from learning.self_mutation.notify import Notifier, compose_rollback_message
from learning.self_mutation.overrides import OverrideStore
from learning.self_mutation.runner import CounterfactualRunner
from learning.self_mutation.types import KIND_OK, Mutation, Rollback


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _add_hours_iso(hours: float, *, base_utc: Optional[str] = None) -> str:
    base = datetime.now(timezone.utc)
    if base_utc:
        try:
            v = base_utc
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            base = datetime.fromisoformat(v)
        except (TypeError, ValueError):
            pass
    return (base + timedelta(hours=float(hours))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _short_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


class RollbackEngine:
    """Evaluates active main promotions and reverts those that have degraded."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        overrides: OverrideStore,
        runner: CounterfactualRunner,
        notifier: Notifier,
        window_days: float = 7.0,
        rollback_pnl_delta_usd: float = 30.0,
        cooldown_hours: float = 168.0,
        dry_run: bool = False,
    ) -> None:
        self.ledger = ledger
        self.overrides = overrides
        self.runner = runner
        self.notifier = notifier
        self.window_days = float(window_days)
        self.rollback_pnl_delta_usd = float(rollback_pnl_delta_usd)
        self.cooldown_hours = float(cooldown_hours)
        self.dry_run = bool(dry_run)

    def evaluate(
        self,
        *,
        mutation_lookup: Callable[[str], Optional[Mutation]],
        now: Optional[datetime] = None,
    ) -> list[Rollback]:
        """Return the list of rollbacks performed in this tick."""
        now = now or datetime.now(timezone.utc)
        active = self.ledger.active_main_promotions()
        rollbacks: list[Rollback] = []
        for row in active:
            promoted = _parse_iso(str(row.get("promoted_utc") or ""))
            if promoted is None:
                continue
            if (now - promoted) < timedelta(days=self.window_days):
                # Not enough live data yet.
                continue
            mutation = mutation_lookup(str(row.get("mutation_id") or ""))
            if mutation is None:
                continue
            baseline = self.runner.run_baseline()
            candidate = self.runner.run_mutation(
                mutation.knob, mutation.baseline_value, mutation.proposed_value
            )
            if baseline.kind != KIND_OK or candidate.kind != KIND_OK:
                continue
            pnl_delta = candidate.total_pnl_usd - baseline.total_pnl_usd
            if pnl_delta >= -self.rollback_pnl_delta_usd:
                # Still acceptable.
                continue
            rollback = self._do_rollback(
                row=row, mutation=mutation, pnl_delta=pnl_delta, now_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            rollbacks.append(rollback)
        return rollbacks

    def _do_rollback(self, *, row: dict, mutation: Mutation, pnl_delta: float, now_utc: str) -> Rollback:
        reverted_value = float(row.get("previous_value") or mutation.baseline_value)
        reason = f"post_promotion_degradation:pnl_delta={pnl_delta:.2f}<-{self.rollback_pnl_delta_usd:.2f}"
        cooldown_until = _add_hours_iso(self.cooldown_hours, base_utc=now_utc)
        rollback = Rollback(
            rollback_id=f"rb_main_{_short_id(mutation.mutation_id + now_utc)}",
            mutation_id=mutation.mutation_id,
            knob=mutation.knob,
            reverted_value=reverted_value,
            reverted_utc=now_utc,
            reason=reason,
            cooldown_until_utc=cooldown_until,
        )
        self.ledger.record_rollback(rollback)
        if not self.dry_run:
            self.overrides.remove_main(mutation.knob)
        self.notifier(
            compose_rollback_message(
                knob=mutation.knob,
                reverted_to=reverted_value,
                reason=reason,
                mutation_id=mutation.mutation_id,
            )
        )
        return rollback


__all__ = ["RollbackEngine"]

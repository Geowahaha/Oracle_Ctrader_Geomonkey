"""Governor — the brain of the Self-Mutation Loop.

A single entry point, `Governor.tick()`, drives the entire loop:

  1. **Ingest losses.**  Pull new loss events from the trigger and record them.
  2. **Mutate.**         Sample up to 3 mutation candidates per loss.
  3. **Backtest.**       Counterfactual baseline + per-mutation outcomes.
  4. **Judge.**          Pick the winning mutation (if any).
  5. **Promote canary.** Write canary override, notify.
  6. **Promote main.**   Re-judge expired canaries, promote or revert.
  7. **Rollback.**       Audit active mains for post-promotion degradation.

The governor is idempotent: every step persists to the ledger before the next
step begins, so a restart mid-tick is safe. It also honours the master
`enabled` flag — when disabled, `tick()` is a no-op that logs once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from learning.self_mutation.ledger import Ledger
from learning.self_mutation.notify import Notifier, LoggingNotifier
from learning.self_mutation.overrides import OverrideStore
from learning.self_mutation.promoter import Promoter
from learning.self_mutation.rollback import RollbackEngine
from learning.self_mutation.runner import CounterfactualRunner
from learning.self_mutation.sampler import KNOB_BY_NAME, Sampler
from learning.self_mutation.trigger import LossEventTrigger
from learning.self_mutation.types import LossEvent, Mutation
from learning.self_mutation.verdict import VerdictEngine, VerdictThresholds


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class TickReport:
    """Summary of what happened in one Governor.tick() call.

    Suitable for logging, CLI inspection, and dashboard display.
    """

    new_loss_events: int = 0
    mutations_proposed: int = 0
    verdicts_recorded: int = 0
    verdicts_passed: int = 0
    canaries_promoted: int = 0
    canaries_expired_checked: int = 0
    mains_promoted: int = 0
    canaries_reverted: int = 0
    mains_rolled_back: int = 0
    notes: list[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        self.notes.append(note)


class Governor:
    """Single-entrypoint orchestrator for the Self-Mutation Loop."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        overrides: OverrideStore,
        runner: CounterfactualRunner,
        sampler: Sampler,
        verdict_engine: VerdictEngine,
        promoter: Promoter,
        rollback_engine: RollbackEngine,
        trigger: LossEventTrigger,
        notifier: Notifier,
        enabled: bool = False,
    ) -> None:
        self.ledger = ledger
        self.overrides = overrides
        self.runner = runner
        self.sampler = sampler
        self.verdict_engine = verdict_engine
        self.promoter = promoter
        self.rollback_engine = rollback_engine
        self.trigger = trigger
        self.notifier = notifier
        self.enabled = bool(enabled)
        self._mutation_cache: dict[str, Mutation] = {}

    # ----- factory ------------------------------------------------------
    @classmethod
    def from_config(cls, *, config_obj: object, notifier: Optional[Notifier] = None) -> "Governor":
        """Build a Governor from the global Config singleton."""
        ledger = Ledger(getattr(config_obj, "SELF_MUTATION_LEDGER_PATH", "data/runtime/self_mutation_ledger.db"))
        overrides = OverrideStore(getattr(config_obj, "SELF_MUTATION_OVERRIDES_PATH", "data/runtime/self_mutation_overrides.json"))
        # cTrader DB path — try the executor's resolved path first (which honours
        # broker-config), then env-var CTRADER_DB_PATH, then the live default
        # `data/ctrader_openapi.db`. The earlier fallback (`data/runtime/ctrader.db`)
        # was wrong on the live VM and caused the loop to be blind to losses.
        journal_db = ""
        try:
            from execution.ctrader_executor import ctrader_executor as _exec  # local import
            journal_db = str(getattr(_exec, "db_path", "") or "")
        except Exception:
            journal_db = ""
        journal_db = journal_db or str(getattr(config_obj, "CTRADER_DB_PATH", "") or "") or "data/ctrader_openapi.db"
        runner = CounterfactualRunner(
            db_path=journal_db,
            lookback_days=int(getattr(config_obj, "SELF_MUTATION_LOOKBACK_DAYS", 14)),
        )

        def baseline_resolver(knob: str) -> float:
            override = overrides.value_for(knob, default=float("nan"))
            if override == override:  # not NaN
                return override
            spec = KNOB_BY_NAME.get(knob)
            default = float(getattr(config_obj, knob, spec.min_value if spec else 0.0))
            return default

        def last_mutation_resolver(knob: str) -> Optional[str]:
            return ledger.last_mutation_utc_for_knob(knob)

        sampler = Sampler(
            baseline_resolver=baseline_resolver,
            last_mutation_resolver=last_mutation_resolver,
            cooldown_hours=float(getattr(config_obj, "SELF_MUTATION_COOLDOWN_HOURS", 24.0)),
        )
        thresholds = VerdictThresholds(
            min_pnl_delta_usd=float(getattr(config_obj, "SELF_MUTATION_MIN_PNL_DELTA", 30.0)),
            min_maxdd_tolerance_usd=float(getattr(config_obj, "SELF_MUTATION_MIN_MAXDD_TOLERANCE", 20.0)),
            min_t_score=float(getattr(config_obj, "SELF_MUTATION_MIN_T_SCORE", 1.0)),
            min_n_trades=int(getattr(config_obj, "SELF_MUTATION_MIN_N_TRADES", 5)),
        )
        verdict_engine = VerdictEngine(thresholds=thresholds)
        notifier = notifier or LoggingNotifier()
        dry_run = bool(getattr(config_obj, "SELF_MUTATION_DRY_RUN", True))
        promoter = Promoter(
            ledger=ledger,
            overrides=overrides,
            runner=runner,
            verdict_engine=verdict_engine,
            notifier=notifier,
            canary_hours=float(getattr(config_obj, "SELF_MUTATION_CANARY_HOURS", 24.0)),
            dry_run=dry_run,
        )
        rollback_engine = RollbackEngine(
            ledger=ledger,
            overrides=overrides,
            runner=runner,
            notifier=notifier,
            window_days=float(getattr(config_obj, "SELF_MUTATION_ROLLBACK_WINDOW_DAYS", 7.0)),
            rollback_pnl_delta_usd=float(getattr(config_obj, "SELF_MUTATION_MIN_PNL_DELTA", 30.0)),
            dry_run=dry_run,
        )
        trigger = LossEventTrigger(
            journal_db_path=journal_db,
            ledger=ledger,
            loss_threshold_usd=float(getattr(config_obj, "SELF_MUTATION_LOSS_THRESHOLD_USD", 20.0)),
        )
        enabled = bool(getattr(config_obj, "SELF_MUTATION_ENABLED", False))
        kill_all = bool(getattr(config_obj, "SELF_MUTATION_KILL_ALL", False))
        if kill_all:
            overrides.kill_all()
            logger.warning("self_mutation_kill_switch_applied")
        return cls(
            ledger=ledger,
            overrides=overrides,
            runner=runner,
            sampler=sampler,
            verdict_engine=verdict_engine,
            promoter=promoter,
            rollback_engine=rollback_engine,
            trigger=trigger,
            notifier=notifier,
            enabled=enabled,
        )

    # ----- internal helpers --------------------------------------------
    def _mutation_lookup(self, mutation_id: str) -> Optional[Mutation]:
        cached = self._mutation_cache.get(mutation_id)
        if cached:
            return cached
        # Rehydrate from ledger.
        for row in self.ledger.recent_mutations(limit=100):
            if row.get("mutation_id") == mutation_id:
                mut = Mutation(
                    mutation_id=str(row["mutation_id"]),
                    knob=str(row["knob"]),
                    baseline_value=float(row["baseline_value"]),
                    proposed_value=float(row["proposed_value"]),
                    direction=str(row["direction"]),
                    rationale=str(row["rationale"]),
                    loss_event_position_id=int(row["loss_event_position_id"]),
                    created_utc=str(row["created_utc"]),
                )
                self._mutation_cache[mutation_id] = mut
                return mut
        return None

    # ----- tick ---------------------------------------------------------
    def tick(self, *, now: Optional[datetime] = None) -> TickReport:
        report = TickReport()
        if not self.enabled:
            report.add_note("self_mutation_disabled")
            return report

        now = now or datetime.now(timezone.utc)

        # 1) Ingest new loss events.
        new_events: list[LossEvent] = self.trigger.fetch_new_loss_events(now=now)
        for event in new_events:
            if self.ledger.record_loss_event(event):
                report.new_loss_events += 1

        # 2-5) Per-event: sample → backtest → judge → promote canary.
        for event in new_events:
            mutations = self.sampler.sample(event, now_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
            for mut in mutations:
                self.ledger.record_mutation(mut)
                self._mutation_cache[mut.mutation_id] = mut
                report.mutations_proposed += 1

            if not mutations:
                continue

            baseline = self.runner.run_baseline()
            verdicts = []
            for mut in mutations:
                candidate = self.runner.run_mutation(mut.knob, mut.baseline_value, mut.proposed_value)
                verdict = self.verdict_engine.evaluate(mutation=mut, baseline=baseline, candidate=candidate)
                self.ledger.record_verdict(verdict)
                verdicts.append(verdict)
                report.verdicts_recorded += 1
                if verdict.passed:
                    report.verdicts_passed += 1

            winner = self.verdict_engine.pick_winner(verdicts)
            if winner is None:
                continue
            winning_mutation = self._mutation_lookup(winner.mutation_id)
            if winning_mutation is None:
                continue
            promotion = self.promoter.promote_to_canary(mutation=winning_mutation, verdict=winner)
            if promotion is not None:
                report.canaries_promoted += 1

        # 6) Re-judge expired canaries.
        for canary_row in self.ledger.expired_canaries():
            report.canaries_expired_checked += 1
            promo, rollback = self.promoter.evaluate_expired_canary(canary_row, mutation_lookup=self._mutation_lookup)
            if promo is not None:
                report.mains_promoted += 1
            if rollback is not None:
                report.canaries_reverted += 1

        # 7) Rollback degraded mains.
        rolled = self.rollback_engine.evaluate(mutation_lookup=self._mutation_lookup, now=now)
        report.mains_rolled_back += len(rolled)
        return report


__all__ = ["Governor", "TickReport"]

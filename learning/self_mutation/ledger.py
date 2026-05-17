"""SQLite-backed audit ledger for the Self-Mutation Loop.

The ledger is the single source of truth for what mutations have been proposed,
how they were judged, and which transitions (canary/main/rollback) occurred.
Every step is recorded with an explicit timestamp and reason.

Design notes:
- Single-writer pattern: all mutations go through `Ledger` instance.
- Idempotent inserts on natural keys (mutation_id, promotion_id, rollback_id).
- No ORM — schema is tiny and we want zero hidden behaviour.
- `WAL` journal mode for concurrent reads (telegram bot, dashboard).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from learning.self_mutation.types import (
    BacktestOutcome,
    LossEvent,
    Mutation,
    Promotion,
    Rollback,
    Verdict,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Ledger:
    """Audit ledger for mutations, verdicts, promotions, and rollbacks."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS loss_events (
                    position_id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    pnl_usd REAL NOT NULL,
                    closed_utc TEXT NOT NULL,
                    signal_run_id TEXT DEFAULT '',
                    raw_meta_json TEXT DEFAULT '{}',
                    triggered_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mutations (
                    mutation_id TEXT PRIMARY KEY,
                    knob TEXT NOT NULL,
                    baseline_value REAL NOT NULL,
                    proposed_value REAL NOT NULL,
                    direction TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    loss_event_position_id INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    FOREIGN KEY(loss_event_position_id) REFERENCES loss_events(position_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mut_knob ON mutations(knob)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mut_created ON mutations(created_utc DESC)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verdicts (
                    mutation_id TEXT PRIMARY KEY,
                    passed INTEGER NOT NULL,
                    pnl_delta_usd REAL NOT NULL,
                    win_rate_delta REAL NOT NULL,
                    maxdd_delta_usd REAL NOT NULL,
                    t_score REAL NOT NULL,
                    n_trades INTEGER NOT NULL,
                    baseline_pnl_usd REAL NOT NULL,
                    mutation_pnl_usd REAL NOT NULL,
                    reasons_json TEXT DEFAULT '[]',
                    decided_utc TEXT NOT NULL,
                    FOREIGN KEY(mutation_id) REFERENCES mutations(mutation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promotions (
                    promotion_id TEXT PRIMARY KEY,
                    mutation_id TEXT NOT NULL,
                    knob TEXT NOT NULL,
                    new_value REAL NOT NULL,
                    previous_value REAL NOT NULL,
                    stage TEXT NOT NULL,
                    promoted_utc TEXT NOT NULL,
                    expires_utc TEXT,
                    reason TEXT DEFAULT '',
                    FOREIGN KEY(mutation_id) REFERENCES mutations(mutation_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prom_knob ON promotions(knob)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prom_stage ON promotions(stage)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rollbacks (
                    rollback_id TEXT PRIMARY KEY,
                    mutation_id TEXT NOT NULL,
                    knob TEXT NOT NULL,
                    reverted_value REAL NOT NULL,
                    reverted_utc TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    cooldown_until_utc TEXT NOT NULL,
                    FOREIGN KEY(mutation_id) REFERENCES mutations(mutation_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rb_knob ON rollbacks(knob)")
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", str(self.SCHEMA_VERSION)),
            )

    # ----- loss events --------------------------------------------------
    def record_loss_event(self, event: LossEvent) -> bool:
        """Returns True if this is a new event (idempotent on position_id)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO loss_events(
                    position_id, source, symbol, direction, pnl_usd,
                    closed_utc, signal_run_id, raw_meta_json, triggered_utc
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(event.position_id),
                    str(event.source),
                    str(event.symbol),
                    str(event.direction),
                    float(event.pnl_usd),
                    str(event.closed_utc),
                    str(event.signal_run_id or ""),
                    json.dumps(event.raw_meta or {}, default=str),
                    _utc_now_iso(),
                ),
            )
            return cur.rowcount > 0

    def has_loss_event(self, position_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM loss_events WHERE position_id=?",
                (int(position_id),),
            ).fetchone()
            return row is not None

    # ----- mutations ----------------------------------------------------
    def record_mutation(self, mutation: Mutation) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO mutations(
                    mutation_id, knob, baseline_value, proposed_value,
                    direction, rationale, loss_event_position_id, created_utc
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    mutation.mutation_id,
                    mutation.knob,
                    float(mutation.baseline_value),
                    float(mutation.proposed_value),
                    mutation.direction,
                    mutation.rationale,
                    int(mutation.loss_event_position_id),
                    mutation.created_utc,
                ),
            )

    def last_mutation_utc_for_knob(self, knob: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_utc FROM mutations WHERE knob=? ORDER BY created_utc DESC LIMIT 1",
                (knob,),
            ).fetchone()
            return str(row["created_utc"]) if row else None

    # ----- verdicts -----------------------------------------------------
    def record_verdict(self, verdict: Verdict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO verdicts(
                    mutation_id, passed, pnl_delta_usd, win_rate_delta,
                    maxdd_delta_usd, t_score, n_trades,
                    baseline_pnl_usd, mutation_pnl_usd, reasons_json, decided_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    verdict.mutation_id,
                    1 if verdict.passed else 0,
                    float(verdict.pnl_delta_usd),
                    float(verdict.win_rate_delta),
                    float(verdict.maxdd_delta_usd),
                    float(verdict.t_score),
                    int(verdict.n_trades),
                    float(verdict.baseline_pnl_usd),
                    float(verdict.mutation_pnl_usd),
                    json.dumps(list(verdict.reasons or ())),
                    verdict.decided_utc,
                ),
            )

    # ----- promotions --------------------------------------------------
    def record_promotion(self, promotion: Promotion) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO promotions(
                    promotion_id, mutation_id, knob, new_value, previous_value,
                    stage, promoted_utc, expires_utc, reason
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    promotion.promotion_id,
                    promotion.mutation_id,
                    promotion.knob,
                    float(promotion.new_value),
                    float(promotion.previous_value),
                    promotion.stage,
                    promotion.promoted_utc,
                    promotion.expires_utc,
                    promotion.reason,
                ),
            )

    def active_canaries(self, now_utc: str | None = None) -> list[dict]:
        """Return canary promotions whose `expires_utc` is in the future."""
        now = now_utc or _utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM promotions
                 WHERE stage='canary' AND expires_utc IS NOT NULL AND expires_utc > ?
                 ORDER BY promoted_utc DESC
                """,
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    def expired_canaries(self, now_utc: str | None = None) -> list[dict]:
        """Canary promotions that have passed `expires_utc` but have no follow-up."""
        now = now_utc or _utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM promotions p
                 WHERE p.stage='canary'
                   AND p.expires_utc IS NOT NULL
                   AND p.expires_utc <= ?
                   AND NOT EXISTS (
                       SELECT 1 FROM promotions q
                        WHERE q.mutation_id=p.mutation_id AND q.stage='main'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM rollbacks r
                        WHERE r.mutation_id=p.mutation_id
                   )
                 ORDER BY p.promoted_utc ASC
                """,
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    def active_main_promotions(self) -> list[dict]:
        """Main promotions that have not been rolled back."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM promotions p
                 WHERE p.stage='main'
                   AND NOT EXISTS (
                       SELECT 1 FROM rollbacks r WHERE r.mutation_id=p.mutation_id
                   )
                 ORDER BY p.promoted_utc DESC
                """,
            ).fetchall()
            return [dict(r) for r in rows]

    # ----- rollbacks ----------------------------------------------------
    def record_rollback(self, rollback: Rollback) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rollbacks(
                    rollback_id, mutation_id, knob, reverted_value,
                    reverted_utc, reason, cooldown_until_utc
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    rollback.rollback_id,
                    rollback.mutation_id,
                    rollback.knob,
                    float(rollback.reverted_value),
                    rollback.reverted_utc,
                    rollback.reason,
                    rollback.cooldown_until_utc,
                ),
            )

    def cooldown_until_for_knob(self, knob: str) -> Optional[str]:
        """Latest rollback-driven cooldown for a knob (UTC ISO) or None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cooldown_until_utc FROM rollbacks
                 WHERE knob=? ORDER BY reverted_utc DESC LIMIT 1
                """,
                (knob,),
            ).fetchone()
            return str(row["cooldown_until_utc"]) if row else None

    # ----- introspection -----------------------------------------------
    def recent_mutations(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, v.passed, v.pnl_delta_usd, v.decided_utc AS verdict_utc
                  FROM mutations m
                  LEFT JOIN verdicts v ON v.mutation_id = m.mutation_id
                 ORDER BY m.created_utc DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]


__all__ = ["Ledger"]

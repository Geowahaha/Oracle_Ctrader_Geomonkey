"""Dexter3 decision journal — SQLite substrate for the self-learning loop.

Every M5 decision (enter, skip, or manage) is written here, including full
reasoning and the lens feature snapshot at decision time. This is what lets
Phase 2+ compute empirical p_win_est by setup/session/regime and score the
"fear cost" of skipped decisions (blueprint KPIs).

Database: ``data/runtime/dexter3_journal.db`` (auto-created, WAL mode).

Tables:
  - ``decisions``      — one row per Decision contract instance (enter/skip/
                          manage), including the full features snapshot as
                          JSON and a UTC ISO-8601 ``created_at``.
  - ``basket_events``  — one row per BasketManager transition (open, repair
                          leg added, resolve, cap-stop), keyed by basket_id.
  - ``skip_outcomes``  — would-have-result for a skip decision, filled in
                          later by an evaluator (Phase 2+); starts NULL.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "runtime" / "dexter3_journal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_close TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    entry_type TEXT,
    entry REAL,
    sl REAL,
    tp REAL,
    size_class TEXT,
    leader_score REAL,
    p_win_est REAL,
    setup TEXT,
    reasons_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_symbol_ts ON decisions(symbol, ts_close);
CREATE INDEX IF NOT EXISTS idx_decisions_setup ON decisions(setup);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);
-- idx_decisions_label is created in _migrate_label_columns() below, AFTER
-- the label column is guaranteed to exist (a legacy pre-H2 DB file only
-- gets the column via the ALTER TABLE migration, and this CREATE TABLE IF
-- NOT EXISTS above is a no-op against an already-existing table, so an
-- index on decisions(label) declared here would 500 on that column not
-- existing yet on first connect against such a file).

CREATE TABLE IF NOT EXISTS basket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_basket_events_basket_id ON basket_events(basket_id);

CREATE TABLE IF NOT EXISTS skip_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    would_have_result TEXT,
    would_have_pnl REAL,
    evaluated_at TEXT,
    label TEXT,
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_skip_outcomes_decision_id ON skip_outcomes(decision_id);
"""

# Tables that gained a nullable ``label`` TEXT column for the H2 fix
# (2026-07-15 cross-lane entanglement audit): two live lanes (Fable/Grok/VP)
# share this one journal database, and none of these three tables previously
# carried any lane identity — so a lane-specific query (empirical stats,
# skip-fear-cost, basket telemetry) could only ever see the POOLED rows of
# every lane at once. ``_SCHEMA`` above already declares the column for
# brand-new DB files; ``_MIGRATE_LABEL_TABLES`` below is the idempotent
# backward-compatible migration for DB files created before this fix, using
# the SAME "PRAGMA table_info -> ALTER TABLE ADD COLUMN if missing" idiom
# already established by learning/neural_brain.py's signal_events migration
# (SQLite has no "ADD COLUMN IF NOT EXISTS").
_MIGRATE_LABEL_TABLES = ("decisions", "basket_events", "skip_outcomes")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DecisionJournal:
    """SQLite-backed journal. Auto-creates the DB directory and schema."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_label_columns()
        self._conn.commit()

    def _migrate_label_columns(self) -> None:
        """Idempotent migration (H2, 2026-07-15 cross-lane entanglement
        audit): add the nullable ``label`` TEXT column to every table listed
        in ``_MIGRATE_LABEL_TABLES`` for DB files created before this fix. A
        fresh DB already has the column via ``_SCHEMA`` above, so this is a
        no-op on every connect after the first migration — safe to run every
        time ``__init__`` runs (same posture as learning/neural_brain.py's
        own signal_events migration, which this mirrors)."""
        for table in _MIGRATE_LABEL_TABLES:
            cols = {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "label" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN label TEXT")
        # Only safe to create AFTER every table above is guaranteed to carry
        # the label column (see the comment in _SCHEMA above).
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_label ON decisions(label)")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DecisionJournal":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- decisions ----------------------------------------------------------

    def insert_decision(self, decision: Any, label: str | None = None) -> int:
        """Insert a Decision (hunter_brain.Decision or an equivalent dict).

        Accepts anything with a ``.to_dict()`` method (Decision dataclasses)
        or a plain dict matching the same contract fields.

        ``label`` (H2, 2026-07-15 cross-lane entanglement audit, optional):
        the ORDER label of the lane writing this row (e.g.
        ``dexter3.executor.LABEL`` / ``GROK_LABEL`` / ``VP_LABEL`` —
        typically ``dexter3.shadow_runner._active_order_label()``). ``None``
        (the default) preserves the pre-fix behavior of an unlabeled row.
        The ``Decision`` dataclass itself has no ``label`` field, so this is
        always the caller-supplied value, never read from ``payload``.
        """
        payload = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        reasons = payload.get("reasons") or []
        features = payload.get("features") or {}
        cur = self._conn.execute(
            """INSERT INTO decisions
               (ts_close, symbol, action, side, entry_type, entry, sl, tp,
                size_class, leader_score, p_win_est, setup, reasons_json,
                features_json, created_at, label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(payload.get("ts_close") or ""),
                str(payload.get("symbol") or ""),
                str(payload.get("action") or ""),
                payload.get("side"),
                payload.get("entry_type"),
                payload.get("entry"),
                payload.get("sl"),
                payload.get("tp"),
                payload.get("size_class"),
                payload.get("leader_score"),
                payload.get("p_win_est"),
                payload.get("setup"),
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(features, ensure_ascii=False, default=str),
                _utc_now_iso(),
                label,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_decision_features(self, decision_id: int, features: dict[str, Any]) -> None:
        """Re-serialize ``features_json`` for an already-inserted decision row.

        2026-07-22 B-tier verdict blocker: the run loop journals an enter
        decision BEFORE the entry gates run, but the gates stamp their
        verdict-critical evidence (``v16_entry_quality`` incl. ``b_tier``,
        ``anti_chase``, ``smart_exit`` meta, sizing selectors) into
        ``decision.features`` AFTER — so none of it ever reached the journal
        (measured: 0 of ~12 live ``pass_b_tier_scout`` admissions since 07-15
        were queryable; the B-tier verdict had to fall back to journalctl
        greps + timestamp guessing). The caller re-syncs features once, after
        the whole gate chain has run. UPDATE-only by design: never touches
        any other column, no-op on an unknown id.
        """
        self._conn.execute(
            "UPDATE decisions SET features_json = ? WHERE id = ?",
            (json.dumps(features or {}, ensure_ascii=False, default=str), int(decision_id)),
        )
        self._conn.commit()

    def recent_decisions(self, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM decisions"
        params: tuple[Any, ...] = ()
        if symbol:
            query += " WHERE symbol = ?"
            params = (symbol,)
        query += " ORDER BY id DESC LIMIT ?"
        params = params + (int(limit),)
        rows = self._conn.execute(query, params).fetchall()
        cols = [d[0] for d in self._conn.execute("SELECT * FROM decisions LIMIT 0").description]
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(cols, row))
            record["reasons"] = json.loads(record.pop("reasons_json") or "[]")
            record["features"] = json.loads(record.pop("features_json") or "{}")
            out.append(record)
        return out

    # -- basket events --------------------------------------------------------

    def insert_basket_event(
        self, basket_id: int, event: str, payload: dict[str, Any] | None = None, label: str | None = None
    ) -> int:
        """``label`` (H2, 2026-07-15 cross-lane entanglement audit, optional):
        same convention as ``insert_decision`` — the writing lane's own order
        label. ``None`` (default) preserves the pre-fix unlabeled row."""
        cur = self._conn.execute(
            "INSERT INTO basket_events (basket_id, event, payload_json, ts, label) VALUES (?, ?, ?, ?, ?)",
            (
                int(basket_id),
                str(event),
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                _utc_now_iso(),
                label,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent_basket_events(self, basket_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT id, basket_id, event, payload_json, ts, label FROM basket_events"
        params: tuple[Any, ...] = ()
        if basket_id is not None:
            query += " WHERE basket_id = ?"
            params = (int(basket_id),)
        query += " ORDER BY id DESC LIMIT ?"
        params = params + (int(limit),)
        rows = self._conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for row_id, bid, event, payload_json, ts, label in rows:
            out.append(
                {
                    "id": row_id,
                    "basket_id": bid,
                    "event": event,
                    "payload": json.loads(payload_json or "{}"),
                    "ts": ts,
                    "label": label,
                }
            )
        return out

    # -- skip outcomes --------------------------------------------------------

    def insert_skip_outcome(
        self,
        decision_id: int,
        would_have_result: str | None = None,
        would_have_pnl: float | None = None,
        label: str | None = None,
    ) -> int:
        """``label`` (H2, 2026-07-15 cross-lane entanglement audit, optional):
        same convention as ``insert_decision`` — normally the label of the
        decision row being evaluated (``dexter3.skip_evaluator`` stamps this
        from the decision's own ``label`` column so a skip_outcomes row can
        be filtered directly without a join). ``None`` (default) preserves
        the pre-fix unlabeled row."""
        cur = self._conn.execute(
            """INSERT INTO skip_outcomes (decision_id, would_have_result, would_have_pnl, evaluated_at, label)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(decision_id),
                would_have_result,
                would_have_pnl,
                _utc_now_iso() if would_have_result is not None else None,
                label,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

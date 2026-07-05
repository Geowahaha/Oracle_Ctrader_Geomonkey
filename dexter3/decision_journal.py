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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_symbol_ts ON decisions(symbol, ts_close);
CREATE INDEX IF NOT EXISTS idx_decisions_setup ON decisions(setup);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);

CREATE TABLE IF NOT EXISTS basket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_basket_events_basket_id ON basket_events(basket_id);

CREATE TABLE IF NOT EXISTS skip_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    would_have_result TEXT,
    would_have_pnl REAL,
    evaluated_at TEXT,
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_skip_outcomes_decision_id ON skip_outcomes(decision_id);
"""


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
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DecisionJournal":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- decisions ----------------------------------------------------------

    def insert_decision(self, decision: Any) -> int:
        """Insert a Decision (hunter_brain.Decision or an equivalent dict).

        Accepts anything with a ``.to_dict()`` method (Decision dataclasses)
        or a plain dict matching the same contract fields.
        """
        payload = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        reasons = payload.get("reasons") or []
        features = payload.get("features") or {}
        cur = self._conn.execute(
            """INSERT INTO decisions
               (ts_close, symbol, action, side, entry_type, entry, sl, tp,
                size_class, leader_score, p_win_est, setup, reasons_json,
                features_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

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

    def insert_basket_event(self, basket_id: int, event: str, payload: dict[str, Any] | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO basket_events (basket_id, event, payload_json, ts) VALUES (?, ?, ?, ?)",
            (int(basket_id), str(event), json.dumps(payload or {}, ensure_ascii=False, default=str), _utc_now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent_basket_events(self, basket_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT id, basket_id, event, payload_json, ts FROM basket_events"
        params: tuple[Any, ...] = ()
        if basket_id is not None:
            query += " WHERE basket_id = ?"
            params = (int(basket_id),)
        query += " ORDER BY id DESC LIMIT ?"
        params = params + (int(limit),)
        rows = self._conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for row_id, bid, event, payload_json, ts in rows:
            out.append(
                {
                    "id": row_id,
                    "basket_id": bid,
                    "event": event,
                    "payload": json.loads(payload_json or "{}"),
                    "ts": ts,
                }
            )
        return out

    # -- skip outcomes --------------------------------------------------------

    def insert_skip_outcome(
        self,
        decision_id: int,
        would_have_result: str | None = None,
        would_have_pnl: float | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO skip_outcomes (decision_id, would_have_result, would_have_pnl, evaluated_at)
               VALUES (?, ?, ?, ?)""",
            (
                int(decision_id),
                would_have_result,
                would_have_pnl,
                _utc_now_iso() if would_have_result is not None else None,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

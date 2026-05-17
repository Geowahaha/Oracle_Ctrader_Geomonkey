"""Tests for the Live Archetype Tournament."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.archetype_tournament import ArchetypeTournament


def _seed(db_path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL,
                created_utc TEXT,
                source TEXT,
                symbol TEXT,
                entry REAL,
                stop_loss REAL,
                position_id INTEGER,
                execution_meta_json TEXT
            )
            """
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        for i, r in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO execution_journal(created_ts, source, symbol, entry, stop_loss, position_id, execution_meta_json) VALUES(?,?,?,?,?,?,?)",
                (
                    now_ts - r.get("hours_ago", 1.0) * 3600.0,
                    r["source"],
                    r.get("symbol", "XAUUSD"),
                    r.get("entry", 2300.0),
                    r.get("stop_loss", 2295.0),
                    i,
                    json.dumps({"closed": {"pnl_usd": r["pnl"], "closed_utc": r.get("closed_utc", "2026-05-17T12:00:00Z")}}),
                ),
            )
        conn.commit()


def test_tournament_refresh_consumes_rows(tmp_path: Path):
    db = tmp_path / "j.db"
    _seed(db, [
        {"source": "scalp_xauusd:winner", "pnl": 12.0},
        {"source": "scalp_xauusd:winner", "pnl": 8.0},
        {"source": "fibo_xauusd", "pnl": -20.0},
    ])
    t = ArchetypeTournament(journal_db_path=db, lookback_days=30)
    n = t.refresh()
    assert n == 3
    snapshot = t.snapshot()
    assert snapshot.n_archetypes >= 1


def test_tournament_idempotent_on_repeat_refresh(tmp_path: Path):
    db = tmp_path / "j.db"
    _seed(db, [
        {"source": "scalp_xauusd:winner", "pnl": 5.0},
        {"source": "scalp_xauusd:winner", "pnl": 10.0},
    ])
    t = ArchetypeTournament(journal_db_path=db, lookback_days=30)
    n1 = t.refresh()
    n2 = t.refresh()
    assert n1 == 2
    assert n2 == 0  # second refresh sees no new position_ids


def test_tournament_multiplier_positive_for_top_archetype(tmp_path: Path):
    db = tmp_path / "j.db"
    # 5 strong winners on h12+sl_2_5_5 vs 5 strong losers on h22+sl_5_10.
    rows = []
    for _ in range(5):
        rows.append({
            "source": "scalp_xauusd:winner",
            "pnl": 30.0,
            "entry": 2300.0,
            "stop_loss": 2297.0,
            "closed_utc": "2026-05-17T12:00:00Z",
        })
    for _ in range(5):
        rows.append({
            "source": "fibo_xauusd",
            "pnl": -30.0,
            "entry": 2300.0,
            "stop_loss": 2293.0,
            "closed_utc": "2026-05-17T22:00:00Z",
        })
    _seed(db, rows)
    t = ArchetypeTournament(journal_db_path=db, lookback_days=30)
    t.refresh()
    multiplier_top = t.multiplier_for(
        source="scalp_xauusd:winner",
        closed_utc="2026-05-17T12:00:00Z",
        entry=2300.0,
        stop=2297.0,
    )
    multiplier_bottom = t.multiplier_for(
        source="fibo_xauusd",
        closed_utc="2026-05-17T22:00:00Z",
        entry=2300.0,
        stop=2293.0,
    )
    assert multiplier_top > 0
    assert multiplier_bottom < 0


def test_tournament_zero_for_unknown_key(tmp_path: Path):
    db = tmp_path / "j.db"
    _seed(db, [{"source": "scalp_xauusd:winner", "pnl": 10.0}])
    t = ArchetypeTournament(journal_db_path=db, lookback_days=30)
    t.refresh()
    assert t.multiplier_for(source="unknown", closed_utc="2026-05-17T12:00:00Z", entry=2300.0, stop=2297.0) == 0.0

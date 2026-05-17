"""Tests for the Regime-Switching Capital Allocator."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.family_allocator import FamilyAllocator


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
                position_id INTEGER,
                execution_meta_json TEXT
            )
            """
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        for i, r in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO execution_journal(created_ts, source, symbol, position_id, execution_meta_json) VALUES(?,?,?,?,?)",
                (
                    now_ts - r.get("hours_ago", 1.0) * 3600.0,
                    r["source"],
                    r.get("symbol", "XAUUSD"),
                    i,
                    json.dumps({"closed": {"pnl_usd": r["pnl"]}}),
                ),
            )
        conn.commit()


def test_allocator_no_data_returns_empty():
    alloc = FamilyAllocator(journal_db_path="/tmp/nonexistent.db")
    out = alloc.compute()
    assert out.shares == {}
    assert "no_journal_data" in out.reasons


def test_allocator_assigns_more_share_to_stronger_family(tmp_path: Path):
    db = tmp_path / "j.db"
    rows = []
    # Family A: 6 winners
    for _ in range(6):
        rows.append({"source": "family_a", "pnl": 30.0})
    # Family B: 6 losers
    for _ in range(6):
        rows.append({"source": "family_b", "pnl": -20.0})
    _seed(db, rows)
    # Ceiling 0.80 leaves enough headroom for one family to dominate (default
    # 0.40 ceiling would force the allocator to saturate both at 0.40).
    alloc = FamilyAllocator(journal_db_path=db, min_trades=5, ceiling_share=0.80, max_daily_shift=1.0)
    out = alloc.compute()
    assert "family_a" in out.shares and "family_b" in out.shares
    assert out.shares["family_a"] > out.shares["family_b"]


def test_allocator_enforces_floor_and_ceiling(tmp_path: Path):
    db = tmp_path / "j.db"
    rows = []
    for _ in range(20):
        rows.append({"source": "winner", "pnl": 100.0})
    for _ in range(5):
        rows.append({"source": "loser", "pnl": -50.0})
    _seed(db, rows)
    alloc = FamilyAllocator(
        journal_db_path=db,
        min_trades=5,
        floor_share=0.10,
        ceiling_share=0.30,
        max_daily_shift=1.0,
    )
    out = alloc.compute()
    for share in out.shares.values():
        assert 0.10 <= share <= 0.30 + 1e-6


def test_allocator_cold_start_keeps_floor(tmp_path: Path):
    db = tmp_path / "j.db"
    rows = []
    # Family A: 6 trades (above min_trades=5)
    for _ in range(6):
        rows.append({"source": "ready", "pnl": 10.0})
    # Family B: 1 trade (below min_trades)
    rows.append({"source": "fresh", "pnl": -5.0})
    _seed(db, rows)
    # Ceiling 0.95 leaves headroom for the eligible family to capture the rest.
    alloc = FamilyAllocator(
        journal_db_path=db, min_trades=5, floor_share=0.10,
        ceiling_share=0.95, max_daily_shift=1.0,
    )
    out = alloc.compute()
    assert out.shares["fresh"] >= 0.10
    # The eligible family captures most of the remaining 0.90 pool.
    assert out.shares["ready"] > out.shares["fresh"]


def test_allocator_daily_shift_cap_prevents_flapping(tmp_path: Path):
    db = tmp_path / "j.db"
    # Day 1 — only family A.
    rows = [{"source": "a", "pnl": 20.0} for _ in range(6)]
    _seed(db, rows)
    alloc = FamilyAllocator(
        journal_db_path=db, min_trades=5, floor_share=0.05, ceiling_share=0.95,
        max_daily_shift=0.10,
    )
    out1 = alloc.compute()
    assert abs(out1.shares.get("a", 0) - 0.95) < 1e-6 or abs(out1.shares.get("a", 0) - 1.0) < 1e-6

    # Day 2 — family B explodes with wins; family A stays positive.
    # Pre-existing previous holds family A near 1.0; shift cap should limit
    # how fast B can take share from A.
    rows.extend({"source": "b", "pnl": 50.0} for _ in range(10))
    _seed(db, rows)
    out2 = alloc.compute()
    # Family A's share is allowed to drop by at most 0.10 per call.
    assert out2.shares.get("a", 0) >= 0.95 - 0.10 - 1e-6

"""Tests for the Equity Governor."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.equity_governor import EquityGovernor


def _seed_pnl(db_path: Path, pnls: list[tuple[float, float]]) -> None:
    """pnls: list of (hours_ago, pnl_usd) tuples."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL,
                created_utc TEXT NOT NULL,
                position_id INTEGER,
                execution_meta_json TEXT
            )
            """
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        for i, (hr_ago, pnl) in enumerate(pnls, start=1):
            conn.execute(
                "INSERT INTO execution_journal(created_ts, created_utc, position_id, execution_meta_json) VALUES(?,?,?,?)",
                (
                    now_ts - hr_ago * 3600.0,
                    "x",
                    i,
                    json.dumps({"closed": {"pnl_usd": pnl}}),
                ),
            )
        conn.commit()


def test_equity_governor_cold_start_returns_neutral(tmp_path: Path):
    db = tmp_path / "j.db"
    _seed_pnl(db, [(1.0, 10.0), (2.0, -5.0)])  # only 2 trades
    gov = EquityGovernor(journal_db_path=db, min_trades=5)
    rec = gov.recommend()
    assert rec.recommended_multiplier == 1.0


def test_equity_governor_winning_streak_pushes_above_one(tmp_path: Path):
    db = tmp_path / "j.db"
    # 7 days of solid wins, with enough trades in both fast (≤24h) and slow windows.
    pnls = [(hr, 30.0) for hr in (1, 2, 5, 8, 12, 18, 22, 30, 50, 80, 120)]
    _seed_pnl(db, pnls)
    gov = EquityGovernor(journal_db_path=db, min_trades=5, target_pnl_fast=50.0, target_pnl_slow=100.0)
    rec = gov.recommend()
    assert rec.recommended_multiplier > 1.0
    assert rec.recommended_multiplier <= 1.5


def test_equity_governor_drawdown_pushes_below_one(tmp_path: Path):
    db = tmp_path / "j.db"
    pnls = [(hr, -30.0) for hr in (1, 2, 5, 8, 12, 18, 22, 30, 50, 80, 120)]
    _seed_pnl(db, pnls)
    gov = EquityGovernor(journal_db_path=db, min_trades=5)
    rec = gov.recommend()
    assert rec.recommended_multiplier < 1.0
    assert rec.recommended_multiplier >= 0.5


def test_equity_governor_takes_min_of_windows(tmp_path: Path):
    db = tmp_path / "j.db"
    # Slow window: huge wins. Fast window (last 24h): huge losses.
    pnls = []
    pnls.extend([(48.0 + i, 60.0) for i in range(8)])  # slow window winners
    pnls.extend([(1.0 + i * 0.5, -50.0) for i in range(8)])  # fast window losers
    _seed_pnl(db, pnls)
    gov = EquityGovernor(
        journal_db_path=db,
        fast_window_hours=24.0,
        slow_window_days=7.0,
        min_trades=5,
        target_pnl_fast=50.0,
        target_pnl_slow=300.0,
    )
    rec = gov.recommend()
    # Fast window says: shrink. Slow window says: grow. Output must follow fast.
    assert rec.recommended_multiplier < 1.0
    assert rec.fast_slice.total_pnl_usd < rec.slow_slice.total_pnl_usd

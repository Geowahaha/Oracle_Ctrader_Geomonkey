"""Tests for the Missed Opportunity Detector."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.missed_opportunity import (
    MissedOpportunityDetector,
    MissedRunnerStore,
)


def _seed_journal(db_path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL,
                created_utc TEXT,
                source TEXT,
                symbol TEXT,
                direction TEXT,
                entry REAL,
                position_id INTEGER,
                execution_meta_json TEXT
            )
            """
        )
        for i, r in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO execution_journal(created_ts, source, symbol, direction, entry, position_id, execution_meta_json) VALUES(?,?,?,?,?,?,?)",
                (
                    r["created_ts"],
                    r["source"],
                    r.get("symbol", "XAUUSD"),
                    r["direction"],
                    r["entry"],
                    r["position_id"],
                    json.dumps({
                        "closed": {
                            "pnl_usd": r["pnl"],
                            "exit_price": r["exit"],
                            "closed_utc": r["closed_utc"],
                            "post_close_price_60m": r.get("post_price"),
                        }
                    }),
                ),
            )
        conn.commit()


def test_detector_emits_event_when_continuation_outweighs_capture(tmp_path: Path):
    journal = tmp_path / "j.db"
    store_db = tmp_path / "m.db"
    store = MissedRunnerStore(store_db)
    base = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)
    # Mirror the live incident: sell 4550 → exit 4545 (5pt captured),
    # 60m later price at 4490 → 55pt continuation = 11x.
    _seed_journal(journal, [{
        "created_ts": base.timestamp(),
        "source": "scalp_xauusd:winner",
        "direction": "short",
        "entry": 4550.0,
        "position_id": 9001,
        "pnl": 50.0,
        "exit": 4545.0,
        "closed_utc": base.replace(hour=5, minute=56).isoformat().replace("+00:00", "Z"),
        "post_price": 4490.0,
    }])
    det = MissedOpportunityDetector(
        journal_db_path=journal, store=store,
        lookback_hours=24.0, evaluation_minutes_after_close=60.0,
        min_missed_factor=3.0, min_captured_pts=0.5,
    )
    events = det.scan(now=base.replace(hour=7, minute=10))
    assert len(events) == 1
    assert events[0].missed_factor >= 3.0
    assert events[0].captured_pts == 5.0
    assert events[0].continuation_pts == 55.0


def test_detector_skips_losing_trades(tmp_path: Path):
    journal = tmp_path / "j.db"
    store = MissedRunnerStore(tmp_path / "m.db")
    base = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)
    _seed_journal(journal, [{
        "created_ts": base.timestamp(),
        "source": "scalp_xauusd",
        "direction": "short",
        "entry": 4550.0,
        "position_id": 9002,
        "pnl": -33.0,  # loser — handled by loss-event trigger, not here
        "exit": 4553.3,
        "closed_utc": base.isoformat().replace("+00:00", "Z"),
        "post_price": 4480.0,
    }])
    det = MissedOpportunityDetector(journal_db_path=journal, store=store)
    events = det.scan(now=base.replace(hour=7, minute=10))
    assert events == []


def test_detector_skips_when_continuation_against_direction(tmp_path: Path):
    journal = tmp_path / "j.db"
    store = MissedRunnerStore(tmp_path / "m.db")
    base = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)
    # Short captured +5pt, but price retraced UP after exit → no missed-runner.
    _seed_journal(journal, [{
        "created_ts": base.timestamp(),
        "source": "scalp_xauusd:winner",
        "direction": "short",
        "entry": 4550.0,
        "position_id": 9003,
        "pnl": 50.0,
        "exit": 4545.0,
        "closed_utc": base.replace(hour=5, minute=56).isoformat().replace("+00:00", "Z"),
        "post_price": 4555.0,
    }])
    det = MissedOpportunityDetector(journal_db_path=journal, store=store)
    events = det.scan(now=base.replace(hour=7, minute=10))
    assert events == []


def test_detector_is_idempotent(tmp_path: Path):
    journal = tmp_path / "j.db"
    store = MissedRunnerStore(tmp_path / "m.db")
    base = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)
    _seed_journal(journal, [{
        "created_ts": base.timestamp(),
        "source": "scalp_xauusd:winner",
        "direction": "short",
        "entry": 4550.0,
        "position_id": 9004,
        "pnl": 44.0,
        "exit": 4545.0,
        "closed_utc": base.replace(hour=5, minute=56).isoformat().replace("+00:00", "Z"),
        "post_price": 4490.0,
    }])
    det = MissedOpportunityDetector(journal_db_path=journal, store=store)
    first = det.scan(now=base.replace(hour=7, minute=10))
    second = det.scan(now=base.replace(hour=7, minute=10))
    assert len(first) == 1
    assert second == []
    assert len(store.recent()) == 1


def test_detector_respects_evaluation_window(tmp_path: Path):
    journal = tmp_path / "j.db"
    store = MissedRunnerStore(tmp_path / "m.db")
    base = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)
    # Trade closed just 10 minutes ago — too fresh.
    _seed_journal(journal, [{
        "created_ts": base.timestamp(),
        "source": "scalp_xauusd:winner",
        "direction": "short",
        "entry": 4550.0,
        "position_id": 9005,
        "pnl": 44.0,
        "exit": 4545.0,
        "closed_utc": (base - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "post_price": 4490.0,
    }])
    det = MissedOpportunityDetector(
        journal_db_path=journal, store=store,
        evaluation_minutes_after_close=60.0,
    )
    events = det.scan(now=base)
    assert events == []

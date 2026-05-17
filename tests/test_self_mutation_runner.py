"""Tests for the counterfactual Runner.

The runner replays a synthetic execution_journal under a mutation. We use a
local SQLite DB seeded with hand-picked rows so the math is exact.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from learning.self_mutation.runner import CounterfactualRunner
from learning.self_mutation.types import KIND_NEEDS_PTS, KIND_OK


def _seed_journal(db_path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL,
                created_utc TEXT NOT NULL,
                source TEXT DEFAULT '',
                lane TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                direction TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                entry REAL DEFAULT 0,
                stop_loss REAL DEFAULT 0,
                take_profit REAL DEFAULT 0,
                entry_type TEXT DEFAULT '',
                position_id INTEGER,
                signal_run_id TEXT DEFAULT '',
                request_json TEXT DEFAULT '{}',
                execution_meta_json TEXT DEFAULT '{}'
            )
            """
        )
        for r in rows:
            conn.execute(
                """
                INSERT INTO execution_journal(
                    created_ts, created_utc, source, symbol, direction, entry,
                    stop_loss, take_profit, entry_type, position_id,
                    request_json, execution_meta_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["created_ts"], r["created_utc"], r["source"], r["symbol"],
                    r["direction"], r["entry"], r["stop_loss"], r["take_profit"],
                    r["entry_type"], r["position_id"],
                    json.dumps(r.get("request", {})),
                    json.dumps({"closed": r["closed"], **({"raw_scores": r.get("raw_scores", {})} if r.get("raw_scores") else {})}),
                ),
            )
        conn.commit()


def _now_ts() -> float:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).timestamp()


def _row(*, position_id: int, pnl: float, mode: str = "", entry_type: str = "limit", **kw) -> dict:
    base_ts = _now_ts() - 60 * 60 * 24  # 1 day ago
    return {
        "created_ts": base_ts,
        "created_utc": "2026-05-16T00:00:00Z",
        "source": "scalp_xauusd:winner",
        "symbol": "XAUUSD",
        "direction": kw.get("direction", "long"),
        "entry": kw.get("entry", 2300.0),
        "stop_loss": kw.get("stop_loss", 2295.0),
        "take_profit": kw.get("take_profit", 2310.0),
        "entry_type": entry_type,
        "position_id": position_id,
        "request": {"raw_scores": kw.get("raw_scores", {}), "risk_usd": kw.get("risk_usd", 3.0)},
        "closed": {"pnl_usd": pnl, "closed_utc": "2026-05-16T01:00:00Z"},
        "raw_scores": {
            "xau_openapi_entry_router": {"mode": mode, "continuation_score": kw.get("score", 0), "continuation_bias": kw.get("bias", 0.0)},
            **({"xau_pair_risk_cap_applied": True} if kw.get("pair_cap") else {}),
        },
    }


def test_runner_baseline_aggregates_pnl(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [
        _row(position_id=1, pnl=10.0),
        _row(position_id=2, pnl=-5.0),
        _row(position_id=3, pnl=20.0),
    ])
    runner = CounterfactualRunner(db_path=db, lookback_days=30)
    outcome = runner.run_baseline()
    assert outcome.kind == KIND_OK
    assert outcome.n_trades == 3
    assert outcome.total_pnl_usd == 25.0
    assert outcome.win_count == 2
    assert outcome.loss_count == 1


def test_runner_risk_multiplier_scales_pnl(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [
        _row(position_id=1, pnl=10.0, mode="wait_break_probe_stop"),
        _row(position_id=2, pnl=-4.0, mode="wait_break_probe_stop"),
        _row(position_id=3, pnl=5.0, mode="other"),  # not affected
    ])
    runner = CounterfactualRunner(db_path=db, lookback_days=30)
    out = runner.run_mutation(
        "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER",
        baseline_value=0.35,
        proposed_value=0.70,  # 2x
    )
    # Two wait_break rows scale 2x; the third stays.
    assert out.kind == KIND_OK
    assert out.total_pnl_usd == 10.0 * 2 + (-4.0) * 2 + 5.0


def test_runner_threshold_filter_removes_below_score(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [
        _row(position_id=1, pnl=10.0, mode="signal_market", score=8),
        _row(position_id=2, pnl=-12.0, mode="signal_market", score=6),
        _row(position_id=3, pnl=4.0, mode="other", score=2),  # not gated
    ])
    runner = CounterfactualRunner(db_path=db, lookback_days=30)
    # Raise threshold from 7 → 8. The score=6 row is filtered out.
    out = runner.run_mutation(
        "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE",
        baseline_value=7.0,
        proposed_value=8.0,
    )
    assert out.kind == KIND_OK
    assert out.n_trades == 2  # row 2 removed, row 1 and 3 remain
    assert out.total_pnl_usd == 10.0 + 4.0


def test_runner_pair_cap_scales_only_capped_rows(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [
        _row(position_id=1, pnl=10.0, pair_cap=True),
        _row(position_id=2, pnl=-6.0, pair_cap=False),
    ])
    runner = CounterfactualRunner(db_path=db, lookback_days=30)
    out = runner.run_mutation(
        "CTRADER_XAU_PAIR_RISK_MAX_USD",
        baseline_value=3.0,
        proposed_value=1.5,  # 0.5x cap
    )
    assert out.kind == KIND_OK
    # Row 1 is the capped trade: PnL halves. Row 2 is unaffected.
    assert out.total_pnl_usd == 10.0 * 0.5 + (-6.0)


def test_runner_needs_pts_for_guardian_runner_r(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [_row(position_id=1, pnl=5.0)])
    runner = CounterfactualRunner(db_path=db, lookback_days=30)
    out = runner.run_mutation(
        "XAU_GUARDIAN_RUNNER_PRESERVE_R",
        baseline_value=1.5,
        proposed_value=1.0,
    )
    assert out.kind == KIND_NEEDS_PTS


def test_runner_empty_db_is_insufficient_data(tmp_path: Path):
    runner = CounterfactualRunner(db_path=tmp_path / "missing.db", lookback_days=30)
    out = runner.run_baseline()
    assert out.n_trades == 0

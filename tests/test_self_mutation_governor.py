"""End-to-end test for the Self-Mutation Governor.

We seed a synthetic execution_journal with a loss event the trigger will pick
up, plus enough closed trades that the runner can compute a baseline and a
non-trivial mutation outcome. The governor then performs a full tick:
ingest → mutate → backtest → judge → promote canary.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.self_mutation.governor import Governor
from learning.self_mutation.ledger import Ledger
from learning.self_mutation.notify import RecordingNotifier
from learning.self_mutation.overrides import OverrideStore
from learning.self_mutation.promoter import Promoter
from learning.self_mutation.rollback import RollbackEngine
from learning.self_mutation.runner import CounterfactualRunner
from learning.self_mutation.sampler import KNOB_BY_NAME, Sampler
from learning.self_mutation.trigger import LossEventTrigger
from learning.self_mutation.verdict import VerdictEngine, VerdictThresholds


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
                    created_ts, created_utc, source, symbol, direction,
                    entry, stop_loss, take_profit, entry_type, position_id,
                    signal_run_id, request_json, execution_meta_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["created_ts"], r["created_utc"], r["source"], r["symbol"],
                    r["direction"], r["entry"], r["stop_loss"], r["take_profit"],
                    r["entry_type"], r["position_id"], r.get("signal_run_id", ""),
                    json.dumps(r.get("request", {})),
                    json.dumps({"closed": r["closed"], **({"raw_scores": r["raw_scores"]} if r.get("raw_scores") else {})}),
                ),
            )
        conn.commit()


def _row(*, position_id: int, pnl: float, mode: str = "signal_market", score: int = 6, ts_offset_hr: float = 1.0) -> dict:
    base_ts = (datetime.now(timezone.utc) - timedelta(hours=ts_offset_hr)).timestamp()
    return {
        "created_ts": base_ts,
        "created_utc": "2026-05-16T00:00:00Z",
        "source": "scalp_xauusd:winner",
        "symbol": "XAUUSD",
        "direction": "short",
        "entry": 2300.0,
        "stop_loss": 2305.0,
        "take_profit": 2290.0,
        "entry_type": "market",
        "position_id": position_id,
        "signal_run_id": f"run-{position_id}",
        "closed": {"pnl_usd": pnl, "closed_utc": "2026-05-16T01:00:00Z"},
        "raw_scores": {
            "xau_openapi_entry_router": {"mode": mode, "continuation_score": score, "continuation_bias": 0.65},
        },
    }


def _build_governor(*, db_path: Path, ledger_path: Path, overrides_path: Path, notifier: RecordingNotifier, enabled: bool) -> Governor:
    ledger = Ledger(ledger_path)
    overrides = OverrideStore(overrides_path)
    runner = CounterfactualRunner(db_path=db_path, lookback_days=30)

    def baseline_resolver(knob: str) -> float:
        return {
            "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE": 7.0,
            "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS": 0.70,
            "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER": 0.35,
            "XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO": 0.08,
            "XAU_GUARDIAN_RUNNER_PRESERVE_R": 1.5,
            "CTRADER_XAU_PAIR_RISK_MAX_USD": 3.0,
            "RISK_PER_TRADE": 0.01,
        }[knob]

    sampler = Sampler(
        baseline_resolver=baseline_resolver,
        last_mutation_resolver=lambda k: None,
    )
    verdict_engine = VerdictEngine(VerdictThresholds(
        min_pnl_delta_usd=5.0,  # low for test
        min_maxdd_tolerance_usd=50.0,
        min_t_score=0.0,
        min_n_trades=3,
    ))
    promoter = Promoter(
        ledger=ledger,
        overrides=overrides,
        runner=runner,
        verdict_engine=verdict_engine,
        notifier=notifier,
        canary_hours=24.0,
        canary_pass_margin_usd=5.0,
        dry_run=False,
    )
    rollback_engine = RollbackEngine(
        ledger=ledger,
        overrides=overrides,
        runner=runner,
        notifier=notifier,
        window_days=7.0,
        rollback_pnl_delta_usd=10.0,
        dry_run=False,
    )
    trigger = LossEventTrigger(
        journal_db_path=db_path,
        ledger=ledger,
        loss_threshold_usd=20.0,
    )
    return Governor(
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


def test_governor_disabled_is_noop(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [_row(position_id=1, pnl=-30.0)])
    notifier = RecordingNotifier()
    gov = _build_governor(
        db_path=db,
        ledger_path=tmp_path / "ledger.db",
        overrides_path=tmp_path / "overrides.json",
        notifier=notifier,
        enabled=False,
    )
    report = gov.tick()
    assert report.new_loss_events == 0
    assert notifier.messages == []


def test_governor_end_to_end_promotes_canary(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    # Build a journal that says: every signal_market trade with score<8 was a loss,
    # but if we'd required score>=8, those trades wouldn't have happened — total
    # PnL improves materially.
    rows = []
    rows.append(_row(position_id=1, pnl=-30.0, mode="signal_market", score=6))  # the loss event
    rows.append(_row(position_id=2, pnl=-15.0, mode="signal_market", score=6, ts_offset_hr=10))
    rows.append(_row(position_id=3, pnl=-12.0, mode="signal_market", score=5, ts_offset_hr=15))
    # Profitable rows that should survive a higher threshold.
    rows.append(_row(position_id=4, pnl=20.0, mode="signal_market", score=9, ts_offset_hr=20))
    rows.append(_row(position_id=5, pnl=18.0, mode="signal_market", score=9, ts_offset_hr=25))
    rows.append(_row(position_id=6, pnl=10.0, mode="signal_market", score=8, ts_offset_hr=30))
    _seed_journal(db, rows)

    notifier = RecordingNotifier()
    gov = _build_governor(
        db_path=db,
        ledger_path=tmp_path / "ledger.db",
        overrides_path=tmp_path / "overrides.json",
        notifier=notifier,
        enabled=True,
    )
    report = gov.tick()

    assert report.new_loss_events >= 1
    assert report.mutations_proposed >= 1
    assert report.canaries_promoted >= 1
    # Telegram-side received a promotion message.
    promotions = [m for m in notifier.messages if "promoted" in m]
    assert promotions, notifier.messages
    # Overrides file now has a canary entry.
    view = gov.overrides.read()
    assert view.canary, "expected a canary override after promotion"


def test_governor_tick_is_idempotent(tmp_path: Path):
    db = tmp_path / "ctrader.db"
    _seed_journal(db, [_row(position_id=1, pnl=-30.0, score=6)])
    notifier = RecordingNotifier()
    gov = _build_governor(
        db_path=db,
        ledger_path=tmp_path / "ledger.db",
        overrides_path=tmp_path / "overrides.json",
        notifier=notifier,
        enabled=True,
    )
    r1 = gov.tick()
    r2 = gov.tick()
    # The loss event is recorded only once; the second tick must not re-record it.
    assert r1.new_loss_events == 1
    assert r2.new_loss_events == 0

import json
import sqlite3
from types import SimpleNamespace

from analysis.signals import TradeSignal
from scheduler import DexterScheduler


def _winner_long_signal() -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction="long",
        confidence=71.2,
        entry=4701.73,
        stop_loss=4698.5,
        take_profit_1=4705.0,
        take_profit_2=4707.0,
        take_profit_3=4710.0,
        risk_reward=1.2,
        timeframe="5m+1m",
        session="london",
        trend="bullish",
        rsi=54.0,
        atr=3.0,
        pattern="SCALP_FLOW_FORCE",
        reasons=[],
        warnings=[],
        raw_scores={
            "scalp_force_m1_aligned_short": True,
            "scalp_force_m1_aligned_long": False,
            "scalp_m1_snapshot": {"close": 4704.9, "ema9": 4705.07, "rsi14": 43.666, "momentum": -0.30029},
            "scalping_trigger": {
                "ok": False,
                "reason": "m1_long_not_confirmed",
                "checks": {"ref_high_break": False, "prev_close_hold": False},
            },
        },
    )


def test_winner_context_guard_blocks_long_when_m1_rejects_and_correction_starts():
    sched = DexterScheduler.__new__(DexterScheduler)
    guard = sched._scalp_xau_winner_context_guard(_winner_long_signal())

    assert guard["allowed"] is False
    assert guard["reason"] == "winner_long_blocked_m1_rejection_correction"
    assert guard["suggested_action"] == "avoid_or_study_short"
    assert guard["features"]["m1_long_rejected"] is True
    assert guard["features"]["m1_long_correction"] is True


def test_winner_context_guard_blocks_long_when_trigger_says_m1_long_not_confirmed_even_if_snapshot_still_positive():
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _winner_long_signal()
    sig.raw_scores["scalp_force_m1_aligned_short"] = False
    sig.raw_scores["scalp_force_m1_aligned_long"] = True
    sig.raw_scores["scalp_m1_snapshot"] = {"close": 4710.5, "ema9": 4708.72328, "rsi14": 59.376, "momentum": 0.6001}
    guard = sched._scalp_xau_winner_context_guard(sig)

    assert guard["allowed"] is False
    assert guard["reason"] == "winner_long_blocked_m1_rejection_correction"
    assert guard["features"]["m1_long_rejected"] is True
    assert guard["features"]["m1_long_correction"] is False


def test_winner_mistake_learning_uses_position_direction_and_persists_bad_context(tmp_path, monkeypatch):
    db = tmp_path / "ctrader_openapi.db"
    raw_scores = _winner_long_signal().raw_scores
    request = {"direction": "long", "raw_scores": raw_scores}
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE ctrader_positions(
                position_id INTEGER PRIMARY KEY, journal_id INTEGER, source TEXT, symbol TEXT,
                direction TEXT, entry_price REAL, first_seen_utc TEXT, last_seen_utc TEXT, is_open INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE ctrader_deals(
                deal_id INTEGER PRIMARY KEY, position_id INTEGER, has_close_detail INTEGER,
                pnl_usd REAL, execution_price REAL, outcome INTEGER, direction TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE execution_journal(id INTEGER PRIMARY KEY, request_json TEXT)
            """
        )
        conn.execute(
            "INSERT INTO execution_journal(id, request_json) VALUES(?,?)",
            (8714, json.dumps(request)),
        )
        conn.execute(
            "INSERT INTO ctrader_positions VALUES(?,?,?,?,?,?,?,?,?)",
            (620679107, 8714, "scalp_xauusd:winner", "XAUUSD", "long", 4701.73, "2026-05-14T08:41:17Z", "2026-05-14T08:52:36Z", 0),
        )
        # close-side deal direction is short; the learner must still keep entry_direction=long from positions.
        conn.execute(
            "INSERT INTO ctrader_deals VALUES(?,?,?,?,?,?,?)",
            (899312890, 620679107, 1, -72.82, 4694.73, 0, "short"),
        )
        conn.commit()

    monkeypatch.setattr("scheduler.config.CTRADER_DB_PATH", str(db), raising=False)
    monkeypatch.setattr("scheduler.config.SCALP_XAU_WINNER_MISTAKE_LEARNING_ENABLED", True, raising=False)
    sched = DexterScheduler.__new__(DexterScheduler)
    report = sched._feed_xau_winner_mistake_learning({})

    assert report["seen"] == 1
    assert report["inserted"] == 1
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM xau_winner_mistake_journal WHERE position_id=620679107").fetchone()
    assert row is not None
    assert row["entry_direction"] == "long"
    assert row["mistake_type"] == "bought_into_m1_rejection_correction"
    assert row["recommended_action"] == "block_long_or_study_short"
    features = json.loads(row["features_json"])
    assert features["m1_aligned_short"] is True
    assert features["trigger_reason"] == "m1_long_not_confirmed"

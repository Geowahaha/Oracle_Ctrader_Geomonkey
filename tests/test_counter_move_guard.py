import sqlite3
import tempfile
from pathlib import Path

from analysis.counter_move_guard import (
    detect_counter_move,
    log_ghost_trade,
    evaluate,
)


def _bull(open_=2050.0, close=2050.5):
    return {"open": open_, "close": close}


def _bear(open_=2050.5, close=2050.0):
    return {"open": open_, "close": close}


def test_long_with_4_bear_bars_triggers_skip():
    candles = [_bear(), _bear(), _bear(), _bear(), _bull()]
    r = detect_counter_move("long", candles)
    assert r["strength"] == 4
    assert r["skip"] is True
    assert "counter_move" in r["reason"]


def test_short_with_4_bull_bars_triggers_skip():
    candles = [_bull(), _bull(), _bull(), _bull(), _bear()]
    r = detect_counter_move("short", candles)
    assert r["strength"] == 4
    assert r["skip"] is True


def test_long_with_3_bear_bars_does_not_skip():
    candles = [_bear(), _bear(), _bear(), _bull(), _bull()]
    r = detect_counter_move("long", candles)
    assert r["strength"] == 3
    assert r["skip"] is False


def test_long_with_aligned_bars_normal():
    candles = [_bull()] * 5
    r = detect_counter_move("long", candles)
    assert r["strength"] == 0
    assert r["skip"] is False


def test_insufficient_data_no_skip():
    r = detect_counter_move("long", [_bull(), _bull()])
    assert r["skip"] is False
    assert r["reason"] == "insufficient_data"


def test_bad_direction_no_skip():
    r = detect_counter_move("", [_bull()] * 5)
    assert r["skip"] is False


def test_never_raises_on_garbage():
    detect_counter_move("long", None)
    detect_counter_move("long", "not_iter")
    detect_counter_move("long", [{"open": "x"}] * 5)


def _make_test_db():
    """Create temp DB with xau_shadow_journal schema."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    f.close()
    con = sqlite3.connect(f.name)
    con.execute("""
        CREATE TABLE xau_shadow_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_utc TEXT, symbol TEXT, direction TEXT,
            confidence REAL, entry REAL, stop_loss REAL,
            take_profit_1 REAL, take_profit_2 REAL, take_profit_3 REAL,
            block_reason TEXT, raw_scores_json TEXT,
            shadow_outcome TEXT, resolved_utc TEXT, shadow_pnl_rr REAL
        )
    """)
    con.commit()
    con.close()
    return Path(f.name)


def test_log_ghost_trade_writes_row():
    db = _make_test_db()
    try:
        class S: pass
        s = S()
        s.symbol = "XAUUSD"
        s.direction = "short"
        s.confidence = 75.0
        s.entry = 2050.0
        s.stop_loss = 2052.0
        s.take_profit_1 = 2048.0
        s.take_profit_2 = 2046.0
        s.take_profit_3 = 2044.0
        s.raw_scores = {"counter_move_tag": {"strength": 4}}

        ok = log_ghost_trade(signal=s, block_reason="counter_move_skip:4_5", db_path=db)
        assert ok is True

        con = sqlite3.connect(str(db))
        rows = con.execute("SELECT symbol, direction, entry, block_reason, shadow_outcome FROM xau_shadow_journal").fetchall()
        con.close()
        assert len(rows) == 1
        assert rows[0][0] == "XAUUSD"
        assert rows[0][1] == "short"
        assert rows[0][2] == 2050.0
        assert "counter_move_skip" in rows[0][3]
        assert rows[0][4] == "pending"
    finally:
        db.unlink(missing_ok=True)


def test_log_ghost_trade_non_xau_returns_false():
    db = _make_test_db()
    try:
        class S: pass
        s = S(); s.symbol = "BTCUSD"; s.direction = "long"
        s.entry = 50000; s.stop_loss = 49500
        s.take_profit_1 = 50500; s.take_profit_2 = 51000; s.take_profit_3 = 51500
        s.raw_scores = {}; s.confidence = 70.0
        assert log_ghost_trade(signal=s, block_reason="x", db_path=db) is False
    finally:
        db.unlink(missing_ok=True)


def test_evaluate_disabled_returns_no_skip():
    class S: pass
    s = S(); s.symbol = "XAUUSD"; s.direction = "long"
    out = evaluate(s, enabled=False)
    assert out["skip"] is False


def test_evaluate_non_xau_no_op():
    class S: pass
    s = S(); s.symbol = "BTCUSD"; s.direction = "long"
    out = evaluate(s, enabled=True)
    assert out["skip"] is False


def test_evaluate_never_raises():
    evaluate(None, enabled=True)
    class Bad: pass
    evaluate(Bad(), enabled=True)

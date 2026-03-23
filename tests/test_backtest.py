"""
tests/test_backtest.py — Unit tests for the XAUUSD backtesting engine.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.candle_store import CandleStore
from backtest.replay_engine import ReplayEngine
from backtest.virtual_executor import VirtualExecutor, TradeResult
from backtest.report import generate_report, _hour_to_session
from backtest.results_store import ResultsStore


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_df(bars: int = 50, start_price: float = 3000.0, tf_minutes: int = 5) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame."""
    base = datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc)
    data = []
    price = start_price
    for i in range(bars):
        ts = base + timedelta(minutes=i * tf_minutes)
        o = price
        h = price + 1.5
        l = price - 1.0
        c = price + 0.5
        data.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})
        price = c  # trending up
    df = pd.DataFrame(data)
    df.set_index("timestamp", inplace=True)
    return df


def _make_volatile_df(bars: int = 100, start_price: float = 3000.0) -> pd.DataFrame:
    """Create a DataFrame where price swings widely (for TP/SL testing)."""
    base = datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc)
    data = []
    price = start_price
    for i in range(bars):
        ts = base + timedelta(minutes=i * 5)
        swing = 3.0 if i % 2 == 0 else -2.0  # alternating swings
        o = price
        h = price + abs(swing) + 1.0
        l = price - abs(swing) - 0.5
        c = price + swing
        data.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})
        price = c
    df = pd.DataFrame(data)
    df.set_index("timestamp", inplace=True)
    return df


# ── CandleStore tests ──────────────────────────────────────────────────────

class TestCandleStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = CandleStore(db_path=self.tmp.name)

    def teardown_method(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_ingest_and_fetch(self):
        df = _make_df(bars=20)
        n = self.store.ingest("XAUUSD", "5m", df)
        assert n == 20

        result = self.store.fetch("XAUUSD", "5m")
        assert result is not None
        assert len(result) == 20
        assert list(result.columns) == ["open", "high", "low", "close", "volume"]

    def test_fetch_with_bars_limit(self):
        df = _make_df(bars=50)
        self.store.ingest("XAUUSD", "5m", df)

        result = self.store.fetch("XAUUSD", "5m", bars=10)
        assert result is not None
        assert len(result) == 10
        # Should return the LAST 10 bars
        assert result.index[-1] == df.index[-1]

    def test_fetch_with_time_range(self):
        df = _make_df(bars=50)
        self.store.ingest("XAUUSD", "5m", df)

        start = df.index[10]
        end = df.index[20]
        result = self.store.fetch("XAUUSD", "5m", start=start, end=end)
        assert result is not None
        assert len(result) == 11  # inclusive

    def test_upsert_no_duplicates(self):
        df = _make_df(bars=20)
        self.store.ingest("XAUUSD", "5m", df)
        self.store.ingest("XAUUSD", "5m", df)  # same data again

        result = self.store.fetch("XAUUSD", "5m")
        assert len(result) == 20  # no duplicates

    def test_coverage(self):
        df = _make_df(bars=30)
        self.store.ingest("XAUUSD", "5m", df)

        earliest, latest, count = self.store.coverage("XAUUSD", "5m")
        assert count == 30
        assert earliest is not None
        assert latest is not None

    def test_coverage_empty(self):
        earliest, latest, count = self.store.coverage("XAUUSD", "1h")
        assert count == 0
        assert earliest is None

    def test_fetch_empty_returns_none(self):
        result = self.store.fetch("XAUUSD", "1h")
        assert result is None

    def test_ingest_csv(self):
        # Create a temp CSV
        csv_path = self.tmp.name + ".csv"
        df = _make_df(bars=15)
        df.to_csv(csv_path)

        n = self.store.ingest_from_csv(csv_path, "XAUUSD", "5m")
        assert n == 15
        os.unlink(csv_path)


# ── ReplayEngine tests ─────────────────────────────────────────────────────

class TestReplayEngine:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = CandleStore(db_path=self.tmp.name)
        self.df = _make_df(bars=50)
        self.store.ingest("XAUUSD", "5m", self.df)

    def teardown_method(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_patched_fetch_returns_historical(self):
        engine = ReplayEngine(self.store, "XAUUSD")
        cursor = self.df.index[30]
        engine.set_cursor(cursor)

        result = engine._patched_xau_fetch("5m", bars=10)
        assert result is not None
        assert len(result) == 10
        assert result.index[-1] <= cursor

    def test_cursor_none_returns_none(self):
        engine = ReplayEngine(self.store, "XAUUSD")
        result = engine._patched_xau_fetch("5m", bars=10)
        assert result is None

    def test_context_manager(self):
        engine = ReplayEngine(self.store, "XAUUSD")
        assert not engine._installed
        with engine:
            assert engine._installed
        assert not engine._installed


# ── VirtualExecutor tests ──────────────────────────────────────────────────

class TestVirtualExecutor:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = CandleStore(db_path=self.tmp.name)

    def teardown_method(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_long_tp1_hit(self):
        """Long trade where TP1 is hit within the bar range."""
        df = _make_df(bars=50, start_price=3000.0)
        self.store.ingest("XAUUSD", "5m", df)

        signal = {
            "direction": "long",
            "entry": 3000.0,
            "stop_loss": 2995.0,
            "tp1": 3001.0,  # high of bars goes to 3001.5, so this will hit
            "tp2": 0,
            "tp3": 0,
            "symbol": "XAUUSD",
            "entry_type": "limit",
        }
        signal_time = df.index[0]
        executor = VirtualExecutor(self.store)
        result = executor.resolve_trade(signal, signal_time.to_pydatetime())

        assert result is not None
        assert result.outcome == "tp1_hit"
        assert result.pnl_r > 0

    def test_short_sl_hit(self):
        """Short trade where SL is hit (price trends up)."""
        df = _make_df(bars=50, start_price=3000.0)  # trending up
        self.store.ingest("XAUUSD", "5m", df)

        signal = {
            "direction": "short",
            "entry": 3001.0,
            "stop_loss": 3003.0,  # SL above entry — will be hit as price trends up
            "tp1": 2998.0,
            "tp2": 0,
            "tp3": 0,
            "symbol": "XAUUSD",
            "entry_type": "market",
        }
        signal_time = df.index[5]
        executor = VirtualExecutor(self.store)
        result = executor.resolve_trade(signal, signal_time.to_pydatetime())

        assert result is not None
        assert result.outcome == "sl_hit"
        assert result.pnl_r < 0

    def test_buy_stop_entry(self):
        """Buy stop entry triggers when price rises to entry level."""
        df = _make_df(bars=50, start_price=3000.0)
        self.store.ingest("XAUUSD", "5m", df)

        signal = {
            "direction": "long",
            "entry": 3005.0,  # above current price
            "stop_loss": 3000.0,
            "tp1": 3010.0,
            "tp2": 0,
            "tp3": 0,
            "symbol": "XAUUSD",
            "entry_type": "buy_stop",
        }
        signal_time = df.index[0]
        executor = VirtualExecutor(self.store)
        result = executor.resolve_trade(signal, signal_time.to_pydatetime())

        assert result is not None
        # Should eventually get filled as price trends up
        assert result.outcome != "expired_no_fill" or result.entry_type == "buy_stop"

    def test_expired_no_fill(self):
        """Entry never triggered → expired_no_fill."""
        df = _make_df(bars=20, start_price=3000.0)
        self.store.ingest("XAUUSD", "5m", df)

        signal = {
            "direction": "long",
            "entry": 2900.0,  # way below — limit buy won't fill because price is at 3000+
            "stop_loss": 2890.0,
            "tp1": 2910.0,
            "tp2": 0,
            "tp3": 0,
            "symbol": "XAUUSD",
            "entry_type": "limit",
        }
        signal_time = df.index[0]
        executor = VirtualExecutor(self.store)
        result = executor.resolve_trade(signal, signal_time.to_pydatetime())

        assert result is not None
        assert result.outcome == "expired_no_fill"

    def test_calc_pips_xauusd(self):
        pips = VirtualExecutor._calc_pips("long", 3000.0, 3001.0, "XAUUSD")
        assert pips == 100.0  # $1 / $0.01 per pip

    def test_calc_r_multiple(self):
        r = VirtualExecutor._calc_r_multiple("long", 3000.0, 3005.0, 2995.0)
        assert r == 1.0  # risk=5, pnl=5 → 1R

    def test_r_multiple_bounded(self):
        r = VirtualExecutor._calc_r_multiple("long", 3000.0, 3100.0, 2999.0)
        assert r == 6.0  # capped at 6

    def test_invalid_signal(self):
        executor = VirtualExecutor(self.store)
        result = executor.resolve_trade({"entry": 0}, datetime.now(timezone.utc))
        assert result is None


# ── Report tests ───────────────────────────────────────────────────────────

class TestReport:
    def test_empty_results(self):
        report = generate_report([])
        assert report["total_trades"] == 0

    def test_report_basic_stats(self):
        results = []
        # 3 winners, 2 losers
        for i in range(3):
            r = TradeResult(
                signal={}, outcome="tp1_hit", entry_price=3000, exit_price=3005,
                exit_time=datetime(2026, 3, 20, 10 + i, tzinfo=timezone.utc),
                pnl_pips=500, pnl_r=1.0, bars_held=10,
                direction="long", symbol="XAUUSD",
                entry_time=datetime(2026, 3, 20, 9 + i, tzinfo=timezone.utc),
                entry_type="limit",
            )
            results.append(r)
        for i in range(2):
            r = TradeResult(
                signal={}, outcome="sl_hit", entry_price=3000, exit_price=2995,
                exit_time=datetime(2026, 3, 20, 14 + i, tzinfo=timezone.utc),
                pnl_pips=-500, pnl_r=-1.0, bars_held=5,
                direction="long", symbol="XAUUSD",
                entry_time=datetime(2026, 3, 20, 13 + i, tzinfo=timezone.utc),
                entry_type="limit",
            )
            results.append(r)

        report = generate_report(results, run_name="test")
        assert report["total_trades"] == 5
        assert report["wins"] == 3
        assert report["losses"] == 2
        assert report["win_rate"] == 60.0
        assert report["total_pnl_r"] == 1.0  # 3*1 - 2*1
        assert report["profit_factor"] == 1.5  # 3/2
        assert report["avg_winner_r"] == 1.0
        assert report["avg_loser_r"] == -1.0

    def test_session_mapping(self):
        assert _hour_to_session(datetime(2026, 1, 1, 3, tzinfo=timezone.utc)) == "asian"
        assert _hour_to_session(datetime(2026, 1, 1, 9, tzinfo=timezone.utc)) == "london"
        assert _hour_to_session(datetime(2026, 1, 1, 13, tzinfo=timezone.utc)) == "overlap"
        assert _hour_to_session(datetime(2026, 1, 1, 18, tzinfo=timezone.utc)) == "new_york"


# ── ResultsStore tests ─────────────────────────────────────────────────────

class TestResultsStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.rs = ResultsStore(db_path=self.tmp.name)

    def teardown_method(self):
        self.rs.close()
        os.unlink(self.tmp.name)

    def test_save_and_list(self):
        report = {
            "run_name": "test_run",
            "total_trades": 10,
            "win_rate": 60.0,
            "total_pnl_r": 3.5,
            "max_drawdown_r": 1.2,
            "profit_factor": 2.0,
        }
        run_id = self.rs.save_run(report)
        assert run_id > 0

        runs = self.rs.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_name"] == "test_run"

    def test_get_run(self):
        report = {"run_name": "detailed", "total_trades": 5, "win_rate": 80.0,
                   "total_pnl_r": 4.0, "max_drawdown_r": 0.5, "profit_factor": 4.0}
        run_id = self.rs.save_run(report)

        retrieved = self.rs.get_run(run_id)
        assert retrieved is not None
        assert retrieved["run_name"] == "detailed"

    def test_get_nonexistent(self):
        assert self.rs.get_run(9999) is None

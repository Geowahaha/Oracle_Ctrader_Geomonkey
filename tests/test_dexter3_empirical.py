"""Unit tests for dexter3/empirical_stats.py — Laplace-smoothed win rates by
(setup, session) and the base/empirical blend consumed by hunter_brain.decide().

Critical invariants under test (per Dexter3 Phase 2 spec):
  - p_win_estimates is pure (rows in, no DB) and Laplace-smoothed
  - below MIN_SAMPLES -> fall back to hunter_brain base rate (no blend)
  - at/above MIN_SAMPLES -> 50/50 blend with the empirical rate
  - compute_from_journal wrapper works against a real (empty or populated) DB
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dexter3 import empirical_stats as es
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import ensure_exec_events_table, insert_exec_event


# -- p_win_estimates (pure, no DB) ---------------------------------------------------


def _rows(*, setup: str, session: str, symbol: str = "BTCUSD", pnls: list[float]) -> list[dict]:
    return [{"symbol": symbol, "setup": setup, "session": session, "pnl": p} for p in pnls]


def test_p_win_estimates_empty_rows_returns_empty_dict():
    assert es.p_win_estimates([], "BTCUSD") == {}


def test_p_win_estimates_skips_rows_missing_setup_or_session():
    rows = [
        {"symbol": "BTCUSD", "setup": "", "session": "london", "pnl": 1.0},
        {"symbol": "BTCUSD", "setup": "leader_continuation", "session": "", "pnl": 1.0},
        {"symbol": "BTCUSD", "pnl": 1.0},
    ]
    assert es.p_win_estimates(rows, "BTCUSD") == {}


def test_p_win_estimates_filters_by_symbol():
    rows = _rows(setup="s1", session="london", symbol="XAUUSD", pnls=[1.0, -1.0, 1.0])
    assert es.p_win_estimates(rows, "BTCUSD") == {}
    out = es.p_win_estimates(rows, "XAUUSD")
    assert ("s1", "london") in out


def test_p_win_estimates_laplace_smoothed_win_rate_never_zero_or_one():
    # 5 wins, 0 losses -> naive rate would be 1.0; Laplace must pull it below 1.0
    rows = _rows(setup="s1", session="london", pnls=[1.0] * 5)
    out = es.p_win_estimates(rows, "BTCUSD")
    entry = out[("s1", "london")]
    assert entry["samples"] == 5
    assert entry["wins"] == 5
    assert entry["losses"] == 0
    assert 0.0 < entry["win_rate"] < 1.0
    # (5+1)/(5+2) = 0.8571
    assert entry["win_rate"] == pytest.approx(6 / 7, abs=1e-4)


def test_p_win_estimates_all_losses_never_exactly_zero():
    rows = _rows(setup="s1", session="ny", pnls=[-1.0] * 4)
    out = es.p_win_estimates(rows, "BTCUSD")
    entry = out[("s1", "ny")]
    # (0+1)/(4+2) = 0.1667
    assert entry["win_rate"] == pytest.approx(1 / 6, abs=1e-4)
    assert entry["win_rate"] > 0.0


def test_p_win_estimates_breakeven_pnl_excluded_from_denominator():
    rows = _rows(setup="s1", session="london", pnls=[1.0, -1.0, 0.0, 0.0])
    out = es.p_win_estimates(rows, "BTCUSD")
    entry = out[("s1", "london")]
    assert entry["samples"] == 2  # the two 0.0 rows are excluded
    assert entry["wins"] == 1
    assert entry["losses"] == 1


def test_p_win_estimates_below_min_samples_flag():
    rows = _rows(setup="s1", session="london", pnls=[1.0, -1.0, 1.0])  # 3 samples < MIN_SAMPLES=10
    out = es.p_win_estimates(rows, "BTCUSD")
    assert out[("s1", "london")]["below_min_samples"] is True


def test_p_win_estimates_at_min_samples_flag_false():
    rows = _rows(setup="s1", session="london", pnls=[1.0] * 6 + [-1.0] * 4)  # exactly 10
    out = es.p_win_estimates(rows, "BTCUSD")
    entry = out[("s1", "london")]
    assert entry["samples"] == 10
    assert entry["below_min_samples"] is False


def test_p_win_estimates_buckets_independently_by_setup_and_session():
    rows = (
        _rows(setup="s1", session="london", pnls=[1.0] * 3)
        + _rows(setup="s1", session="ny", pnls=[-1.0] * 3)
        + _rows(setup="s2", session="london", pnls=[1.0] * 2)
    )
    out = es.p_win_estimates(rows, "BTCUSD")
    assert set(out.keys()) == {("s1", "london"), ("s1", "ny"), ("s2", "london")}
    assert out[("s1", "london")]["wins"] == 3
    assert out[("s1", "ny")]["losses"] == 3


def test_p_win_estimates_accepts_explicit_won_bool_field():
    rows = [
        {"symbol": "BTCUSD", "setup": "s1", "session": "london", "won": True},
        {"symbol": "BTCUSD", "setup": "s1", "session": "london", "won": False},
    ]
    out = es.p_win_estimates(rows, "BTCUSD")
    entry = out[("s1", "london")]
    assert entry["wins"] == 1
    assert entry["losses"] == 1


def test_p_win_estimates_ignores_non_dict_rows():
    rows = [None, "not a dict", 42]
    assert es.p_win_estimates(rows, "BTCUSD") == {}


def test_p_win_estimates_symbol_defaults_from_row_when_present():
    """A row missing 'symbol' is treated as belonging to the queried symbol."""
    rows = [{"setup": "s1", "session": "london", "pnl": 1.0}]
    out = es.p_win_estimates(rows, "BTCUSD")
    assert ("s1", "london") in out


# -- blended_p_win --------------------------------------------------------------------


def test_blended_p_win_returns_base_when_stats_none():
    assert es.blended_p_win(0.52, "s1", "london", None) == 0.52


def test_blended_p_win_returns_base_when_stats_empty_dict():
    assert es.blended_p_win(0.52, "s1", "london", {}) == 0.52


def test_blended_p_win_returns_base_when_key_missing():
    stats = {("s2", "london"): {"win_rate": 0.9, "below_min_samples": False}}
    assert es.blended_p_win(0.52, "s1", "london", stats) == 0.52


def test_blended_p_win_returns_base_when_below_min_samples():
    stats = {("s1", "london"): {"win_rate": 0.9, "below_min_samples": True}}
    assert es.blended_p_win(0.52, "s1", "london", stats) == 0.52


def test_blended_p_win_blends_50_50_at_or_above_min_samples():
    stats = {("s1", "london"): {"win_rate": 0.80, "below_min_samples": False}}
    result = es.blended_p_win(0.50, "s1", "london", stats)
    # (0.5*0.50) + (0.5*0.80) = 0.65
    assert result == pytest.approx(0.65)


def test_blended_p_win_clamped_to_005_095_range():
    stats = {("s1", "london"): {"win_rate": 0.99, "below_min_samples": False}}
    result = es.blended_p_win(0.95, "s1", "london", stats)
    assert result <= 0.95
    stats_low = {("s1", "london"): {"win_rate": 0.0, "below_min_samples": False}}
    result_low = es.blended_p_win(0.05, "s1", "london", stats_low)
    assert result_low >= 0.05


# -- compute_from_journal (DB-facing wrapper) ------------------------------------------


@pytest.fixture()
def journal(tmp_path: Path) -> DecisionJournal:
    j = DecisionJournal(tmp_path / "empirical_test_journal.db")
    yield j
    j.close()


def test_compute_from_journal_empty_journal_returns_empty_dict(journal: DecisionJournal):
    assert es.compute_from_journal(journal, "BTCUSD") == {}


def test_compute_from_journal_accepts_bare_sqlite_connection(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    # No exec_events/basket_events tables at all -> must not raise
    assert es.compute_from_journal(conn, "BTCUSD") == {}
    conn.close()


def test_compute_from_journal_reads_closed_lane_outcomes_from_exec_events(journal: DecisionJournal):
    ensure_exec_events_table(journal._conn)
    for pnl in [1.0, 1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, 1.0, 1.0]:  # 10 samples, 7 wins
        insert_exec_event(
            journal._conn,
            symbol="BTCUSD",
            event="lane_position_closed",
            verified=True,
            payload={"setup": "leader_continuation", "session": "london", "pnl": pnl},
        )
    stats = es.compute_from_journal(journal, "BTCUSD")
    assert ("leader_continuation", "london") in stats
    entry = stats[("leader_continuation", "london")]
    assert entry["samples"] == 10
    assert entry["wins"] == 7
    assert entry["below_min_samples"] is False


def test_compute_from_journal_ignores_other_symbols(journal: DecisionJournal):
    ensure_exec_events_table(journal._conn)
    insert_exec_event(
        journal._conn,
        symbol="XAUUSD",
        event="lane_position_closed",
        verified=True,
        payload={"setup": "s1", "session": "london", "pnl": 1.0},
    )
    assert es.compute_from_journal(journal, "BTCUSD") == {}


def test_compute_from_journal_ignores_rows_missing_setup_or_session(journal: DecisionJournal):
    ensure_exec_events_table(journal._conn)
    insert_exec_event(
        journal._conn,
        symbol="BTCUSD",
        event="lane_position_closed",
        verified=True,
        payload={"pnl": 1.0},  # missing setup/session
    )
    assert es.compute_from_journal(journal, "BTCUSD") == {}


def test_stats_to_journal_stats_arg_flattens_tuple_keys():
    stats = {("s1", "london"): {"win_rate": 0.7, "samples": 12}}
    flat = es.stats_to_journal_stats_arg(stats)
    assert flat == {"s1|london": {"win_rate": 0.7, "samples": 12}}


# -- integration: hunter_brain.decide() blend wiring -----------------------------------


def test_hunter_brain_decide_uses_base_rate_when_journal_stats_none():
    from dexter3 import hunter_brain

    bars = []
    price = 2000.0
    for i in range(30):
        price += 0.5
        bars.append(
            {"open": price - 0.3, "high": price + 0.6, "low": price - 0.4, "close": price, "ts": f"2026-07-05T09:{i:02d}:00Z"}
        )
    d1 = hunter_brain.decide("XAUUSD", None, bars, journal_stats=None)
    d2 = hunter_brain.decide("XAUUSD", None, bars, journal_stats={})
    # Both None and {} must behave identically (no blend applied either way)
    assert d1.p_win_est == d2.p_win_est


def _ts_series(n: int, step_min: int = 5) -> list[str]:
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    return [(start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def _downtrend_then_lower_sweep(n: int = 29) -> list[dict]:
    """Downtrend into a lower-shelf sweep + bullish reclaim close -> sweep_reclaim enter.

    Mirrors tests/test_dexter3_brain.py's fixture of the same name (proven
    to produce action='enter') so this integration test exercises the real
    blend wiring in hunter_brain.decide() rather than a synthetic Decision.
    """
    ts = _ts_series(n + 1)
    bars = []
    price = 2000.0
    for i in range(n):
        o = price
        c = price - 0.8
        h = o + 0.1
        l = c - 0.2
        bars.append({"open": o, "high": h, "low": l, "close": c, "ts": ts[i]})
        price = c
    prior_low = min(b["low"] for b in bars[-10:])
    reclaim = {"open": price, "high": prior_low + 1.3, "low": prior_low - 1.0, "close": prior_low + 1.0, "ts": ts[n]}
    bars.append(reclaim)
    return bars


def test_hunter_brain_decide_blends_p_win_when_journal_stats_present_and_enters():
    from dexter3 import hunter_brain

    bars = _downtrend_then_lower_sweep()
    baseline = hunter_brain.decide("XAUUSD", None, bars, journal_stats=None)
    assert baseline.action == "enter"  # sanity: fixture is proven (see test_dexter3_brain.py)

    session_label = str(baseline.features.get("session_context", {}).get("value") or "unknown")
    stats = {(baseline.setup, session_label): {"win_rate": 0.90, "below_min_samples": False}}
    blended = hunter_brain.decide("XAUUSD", None, bars, journal_stats=stats)
    assert blended.action == "enter"
    assert blended.p_win_est != baseline.p_win_est
    assert blended.p_win_est > baseline.p_win_est  # empirical rate (0.90) pulls it up


def test_hunter_brain_decide_below_min_samples_leaves_p_win_unchanged():
    from dexter3 import hunter_brain

    bars = _downtrend_then_lower_sweep()
    baseline = hunter_brain.decide("XAUUSD", None, bars, journal_stats=None)
    assert baseline.action == "enter"

    session_label = str(baseline.features.get("session_context", {}).get("value") or "unknown")
    stats = {(baseline.setup, session_label): {"win_rate": 0.90, "below_min_samples": True}}
    unchanged = hunter_brain.decide("XAUUSD", None, bars, journal_stats=stats)
    assert unchanged.p_win_est == baseline.p_win_est

"""Unit tests for dexter3/skip_evaluator.py — the blueprint's "fear cost" KPI.

Critical invariants under test (per Dexter3 Phase 2 spec):
  - hit/miss/unevaluable simulation outcomes
  - side determined from the recorded feature snapshot only; no future-bar
    direction fallback is allowed
  - missing history is marked unevaluable, never guessed
  - evaluate_pending_skips only touches skip rows older than the delay,
    without an existing skip_outcomes row, and writes exactly one row each
  - fear_cost_summary aggregates correctly and excludes unevaluable rows
    from the win-count/pnl sum while still reporting their count

NO live MCP calls — a fake client exposing get_trendbars(symbol, period, count).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dexter3 import skip_evaluator as se
from dexter3.decision_journal import DecisionJournal


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _m5_ts(i: int, base_min: int = 0) -> str:
    total_min = base_min + i * 5
    hour, minute = divmod(total_min, 60)
    return f"2026-07-05T{hour:02d}:{minute:02d}:00Z"


class FakeMcp:
    def __init__(self, bars: list[dict]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str, int]] = []

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        self.calls.append((symbol, period, count))
        return list(self._bars)


STRONG_BUY_FEATURES = {
    "liquidity_sweep": {"value": True, "side": "buy", "evidence": "x"},
    "displacement": {"value": True, "direction": "buy"},
    "compression_release": {"value": False},
    "close_location_pressure": {"bias": "buy"},
    "swing_structure": {"value": "uptrend"},
}
STRONG_SELL_FEATURES = {
    "liquidity_sweep": {"value": True, "side": "sell", "evidence": "x"},
    "displacement": {"value": True, "direction": "sell"},
    "compression_release": {"value": False},
    "close_location_pressure": {"bias": "sell"},
    "swing_structure": {"value": "downtrend"},
}
WEAK_UNCLEAR_FEATURES = {
    "liquidity_sweep": {"value": False, "side": None},
    "displacement": {"value": False},
    "compression_release": {"value": False},
    "close_location_pressure": {"bias": None},
    "swing_structure": {"value": "range"},
}


# -- determine_candidate_side ---------------------------------------------------------


def test_determine_candidate_side_uses_strong_features_candidate():
    side, source = se.determine_candidate_side(STRONG_BUY_FEATURES)
    assert side == "buy"
    assert source == "features_candidate"


def test_determine_candidate_side_refuses_future_derived_direction_when_features_unclear():
    side, source = se.determine_candidate_side(WEAK_UNCLEAR_FEATURES)
    assert side is None
    assert source == "no_recorded_candidate"


def test_determine_candidate_side_none_when_both_unclear():
    side, source = se.determine_candidate_side(WEAK_UNCLEAR_FEATURES)
    assert side is None
    assert source == "no_recorded_candidate"


def test_determine_candidate_side_none_with_insufficient_bars_for_drift():
    side, source = se.determine_candidate_side(WEAK_UNCLEAR_FEATURES)
    assert side is None
    assert source == "no_recorded_candidate"


# -- simulate_would_have_trade ---------------------------------------------------------


def test_simulate_would_have_trade_hits_tp_first_buy():
    window_bars = [_bar(100, 101, 99, 100.5, "t0"), _bar(100.5, 106, 100, 105, "t1")]
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=98.0, tp=105.0, window_bars=window_bars)
    assert result["hit"] == "win"
    assert result["pnl_r"] == pytest.approx(2.5)  # reward=5, risk=2 -> 2.5R


def test_simulate_would_have_trade_hits_sl_first_buy():
    window_bars = [_bar(100, 101, 97, 98, "t0")]  # low touches SL 98
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=98.0, tp=110.0, window_bars=window_bars)
    assert result["hit"] == "loss"
    assert result["pnl_r"] == -1.0


def test_simulate_would_have_trade_hits_tp_first_sell():
    window_bars = [_bar(100, 100.5, 94, 95, "t0")]
    result = se.simulate_would_have_trade(side="sell", entry=100.0, sl=102.0, tp=95.0, window_bars=window_bars)
    assert result["hit"] == "win"
    assert result["pnl_r"] == pytest.approx(2.5)  # reward=5, risk=2


def test_simulate_would_have_trade_hits_sl_first_sell():
    window_bars = [_bar(100, 103, 99, 102, "t0")]
    result = se.simulate_would_have_trade(side="sell", entry=100.0, sl=102.0, tp=90.0, window_bars=window_bars)
    assert result["hit"] == "loss"
    assert result["pnl_r"] == -1.0


def test_simulate_would_have_trade_same_bar_ambiguous_assumes_loss():
    # a single wide bar spans both SL (98) and TP (105)
    window_bars = [_bar(100, 106, 97, 101, "t0")]
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=98.0, tp=105.0, window_bars=window_bars)
    assert result["hit"] == "loss"
    assert result["reason"] == "same_bar_ambiguous_assume_loss"


def test_simulate_would_have_trade_window_elapses_without_touch_marks_open_at_window_end():
    window_bars = [_bar(100, 101, 99, 100.5, "t0"), _bar(100.5, 101.5, 100, 101, "t1")]
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=90.0, tp=200.0, window_bars=window_bars)
    assert result["hit"] == "open_at_window_end"
    assert result["pnl_r"] == pytest.approx((101.0 - 100.0) / 10.0)


def test_simulate_would_have_trade_zero_risk_is_unevaluable():
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=100.0, tp=105.0, window_bars=[])
    assert result["hit"] == "unevaluable"
    assert result["reason"] == "zero_risk_distance"


def test_simulate_would_have_trade_no_bars_is_unevaluable():
    result = se.simulate_would_have_trade(side="buy", entry=100.0, sl=98.0, tp=105.0, window_bars=[])
    assert result["hit"] == "unevaluable"
    assert result["reason"] == "no_bars_in_window"


# -- evaluate_decision (single row) -----------------------------------------------------


def _decision_row(*, decision_id: int, symbol: str, ts_close: str, features: dict) -> dict:
    return {"id": decision_id, "symbol": symbol, "ts_close": ts_close, "features": features}


def test_evaluate_decision_wins_with_strong_features_candidate():
    ts_close = "2026-07-05T09:00:00Z"
    # bars covering [09:00, 09:30]: decision bar at 09:00, then a strong upmove
    bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T08:55:00Z"),  # lookback
        _bar(2000.0, 2001.0, 1999.5, 2000.0, "2026-07-05T09:00:00Z"),  # decision bar (entry=close)
        _bar(2000.0, 2010.0, 1999.5, 2008.0, "2026-07-05T09:05:00Z"),  # big up move -> should hit TP
    ]
    mcp = FakeMcp(bars)
    row = _decision_row(decision_id=1, symbol="XAUUSD", ts_close=ts_close, features=STRONG_BUY_FEATURES)
    result = se.evaluate_decision(row, mcp)
    assert result["evaluated"] is True
    assert result["decision_id"] == 1
    assert result["would_have_result"]["side"] == "buy"
    assert result["would_have_result"]["side_source"] == "features_candidate"


def test_evaluate_decision_unevaluable_with_no_history_in_window():
    ts_close = "2026-07-05T09:00:00Z"
    bars = [_bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T07:00:00Z")]  # nothing near ts_close
    mcp = FakeMcp(bars)
    row = _decision_row(decision_id=2, symbol="XAUUSD", ts_close=ts_close, features=STRONG_BUY_FEATURES)
    result = se.evaluate_decision(row, mcp)
    assert result["evaluated"] is False
    assert result["reason"] == "insufficient_history_for_window"


def test_evaluate_decision_unevaluable_when_no_determinable_side():
    ts_close = "2026-07-05T09:00:00Z"
    flat_bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T09:00:00Z"),
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T09:05:00Z"),
    ]
    mcp = FakeMcp(flat_bars)
    row = _decision_row(decision_id=3, symbol="XAUUSD", ts_close=ts_close, features=WEAK_UNCLEAR_FEATURES)
    result = se.evaluate_decision(row, mcp)
    assert result["evaluated"] is False
    assert result["reason"] == "no_determinable_side"


def test_evaluate_decision_unevaluable_on_invalid_row():
    result = se.evaluate_decision({"id": 4, "symbol": "", "ts_close": "", "features": {}}, FakeMcp([]))
    assert result["evaluated"] is False
    assert result["reason"] == "invalid_decision_row"


def test_evaluate_decision_unevaluable_on_mcp_error():
    from dexter3.mcp_client import McpClientError

    class RaisingMcp:
        def get_trendbars(self, *a, **kw):
            raise McpClientError("boom")

    row = _decision_row(decision_id=5, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features=STRONG_BUY_FEATURES)
    result = se.evaluate_decision(row, RaisingMcp())
    assert result["evaluated"] is False
    assert "mcp_error" in result["reason"]


def test_evaluate_decision_sell_setup_simulated_correctly():
    ts_close = "2026-07-05T09:00:00Z"
    bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T08:55:00Z"),
        _bar(2000.0, 2001.0, 1999.5, 2000.0, ts_close),
        _bar(2000.0, 2000.5, 1990.0, 1992.0, "2026-07-05T09:05:00Z"),  # sharp drop
    ]
    mcp = FakeMcp(bars)
    row = _decision_row(decision_id=6, symbol="XAUUSD", ts_close=ts_close, features=STRONG_SELL_FEATURES)
    result = se.evaluate_decision(row, mcp)
    assert result["evaluated"] is True
    assert result["would_have_result"]["side"] == "sell"


# -- evaluate_pending_skips (DB-facing) --------------------------------------------------


@pytest.fixture()
def journal(tmp_path: Path) -> DecisionJournal:
    j = DecisionJournal(tmp_path / "skipeval_test_journal.db")
    yield j
    j.close()


def _insert_skip_decision(journal: DecisionJournal, *, symbol: str, ts_close: str, features: dict) -> int:
    from dexter3.hunter_brain import Decision

    d = Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="skip",
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=0.3,
        p_win_est=0.0,
        setup="none",
        reasons=["test skip"],
        features=features,
    )
    return journal.insert_decision(d)


def test_evaluate_pending_skips_only_touches_eligible_rows(journal: DecisionJournal):
    now = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)
    old_id = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features=STRONG_BUY_FEATURES)
    too_recent_id = _insert_skip_decision(
        journal, symbol="XAUUSD", ts_close="2026-07-05T09:55:00Z", features=STRONG_BUY_FEATURES
    )

    bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T08:55:00Z"),
        _bar(2000.0, 2001.0, 1999.5, 2000.0, "2026-07-05T09:00:00Z"),
        _bar(2000.0, 2010.0, 1999.5, 2008.0, "2026-07-05T09:05:00Z"),
    ]
    mcp = FakeMcp(bars)

    result = se.evaluate_pending_skips(journal, mcp, now=now, delay_min=30)
    assert result["checked"] == 1  # only old_id is >=30min old

    rows = journal._conn.execute("SELECT decision_id FROM skip_outcomes").fetchall()
    decision_ids = {r[0] for r in rows}
    assert old_id in decision_ids
    assert too_recent_id not in decision_ids


def test_evaluate_pending_skips_does_not_reevaluate_already_evaluated_rows(journal: DecisionJournal):
    from datetime import datetime, timezone

    now = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)
    decision_id = _insert_skip_decision(
        journal, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features=STRONG_BUY_FEATURES
    )
    bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T08:55:00Z"),
        _bar(2000.0, 2001.0, 1999.5, 2000.0, "2026-07-05T09:00:00Z"),
        _bar(2000.0, 2010.0, 1999.5, 2008.0, "2026-07-05T09:05:00Z"),
    ]
    mcp = FakeMcp(bars)

    first = se.evaluate_pending_skips(journal, mcp, now=now, delay_min=30)
    assert first["checked"] == 1
    second = se.evaluate_pending_skips(journal, mcp, now=now, delay_min=30)
    assert second["checked"] == 0  # already has a skip_outcomes row

    rows = journal._conn.execute(
        "SELECT COUNT(*) FROM skip_outcomes WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    assert rows[0] == 1


def test_evaluate_pending_skips_writes_unevaluable_row_on_failure(journal: DecisionJournal):
    from datetime import datetime, timezone
    import json

    now = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)
    decision_id = _insert_skip_decision(
        journal, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features=WEAK_UNCLEAR_FEATURES
    )
    flat_bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T09:00:00Z"),
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-05T09:05:00Z"),
    ]
    mcp = FakeMcp(flat_bars)

    result = se.evaluate_pending_skips(journal, mcp, now=now, delay_min=30)
    assert result["unevaluable"] == 1
    assert result["evaluated"] == 0

    row = journal._conn.execute(
        "SELECT would_have_result, would_have_pnl FROM skip_outcomes WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["unevaluable"] is True
    assert row[1] is None


def test_evaluate_pending_skips_handles_empty_journal(journal: DecisionJournal):
    result = se.evaluate_pending_skips(journal, FakeMcp([]))
    assert result == {"checked": 0, "evaluated": 0, "unevaluable": 0}


# -- fear_cost_summary ------------------------------------------------------------------


def test_fear_cost_summary_aggregates_evaluated_rows_only(journal: DecisionJournal):
    import json
    from datetime import datetime, timezone

    d1 = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features=STRONG_BUY_FEATURES)
    d2 = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:05:00Z", features=STRONG_BUY_FEATURES)
    d3 = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:10:00Z", features=STRONG_BUY_FEATURES)

    journal.insert_skip_outcome(d1, would_have_result=json.dumps({"hit": "win"}), would_have_pnl=1.5)
    journal.insert_skip_outcome(d2, would_have_result=json.dumps({"hit": "loss"}), would_have_pnl=-1.0)
    journal.insert_skip_outcome(d3, would_have_result=json.dumps({"unevaluable": True}), would_have_pnl=None)

    summary = se.fear_cost_summary(journal, hours=24, now=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc))
    assert summary["skips_evaluated"] == 2
    assert summary["unevaluable"] == 1
    assert summary["invalid_lookahead"] == 0
    assert summary["would_have_wins"] == 1
    assert summary["would_have_pnl_r"] == pytest.approx(0.5)


def test_fear_cost_summary_empty_journal_returns_zeros(journal: DecisionJournal):
    summary = se.fear_cost_summary(journal, hours=24)
    assert summary == {
        "hours": 24,
        "skips_evaluated": 0,
        "unevaluable": 0,
        "invalid_lookahead": 0,
        "would_have_wins": 0,
        "would_have_pnl_r": 0.0,
    }


def test_fear_cost_summary_excludes_legacy_lookahead_rows(journal: DecisionJournal):
    import json

    legacy = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:00:00Z", features={})
    valid = _insert_skip_decision(journal, symbol="XAUUSD", ts_close="2026-07-05T09:05:00Z", features={})
    journal.insert_skip_outcome(
        legacy,
        would_have_result=json.dumps({"hit": "win", "side_source": "day_range_drift"}),
        would_have_pnl=1.2,
    )
    journal.insert_skip_outcome(
        valid,
        would_have_result=json.dumps({"hit": "loss", "side_source": "features_candidate"}),
        would_have_pnl=-1.0,
    )

    summary = se.fear_cost_summary(journal, hours=24, now=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc))
    assert summary["invalid_lookahead"] == 1
    assert summary["skips_evaluated"] == 1
    assert summary["would_have_pnl_r"] == pytest.approx(-1.0)


def test_fear_cost_summary_excludes_rows_outside_hours_window(journal: DecisionJournal):
    import json

    old_decision = _insert_skip_decision(
        journal, symbol="XAUUSD", ts_close="2020-01-01T00:00:00Z", features=STRONG_BUY_FEATURES
    )
    journal.insert_skip_outcome(old_decision, would_have_result=json.dumps({"hit": "win"}), would_have_pnl=2.0)

    summary = se.fear_cost_summary(journal, hours=24, now=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc))
    assert summary["skips_evaluated"] == 0

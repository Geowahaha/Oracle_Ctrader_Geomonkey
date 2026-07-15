"""Regression tests for the 2026-07-15 cross-lane incident triad:

  1. executor.py ``reconcile_vanished_lane_positions`` — lane-label filter +
     defer-not-guess when a candidate's closing deal is not yet visible.
  2. shadow_runner.py ``_lane_realized_today`` — UTC-day cache invalidation
     (a fetch computed just before midnight must not keep being served into
     the new UTC day) + ``_governor_entry_risk`` pre-entry loss-cap refusal.
  3. shadow_runner.py ``_apply_v16_entry_quality_gate`` — grok minimum
     leader_score floor (grok bypasses the v16 gate entirely otherwise).

Plus the one-shot repair script for the pre-fix poisoned journal backlog,
``ops/dexter3_repair_cross_lane_vanish.py``.

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB, same conventions as
tests/test_dexter3_executor.py and tests/test_dexter3_wiring.py.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import (
    Dexter3Executor,
    ExecutorConfig,
    ensure_exec_events_table,
    insert_exec_event,
    position_id_of,
    recent_exec_events,
)
from dexter3.grok_v10 import GROK_LABEL


# ---------------------------------------------------------------------------
# shared fixtures / fakes (mirrors tests/test_dexter3_executor.py)
# ---------------------------------------------------------------------------


class FakeMcp:
    """Records calls; canned responses configurable per test via attrs."""

    def __init__(self) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self._positions: list[dict] = []
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.deals: list[dict] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {}))
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {}))
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        if self.post_entry_position is not None and any(c[0] == "place_market_order" for c in self.calls):
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        self.calls.append(("get_balance", {}))
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        self.calls.append(("amend_position", {"position_id": position_id}))
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        self.calls.append(("close_position", {"position_id": position_id}))
        self._positions = [p for p in self._positions if position_id_of(p) != position_id]
        return {"status": "closed"}

    def get_deals(self, count: int = 200) -> list[dict]:
        self.calls.append(("get_deals", {"count": count}))
        return list(self.deals)


class FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="BTCUSD", action="enter", side="buy", entry=62000.0, sl=61900.0, tp=62150.0,
            setup="leader_continuation", reasons=["test_reason"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


DEMO_ACCOUNT = {"traderId": 9922808}


def _entered(ex: Dexter3Executor, mcp: FakeMcp, pid: int, **decision_kw: Any) -> dict:
    """Drive a real entry so entry_executed exists for pid, stamped with
    whatever dexter3.executor.LABEL is active right now."""
    mcp.post_entry_position = {
        "positionId": pid, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": ex_mod.LABEL,
    }
    result = ex.execute_entry(FakeDecision(**decision_kw), DEMO_ACCOUNT)
    assert result["action"] == "entered" and result["position_id"] == pid
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None
    return result


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "cross_lane_journal.db")
    yield j
    j.close()


# ---------------------------------------------------------------------------
# 1. reconcile_vanished_lane_positions — cross-lane isolation
# ---------------------------------------------------------------------------


def test_reconcile_never_touches_a_foreign_lanes_open_elsewhere_entry(journal, monkeypatch):
    mcp = FakeMcp()

    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    ex_fable = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex_fable, mcp, 501, setup="dragon_shelf_short", session="overlap")

    monkeypatch.setattr(ex_mod, "LABEL", GROK_LABEL)
    ex_grok = Dexter3Executor(mcp, journal, ExecutorConfig())

    # Position 501 is still open under Fable's label — invisible to Grok's
    # own label-filtered lane list, so it LOOKS vanished from Grok's view.
    # Its zero-pnl entry-leg deal is enough to satisfy a naive "found a pnl"
    # check, which is exactly how the 2026-07-15 incident fabricated a close.
    mcp.deals = [{"positionId": 501, "netProfit": 0.0}]
    assert ex_grok.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)

    # Fable's OWN reconcile pass, with a real closing deal in the window,
    # still works correctly.
    mcp.deals = [{"positionId": 501, "netProfit": 0.0}, {"positionId": 501, "netProfit": -4.0}]
    out = ex_fable.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1
    assert out[0]["position_id"] == 501
    assert out[0]["pnl"] == pytest.approx(-4.0)


# ---------------------------------------------------------------------------
# 2. reconcile_vanished_lane_positions — defer, never guess pnl=0.0
# ---------------------------------------------------------------------------


def test_reconcile_defers_own_lane_candidate_when_closing_deal_not_yet_visible(journal, monkeypatch):
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 601)

    # deals fetch succeeds but carries nothing at all for pid 601 this round
    mcp.deals = [{"positionId": 999, "netProfit": 3.0}]
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)
    assert not any(e["event"] == "vanish_reconcile_deferred" for e in events)  # per-candidate defer, not the whole-round path

    # closing deal appears next round -> reconciles for real, no guess
    mcp.deals = [{"positionId": 601, "netProfit": 2.4}]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1 and out[0]["pnl"] == pytest.approx(2.4)


def test_reconcile_ignores_own_lane_entry_leg_when_close_detail_marker_present(journal, monkeypatch):
    """Openapi-normalized deals stamp netProfit=0.0 on ENTRY legs too — an
    own-lane candidate whose only visible deal is its zero-pnl entry leg must
    DEFER (hasCloseDetail=False), not journal a pnl=0.0 guess."""
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 701)

    # only the entry leg is in the window (openapi shape: marker present, False)
    mcp.deals = [{"positionId": 701, "netProfit": 0.0, "hasCloseDetail": False}]
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)

    # close leg arrives -> real pnl journaled, entry leg still excluded
    mcp.deals = [
        {"positionId": 701, "netProfit": 0.0, "hasCloseDetail": False},
        {"positionId": 701, "netProfit": -4.47, "hasCloseDetail": True},
    ]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1 and out[0]["pnl"] == pytest.approx(-4.47)


# ---------------------------------------------------------------------------
# 3a. _lane_realized_today — UTC-day cache invalidation across midnight
# ---------------------------------------------------------------------------


def _ms(iso: str) -> int:
    return int(_dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _frozen_datetime(iso: str):
    frozen = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))

    class _Frozen:
        strptime = _dt.datetime.strptime
        fromisoformat = _dt.datetime.fromisoformat
        fromtimestamp = _dt.datetime.fromtimestamp

        @staticmethod
        def now(tz=None):
            return frozen

    return _Frozen


def test_lane_realized_today_only_counts_the_current_utc_day(monkeypatch):
    monkeypatch.setattr(sr, "_LANE_REALIZED_CACHE", {})
    mcp = FakeMcp()
    mcp.deals = [
        {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0, "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
        {"label": "dexter3:fable:m5h-v1", "netProfit": -5.0, "execution_timestamp_ms": _ms("2026-07-15T00:00:10Z")},
    ]
    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-15T00:05:00Z"))
    realized, pnls = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized == pytest.approx(-5.0)
    assert pnls == [pytest.approx(-5.0)]


def test_lane_realized_today_cache_invalidates_across_utc_midnight(monkeypatch):
    """The exact 2026-07-15 incident mechanism: a fetch computed just before
    UTC midnight must not keep being served (within the 60s cache window)
    once the calendar day has rolled over."""
    monkeypatch.setattr(sr, "_LANE_REALIZED_CACHE", {})
    calls = {"n": 0}

    class _MultiCallMcp:
        def get_deals(self, count: int = 500) -> list[dict]:
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0,
                     "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
                ]
            return [
                {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0,
                 "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
                {"label": "dexter3:fable:m5h-v1", "netProfit": -2.0,
                 "execution_timestamp_ms": _ms("2026-07-15T00:00:05Z")},
            ]

    mcp = _MultiCallMcp()

    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-14T23:59:50Z"))
    realized1, _ = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized1 == pytest.approx(-20.0)

    # Only 30s later (inside LANE_REALIZED_CACHE_SEC=60) but a NEW UTC day —
    # must refetch and reflect ONLY today's -2.0, not the cached -20.0.
    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-15T00:00:20Z"))
    realized2, pnls2 = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized2 == pytest.approx(-2.0)
    assert pnls2 == [pytest.approx(-2.0)]
    assert calls["n"] == 2  # proves it actually refetched instead of serving stale cache


# ---------------------------------------------------------------------------
# 4. _governor_entry_risk — pre-entry loss-cap refusal
# ---------------------------------------------------------------------------


def _reset_governor(monkeypatch: pytest.MonkeyPatch, *, daily_loss_usd: str = "15") -> None:
    monkeypatch.setattr(sr, "_GOVERNOR_INSTANCE", None)
    monkeypatch.setenv("DEXTER3_DAILY_LOSS_USD", daily_loss_usd)
    monkeypatch.delenv("DEXTER3_MODE", raising=False)


def test_governor_entry_risk_refuses_when_cap_already_breached(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_allows_when_below_cap(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-5.0, [-5.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result.get("allow", True) is True
    assert "risk_usd" in result


def test_governor_entry_risk_counts_floating_toward_the_cap(monkeypatch):
    """'realized + current floating if already available in state'."""
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-10.0, [-10.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    state = {"governor": {"floating_by_symbol": {"XAUUSD": -6.0}}}  # -10 + -6 = -16 <= -15 cap
    result = sr._governor_entry_risk(FakeMcp(), d, state)
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_reuses_the_same_cap_run_governor_tick_uses(monkeypatch):
    """No duplicated constant — both read _get_governor().config.daily_loss_usd."""
    _reset_governor(monkeypatch, daily_loss_usd="9")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-9.0, [-9.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result["allow"] is False
    assert sr._get_governor().config.daily_loss_usd == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# 5. _apply_v16_entry_quality_gate — grok minimum leader_score floor
# ---------------------------------------------------------------------------


def test_grok_min_leader_score_floor_blocks_low_score_by_default(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = FakeDecision(leader_score=0.056, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is False
    assert result["reason"] == "grok_min_leader_score"


def test_grok_min_leader_score_floor_allows_above_default(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = FakeDecision(leader_score=0.25, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is True
    assert result["reason"] == "grok_bypass"


def test_grok_min_leader_score_floor_disabled_by_non_positive_env(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.setenv("DEXTER3_GROK_MIN_LEADER_SCORE", "0")
    d = FakeDecision(leader_score=0.056, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is True
    assert result["reason"] == "grok_bypass"


def test_grok_min_leader_score_floor_handles_missing_leader_score_as_zero(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = SimpleNamespace(symbol="XAUUSD", setup="s", features={})  # no leader_score attr at all
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is False
    assert result["reason"] == "grok_min_leader_score"


def test_fable_mode_is_unaffected_by_the_grok_leader_score_floor(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    d = FakeDecision(
        symbol="XAUUSD", setup="hunt_test", leader_score=0.0, features={},
    )
    result = sr._apply_v16_entry_quality_gate({}, d)
    # a leader_score of 0.0 would fail the grok floor if it wrongly applied
    # here — fable mode must run the real v16 gate instead.
    assert result["reason"] not in ("grok_bypass", "grok_min_leader_score")
    assert d.features.get("grok_bypass") is None
    assert "v16_entry_quality" in d.features


# ---------------------------------------------------------------------------
# 6. ops/dexter3_repair_cross_lane_vanish.py
# ---------------------------------------------------------------------------


def test_repair_script_dry_run_lists_only_poisoned_then_apply_deletes_only_it(tmp_path):
    from ops.dexter3_repair_cross_lane_vanish import find_poisoned_rows, main

    db_path = tmp_path / "repair_journal.db"
    conn = sqlite3.connect(str(db_path))
    ensure_exec_events_table(conn)
    # poisoned: vanish-writer signature (reconciled=True) + pnl exactly 0.0
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=701, verified=True,
        payload={
            "reason": "broker_side_close_reconciled", "label": "dexter3:fable:m5h-v1",
            "pnl": 0.0, "exit_reason": "broker_side_close", "reconciled": True,
        },
    )
    # legit: same vanish signature, but a REAL nonzero pnl -> must NOT be listed
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=702, verified=True,
        payload={
            "reason": "broker_side_close_reconciled", "label": "dexter3:grok-v1.0:scalper",
            "pnl": -6.4, "exit_reason": "broker_side_close", "reconciled": True,
        },
    )
    conn.close()

    conn_check = sqlite3.connect(str(db_path))
    poisoned = find_poisoned_rows(conn_check)
    assert len(poisoned) == 1
    assert poisoned[0]["position_id"] == 701
    conn_check.close()

    # dry-run (default): lists but changes nothing
    rc = main(["--db", str(db_path)])
    assert rc == 0
    conn_after_dry = sqlite3.connect(str(db_path))
    assert conn_after_dry.execute("SELECT COUNT(*) FROM exec_events").fetchone()[0] == 2
    conn_after_dry.close()

    # --apply: deletes exactly the poisoned row
    rc2 = main(["--db", str(db_path), "--apply"])
    assert rc2 == 0
    conn_after_apply = sqlite3.connect(str(db_path))
    remaining = [r[0] for r in conn_after_apply.execute("SELECT position_id FROM exec_events").fetchall()]
    conn_after_apply.close()
    assert remaining == [702]


def test_repair_script_never_touches_rows_without_the_vanish_signature(tmp_path):
    from ops.dexter3_repair_cross_lane_vanish import main

    db_path = tmp_path / "repair_journal_manual.db"
    conn = sqlite3.connect(str(db_path))
    ensure_exec_events_table(conn)
    # a normal manual close (close_lane_position's own writer) — no
    # "reconciled" key at all, even though it also happens to be pnl=0.0
    # (breakeven) — must never be treated as poisoned.
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=703, verified=True,
        payload={"reason": "om_exit", "label": "dexter3:fable:m5h-v1", "pnl": 0.0, "exit_reason": "om_exit"},
    )
    conn.close()

    rc = main(["--db", str(db_path), "--apply"])
    assert rc == 0
    conn2 = sqlite3.connect(str(db_path))
    assert conn2.execute("SELECT COUNT(*) FROM exec_events").fetchone()[0] == 1
    conn2.close()

"""Tests for tonight's two additive fixes from the 2026-07-15 17:04Z-00:54Z
live-audit (docs/AGENT_SYNC_BOARD.md):

FIX #1 -- cross-lane duplicate-entry guard (``dexter3/executor.py``): both
the fable and grok lanes run the SAME producer (``decide_hunt``) over the
SAME bars, so they fired near-identical twin trades on the same
symbol+side within minutes of each other with zero diversification (net
-34.00 / 19 trades that window). Env ``DEXTER3_CROSS_LANE_DEDUP``:
off (default) | skip (refuse) | downsize (halve risk instead of refusing).

FIX #2 -- harvest shadow multi-leg observation (``dexter3/shadow_runner.py``):
the Repair-Scalp Harvester's single-leg-only episode-open requirement
starves it of shadow data whenever the basket's OWN repair-leg engine adds
a second leg before aggregate_r reaches -trigger_r. Env
``DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE=1`` (shadow mode ONLY) relaxes
that requirement, using the basket's NET direction instead of a single
parent leg.

NO live MCP calls, NO touching data/runtime/* -- every test drives a fake
transport / temp sqlite DB, same conventions as
tests/test_dexter3_lane_decoupling.py and tests/test_dexter3_repair_harvest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import Dexter3Executor, ExecutorConfig, recent_exec_events

GROK_LABEL = "dexter3:grok-v1.0:scalper"
DEMO_ACCOUNT = {"traderId": 9922808}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every cross-lane-dedup / multileg-observe env var starts unset for
    every test in this file -- each test opts IN to exactly what it needs."""
    for var in (
        "DEXTER3_CROSS_LANE_DEDUP",
        "DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN",
        "DEXTER3_CROSS_LANE_DEDUP_MULT",
        "DEXTER3_REPAIR_HARVEST",
        "DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE",
    ):
        monkeypatch.delenv(var, raising=False)


def _minutes_ago(minutes: float) -> str:
    """Dynamic (never a fixed past date -- a fixed date is a time bomb
    against a window computed relative to NOW)."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ===========================================================================
# FIX #1 -- cross-lane duplicate-entry guard (dexter3/executor.py)
# ===========================================================================


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "twin_entry_journal.db")
    yield j
    j.close()


class _FakeMcp:
    """Records calls; canned responses configurable per test via attrs.
    Mirrors tests/test_dexter3_lane_decoupling.py's ``_H4FakeMcp``."""

    def __init__(self, *, positions: list[dict] | None = None) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self._positions = positions or []
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.calls: list[str] = []

    def get_symbol_details(self, symbol: str) -> dict:
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append("get_positions")
        if self.post_entry_position is not None and "place_market_order" in self.calls:
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append("place_market_order")
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        return []


class _FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="BTCUSD", action="enter", side="buy", entry=62000.0, sl=61900.0, tp=62150.0,
            setup="hunt_swing_structure", reasons=["twin_entry_test"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


def _foreign_position(
    *, pid: int = 900, side: str = "Buy", label: str = GROK_LABEL, open_time: str | None,
) -> dict:
    pos = {
        "positionId": pid,
        "symbolName": "BTCUSD",
        "tradeSide": side,
        "volumeInUnits": 0.3,
        "entryPrice": 62000.0,
        "stopLoss": 61900.0,
        "takeProfit": 62150.0,
        "label": label,
    }
    if open_time is not None:
        pos["openTime"] = open_time
    return pos


def _our_post_entry_position(pid: int = 999) -> dict:
    return {
        "positionId": pid,
        "symbolName": "BTCUSD",
        "tradeSide": "Buy",
        "volumeInUnits": 0.05,
        "entryPrice": 62000.0,
        "stopLoss": 61900.0,
        "takeProfit": 62150.0,
        "label": ex_mod.LABEL,
    }


def test_skip_mode_refuses_same_direction_foreign_lane_duplicate_within_window(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(5))])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "cross_lane_duplicate"
    assert "place_market_order" not in mcp.calls


def test_downsize_mode_halves_risk_when_duplicate_present(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "downsize")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(5))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["volume_meta"]["risk_usd"] == pytest.approx(5.0)  # 10.0 x default mult 0.5
    rows = [r for r in recent_exec_events(ex._conn, "BTCUSD", limit=50) if r["event"] == "cross_lane_dedup_downsized"]
    assert len(rows) == 1
    assert rows[0]["payload"]["base_risk_usd"] == pytest.approx(10.0)
    assert rows[0]["payload"]["downsized_risk_usd"] == pytest.approx(5.0)
    assert rows[0]["payload"]["mult"] == pytest.approx(0.5)


def test_downsize_mode_custom_mult_env(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "downsize")
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP_MULT", "0.25")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(5))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["volume_meta"]["risk_usd"] == pytest.approx(2.5)


def test_opposite_direction_is_not_a_duplicate(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    mcp = _FakeMcp(positions=[_foreign_position(side="Sell", open_time=_minutes_ago(5))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_same_direction_older_than_window_is_allowed(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    # default window is 30 minutes -- 45 minutes old is stale.
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(45))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_custom_window_env_respected(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN", "5")
    # 10 minutes old -- within the default 30min window but outside a 5min one.
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(10))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_own_family_position_is_not_a_cross_lane_duplicate(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    # Same-symbol, same-side, OWN label -- basket_authorized=True bypasses the
    # separate duplicate_label_position_open gate so this test isolates the
    # cross-lane check specifically (own-family is that OTHER gate's job).
    own_pos = {
        "positionId": 700, "symbolName": "BTCUSD", "tradeSide": "Buy", "volumeInUnits": 0.05,
        "entryPrice": 62000.0, "stopLoss": 61900.0, "takeProfit": 62150.0,
        "label": ex_mod.LABEL, "openTime": _minutes_ago(1),
    }
    mcp = _FakeMcp(positions=[own_pos])
    mcp.post_entry_position = _our_post_entry_position(pid=998)
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0, allow_basket_legs=True))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_missing_open_time_treated_as_duplicate(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "skip")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=None)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "cross_lane_duplicate"


def test_off_mode_default_is_byte_identical_to_pre_fix(journal, monkeypatch):
    # env left unset by the autouse fixture -- "off" is the code default.
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(1))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["volume_meta"]["risk_usd"] == pytest.approx(10.0)  # unchanged, no downsize
    rows = [r for r in recent_exec_events(ex._conn, "BTCUSD", limit=50) if r["event"] == "cross_lane_dedup_downsized"]
    assert rows == []


def test_off_mode_explicit_is_byte_identical(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "off")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(1))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_unrecognized_mode_value_collapses_to_off(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_CROSS_LANE_DEDUP", "some_typo")
    mcp = _FakeMcp(positions=[_foreign_position(side="Buy", open_time=_minutes_ago(1))])
    mcp.post_entry_position = _our_post_entry_position()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=10.0))
    result = ex.execute_entry(_FakeDecision(side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"


# ===========================================================================
# FIX #2 -- harvest shadow multi-leg observation (dexter3/shadow_runner.py)
# ===========================================================================


class _HarvestFakeMcp:
    """Mirrors tests/test_dexter3_repair_harvest.py's ``FakeMcp``."""

    def __init__(self) -> None:
        self.m5_bars: list[dict] = []

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        if period == "m5":
            return list(self.m5_bars)
        return []


def _harvest_lane_pos(
    pid: int,
    pnl: float,
    *,
    side: str = "Sell",
    entry: float = 100.0,
    sl: float = 110.0,
    tp: float = 70.0,
    volume: float = 1.0,
    open_time: str = "2026-07-15T04:00:00Z",
    label: str | None = None,
) -> dict:
    return {
        "positionId": pid,
        "symbolName": "XAUUSD",
        "tradeSide": side,
        "volumeInUnits": volume,
        "entryPrice": entry,
        "stopLoss": sl,
        "takeProfit": tp,
        "label": label or ex_mod.LABEL,
        "netProfit": pnl,
        "openTime": open_time,
    }


def _isolate_shadow_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sr, "STATE_FILE", tmp_path / "dexter3_shadow_state.json")
    monkeypatch.setattr(sr, "log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sr, "log_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    monkeypatch.setattr(sr, "_RSH_MODE_WARNED", set())


def _basket_events(journal: DecisionJournal, event: str | None = None) -> list[dict]:
    rows = journal.recent_basket_events(limit=500)
    if event is None:
        return rows
    return [r for r in rows if r["event"] == event]


@pytest.fixture()
def harvest_journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "harvest_obs_journal.db")
    yield j
    j.close()


def test_shadow_multileg_flag_opens_episode_with_net_direction(harvest_journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE", "1")
    lane = [
        _harvest_lane_pos(1, -30.0, side="Sell", volume=1.0),
        _harvest_lane_pos(2, -5.0, side="Sell", volume=0.5),
    ]
    state: dict = {}
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane, lane)

    assert "XAUUSD" in state.get("repair_harvest", {})
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["is_multileg_shadow"] is True
    assert ep["parent_side"] == "sell"
    assert set(ep["parent_position_ids"]) == {1, 2}
    assert ep["mode"] == "shadow"

    opened = _basket_events(harvest_journal, "rsh_episode_opened")
    assert len(opened) == 1
    assert opened[0]["payload"]["multileg_shadow"] is True
    assert opened[0]["payload"]["parent_side"] == "sell"
    assert set(opened[0]["payload"]["parent_position_ids"]) == {1, 2}

    skips = _basket_events(harvest_journal, "rsh_skip_multileg")
    assert skips == []


def test_live_mode_multileg_flag_leaves_single_leg_requirement_unchanged(harvest_journal, monkeypatch, tmp_path):
    """Guard test: DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE=1 must NEVER take
    effect in live mode -- the legacy single-leg-only skip path must fire
    exactly as it did before this fix existed."""
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "live")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE", "1")
    lane = [
        _harvest_lane_pos(1, -30.0, side="Sell", volume=1.0),
        _harvest_lane_pos(2, -5.0, side="Sell", volume=0.5),
    ]
    state: dict = {}
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane, lane)

    assert "XAUUSD" not in state.get("repair_harvest", {})
    skips = _basket_events(harvest_journal, "rsh_skip_multileg")
    assert len(skips) == 1
    assert skips[0]["payload"]["legs"] == 2
    opened = _basket_events(harvest_journal, "rsh_episode_opened")
    assert opened == []
    blocked = _basket_events(harvest_journal, "rsh_multileg_observe_blocked_non_shadow")
    assert blocked == []  # caller never even calls the helper in live mode


def test_multileg_observe_flag_off_is_unchanged(harvest_journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    # DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE left unset (default 0/off).
    lane = [
        _harvest_lane_pos(1, -30.0, side="Sell", volume=1.0),
        _harvest_lane_pos(2, -5.0, side="Sell", volume=0.5),
    ]
    state: dict = {}
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane, lane)

    assert "XAUUSD" not in state.get("repair_harvest", {})
    skips = _basket_events(harvest_journal, "rsh_skip_multileg")
    assert len(skips) == 1


def test_multileg_observe_flat_net_skips(harvest_journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE", "1")
    # equal opposing volumes -> net == 0 -> directionless, must never guess.
    lane = [
        _harvest_lane_pos(1, -30.0, side="Buy", volume=1.0),
        _harvest_lane_pos(2, -5.0, side="Sell", volume=1.0),
    ]
    state: dict = {}
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane, lane)

    assert "XAUUSD" not in state.get("repair_harvest", {})
    flat = _basket_events(harvest_journal, "rsh_skip_flat_net")
    assert len(flat) == 1
    assert flat[0]["payload"]["legs"] == 2
    opened = _basket_events(harvest_journal, "rsh_episode_opened")
    assert opened == []


def test_multileg_shadow_episode_ends_only_when_basket_fully_empties(harvest_journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE", "1")
    lane_full = [
        _harvest_lane_pos(1, -30.0, side="Sell", volume=1.0),
        _harvest_lane_pos(2, -5.0, side="Sell", volume=0.5),
    ]
    state: dict = {}
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane_full, lane_full)
    assert "XAUUSD" in state.get("repair_harvest", {})

    # ONE of the two legs resolves -- the episode must stay open (the basket
    # has not FULLY emptied yet).
    lane_partial = [_harvest_lane_pos(1, -30.0, side="Sell", volume=1.0)]
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, lane_partial, lane_partial)
    assert "XAUUSD" in state.get("repair_harvest", {})
    assert state["repair_harvest"]["XAUUSD"]["is_multileg_shadow"] is True
    closed_mid = _basket_events(harvest_journal, "rsh_episode_closed")
    assert closed_mid == []

    # Both legs gone -- NOW the episode ends.
    sr._run_repair_harvest_tick(_HarvestFakeMcp(), harvest_journal, state, "XAUUSD", None, [], [])
    assert "XAUUSD" not in state.get("repair_harvest", {})
    closed = _basket_events(harvest_journal, "rsh_episode_closed")
    assert len(closed) == 1
    assert closed[0]["payload"]["reason"] == "parent_resolved"

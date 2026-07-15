"""Unit tests for dexter3/executor.py — the ONLY dexter3 module allowed to
call mutating MCP methods.

Critical invariants under test (per Dexter3 Phase 2 spec):
  - demo refusal when account cannot be confirmed demo (missing/foreign traderId)
  - every pre-flight gate refuses (and journals) rather than raising
  - duplicate-label guard: never a second open position with our LABEL
  - sizing math incl. minVolume clamp-up
  - naked-position repair: SL missing -> amend; amend fails -> close
  - peer-label untouchability (close_lane_position / amend_lane_sl_tp)
  - post-flight verification mirrors scripts/btc_scalp_monitor.py's geometry check

NO live MCP calls anywhere in this file — every test drives a fake
transport object exposing the same method surface as
dexter3.mcp_client.Dexter3McpClient (get_symbol_details/get_spot_price/
get_positions/place_market_order/amend_position/close_position/get_balance),
recording calls so assertions can inspect exactly what was sent.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dexter3.executor import (
    Dexter3Executor,
    ExecutorConfig,
    LABEL,
    floor_to_step,
    is_our_position,
    planned_volume_units,
    position_id_of,
    recent_exec_events,
    verify_entry_snapshot,
)
from dexter3.mcp_client import McpClientError, McpZombieError


# -- fake decision (mirrors hunter_brain.Decision's fields the executor reads) --


@dataclass
class FakeDecision:
    symbol: str = "BTCUSD"
    action: str = "enter"
    side: str | None = "buy"
    entry: float | None = 62000.0
    sl: float | None = 61900.0
    tp: float | None = 62150.0
    setup: str = "leader_continuation"
    reasons: list[str] = field(default_factory=lambda: ["test_reason"])
    session: str = "london"


# -- fake MCP transport -------------------------------------------------------


class FakeMcp:
    """Records every call; canned responses configurable per test via attrs."""

    def __init__(
        self,
        *,
        symbol_details: dict | None = None,
        spot: dict | None = None,
        positions: list[dict] | None = None,
        order_response: dict | None = None,
        post_entry_position: dict | None = None,
        balance: dict | None = None,
        raise_on: dict[str, Exception] | None = None,
    ) -> None:
        self.symbol_details = symbol_details or {
            "minVolume": 0.01,
            "maxVolume": 10.0,
            "volumeStep": 0.01,
            "lotSize": 1.0,
            "pipSize": 0.01,
        }
        self.spot = spot or {"bid": 62000.0, "ask": 62006.0}
        self._positions = list(positions or [])
        self.order_response = order_response or {"dealStatus": "FILLED"}
        self.post_entry_position = post_entry_position
        self.balance = balance or {"traderId": 9922808, "balance": 10000.0}
        self.raise_on = raise_on or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_raise(self, name: str) -> None:
        if name in self.raise_on:
            raise self.raise_on[name]

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {"symbol": symbol}))
        self._maybe_raise("get_symbol_details")
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {"symbol": symbol}))
        self._maybe_raise("get_spot_price")
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        self._maybe_raise("get_positions")
        if self.post_entry_position is not None and any(c[0] == "place_market_order" for c in self.calls):
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        self.calls.append(("get_balance", {}))
        self._maybe_raise("get_balance")
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        self._maybe_raise("place_market_order")
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        self.calls.append(("amend_position", {"position_id": position_id, "stop_loss": stop_loss, "take_profit": take_profit}))
        self._maybe_raise("amend_position")
        if self.post_entry_position is not None and position_id == position_id_of(self.post_entry_position):
            self.post_entry_position = dict(self.post_entry_position)
            if stop_loss is not None:
                self.post_entry_position["stopLoss"] = stop_loss
            if take_profit is not None:
                self.post_entry_position["takeProfit"] = take_profit
        for pos in self._positions:
            if position_id_of(pos) == position_id:
                if stop_loss is not None:
                    pos["stopLoss"] = stop_loss
                if take_profit is not None:
                    pos["takeProfit"] = take_profit
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        self.calls.append(("close_position", {"position_id": position_id}))
        self._maybe_raise("close_position")
        if self.post_entry_position is not None and position_id == position_id_of(self.post_entry_position):
            self.post_entry_position = None
        self._positions = [p for p in self._positions if position_id_of(p) != position_id]
        return {"status": "closed"}

    def get_deals(self, count: int = 200) -> list[dict]:
        self.calls.append(("get_deals", {"count": count}))
        self._maybe_raise("get_deals")
        return list(getattr(self, "deals", []))

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture()
def journal_conn(tmp_path: Path) -> sqlite3.Connection:
    from dexter3.decision_journal import DecisionJournal

    j = DecisionJournal(tmp_path / "exec_test_journal.db")
    yield j
    j.close()


def _filled_position(
    *,
    position_id: int = 555,
    symbol: str = "BTCUSD",
    side: str = "Buy",
    volume: float = 0.01,
    entry_price: float = 62000.0,
    stop_loss: float = 61900.0,
    take_profit: float = 62150.0,
    label: str = LABEL,
) -> dict:
    return {
        "positionId": position_id,
        "symbolName": symbol,
        "tradeSide": side,
        "volumeInUnits": volume,
        "entryPrice": entry_price,
        "stopLoss": stop_loss,
        "takeProfit": take_profit,
        "label": label,
    }


DEMO_ACCOUNT = {"traderId": 9922808}


# -- demo refusal --------------------------------------------------------------


def test_execute_entry_refuses_when_traderid_not_in_demo_allowlist(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), {"traderId": 123456})
    assert result["action"] == "refused"
    assert result["reason"] == "account_not_confirmed_demo"
    assert "place_market_order" not in mcp.call_names()


def test_execute_entry_refuses_when_traderid_missing(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), {})
    assert result["action"] == "refused"
    assert result["reason"] == "account_not_confirmed_demo"
    assert "place_market_order" not in mcp.call_names()


def test_execute_entry_refuses_when_traderid_unparseable(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), {"traderId": "not-a-number"})
    assert result["action"] == "refused"
    assert result["reason"] == "account_not_confirmed_demo"


def test_execute_entry_accepts_explicit_is_demo_true_override(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), {"is_demo": True})
    assert result["action"] == "entered"


def test_execute_entry_refuses_explicit_is_demo_false_override(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), {"is_demo": False, "traderId": 9922808})
    assert result["action"] == "refused"
    assert result["reason"] == "account_not_confirmed_demo"


# -- action gate ----------------------------------------------------------------


def test_execute_entry_refuses_non_enter_decision(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(action="skip"), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "decision_action_not_enter"
    assert "place_market_order" not in mcp.call_names()


# -- pre-flight: spot quote sanity ------------------------------------------------


def test_execute_entry_refuses_insane_spot_quote_bid_gte_ask(journal_conn):
    mcp = FakeMcp(spot={"bid": 62010.0, "ask": 62000.0})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "spot_quote_insane"


def test_execute_entry_refuses_zero_bid_or_ask(journal_conn):
    mcp = FakeMcp(spot={"bid": 0.0, "ask": 62006.0})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "spot_quote_insane"


def test_execute_entry_refuses_spread_too_wide(journal_conn):
    # spread_bps = (30/62000)*10000 ~= 4.8bps -> fine at default cap 15; blow it up
    mcp = FakeMcp(spot={"bid": 62000.0, "ask": 62300.0})  # ~48bps
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_spread_bps=15.0))
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "spread_too_wide"


# -- pre-flight: SL/TP sidedness -------------------------------------------------


def test_execute_entry_refuses_buy_with_inverted_sltp(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    bad = FakeDecision(side="buy", entry=62000.0, sl=62100.0, tp=61900.0)  # SL above entry, TP below -> wrong for buy
    result = ex.execute_entry(bad, DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "sltp_sidedness_invalid"


def test_execute_entry_refuses_sell_with_inverted_sltp(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    bad = FakeDecision(side="sell", entry=62000.0, sl=61900.0, tp=62100.0)  # wrong sidedness for sell
    result = ex.execute_entry(bad, DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "sltp_sidedness_invalid"


def test_execute_entry_refuses_incomplete_geometry_missing_sl(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(sl=None), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "incomplete_decision_geometry"


def test_execute_entry_refuses_invalid_side(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(side="sideways"), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "incomplete_decision_geometry"


# -- pre-flight: duplicate-label guard --------------------------------------------


def test_execute_entry_refuses_duplicate_label_position_open(journal_conn):
    existing = _filled_position(position_id=1, label=LABEL)
    mcp = FakeMcp(positions=[existing])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "duplicate_label_position_open"
    assert "place_market_order" not in mcp.call_names()


def test_execute_entry_ignores_foreign_label_position_for_duplicate_guard(journal_conn):
    foreign = _filled_position(position_id=1, label="dexter-scalp:codex:v3.7-m1-close-entry")
    mcp = FakeMcp(positions=[foreign], post_entry_position=_filled_position(position_id=2))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_execute_entry_allows_duplicate_when_allow_basket_legs_true(journal_conn):
    existing = _filled_position(position_id=1, label=LABEL)
    mcp = FakeMcp(positions=[existing], post_entry_position=_filled_position(position_id=2))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(allow_basket_legs=True))
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "entered"


# -- pre-flight: daily guards ------------------------------------------------------


def test_execute_entry_refuses_at_max_live_entries_per_day(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_live_entries_per_day=6))
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT, today_entry_count=6)
    assert result["action"] == "refused"
    assert result["reason"] == "max_live_entries_per_day_reached"


def test_execute_entry_refuses_at_stop_after_daily_losses(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(stop_after_daily_losses=2))
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT, today_losing_count=2)
    assert result["action"] == "refused"
    assert result["reason"] == "stop_after_daily_losses_reached"


def test_execute_entry_allows_below_daily_caps(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_live_entries_per_day=6, stop_after_daily_losses=2))
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT, today_entry_count=5, today_losing_count=1)
    assert result["action"] == "entered"


# -- sizing math ---------------------------------------------------------------------


def test_planned_volume_units_normal_case():
    details = {"minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0}
    volume, meta = planned_volume_units(details, sl_distance=100.0, risk_usd=1.0, max_volume_units=0.05)
    assert volume == pytest.approx(0.01)  # 1.0/100 = 0.01 raw -> exactly at minVolume
    assert not meta.get("min_volume_clamped_up")


def test_planned_volume_units_clamps_up_to_min_volume():
    # BTCUSD price ~62000, sl_distance ~80pt, risk_usd 0.50 -> raw units ~0.00625 -> clamps to 0.01
    details = {"minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0}
    volume, meta = planned_volume_units(details, sl_distance=80.0, risk_usd=0.50, max_volume_units=0.05)
    assert volume == pytest.approx(0.01)
    assert meta["min_volume_clamped_up"] is True
    assert meta["estimated_min_volume_risk_usd"] == pytest.approx(0.01 * 80.0)


def test_planned_volume_units_refuses_when_min_volume_exceeds_cap():
    details = {"minVolume": 1.0, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0}
    volume, meta = planned_volume_units(details, sl_distance=80.0, risk_usd=0.50, max_volume_units=0.05)
    assert volume == 0.0
    assert meta["refuse_reason"] == "min_volume_exceeds_max_volume_units_cap"


def test_planned_volume_units_rounds_down_to_step():
    details = {"minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0}
    # risk 1.0 / sl 37 = 0.027027... -> floor to step 0.01 -> 0.02
    volume, meta = planned_volume_units(details, sl_distance=37.0, risk_usd=1.0, max_volume_units=0.05)
    assert volume == pytest.approx(0.02)


def test_floor_to_step_basic():
    assert floor_to_step(0.027, 0.01) == pytest.approx(0.02)
    assert floor_to_step(0.01, 0.01) == pytest.approx(0.01)
    assert floor_to_step(0.0, 0.01) == 0.0


def test_execute_entry_sizing_clamped_up_is_journaled_not_refused(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position(volume=0.01))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=0.50, max_volume_units=0.05))
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61920.0, tp=62150.0), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["volume_meta"]["min_volume_clamped_up"] is True
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "sizing_min_volume_clamped" for e in events)


# -- min-volume floor x disaster stop + absolute $ cap (2026-07-10 live lesson) ------

_XAU_STYLE_DETAILS = {
    # XAUUSD-scale constraints (OpenAPI): 1 oz floor, whole-oz steps
    "minVolume": 1.0,
    "maxVolume": 100.0,
    "volumeStep": 1.0,
    "lotSize": 100.0,
    "pipSize": 0.01,
}


def test_min_vol_floor_disaster_stop_reverts_to_tight(journal_conn, monkeypatch):
    """At the volume floor, disaster widening cannot shrink size — it only
    multiplies real $ risk (live: ~9pt tight became an 18pt broker stop at the
    1oz floor = -$18.11 on a $1.68-design trade). The executor must revert to
    the TIGHT stop, restoring the equal-$-risk invariant."""
    monkeypatch.delenv("DEXTER3_MIN_VOL_DISASTER_TIGHTEN", raising=False)
    mcp = FakeMcp(symbol_details=dict(_XAU_STYLE_DETAILS), post_entry_position=_filled_position(volume=1.0, stop_loss=61994.0, take_profit=62012.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61994.0, tp=62012.0),  # 6pt tight stop
        DEMO_ACCOUNT,
        risk_usd_override=1.68,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    assert result["action"] == "entered"
    kwargs = next(c for c in mcp.calls if c[0] == "place_market_order")[1]
    # tight 6pt at pipSize 0.01 -> 600 pips, NOT the widened 1200
    assert kwargs["stop_loss_pips"] == pytest.approx(600, abs=1)
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    tightened = [e for e in events if e["event"] == "min_vol_disaster_tightened"]
    assert len(tightened) == 1
    assert tightened[0]["payload"]["tight_sl_distance"] == pytest.approx(6.0)


def test_min_vol_floor_disaster_tighten_kill_switch(journal_conn, monkeypatch):
    monkeypatch.setenv("DEXTER3_MIN_VOL_DISASTER_TIGHTEN", "0")
    mcp = FakeMcp(symbol_details=dict(_XAU_STYLE_DETAILS), post_entry_position=_filled_position(volume=1.0, stop_loss=61988.0, take_profit=62012.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61994.0, tp=62012.0),
        DEMO_ACCOUNT,
        risk_usd_override=1.68,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    assert result["action"] == "entered"
    kwargs = next(c for c in mcp.calls if c[0] == "place_market_order")[1]
    assert kwargs["stop_loss_pips"] == pytest.approx(1200, abs=1)  # legacy widened


def test_disaster_widening_kept_when_properly_sized(journal_conn, monkeypatch):
    """Fix must NOT touch trades whose size is above the floor — the widened
    stop + reduced size is the designed equal-$-risk trade there."""
    monkeypatch.delenv("DEXTER3_MIN_VOL_DISASTER_TIGHTEN", raising=False)
    mcp = FakeMcp(post_entry_position=_filled_position(volume=0.05, stop_loss=61800.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0),  # 100pt tight
        DEMO_ACCOUNT,
        risk_usd_override=10.0,  # 10/200 = 0.05 >= minVolume 0.01 -> no clamp
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    assert result["action"] == "entered"
    kwargs = next(c for c in mcp.calls if c[0] == "place_market_order")[1]
    assert kwargs["stop_loss_pips"] == pytest.approx(20000, abs=1)  # widened kept
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert not any(e["event"] == "min_vol_disaster_tightened" for e in events)


def test_min_vol_abs_cap_refuses_oversized_floor_risk(journal_conn, monkeypatch):
    """DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD keys off ACCOUNT economics (absolute
    dollars), not the crushed design risk the old ratio cap used."""
    monkeypatch.setenv("DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD", "9")
    mcp = FakeMcp(symbol_details=dict(_XAU_STYLE_DETAILS))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61988.0, tp=62030.0),  # 12pt -> 1oz = $12 > $9 cap
        DEMO_ACCOUNT,
        risk_usd_override=0.25,
    )
    assert result["action"] == "refused"
    assert result["reason"] == "min_volume_risk_exceeds_abs_cap"
    assert result["estimated_min_volume_risk_usd"] == pytest.approx(12.0)
    assert not any(c[0] == "place_market_order" for c in mcp.calls)


def test_min_vol_abs_cap_default_off_accepts(journal_conn, monkeypatch):
    monkeypatch.delenv("DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD", raising=False)
    monkeypatch.delenv("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", raising=False)
    mcp = FakeMcp(symbol_details=dict(_XAU_STYLE_DETAILS), post_entry_position=_filled_position(volume=1.0, stop_loss=61988.0, take_profit=62030.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61988.0, tp=62030.0),
        DEMO_ACCOUNT,
        risk_usd_override=0.25,
    )
    assert result["action"] == "entered"


def test_min_vol_abs_cap_allows_tight_floor_risk_under_cap(journal_conn, monkeypatch):
    monkeypatch.setenv("DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD", "9")
    mcp = FakeMcp(symbol_details=dict(_XAU_STYLE_DETAILS), post_entry_position=_filled_position(volume=1.0, stop_loss=61994.0, take_profit=62012.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61994.0, tp=62012.0),  # 6pt -> 1oz = $6 <= $9
        DEMO_ACCOUNT,
        risk_usd_override=0.25,
    )
    assert result["action"] == "entered"


# -- entry placement + verification --------------------------------------------------


def test_execute_entry_places_order_with_pip_distance_sltp_and_our_label(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    order_call = next(c for c in mcp.calls if c[0] == "place_market_order")
    kwargs = order_call[1]
    assert kwargs["label"] == LABEL
    assert kwargs["side"] == "buy"
    assert kwargs["symbol"] == "BTCUSD"
    # sl_distance=100 -> pips at pipSize 0.01 -> 10000 pips; just check positive ints, not exact broker math
    assert kwargs["stop_loss_pips"] > 0
    assert kwargs["take_profit_pips"] > 0


def test_execute_entry_verified_true_on_clean_fill(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position(side="Buy", stop_loss=61900.0, take_profit=62150.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["verified"] is True
    assert result["verification"]["geometry_ok"] is True


def test_execute_entry_handles_broker_rejection(journal_conn):
    mcp = FakeMcp(order_response={"dealStatus": "REJECTED", "reason": "no liquidity"})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "rejected"
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "entry_rejected_by_broker" for e in events)


def test_execute_entry_journals_exec_event_on_success(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "entry_executed" and e["verified"] is True for e in events)


def test_execute_entry_handles_mcp_error_on_place_order(journal_conn):
    mcp = FakeMcp(raise_on={"place_market_order": McpClientError("boom")})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "place_market_order_failed"


def test_execute_entry_handles_mcp_zombie_on_preflight_read(journal_conn):
    mcp = FakeMcp(raise_on={"get_symbol_details": McpZombieError("dead")})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "mcp_read_failed_preflight"


# -- naked-position repair path ----------------------------------------------------


def test_execute_entry_repairs_naked_position_via_amend(journal_conn):
    naked = _filled_position(stop_loss=0.0, take_profit=0.0)  # SL/TP missing after fill
    mcp = FakeMcp(post_entry_position=naked)
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["repair"] is not None
    assert result["repair"]["action"] == "amended"
    assert result["verified"] is True
    assert any(c[0] == "amend_position" for c in mcp.calls)
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "naked_position_repaired_via_amend" for e in events)


def test_execute_entry_closes_position_when_amend_repair_fails(journal_conn):
    naked = _filled_position(stop_loss=0.0, take_profit=0.0)
    mcp = FakeMcp(post_entry_position=naked, raise_on={"amend_position": McpClientError("amend rejected")})
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["repair"]["action"] == "closed"
    assert result["verified"] is False
    assert any(c[0] == "close_position" for c in mcp.calls)
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "naked_position_closed" for e in events)


def test_execute_entry_never_leaves_naked_position_when_both_amend_and_close_fail(journal_conn):
    naked = _filled_position(stop_loss=0.0, take_profit=0.0)
    mcp = FakeMcp(
        post_entry_position=naked,
        raise_on={
            "amend_position": McpClientError("amend rejected"),
            "close_position": McpClientError("close rejected too"),
        },
    )
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["repair"]["action"] == "close_failed"
    assert result["verified"] is False
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "naked_position_close_failed" for e in events)


def test_execute_entry_no_repair_needed_when_sltp_present_and_correct(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position(stop_loss=61900.0, take_profit=62150.0))
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["repair"] is None
    assert not any(c[0] == "amend_position" for c in mcp.calls)


# -- peer isolation: close_lane_position / amend_lane_sl_tp --------------------------


def test_close_lane_position_refuses_foreign_label(journal_conn):
    foreign = _filled_position(position_id=99, label="dexter-scalp:grok:some-strategy")
    mcp = FakeMcp(positions=[foreign])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.close_lane_position(99, reason="test")
    assert result["action"] == "refused"
    assert result["reason"] == "refused_foreign_label"
    assert "close_position" not in mcp.call_names()


def test_close_lane_position_refuses_unknown_position(journal_conn):
    mcp = FakeMcp(positions=[])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.close_lane_position(12345, reason="test")
    assert result["action"] == "refused"
    assert result["reason"] == "position_not_found"


def test_close_lane_position_closes_our_label(journal_conn):
    ours = _filled_position(position_id=42, label=LABEL)
    mcp = FakeMcp(positions=[ours])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.close_lane_position(42, reason="target hit")
    assert result["action"] == "closed"
    assert any(c[0] == "close_position" for c in mcp.calls)


def test_amend_lane_sl_tp_refuses_foreign_label(journal_conn):
    foreign = _filled_position(position_id=7, label="dexter-scalp:cursor:whatever")
    mcp = FakeMcp(positions=[foreign])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.amend_lane_sl_tp(7, sl=100.0, tp=200.0)
    assert result["action"] == "refused"
    assert result["reason"] == "refused_foreign_label"
    assert "amend_position" not in mcp.call_names()


def test_amend_lane_sl_tp_amends_our_label(journal_conn):
    ours = _filled_position(position_id=42, label=LABEL, stop_loss=61900.0, take_profit=62150.0)
    mcp = FakeMcp(positions=[ours])
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.amend_lane_sl_tp(42, sl=61950.0, tp=62200.0)
    assert result["action"] == "amended"
    amend_call = next(c for c in mcp.calls if c[0] == "amend_position")
    assert amend_call[1]["stop_loss"] == 61950.0
    assert amend_call[1]["take_profit"] == 62200.0


# -- verify_entry_snapshot (mirrors btc_scalp_monitor.verify_position_snapshot) -----


def test_verify_entry_snapshot_requires_side_volume_and_geometry():
    position = {
        "positionId": 1,
        "symbolName": "BTCUSD",
        "tradeSide": "Buy",
        "volumeInUnits": 0.01,
        "stopLoss": 61900.0,
        "takeProfit": 62150.0,
    }
    ok, meta = verify_entry_snapshot(position, "buy", 62000.0, 0.01)
    assert ok is True
    assert meta["geometry_ok"] is True

    ok2, meta2 = verify_entry_snapshot({**position, "stopLoss": 0}, "buy", 62000.0, 0.01)
    assert ok2 is False
    assert meta2["geometry_ok"] is False


def test_verify_entry_snapshot_none_position_is_not_verified():
    ok, meta = verify_entry_snapshot(None, "buy", 62000.0, 0.01)
    assert ok is False
    assert meta["reason"] == "position_not_found"


def test_verify_entry_snapshot_side_mismatch_fails():
    position = {
        "positionId": 1,
        "symbolName": "BTCUSD",
        "tradeSide": "Sell",
        "volumeInUnits": 0.01,
        "stopLoss": 62100.0,
        "takeProfit": 61850.0,
    }
    ok, meta = verify_entry_snapshot(position, "buy", 62000.0, 0.01)
    assert ok is False
    assert meta["side_ok"] is False


def test_verify_entry_snapshot_insufficient_volume_fails():
    position = {
        "positionId": 1,
        "symbolName": "BTCUSD",
        "tradeSide": "Buy",
        "volumeInUnits": 0.005,
        "stopLoss": 61900.0,
        "takeProfit": 62150.0,
    }
    ok, meta = verify_entry_snapshot(position, "buy", 62000.0, 0.01)
    assert ok is False
    assert meta["volume_ok"] is False


def test_is_our_position_matches_exact_label_only():
    assert is_our_position({"label": LABEL}) is True
    assert is_our_position({"label": LABEL + ":extra"}) is False
    assert is_our_position({"label": "dexter-scalp:codex:v3.7-m1-close-entry"}) is False
    assert is_our_position({}) is False


# -- exec_events table ---------------------------------------------------------------


def test_exec_events_table_created_lazily_and_journal_has_no_prior_rows(journal_conn):
    ex = Dexter3Executor(FakeMcp(), journal_conn, ExecutorConfig())
    events = recent_exec_events(journal_conn._conn)
    assert events == []


def test_exec_events_roundtrip_with_thai_text(journal_conn):
    from dexter3.executor import insert_exec_event

    insert_exec_event(
        journal_conn._conn,
        symbol="XAUUSD",
        event="entry_refused",
        verified=False,
        payload={"reason": "บัญชีไม่ใช่ demo"},
    )
    events = recent_exec_events(journal_conn._conn, symbol="XAUUSD")
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "บัญชีไม่ใช่ demo"
    assert events[0]["verified"] is False


# ---------------------------------------------------------------------------
# SMART EXIT (owner directive 2026-07-08) — disaster-stop sizing + regime
# ---------------------------------------------------------------------------
#
# The backtest proved a gated smart adaptive exit amplifies edge on
# non-chase entries but amplifies loss on chase entries — see
# dexter3/smart_exit.py. execute_entry's new `smart_exit` kwarg (additive,
# default None) widens the BROKER stop to disaster_mult x sl_distance and
# scales sizing down proportionally so $ risk to the wide stop stays equal
# to the tight-stop risk; TP is untouched. These tests assert that
# equal-risk invariant plus the byte-identical-when-omitted contract.


def test_execute_entry_omitting_smart_exit_is_byte_identical_to_tight(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=1.0))
    result = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["smart_exit_regime"] == "tight"
    order_call = next(c for c in mcp.calls if c[0] == "place_market_order")
    kwargs = order_call[1]
    # sl_distance = 100, pipSize 0.01 -> 10000 pips (tight, unwidened)
    assert kwargs["stop_loss_pips"] == 10000
    assert result["broker_sl_distance"] == pytest.approx(100.0)


def test_execute_entry_tight_regime_explicit_matches_omitted(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=1.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "tight", "disaster_mult": 2.0},
    )
    assert result["smart_exit_regime"] == "tight"
    assert result["broker_sl_distance"] == pytest.approx(100.0)


def test_execute_entry_disaster_regime_widens_broker_stop_distance(journal_conn):
    # risk 4.0 / 200pt widened = 0.02 units >= minVolume 0.01 -> properly sized
    # (at the volume FLOOR the widening now reverts to tight — covered by
    # test_min_vol_floor_disaster_stop_reverts_to_tight)
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=4.0, max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    assert result["action"] == "entered"
    assert result["smart_exit_regime"] == "disaster"
    # tight sl_distance=100 -> disaster broker distance = 200
    assert result["broker_sl_distance"] == pytest.approx(200.0)
    assert result["sl"] == pytest.approx(61900.0)  # tight sl still recorded (journal truth)
    order_call = next(c for c in mcp.calls if c[0] == "place_market_order")
    assert order_call[1]["stop_loss_pips"] == 20000  # 200 / 0.01


def test_execute_entry_disaster_regime_risk_usd_equals_tight_regime_risk_usd(journal_conn):
    """THE core invariant: $ risk to the disaster stop must equal $ risk to
    the tight stop — NOT disaster_mult x bigger. Sizing shrinks ~1/mult."""
    risk_usd = 2.0
    symbol_details = {"minVolume": 0.0001, "maxVolume": 10.0, "volumeStep": 0.0001, "lotSize": 1.0, "pipSize": 0.01}

    mcp_tight = FakeMcp(symbol_details=symbol_details, post_entry_position=_filled_position())
    ex_tight = Dexter3Executor(mcp_tight, journal_conn, ExecutorConfig(risk_usd=risk_usd, max_volume_units=10.0))
    result_tight = ex_tight.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT
    )
    tight_volume = result_tight["volume"]
    tight_distance = result_tight["broker_sl_distance"]
    tight_risk_usd = tight_volume * tight_distance

    mcp_disaster = FakeMcp(symbol_details=symbol_details, post_entry_position=_filled_position(position_id=556))
    ex_disaster = Dexter3Executor(mcp_disaster, journal_conn, ExecutorConfig(risk_usd=risk_usd, max_volume_units=10.0))
    result_disaster = ex_disaster.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    disaster_volume = result_disaster["volume"]
    disaster_distance = result_disaster["broker_sl_distance"]
    disaster_risk_usd = disaster_volume * disaster_distance

    assert disaster_distance == pytest.approx(tight_distance * 2.0)
    assert disaster_volume == pytest.approx(tight_volume / 2.0, rel=1e-3)
    # the actual $ risk to each stop must match — never bigger under disaster.
    assert disaster_risk_usd == pytest.approx(tight_risk_usd, rel=1e-3)
    assert disaster_risk_usd == pytest.approx(risk_usd, rel=1e-3)


def test_execute_entry_disaster_regime_tp_unchanged(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=1.0, max_volume_units=10.0))
    result_tight = ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    mcp2 = FakeMcp(post_entry_position=_filled_position(position_id=557))
    ex2 = Dexter3Executor(mcp2, journal_conn, ExecutorConfig(risk_usd=1.0, max_volume_units=10.0))
    result_disaster = ex2.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    tp_call_tight = next(c for c in mcp.calls if c[0] == "place_market_order")[1]
    tp_call_disaster = next(c for c in mcp2.calls if c[0] == "place_market_order")[1]
    assert tp_call_tight["take_profit_pips"] == tp_call_disaster["take_profit_pips"]
    assert result_tight["tp"] == result_disaster["tp"] == pytest.approx(62150.0)


def test_execute_entry_disaster_regime_sell_side_broker_sl_price_above_entry(journal_conn):
    mcp = FakeMcp(
        post_entry_position=_filled_position(side="Sell", stop_loss=62300.0, take_profit=61700.0),
    )
    # risk 4.0 keeps the trade above the volume floor (see buy-side test note)
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=4.0, max_volume_units=10.0))
    result = ex.execute_entry(
        FakeDecision(entry=62000.0, sl=62100.0, tp=61700.0, side="sell"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    assert result["action"] == "entered"
    # tight distance = 100 -> disaster distance 200 -> broker_sl = entry + 200 = 62200
    assert result["broker_sl"] == pytest.approx(62200.0)
    assert result["broker_sl_distance"] == pytest.approx(200.0)


def test_execute_entry_smart_exit_classification_always_journaled(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=1.0, max_volume_units=10.0))
    ex.execute_entry(
        FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"),
        DEMO_ACCOUNT,
        smart_exit={"regime": "disaster", "disaster_mult": 2.0},
    )
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert any(e["event"] == "smart_exit_classified" for e in events)
    classified = next(e for e in events if e["event"] == "smart_exit_classified")
    assert classified["payload"]["regime"] == "disaster"
    assert classified["payload"]["disaster_mult"] == pytest.approx(2.0)


def test_execute_entry_smart_exit_classification_journaled_even_when_none(journal_conn):
    # smart_exit=None (omitted) still journals the 'tight' classification —
    # shadow measurement per the module contract, no special-casing needed.
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig(risk_usd=1.0))
    ex.execute_entry(FakeDecision(entry=62000.0, sl=61900.0, tp=62150.0, side="buy"), DEMO_ACCOUNT)
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    classified = next(e for e in events if e["event"] == "smart_exit_classified")
    assert classified["payload"]["regime"] == "tight"


# -- close -> learner integration (2026-07-10: make self-learning real) ---------------


def test_entry_executed_journals_session_from_decision(journal_conn):
    mcp = FakeMcp(post_entry_position=_filled_position())
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(session="overlap"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert result["session"] == "overlap"
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    entry = next(e for e in events if e["event"] == "entry_executed")
    assert entry["payload"]["session"] == "overlap"


def test_close_lane_position_feeds_empirical_stats(journal_conn):
    """Full learner loop on a temp DB: entry_executed (setup+session) ->
    close_lane_position (pnl snapshot) -> empirical_stats sees the outcome.
    Before 2026-07-10 the close payload had no setup/session/pnl, so
    _exec_events_outcome_rows skipped every row (the audit's 'learner reads
    zero')."""
    from dexter3 import empirical_stats as es

    filled = _filled_position(position_id=777, volume=0.01)
    filled["netProfit"] = 6.15  # floating PnL the close snapshot reads
    mcp = FakeMcp(post_entry_position=filled)
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())

    entered = ex.execute_entry(
        FakeDecision(setup="sweep_reclaim", session="london"), DEMO_ACCOUNT
    )
    assert entered["action"] == "entered" and entered["position_id"] == 777

    closed = ex.close_lane_position(777, reason="om_ladder_exit")
    assert closed["action"] == "closed"

    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    close_ev = next(e for e in events if e["event"] == "lane_position_closed")
    assert close_ev["payload"]["setup"] == "sweep_reclaim"
    assert close_ev["payload"]["session"] == "london"
    assert close_ev["payload"]["pnl"] == pytest.approx(6.15)
    assert close_ev["payload"]["exit_reason"] == "om_ladder_exit"

    stats = es.compute_from_journal(journal_conn, "BTCUSD")
    key = ("sweep_reclaim", "london")
    assert key in stats  # the learner SEES the outcome now
    assert stats[key]["samples"] == 1
    assert stats[key]["below_min_samples"] is True  # honest: 1 < MIN_SAMPLES


# -- broker-side close vanish reconcile (2026-07-11: last learner coverage gap) --------


def _entered(ex, mcp, pid: int, setup: str = "sweep_reclaim", session: str = "london"):
    """Drive a real entry so entry_executed exists for pid (the reconcile's input)."""
    mcp.post_entry_position = _filled_position(position_id=pid, volume=0.01)
    result = ex.execute_entry(FakeDecision(setup=setup, session=session), DEMO_ACCOUNT)
    assert result["action"] == "entered" and result["position_id"] == pid
    # subsequent entries must not see this one as open (fresh call log)
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None
    return result


def test_vanish_reconcile_journals_broker_close_into_learner(journal_conn):
    from dexter3 import empirical_stats as es

    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    _entered(ex, mcp, 901, setup="dragon_shelf_short", session="overlap")
    # broker closed it: pid 901 absent from lane; closing deal carries the pnl
    mcp.deals = [
        {"positionId": 901, "netProfit": 0.0},     # entry leg
        {"positionId": 901, "netProfit": -7.25},   # SL close leg
        {"positionId": 999, "netProfit": 3.0},     # foreign position noise
    ]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1
    assert out[0]["position_id"] == 901
    assert out[0]["setup"] == "dragon_shelf_short"
    assert out[0]["session"] == "overlap"
    assert out[0]["pnl"] == pytest.approx(-7.25)
    stats = es.compute_from_journal(journal_conn, "BTCUSD")
    assert ("dragon_shelf_short", "overlap") in stats  # the learner SEES the SL hit


def test_vanish_reconcile_skips_still_open_and_dedups(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    _entered(ex, mcp, 902)
    mcp.deals = [{"positionId": 902, "netProfit": 4.0}]
    still_open = [_filled_position(position_id=902, volume=0.01)]
    # still open -> untouched
    assert ex.reconcile_vanished_lane_positions("BTCUSD", still_open) == []
    # vanished -> journaled exactly once; second call dedups via the journal
    assert len(ex.reconcile_vanished_lane_positions("BTCUSD", [])) == 1
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    closes = [e for e in events if e["event"] == "lane_position_closed"]
    assert len(closes) == 1


def test_vanish_reconcile_manual_close_not_double_counted(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    entered = _entered(ex, mcp, 903)
    # manual close journals lane_position_closed -> reconcile must NOT re-add
    pos = _filled_position(position_id=903, volume=0.01)
    pos["netProfit"] = 2.0
    mcp._positions = [pos]
    assert ex.close_lane_position(903, reason="om_exit")["action"] == "closed"
    mcp.deals = [{"positionId": 903, "netProfit": 2.0}]
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []


def test_vanish_reconcile_deals_failure_defers_not_journal(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    _entered(ex, mcp, 904)
    mcp.raise_on = {"get_deals": McpClientError("transient")}
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal_conn._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)
    assert any(e["event"] == "vanish_reconcile_deferred" for e in events)
    # deals recover -> journaled on the next bar
    mcp.raise_on = {}
    mcp.deals = [{"positionId": 904, "netProfit": 1.5}]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1 and out[0]["pnl"] == pytest.approx(1.5)


def test_vanish_reconcile_unknown_lane_is_noop(journal_conn):
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal_conn, ExecutorConfig())
    _entered(ex, mcp, 905)
    assert ex.reconcile_vanished_lane_positions("BTCUSD", None) == []  # unknown broker state

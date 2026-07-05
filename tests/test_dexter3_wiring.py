"""HUNT MODE wiring tests: executor basket surface + runner glue helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dexter3.decision_journal import DecisionJournal
from dexter3.executor import LABEL, Dexter3Executor, ExecutorConfig
import dexter3.shadow_runner as sr


class FakeMcp:
    def __init__(self, positions: list[dict] | None = None) -> None:
        self._positions = list(positions or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.symbol_details = {"minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01}
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.filled_next: dict | None = None

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {}))
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {}))
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        out = list(self._positions)
        if self.filled_next is not None and any(c[0] == "place_market_order" for c in self.calls):
            out.append(dict(self.filled_next))
        return out

    def get_balance(self) -> dict:
        self.calls.append(("get_balance", {}))
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        return {"dealStatus": "FILLED"}

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        self.calls.append(("amend_position", {"position_id": position_id}))
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        self.calls.append(("close_position", {"position_id": position_id}))
        self._positions = [p for p in self._positions if int(p.get("positionId", 0)) != int(position_id)]
        return {"status": "closed"}


def _lane_pos(pid: int, label: str = LABEL, side: str = "Buy") -> dict:
    return {
        "positionId": pid, "symbolName": "BTCUSD", "tradeSide": side,
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": label,
    }


class EnterDecision:
    symbol = "BTCUSD"
    action = "enter"
    side = "buy"
    entry_type = "market"
    entry = 62003.0
    sl = 61940.0
    tp = 62120.0
    size_class = "scout"
    setup = "hunt_test"
    ts_close = "2026-07-05T12:00:00Z"
    reasons: list[str] = ["test"]
    features: dict[str, Any] = {}
    leader_score = 0.0
    p_win_est = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "action": self.action}


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "wiring_journal.db")
    yield j
    j.close()


# -- execute_close_all ---------------------------------------------------------


def test_close_all_closes_every_lane_position(journal):
    mcp = FakeMcp(positions=[_lane_pos(1), _lane_pos(2, side="Sell")])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_close_all([1, 2], reason="close_all_in_profit")
    assert out["action"] == "closed_all"
    assert out["closed"] == 2
    assert [c for c in mcp.calls if c[0] == "close_position"] == [
        ("close_position", {"position_id": 1}),
        ("close_position", {"position_id": 2}),
    ]


def test_close_all_refuses_foreign_label_positions(journal):
    mcp = FakeMcp(positions=[_lane_pos(1), _lane_pos(9, label="dexter-scalp:codex:btc-v1")])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_close_all([1, 9], reason="cap_stop")
    assert out["action"] == "close_all_partial"
    assert out["closed"] == 1
    closed_ids = [c[1]["position_id"] for c in mcp.calls if c[0] == "close_position"]
    assert closed_ids == [1]  # peer position never receives a close call


# -- basket_authorized bypass ---------------------------------------------------


def test_repair_leg_bypasses_duplicate_guard_only(journal):
    mcp = FakeMcp(positions=[_lane_pos(1)])
    mcp.filled_next = _lane_pos(2, side="Sell")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    plain = ex.execute_entry(EnterDecision(), {"traderId": 9922808})
    assert plain["action"] == "refused"
    assert plain["reason"] == "duplicate_label_position_open"
    repaired = ex.execute_repair_leg(EnterDecision(), {"traderId": 9922808})
    assert repaired["action"] != "refused" or repaired.get("reason") != "duplicate_label_position_open"


def test_repair_leg_still_enforces_demo_gate(journal):
    mcp = FakeMcp(positions=[_lane_pos(1)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_repair_leg(EnterDecision(), {"traderId": 111111})
    assert out["action"] == "refused"
    assert out["reason"] == "account_not_confirmed_demo"


# -- runner glue helpers ---------------------------------------------------------


def test_daily_state_rollover(monkeypatch):
    state: dict[str, Any] = {}
    d1 = sr._daily_state(state)
    d1["entries"] = 4
    d1["loss_baskets"] = 1
    same = sr._daily_state(state)
    assert same["entries"] == 4  # same day: counters preserved

    import datetime as _dt

    class Tomorrow:
        strptime = _dt.datetime.strptime
        fromisoformat = _dt.datetime.fromisoformat
        fromtimestamp = _dt.datetime.fromtimestamp

        @staticmethod
        def now(tz=None):
            return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)

    monkeypatch.setattr(sr, "datetime", Tomorrow)
    d2 = sr._daily_state(state)
    assert d2["entries"] == 0 and d2["loss_baskets"] == 0  # new day resets


def _chop_bars(n: int = 50, base: float = 62000.0) -> list[dict[str, Any]]:
    bars = []
    for i in range(n):
        px = base + (7.0 if i % 2 == 0 else -7.0)
        bars.append({
            "ts": f"2026-07-05T{8 + i // 12:02d}:{(i % 12) * 5:02d}:00Z",
            "open": px, "high": px + 30.0, "low": px - 30.0, "close": px + (5.0 if i % 2 else -5.0),
        })
    return bars


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_repair_geometry_cost_guards(side):
    spread = 12.0
    entry, sl, tp = sr._repair_geometry(_chop_bars(), side, spread)
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    assert sl_dist >= 6 * spread - 1e-6
    assert tp_dist >= 8 * spread - 1e-6
    assert tp_dist >= 1.2 * sl_dist - 1e-6
    if side == "buy":
        assert sl < entry < tp
    else:
        assert tp < entry < sl

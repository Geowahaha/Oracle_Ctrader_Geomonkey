"""Regression tests for the 2026-07-15 OM-blindspot triad — incident
forensics on position 652652362 (docs/AGENT_SYNC_BOARD.md 2026-07-15
~07:30Z "OM ROUND-TRIP INCIDENT"):

  1. dexter3/openapi_client.py + dexter3/openapi_daemon.py — an
     INDEPENDENT client-side spot-staleness gate (``DEXTER3_SPOT_MAX_AGE_SEC``)
     so a stale quote can never again be stamped
     ``pnl_source=computed_from_live_spot``. Real incident: 05:30->06:46Z
     the OM read this position's PnL as -5 to -15 USD while the broker's
     true floating was roughly -1 to +5 USD (implied spot ~4044, hours old).
  2. dexter3/shadow_runner.py — a per-position_id peak-R durability ledger
     that lets a resetting ``basket_runtime`` (transient empty-lane read, a
     label-family boundary crossing, or a genuine process restart) resume
     its peak from real history instead of restarting blind.
  3. dexter3/executor.py + dexter3/shadow_runner.py — repair-lineage
     enrichment: ``entry_executed`` + ``basket_repair_leg`` journal rows
     carry parent_position_ids/parent_setup/basket_id/repair_side_mode/
     basket_agg_r_at_repair/basket_pnl_at_repair/evidence flags/session.

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB / tmp_path state file, same conventions as
tests/test_dexter3_cross_lane_incident_fixes.py and tests/test_dexter3_wiring.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.openapi_client as openapi_client_module
import dexter3.shadow_runner as sr
from dexter3.basket_live import aggregate_lane
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import Dexter3Executor, ExecutorConfig, recent_exec_events
from dexter3.openapi_client import (
    CLIENT_SPOT_MAX_AGE_ENV_VAR,
    DEFAULT_ACCOUNT_ID_PIN,
    DEFAULT_CLIENT_SPOT_MAX_AGE_SEC,
    Dexter3OpenApiClient,
    resolve_client_spot_max_age_sec,
)

# ---------------------------------------------------------------------------
# shared fixtures / fakes (mirrors tests/test_dexter3_cross_lane_incident_fixes.py
# and tests/test_dexter3_wiring.py)
# ---------------------------------------------------------------------------


class FakeMcp:
    """Records calls; canned responses configurable per test via attrs."""

    def __init__(self) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 4100.0, "ask": 4100.2}
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

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        self.calls.append(("get_trendbars", {"period": period, "count": count}))
        return []

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
        self._positions = [p for p in self._positions if int(p.get("positionId", 0) or 0) != position_id]
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        self.calls.append(("get_deals", {"count": count, "from_timestamp_ms": from_timestamp_ms}))
        return list(self.deals)


class FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="XAUUSD", action="enter", side="buy", entry=4100.0, sl=4090.0, tp=4150.0,
            setup="leader_continuation", reasons=["test_reason"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


DEMO_ACCOUNT = {"traderId": 9922808}


def _entered(ex: Dexter3Executor, mcp: FakeMcp, pid: int, **decision_kw: Any) -> dict:
    """Drive a real entry so entry_executed exists for pid, stamped with
    whatever dexter3.executor.LABEL is active right now."""
    mcp.post_entry_position = {
        "positionId": pid, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.01, "stopLoss": 4090.0, "takeProfit": 4150.0, "label": ex_mod.LABEL,
    }
    result = ex.execute_entry(FakeDecision(**decision_kw), DEMO_ACCOUNT)
    assert result["action"] == "entered" and result["position_id"] == pid
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None
    return result


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "om_blindspot_journal.db")
    yield j
    j.close()


def _isolate_shadow_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep runner-glue tests from touching the live lane's state or log."""
    monkeypatch.setattr(sr, "STATE_FILE", tmp_path / "dexter3_shadow_state.json")
    monkeypatch.setattr(sr, "GROK_STATE_FILE", tmp_path / "dexter3_grok_shadow_state.json")
    monkeypatch.setattr(sr, "log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sr, "log_error", lambda *_args, **_kwargs: None)


def _lane_pos(
    pid: int,
    pnl: float,
    *,
    open_time: str = "2026-07-15T05:00:00.000Z",
    side: str = "Sell",
    label: str | None = None,
) -> dict:
    """Lane position dict with netProfit stamped directly — bypasses the
    live-spot pnl computation entirely (that path is FIX #1's own section
    below), so FIX #2/#3 tests control the basket's PnL precisely."""
    return {
        "positionId": pid,
        "symbolName": "XAUUSD",
        "tradeSide": side,
        "volumeInUnits": 1.0,
        "entryPrice": 4100.0,
        "stopLoss": 4110.0 if side == "Sell" else 4090.0,
        "takeProfit": 4050.0 if side == "Sell" else 4150.0,
        "label": label or ex_mod.LABEL,
        "netProfit": pnl,
        "openTime": open_time,
    }


class _FakeHttpResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = int(status_code)
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


def _accounts_ok() -> dict[str, Any]:
    return {
        "ok": True, "status": "accounts_loaded", "message": "loaded 1 accounts",
        "environment": "demo",
        "accounts": [
            {
                "accountId": DEFAULT_ACCOUNT_ID_PIN, "accountNumber": 9922808,
                "traderLogin": 9922808, "live": False, "isLive": False,
            }
        ],
        "token_refresh": {},
    }


class _InvokeRouter:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.responses.setdefault("accounts", _accounts_ok())
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def __call__(self, mode, payload, *, mutating=False, timeout_sec=None):
        self.calls.append((mode, dict(payload), mutating))
        resp = self.responses.get(mode)
        if resp is None:
            raise AssertionError(f"no canned response for mode={mode!r}")
        return resp


# Mirrors the incident's real leg (docs/AGENT_SYNC_BOARD.md 2026-07-15
# ~07:30Z): short XAUUSD, entry 4032.75.
_SHORT_POSITION = {
    "position_id": 652652362,
    "symbol_id": 41,
    "symbol": "XAUUSD",
    "direction": "short",
    "volume": 100,  # raw -> 1.0 oz
    "entry_price": 4032.75,
    "stop_loss": 4042.75,
    "take_profit": 4010.0,
    "label": "dexter3:fable:m5h-v1",
    "comment": "",
    "open_timestamp_ms": 1783623090337,
    "open_utc": "2026-07-15T05:00:00Z",
    "updated_timestamp_ms": 0,
    "updated_utc": "",
    "status": "POSITION_STATUS_OPEN",
    "swap": 0.0,
    "commission": 0.0,
    "used_margin": 12.0,
    "money_digits": 2,
    "raw": {},
}


# ---------------------------------------------------------------------------
# FIX #1 — client-side independent spot-staleness gate
# ---------------------------------------------------------------------------


def test_stale_spot_rejects_pnl_and_marks_unreliable(monkeypatch):
    """The exact incident shape: a spot the daemon answers successfully but
    whose OWN age exceeds the client's independent ceiling must NEVER be
    stamped computed_from_live_spot. netProfit stays unset -> basket_live
    reports unreliable=True -> the OM holds (unreliable_pnl), never acting
    on a fabricated number."""
    c = Dexter3OpenApiClient()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_SHORT_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    # 4560s (76 min) old — exactly the incident's 05:30->06:46Z gap.
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4044.0, "ask": 4044.2, "symbol": sym, "spot_age_sec": 4560.0})
    [pos] = c.get_positions()
    assert "netProfit" not in pos
    assert pos["pnl_source"] == "stale_spot_rejected"
    assert pos["pnl_spot_age_sec"] == pytest.approx(4560.0)

    agg = aggregate_lane([pos], base_risk_usd=5.0)
    assert agg["unreliable"] is True
    assert agg["pnl_sources"] == {"stale_spot_rejected": 1}
    assert agg["pnl_spot_ages"] == {"652652362": pytest.approx(4560.0)}


def test_fresh_spot_within_threshold_computes_pnl_normally(monkeypatch):
    c = Dexter3OpenApiClient()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_SHORT_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4031.45, "ask": 4031.60, "symbol": sym, "spot_age_sec": 3.2})
    [pos] = c.get_positions()
    # short at 4032.75, closes at ask 4031.60 -> (4032.75 - 4031.60) x 1.0 oz = +1.15
    assert pos["netProfit"] == pytest.approx(1.15, abs=1e-2)
    assert pos["pnl_source"] == "computed_from_live_spot"
    assert pos["pnl_spot_age_sec"] == pytest.approx(3.2)

    agg = aggregate_lane([pos], base_risk_usd=5.0)
    assert agg["unreliable"] is False
    assert agg["pnl_sources"] == {"computed_from_live_spot": 1}


def test_client_gate_disabled_when_env_le_zero(monkeypatch):
    monkeypatch.setenv(CLIENT_SPOT_MAX_AGE_ENV_VAR, "0")
    c = Dexter3OpenApiClient()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_SHORT_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4044.0, "ask": 4044.2, "symbol": sym, "spot_age_sec": 4560.0})
    [pos] = c.get_positions()
    # gate disabled -> not rejected despite a huge age
    assert pos["pnl_source"] == "computed_from_live_spot"
    assert "netProfit" in pos


def test_client_gate_fails_open_on_missing_age(monkeypatch):
    """Legacy/test fakes that supply no age field at all must not be
    rejected — there is nothing to compare, so the gate fails OPEN on
    absent metadata (only fails CLOSED on a KNOWN stale age). This
    preserves every pre-existing get_positions/get_spot_price fixture in
    tests/test_dexter3_openapi_client.py that never modeled quote age."""
    c = Dexter3OpenApiClient()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_SHORT_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4031.45, "ask": 4031.60, "symbol": sym})
    [pos] = c.get_positions()
    assert pos["pnl_source"] == "computed_from_live_spot"
    assert "pnl_spot_age_sec" not in pos


def test_resolve_client_spot_max_age_sec_env_parsing(monkeypatch):
    monkeypatch.delenv(CLIENT_SPOT_MAX_AGE_ENV_VAR, raising=False)
    assert resolve_client_spot_max_age_sec() == DEFAULT_CLIENT_SPOT_MAX_AGE_SEC == 90.0
    monkeypatch.setenv(CLIENT_SPOT_MAX_AGE_ENV_VAR, "45")
    assert resolve_client_spot_max_age_sec() == 45.0
    monkeypatch.setenv(CLIENT_SPOT_MAX_AGE_ENV_VAR, "-5")
    assert resolve_client_spot_max_age_sec() is None
    monkeypatch.setenv(CLIENT_SPOT_MAX_AGE_ENV_VAR, "garbage")
    assert resolve_client_spot_max_age_sec() == DEFAULT_CLIENT_SPOT_MAX_AGE_SEC


def test_daemon_mode_get_spot_price_exposes_spot_age_sec(monkeypatch):
    """The daemon's own spot_quote success payload now carries spot_age_sec
    alongside quote_age_sec (openapi_daemon.py's _mode_spot_quote), and the
    client threads it through unchanged."""

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        if json["mode"] == "accounts":
            return _FakeHttpResponse(_accounts_ok())
        if json["mode"] == "spot_quote":
            return _FakeHttpResponse({
                "ok": True, "status": "spot_quote", "source": "live_cache",
                "quote_age_sec": 0.42, "spot_age_sec": 0.42,
                "spots": [{"bid": 4031.45, "ask": 4031.60, "event_utc": "2026-07-15T06:46:00Z"}],
            })
        raise AssertionError(f"unexpected mode {json['mode']}")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_URL", "http://127.0.0.1:9877")
    c = Dexter3OpenApiClient()
    quote = c.get_spot_price("XAUUSD")
    assert quote["quote_age_sec"] == pytest.approx(0.42)
    assert quote["spot_age_sec"] == pytest.approx(0.42)


def test_subprocess_mode_computes_spot_age_from_event_ts(monkeypatch):
    """The subprocess/capture_market transport had NO age concept at all
    before this fix; it must now derive one from the event's own event_ts."""
    import time as _time

    c = Dexter3OpenApiClient()
    stale_event_ts = _time.time() - 120.0
    router = _InvokeRouter({
        "capture_market": {
            "ok": True,
            "spots": [{"bid": 2400.0, "ask": 2400.2, "event_utc": "t", "event_ts": stale_event_ts}],
        }
    })
    monkeypatch.setattr(c, "_invoke", router)
    quote = c.get_spot_price("XAUUSD")
    assert quote["spot_age_sec"] == pytest.approx(120.0, abs=2.0)
    assert quote["quote_age_sec"] == pytest.approx(120.0, abs=2.0)


# ---------------------------------------------------------------------------
# FIX #2 — per-position peak-R durability ledger
# ---------------------------------------------------------------------------


def test_position_peak_ledger_raises_and_never_lowers():
    state: dict = {}
    lane = [_lane_pos(501, 2.0)]
    sr._update_position_peak_ledger(state, "XAUUSD", lane, peak_r=0.3, floor_r=None)
    assert state["position_peak_r"]["501"]["peak_r"] == pytest.approx(0.3)
    # a LOWER peak_r this tick must never lower the recorded ledger value
    sr._update_position_peak_ledger(state, "XAUUSD", lane, peak_r=0.1, floor_r=None)
    assert state["position_peak_r"]["501"]["peak_r"] == pytest.approx(0.3)
    # a HIGHER peak_r raises it, and floor_r is recorded when armed
    sr._update_position_peak_ledger(state, "XAUUSD", lane, peak_r=0.5, floor_r=0.2)
    assert state["position_peak_r"]["501"]["peak_r"] == pytest.approx(0.5)
    assert state["position_peak_r"]["501"]["floor_r"] == pytest.approx(0.2)


def test_position_peak_ledger_purges_when_position_no_longer_in_lane():
    state: dict = {
        "position_peak_r": {
            "501": {"symbol": "XAUUSD", "peak_r": 0.5},
            "999": {"symbol": "BTCUSD", "peak_r": 0.9},
        }
    }
    # lane=[] for XAUUSD is the "position no longer open" evidence
    sr._update_position_peak_ledger(state, "XAUUSD", [], peak_r=None, floor_r=None)
    assert "501" not in state["position_peak_r"]
    assert "999" in state["position_peak_r"]  # a peer symbol's entry is untouched


def test_seed_peak_r_from_ledger_never_lowers_and_resumes_higher():
    state: dict = {"position_peak_r": {"501": {"symbol": "XAUUSD", "peak_r": 0.42}}}
    lane = [_lane_pos(501, 0.5)]
    assert sr._seed_peak_r_from_ledger(state, lane, reset_peak_r=0.05) == pytest.approx(0.42)
    assert sr._seed_peak_r_from_ledger(state, lane, reset_peak_r=0.99) == pytest.approx(0.99)  # never below reset


def test_run_om_tick_peak_r_survives_simulated_restart_and_reseeds(journal, monkeypatch, tmp_path):
    """The exact incident gap (defect #2, 'OM amnesia'): a basket_runtime
    reset — simulated here as the incident describes it, the symbol's
    basket_runtime entry is gone even though the SAME broker position is
    still open — must resume its peak from the durability ledger instead of
    restarting blind at this tick's live_r."""
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    _isolate_shadow_runtime(monkeypatch, tmp_path)

    # TIME-BOMB FIX (2026-07-15): open_time must be dynamic, never the file's
    # hardcoded 2026-07-15T05:00Z literal — from 08:00:00Z (05:00Z +
    # BasketConfig.time_stop_min=180) onward that literal makes enforce_caps
    # fire cap_stop -> close_all -> _clear_basket_runtime pops the very key
    # this test asserts (KeyError 'XAUUSD'), so the test only ever passed for
    # the 3h window after it was written (owner rule: no hardcoded
    # timestamps — memory feedback_no_careless_hardcode.md). A fresh
    # timestamp keeps the OM on the hold path forever; every assertion below
    # is unchanged.
    open_ts = sr.utc_now_iso()

    mcp = FakeMcp()
    mcp._positions = [_lane_pos(652652362, 0.5, open_time=open_ts)]  # $0.50 pnl / $10 risk = 0.05R
    state: dict = {}
    status = sr.run_om_tick(mcp, journal, state, "XAUUSD", executor=None)
    assert status.startswith("om_")
    assert state["basket_runtime"]["XAUUSD"]["peak_r"] == pytest.approx(0.05)
    assert state["position_peak_r"]["652652362"]["peak_r"] == pytest.approx(0.05)

    # -- simulate restart: real disk round-trip through the SAME state file --
    sr.save_shadow_state(state)
    restarted_state = sr.load_shadow_state()
    assert restarted_state["position_peak_r"]["652652362"]["peak_r"] == pytest.approx(0.05)

    # -- simulate the incident's "OM amnesia": basket_runtime for this
    # symbol is gone even though the position never closed --
    restarted_state["basket_runtime"].pop("XAUUSD", None)
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)  # fresh OM instance ("new process")
    mcp2 = FakeMcp()
    # SAME open_ts as the first tick — the same broker position, still open.
    mcp2._positions = [_lane_pos(652652362, 0.1, open_time=open_ts)]  # a LOWER pnl this tick: 0.1/10 = 0.01R
    status2 = sr.run_om_tick(mcp2, journal, restarted_state, "XAUUSD", executor=None)
    assert status2.startswith("om_")
    # A blind reset would have started peak_r at 0.01 (this tick's live_r).
    # The ledger must have re-seeded it from the surviving 0.05 peak instead.
    assert restarted_state["basket_runtime"]["XAUUSD"]["peak_r"] == pytest.approx(0.05)


def test_run_om_tick_purges_ledger_when_position_closes(journal, monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    _isolate_shadow_runtime(monkeypatch, tmp_path)

    mcp = FakeMcp()
    mcp._positions = [_lane_pos(777, 1.0)]
    state: dict = {}
    sr.run_om_tick(mcp, journal, state, "XAUUSD", executor=None)
    assert "777" in state["position_peak_r"]

    mcp._positions = []  # position closed at the broker
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    status = sr.run_om_tick(mcp, journal, state, "XAUUSD", executor=None)
    assert status == "om_no_lane"
    assert "777" not in state["position_peak_r"]


# ---------------------------------------------------------------------------
# FIX #3 — repair-lineage enrichment
# ---------------------------------------------------------------------------


def test_execute_repair_leg_enriches_entry_executed_and_basket_repair_leg(journal, monkeypatch):
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())

    # Parent leg's own entry_executed row — parent_setup must resolve FROM
    # this row (dragon_shelf_short), never be invented.
    _entered(ex, mcp, 501, setup="dragon_shelf_short", session="asian")

    repair_context = {
        "parent_position_ids": [501],
        "basket_id": "2026-07-15T05:00:00.000Z",
        "basket_side": "sell",
        "basket_agg_r_at_repair": -1.2,
        "basket_pnl_at_repair": -12.0,
        "level_lost": True,
        "close_beyond": True,
    }
    repair_decision = FakeDecision(side="buy", session="overlap", setup="opening_manager_repair")
    mcp.post_entry_position = {
        "positionId": 900, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 4090.0, "takeProfit": 4150.0, "label": ex_mod.LABEL,
    }
    result = ex.execute_repair_leg(repair_decision, DEMO_ACCOUNT, repair_context=repair_context)
    assert result["action"] == "entered" and result["position_id"] == 900

    events = recent_exec_events(journal._conn, symbol="XAUUSD")
    entry_row = next(e for e in events if e["event"] == "entry_executed" and e["position_id"] == 900)
    assert entry_row["payload"]["session"] == "overlap"
    lineage = entry_row["payload"]["repair_context"]
    assert lineage["parent_position_ids"] == [501]
    assert lineage["parent_setup"] == "dragon_shelf_short"
    assert lineage["basket_id"] == "2026-07-15T05:00:00.000Z"
    assert lineage["repair_side_mode"] == "hedge_lock"  # decision.side=buy != basket_side=sell
    assert lineage["basket_agg_r_at_repair"] == pytest.approx(-1.2)
    assert lineage["basket_pnl_at_repair"] == pytest.approx(-12.0)
    assert lineage["level_lost"] is True
    assert lineage["close_beyond"] is True

    repair_row = next(e for e in events if e["event"] == "basket_repair_leg")
    assert repair_row["position_id"] == 900
    assert repair_row["payload"]["entry_result_action"] == "entered"
    for key in (
        "parent_position_ids", "parent_setup", "basket_id", "repair_side_mode",
        "basket_agg_r_at_repair", "basket_pnl_at_repair", "level_lost", "close_beyond",
    ):
        assert repair_row["payload"][key] == lineage[key]


def test_execute_repair_leg_same_side_mode(journal, monkeypatch):
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 502, setup="sweep_reclaim", session="london")
    repair_context = {"parent_position_ids": [502], "basket_side": "sell"}
    repair_decision = FakeDecision(side="sell", session="london", setup="opening_manager_repair")
    mcp.post_entry_position = {
        "positionId": 901, "symbolName": "XAUUSD", "tradeSide": "Sell",
        "volumeInUnits": 1.0, "stopLoss": 4110.0, "takeProfit": 4050.0, "label": ex_mod.LABEL,
    }
    ex.execute_repair_leg(repair_decision, DEMO_ACCOUNT, repair_context=repair_context)
    events = recent_exec_events(journal._conn, symbol="XAUUSD")
    repair_row = next(e for e in events if e["event"] == "basket_repair_leg")
    assert repair_row["payload"]["repair_side_mode"] == "same_side"


def test_execute_entry_without_repair_context_unaffected(journal, monkeypatch):
    """Non-repair callers must see byte-identical entry_executed payloads —
    no repair_context key appears at all."""
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    mcp.post_entry_position = {
        "positionId": 1001, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 4090.0, "takeProfit": 4150.0, "label": ex_mod.LABEL,
    }
    result = ex.execute_entry(FakeDecision(session="ny"), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert "repair_context" not in result

    events = recent_exec_events(journal._conn, symbol="XAUUSD")
    entry_row = next(e for e in events if e["event"] == "entry_executed" and e["position_id"] == 1001)
    assert "repair_context" not in entry_row["payload"]


def test_manage_lane_basket_repair_leg_carries_session_and_lineage(journal, monkeypatch, tmp_path):
    """Wiring test — isolates shadow_runner._manage_lane_basket's own
    plumbing (session computation + repair_context construction) from
    basket_live.decide_basket_action's decision logic, which is already
    covered by tests/test_dexter3_basket_live.py and
    tests/test_dexter3_opening_manager.py: a real M5-path repair leg must
    carry the real session label + the basket facts that triggered it."""
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 501, setup="dragon_shelf_short", session="asian")

    lane = [_lane_pos(501, -12.0, side="Sell")]
    canned_action = {"action": "add_repair_leg", "side": "buy", "note": "counter_trend_recovery", "conviction": 0.7}
    monkeypatch.setattr(sr.basket_live, "decide_basket_action", lambda *a, **k: dict(canned_action))

    prefix = [
        {"ts": f"2026-07-15T04:{m:02d}:00Z", "open": 4100.0, "high": 4105.0, "low": 4095.0, "close": 4102.0}
        for m in range(0, 55, 5)
    ]
    mcp.post_entry_position = {
        "positionId": 900, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 0, "takeProfit": 0, "label": ex_mod.LABEL,
    }
    state: dict = {}
    sr._manage_lane_basket(ex, "XAUUSD", prefix[-1]["ts"], prefix, lane, state, spread_abs=0.5)

    events = recent_exec_events(journal._conn, symbol="XAUUSD")
    entry_row = next(e for e in events if e["event"] == "entry_executed" and e["position_id"] == 900)
    # ts_close = bar_ts (04:50Z) + 5min = 04:55Z -> hour 4 -> asian (00:00-07:00)
    assert entry_row["payload"]["session"] == "asian"
    lineage = entry_row["payload"]["repair_context"]
    assert lineage["parent_position_ids"] == [501]
    assert lineage["parent_setup"] == "dragon_shelf_short"
    assert lineage["basket_id"] == "2026-07-15T05:00:00.000Z"
    assert lineage["repair_side_mode"] == "hedge_lock"  # side=buy vs basket_side=sell
    assert lineage["basket_agg_r_at_repair"] == pytest.approx(-1.2)  # -12.0 pnl / 10.0 risk
    assert lineage["basket_pnl_at_repair"] == pytest.approx(-12.0)
    assert lineage["level_lost"] in (True, False)
    assert lineage["close_beyond"] in (True, False)

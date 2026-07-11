"""Dexter3 OpenAPI transport — golden-payload normalization + wiring tests.

No live network: every test monkeypatches ``Dexter3OpenApiClient._invoke``
(or, for the transport-classification tests, ``_run_worker_once``) so the
worker subprocess is never actually spawned. Golden payload shapes below
mirror ``ops/ctrader_execute_once.py``'s ACTUAL response envelopes (read
directly from that file — see dexter3/openapi_client.py's module docstring
for the field-by-field evidence trail), not invented data.
"""
from __future__ import annotations

import os
from typing import Any

import pytest
import requests

import dexter3.openapi_client as openapi_client_module
from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpMutationUncertain
from dexter3.openapi_client import (
    DEFAULT_ACCOUNT_ID_PIN,
    DEFAULT_DAEMON_TIMEOUT_SEC,
    DEFAULT_TIMEOUT_SEC,
    Dexter3OpenApiAccountPinError,
    Dexter3OpenApiClient,
    Dexter3OpenApiNotImplementedError,
    Dexter3OpenApiTransportError,
    _ms_to_iso_with_millis,
    _raw_volume_to_units,
    _units_to_raw_volume,
)
from dexter3.transport import make_client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _accounts_ok(account_id: int = DEFAULT_ACCOUNT_ID_PIN) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "accounts_loaded",
        "message": "loaded 1 accounts",
        "environment": "demo",
        "accounts": [
            {
                "accountId": account_id,
                "accountNumber": 9922808,
                "traderLogin": 9922808,
                "live": False,
                "isLive": False,
            }
        ],
        "token_refresh": {},
    }


def _client(**kwargs) -> Dexter3OpenApiClient:
    return Dexter3OpenApiClient(**kwargs)


class _InvokeRouter:
    """Routes ``_invoke(mode, payload, ...)`` calls to per-mode canned
    responses; records every call for assertions. Always answers "accounts"
    with a passing pin unless overridden."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.responses.setdefault("accounts", _accounts_ok())
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def __call__(self, mode, payload, *, mutating=False, timeout_sec=None):
        self.calls.append((mode, dict(payload), mutating))
        resp = self.responses.get(mode)
        if callable(resp):
            return resp(payload)
        if resp is None:
            raise AssertionError(f"no canned response for mode={mode!r}")
        return resp


# ---------------------------------------------------------------------------
# volume conversion
# ---------------------------------------------------------------------------


def test_units_to_raw_volume_round_trip():
    assert _units_to_raw_volume(1.0) == 100
    assert _units_to_raw_volume(0.01) == 1
    assert _raw_volume_to_units(100) == pytest.approx(1.0)
    assert _raw_volume_to_units(1) == pytest.approx(0.01)


def test_ms_to_iso_with_millis_formats_milliseconds():
    # 2026-07-07T19:11:30.337Z
    import calendar
    from datetime import datetime, timezone

    dt = datetime(2026, 7, 7, 19, 11, 30, 337000, tzinfo=timezone.utc)
    ms = calendar.timegm(dt.timetuple()) * 1000 + 337
    assert _ms_to_iso_with_millis(ms) == "2026-07-07T19:11:30.337Z"
    assert _ms_to_iso_with_millis(0) == ""
    assert _ms_to_iso_with_millis(None) == ""


# ---------------------------------------------------------------------------
# account pin
# ---------------------------------------------------------------------------


def test_account_pin_default_is_design_doc_value():
    c = _client()
    assert c.account_id == DEFAULT_ACCOUNT_ID_PIN == 46670728


def test_account_pin_env_override(monkeypatch):
    monkeypatch.setenv("DEXTER3_OPENAPI_ACCOUNT_ID", "12345")
    c = _client()
    assert c.account_id == 12345


def test_account_pin_success_allows_calls(monkeypatch):
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    assert c.get_positions() == []
    assert c._pin_verified is True
    modes = [call[0] for call in router.calls]
    assert modes[0] == "accounts"  # pin checked before the real operation


def test_account_pin_refusal_when_account_not_in_token_list(monkeypatch):
    c = _client(account_id=999999)
    router = _InvokeRouter({"accounts": _accounts_ok(account_id=46670728)})
    monkeypatch.setattr(c, "_invoke", router)
    with pytest.raises(Dexter3OpenApiAccountPinError):
        c.get_positions()
    # and every OTHER operation is refused too, not just the first
    with pytest.raises(Dexter3OpenApiAccountPinError):
        c.get_balance()


def test_account_pin_refusal_is_mcp_client_error_subclass():
    assert issubclass(Dexter3OpenApiAccountPinError, McpClientError)


def test_account_pin_cached_after_first_success(monkeypatch):
    c = _client()
    router = _InvokeRouter(
        {
            "reconcile": {"ok": True, "positions": [], "orders": [], "deals": []},
            "health": {"ok": True, "balance": 100000, "money_digits": 2, "account_id": DEFAULT_ACCOUNT_ID_PIN, "environment": "demo"},
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    c.get_positions()
    c.get_balance()
    accounts_calls = [call for call in router.calls if call[0] == "accounts"]
    assert len(accounts_calls) == 1  # verified once, cached for the rest of the instance's life


# ---------------------------------------------------------------------------
# diagnose_account_pin — non-raising preflight (added 2026-07-10, P2 token
# investigation: distinguishes "worker/token broken" from "account not in
# token's list" without reading logs)
# ---------------------------------------------------------------------------


def test_diagnose_account_pin_ok_fresh(monkeypatch):
    c = _client()
    router = _InvokeRouter({})
    monkeypatch.setattr(c, "_invoke", router)
    result = c.diagnose_account_pin()
    assert result == {
        "ok": True,
        "reason": "pin_ok",
        "configured_account_id": DEFAULT_ACCOUNT_ID_PIN,
        "seen_account_ids": [DEFAULT_ACCOUNT_ID_PIN],
        "error_type": None,
        "message": f"account {DEFAULT_ACCOUNT_ID_PIN} confirmed among token accounts [{DEFAULT_ACCOUNT_ID_PIN}]",
    }
    assert c._pin_verified is True
    assert [call[0] for call in router.calls] == ["accounts"]


def test_diagnose_account_pin_never_raises_and_does_not_repeat_network_call(monkeypatch):
    c = _client()
    router = _InvokeRouter({})
    monkeypatch.setattr(c, "_invoke", router)
    first = c.diagnose_account_pin()
    second = c.diagnose_account_pin()
    assert first["reason"] == "pin_ok"
    assert second["reason"] == "pin_ok_cached"
    assert second["ok"] is True
    assert len(router.calls) == 1  # second call made no network round trip


def test_diagnose_account_pin_account_not_in_token_list(monkeypatch):
    c = _client(account_id=999999)
    router = _InvokeRouter({"accounts": _accounts_ok(account_id=46670728)})
    monkeypatch.setattr(c, "_invoke", router)
    result = c.diagnose_account_pin()
    assert result["ok"] is False
    assert result["reason"] == "account_not_in_token_list"
    assert result["seen_account_ids"] == [46670728]
    assert result["error_type"] is None
    assert c._pin_verified is False  # never raises, but also never falsely marks verified


def test_diagnose_account_pin_worker_call_failed_tool_error(monkeypatch):
    c = _client()

    def _raise_invalid_token(mode, payload, *, mutating=False, timeout_sec=None):
        raise McpClientError("openapi worker mode=accounts failed status=accounts_failed: Invalid access token")

    monkeypatch.setattr(c, "_invoke", _raise_invalid_token)
    result = c.diagnose_account_pin()
    assert result["ok"] is False
    assert result["reason"] == "worker_call_failed"
    assert result["error_type"] == "McpClientError"
    assert "Invalid access token" in result["message"]
    assert c._pin_verified is False


def test_diagnose_account_pin_worker_call_failed_transport_error(monkeypatch):
    c = _client()

    def _raise_transport(mode, payload, *, mutating=False, timeout_sec=None):
        raise Dexter3OpenApiTransportError("worker transport failed twice for mode=accounts: timeout")

    monkeypatch.setattr(c, "_invoke", _raise_transport)
    result = c.diagnose_account_pin()
    assert result["ok"] is False
    assert result["reason"] == "worker_call_failed"
    assert result["error_type"] == "Dexter3OpenApiTransportError"
    assert c._pin_verified is False


# ---------------------------------------------------------------------------
# positions normalization
# ---------------------------------------------------------------------------

_GOLDEN_POSITION = {
    "position_id": 62664990,
    "symbol_id": 41,
    "symbol": "XAUUSD",
    "direction": "long",
    "volume": 100,  # raw centiunits -> 1.0 oz
    "entry_price": 2400.55,
    "stop_loss": 2395.10,
    "take_profit": 2410.90,
    "label": "dexter3:fable:m5h-v1",
    "comment": "pullback|m5h",
    "open_timestamp_ms": 1783623090337,  # 2026-07-08T02:11:30.337Z
    "open_utc": "2026-07-08T02:11:30Z",
    "updated_timestamp_ms": 0,
    "updated_utc": "",
    "status": "POSITION_STATUS_OPEN",
    "swap": -0.02,
    "commission": -0.05,
    "used_margin": 12.0,
    "money_digits": 2,
    "raw": {},
}


def test_get_positions_normalizes_confirmed_fields(monkeypatch):
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_GOLDEN_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    [pos] = c.get_positions()
    assert pos["positionId"] == 62664990
    assert pos["symbolName"] == "XAUUSD"
    assert pos["symbol"] == "XAUUSD"
    assert pos["tradeSide"] == "BUY"
    assert pos["volume"] == pytest.approx(1.0)
    assert pos["volumeInUnits"] == pytest.approx(1.0)
    assert pos["entryPrice"] == pytest.approx(2400.55)
    assert pos["stopLoss"] == pytest.approx(2395.10)
    assert pos["takeProfit"] == pytest.approx(2410.90)
    assert pos["label"] == "dexter3:fable:m5h-v1"
    assert pos["openTime"].endswith("Z") and "." in pos["openTime"]  # ms precision, basket timing depends on it


def test_get_positions_om_path_skips_historical_deals(monkeypatch):
    """The every-tick OM book read must not pay for a historical deal list."""
    c = _client()
    c._pin_verified = True
    seen: dict[str, object] = {}

    def _capture(mode, payload, *, mutating=False, timeout_sec=None):
        seen["mode"] = mode
        seen["payload"] = dict(payload)
        return {"ok": True, "positions": [], "orders": [], "deals": []}

    monkeypatch.setattr(c, "_invoke", _capture)
    assert c.get_positions() == []
    assert seen["mode"] == "reconcile"
    assert seen["payload"]["include_deals"] is False


def test_get_positions_short_side_maps_sell(monkeypatch):
    c = _client()
    short_pos = dict(_GOLDEN_POSITION)
    short_pos["direction"] = "short"
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [short_pos], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    [pos] = c.get_positions()
    assert pos["tradeSide"] == "SELL"


def test_get_positions_no_pnl_when_spot_unavailable(monkeypatch):
    """Gap #3 fix (2026-07-11): when no live spot is available (market closed /
    stale quote / unconfigured mode), get_positions must NOT invent a PnL — the
    position stays without netProfit so aggregate_lane degrades to
    unreliable=True/hold. Enrichment is best-effort and must never raise."""
    c = _client()
    # router answers reconcile but has NO spot_quote — the spot fetch fails and
    # enrichment leaves the position blind (the correct market-closed behavior).
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [dict(_GOLDEN_POSITION)], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    [pos] = c.get_positions()
    assert "netProfit" not in pos

    from dexter3.basket_live import aggregate_lane

    agg = aggregate_lane([pos], base_risk_usd=5.0)
    assert agg["unreliable"] is True  # confirms the safe degrade still fires


def test_get_positions_computes_live_pnl_from_spot(monkeypatch):
    """Gap #3 fix: with a fresh spot quote, get_positions computes a REAL
    netProfit (not faked) so the basket/OM can actually manage the position
    instead of holding blind to broker SL. SHORT closes at ask."""
    c = _client()
    short_pos = dict(_GOLDEN_POSITION)
    short_pos["direction"] = "short"
    short_pos["entry_price"] = 4093.36
    short_pos["volume"] = 100  # raw → 1.0 oz
    short_pos["swap"] = 0.0
    short_pos["commission"] = 0.0
    short_pos["symbol"] = "XAUUSD"
    router = _InvokeRouter({
        "reconcile": {"ok": True, "positions": [short_pos], "orders": [], "deals": []},
    })
    monkeypatch.setattr(c, "_invoke", router)
    # transport-independent: enrichment calls self.get_spot_price; feed a fresh quote.
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4110.90, "ask": 4111.00, "symbol": sym})
    [pos] = c.get_positions()
    # short at 4093.36, closes at ask 4111.00 → (4093.36 - 4111.00) × 1.0 = -17.64
    assert pos["netProfit"] == pytest.approx(-17.64, abs=1e-2)
    assert pos["pnl_source"] == "computed_from_live_spot"

    from dexter3.basket_live import aggregate_lane

    agg = aggregate_lane([pos], base_risk_usd=5.0)
    assert agg["unreliable"] is False  # now the OM can SEE the loss and act


def test_get_positions_computed_pnl_respects_long_volume_and_costs(monkeypatch):
    """Regression: OpenAPI raw volume 300 is 3 oz; long exits at bid and
    position swap/commission remain part of net PnL."""
    c = _client()
    long_pos = dict(_GOLDEN_POSITION)
    long_pos.update({"direction": "long", "entry_price": 4100.0, "volume": 300,
                     "symbol": "XAUUSD", "swap": -0.25, "commission": -0.75})
    monkeypatch.setattr(c, "_invoke", _InvokeRouter({
        "reconcile": {"ok": True, "positions": [long_pos], "orders": [], "deals": []},
    }))
    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": 4102.0, "ask": 4102.1, "symbol": sym})
    [pos] = c.get_positions()
    # (4102 - 4100) x 3 oz - 0.25 swap - 0.75 commission = +5.00 USD.
    assert pos["grossProfit"] == pytest.approx(6.0)
    assert pos["netProfit"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# balance normalization
# ---------------------------------------------------------------------------


def test_get_balance_scales_by_money_digits(monkeypatch):
    c = _client()
    router = _InvokeRouter(
        {
            "health": {
                "ok": True,
                "status": "connected",
                "account_id": DEFAULT_ACCOUNT_ID_PIN,
                "environment": "demo",
                "balance": 100000,  # raw int, money_digits=2 -> $1000.00
                "money_digits": 2,
                "leverage_in_cents": 10000,
                "positions": 0,
                "orders": 0,
            }
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    bal = c.get_balance()
    assert bal["balance"] == pytest.approx(1000.00)
    assert bal["traderId"] == DEFAULT_ACCOUNT_ID_PIN
    # gaps: present for shape parity, honestly None (never guessed)
    assert bal["equity"] is None
    assert bal["margin"] is None
    assert bal["accountType"] is None


def test_get_balance_traderId_is_what_executor_demo_gate_reads(monkeypatch):
    """dexter3.executor._is_demo_account reads exactly balance['traderId'];
    confirm that key exists and round-trips as an int."""
    c = _client()
    router = _InvokeRouter(
        {"health": {"ok": True, "balance": 0, "money_digits": 2, "account_id": DEFAULT_ACCOUNT_ID_PIN, "environment": "demo"}}
    )
    monkeypatch.setattr(c, "_invoke", router)
    bal = c.get_balance()

    from dexter3.executor import ExecutorConfig

    cfg = ExecutorConfig(demo_trader_ids=(DEFAULT_ACCOUNT_ID_PIN,))
    assert bal["traderId"] in cfg.demo_trader_ids


# ---------------------------------------------------------------------------
# deals normalization
# ---------------------------------------------------------------------------

_GOLDEN_DEAL = {
    "deal_id": 5551,
    "order_id": 5550,
    "position_id": 62664990,
    "symbol_id": 41,
    "symbol": "XAUUSD",
    "direction": "long",
    "volume": 100,
    "filled_volume": 100,
    "execution_price": 2405.10,
    "execution_timestamp_ms": 1783623200123,
    "create_timestamp_ms": 0,
    "deal_status": "FILLED",
    "gross_profit_usd": 5.0,
    "swap_usd": -0.02,
    "commission_usd": -0.05,
    "pnl_conversion_fee_usd": 0.0,
    "pnl_usd": 4.93,
    "has_close_detail": True,
    "entry_price": 2400.55,
    "closed_volume": 100,
    "balance_after_usd": 1004.93,
    "raw": {},
}


def test_get_deals_normalizes_confirmed_fields(monkeypatch):
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [], "orders": [], "deals": [dict(_GOLDEN_DEAL)]}})
    monkeypatch.setattr(c, "_invoke", router)
    [deal] = c.get_deals(count=50)
    assert deal["dealId"] == 5551
    assert deal["positionId"] == 62664990
    assert deal["netProfit"] == pytest.approx(4.93)
    assert deal["time"].endswith("Z")
    # gap #4: label is honestly empty, never guessed
    assert deal["label"] == ""


def test_get_deals_uses_longer_timeout_than_get_positions_in_daemon_mode(monkeypatch):
    """The labeled reconcile (get_deals) runs 2 extra heavy broker round-trips
    (deal-list + order-list join) and measured ~8s on the VM — it exceeds the
    5s daemon timeout get_positions needs every bar. get_deals must get a
    longer dedicated timeout; get_positions stays at the fast daemon default,
    or the governor's realized-PnL reconcile times out (cached/zero)."""
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_URL", "http://127.0.0.1:9877")
    monkeypatch.delenv("DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("DEXTER3_OPENAPI_DEALS_TIMEOUT_SEC", raising=False)
    c = _client()
    assert c.timeout_sec == DEFAULT_DAEMON_TIMEOUT_SEC  # daemon-mode fast default (12.0 since 2026-07-11)

    seen: dict[str, float | None] = {}

    def _cap(mode, payload, *, mutating=False, timeout_sec=None):
        key = "labels" if payload.get("include_deal_labels") else mode
        seen[key] = timeout_sec
        if mode == "accounts":
            return _accounts_ok()
        return {"ok": True, "positions": [], "orders": [], "deals": []}

    monkeypatch.setattr(c, "_invoke", _cap)
    c.get_positions()          # unlabeled -> fast path
    c.get_deals(count=50)      # labeled -> long path
    assert seen["reconcile"] == DEFAULT_DAEMON_TIMEOUT_SEC  # get_positions stays on the fast default
    assert seen["labels"] >= 20.0            # get_deals gets the long timeout
    assert seen["labels"] > seen["reconcile"]


def test_get_deals_timeout_is_env_tunable(monkeypatch):
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_URL", "http://127.0.0.1:9877")
    monkeypatch.setenv("DEXTER3_OPENAPI_DEALS_TIMEOUT_SEC", "35")
    c = _client()
    seen: dict[str, float | None] = {}

    def _cap(mode, payload, *, mutating=False, timeout_sec=None):
        if payload.get("include_deal_labels"):
            seen["labels"] = timeout_sec
        if mode == "accounts":
            return _accounts_ok()
        return {"ok": True, "positions": [], "orders": [], "deals": []}

    monkeypatch.setattr(c, "_invoke", _cap)
    c.get_deals(count=50)
    assert seen["labels"] == 35.0


def test_get_deals_label_gap_means_lane_filter_matches_zero(monkeypatch):
    """Documents gap #4 concretely: shadow_runner._lane_realized_today
    filters deals by `"dexter3:fable" in label`; with label always "" that
    filter matches nothing via this transport."""
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [], "orders": [], "deals": [dict(_GOLDEN_DEAL)]}})
    monkeypatch.setattr(c, "_invoke", router)
    [deal] = c.get_deals()
    assert "dexter3:fable" not in (deal.get("label") or deal.get("comment") or "")


# ---------------------------------------------------------------------------
# pending orders (best-effort, gap #5)
# ---------------------------------------------------------------------------

_GOLDEN_ORDER = {
    "orderId": 777,
    "positionId": 0,
    "orderStatus": "ORDER_STATUS_ACCEPTED",
    "limitPrice": 2398.0,
    "stopPrice": 0.0,
    "stopLoss": 2392.0,
    "takeProfit": 2408.0,
    "tradeData": {
        "symbolId": 41,
        "volume": 100,
        "tradeSide": "BUY",
        "label": "dexter3:fable:m5h-v1",
        "comment": "",
        "openTimestamp": 0,
    },
}


def test_get_pending_orders_best_effort_normalization(monkeypatch):
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [], "orders": [dict(_GOLDEN_ORDER)], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    [order] = c.get_pending_orders()
    assert order["orderId"] == 777
    assert order["tradeSide"] == "BUY"
    assert order["volume"] == pytest.approx(1.0)
    assert order["label"] == "dexter3:fable:m5h-v1"


# ---------------------------------------------------------------------------
# trendbars
# ---------------------------------------------------------------------------


def test_get_trendbars_maps_period_and_shape(monkeypatch):
    c = _client()

    def _tb_response(payload):
        assert payload["timeframe"] == "5m"  # mcp_client "m5" -> worker "5m"
        return {
            "ok": True,
            "status": "trendbars_loaded",
            "bars": [
                {"ts_ms": 1, "ts_utc": "2026-07-08T00:00:00Z", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10},
                {"ts_ms": 2, "ts_utc": "2026-07-08T00:05:00Z", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 12},
            ],
        }

    router = _InvokeRouter({"get_trendbars": _tb_response})
    monkeypatch.setattr(c, "_invoke", router)
    bars = c.get_trendbars("XAUUSD", "m5", 2)
    assert len(bars) == 2
    assert set(bars[0].keys()) == {"open", "high", "low", "close", "ts", "volume"}
    assert bars[0]["ts"] < bars[1]["ts"]  # oldest -> newest
    # tick volume passthrough (2026-07-11): the daemon always returned it but
    # this normalization silently dropped it — volume-profile logic needs it
    assert bars[0]["volume"] == pytest.approx(10.0)


def test_get_trendbars_unsupported_period_raises(monkeypatch):
    c = _client()
    with pytest.raises(McpClientError):
        c.get_trendbars("XAUUSD", "m3", 10)


def test_get_trendbars_trims_to_requested_count(monkeypatch):
    c = _client()

    def _tb_response(payload):
        return {
            "ok": True,
            "bars": [
                {"ts_utc": f"2026-07-08T00:0{i}:00Z", "open": i, "high": i, "low": i, "close": i}
                for i in range(5)
            ],
        }

    router = _InvokeRouter({"get_trendbars": _tb_response})
    monkeypatch.setattr(c, "_invoke", router)
    bars = c.get_trendbars("XAUUSD", "m5", 2)
    assert len(bars) == 2


# ---------------------------------------------------------------------------
# spot price
# ---------------------------------------------------------------------------


def test_get_spot_price_uses_latest_capture_event(monkeypatch):
    c = _client()
    router = _InvokeRouter(
        {
            "capture_market": {
                "ok": True,
                "spots": [
                    {"bid": 2400.0, "ask": 2400.2, "event_utc": "2026-07-08T00:00:00Z"},
                    {"bid": 2400.1, "ask": 2400.3, "event_utc": "2026-07-08T00:00:01Z"},
                ],
            }
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    quote = c.get_spot_price("XAUUSD")
    assert quote["bid"] == pytest.approx(2400.1)
    assert quote["ask"] == pytest.approx(2400.3)
    assert quote["quote_source"] == "openapi_capture_market"


def test_get_spot_price_raises_when_no_spots(monkeypatch):
    c = _client()
    router = _InvokeRouter({"capture_market": {"ok": True, "spots": []}})
    monkeypatch.setattr(c, "_invoke", router)
    with pytest.raises(McpClientError):
        c.get_spot_price("XAUUSD")


# ---------------------------------------------------------------------------
# symbol details — explicit gap
# ---------------------------------------------------------------------------


def test_get_symbol_details_raises_documented_not_implemented():
    c = _client()
    with pytest.raises(Dexter3OpenApiNotImplementedError):
        c.get_symbol_details("XAUUSD")


def test_symbol_details_gap_is_still_caught_by_existing_except_clauses():
    """dexter3/executor.py's execute_entry catches (McpClientError,
    McpZombieError) around get_symbol_details — confirm our NotImplementedError
    would still be caught there instead of crashing the caller."""
    c = _client()
    try:
        c.get_symbol_details("XAUUSD")
        raise AssertionError("expected to raise")
    except (McpClientError,) as exc:
        assert isinstance(exc, NotImplementedError)


# ---------------------------------------------------------------------------
# mutating: place_market_order
# ---------------------------------------------------------------------------


def test_place_market_order_translates_pips_and_volume(monkeypatch):
    c = _client()
    seen_execute_payload = {}

    def _execute(payload):
        seen_execute_payload.update(payload)
        return {"ok": True, "status": "filled", "order_id": 1, "position_id": 2, "deal_id": 3, "message": "ctrader order_filled"}

    router = _InvokeRouter(
        {
            "capture_market": {"ok": True, "spots": [{"bid": 2400.0, "ask": 2400.2, "event_utc": "t"}]},
            "execute": _execute,
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    result = c.place_market_order(
        "XAUUSD", "buy", 1.0, stop_loss_pips=500, take_profit_pips=1000, label="dexter3:fable:m5h-v1", comment="c"
    )
    assert result["ok"] is True
    assert seen_execute_payload["direction"] == "long"
    assert seen_execute_payload["fixed_volume"] == 100  # 1.0 units -> 100 raw
    # buy: entry=ask=2400.2; sl 500 pips * 0.01 = 5.0 below; tp 1000 pips * 0.01 = 10.0 above
    assert seen_execute_payload["entry"] == pytest.approx(2400.2)
    assert seen_execute_payload["stop_loss"] == pytest.approx(2395.2)
    assert seen_execute_payload["take_profit"] == pytest.approx(2410.2)
    # execute call must be flagged mutating
    execute_calls = [call for call in router.calls if call[0] == "execute"]
    assert execute_calls[0][2] is True


def test_place_market_order_sell_side_geometry(monkeypatch):
    c = _client()
    seen = {}

    def _execute(payload):
        seen.update(payload)
        return {"ok": True, "status": "filled", "order_id": 1, "position_id": 2, "deal_id": 3}

    router = _InvokeRouter(
        {
            "capture_market": {"ok": True, "spots": [{"bid": 2400.0, "ask": 2400.2, "event_utc": "t"}]},
            "execute": _execute,
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    c.place_market_order("XAUUSD", "sell", 1.0, stop_loss_pips=500, take_profit_pips=1000, label="l")
    assert seen["direction"] == "short"
    # sell: entry=bid=2400.0; sl above, tp below
    assert seen["entry"] == pytest.approx(2400.0)
    assert seen["stop_loss"] == pytest.approx(2405.0)
    assert seen["take_profit"] == pytest.approx(2390.0)


def test_place_market_order_unknown_symbol_pip_size_raises(monkeypatch):
    c = _client()
    router = _InvokeRouter({})
    monkeypatch.setattr(c, "_invoke", router)
    with pytest.raises(McpClientError):
        c.place_market_order("EURUSD", "buy", 1.0, 100, 200, "label")


def test_place_market_order_invalid_side_raises(monkeypatch):
    c = _client()
    router = _InvokeRouter({})
    monkeypatch.setattr(c, "_invoke", router)
    with pytest.raises(McpClientError):
        c.place_market_order("XAUUSD", "hold", 1.0, 100, 200, "label")


# ---------------------------------------------------------------------------
# mutating: amend_position
# ---------------------------------------------------------------------------


def test_amend_position_only_sends_present_legs(monkeypatch):
    c = _client()
    seen = {}

    def _amend(payload):
        seen.update(payload)
        return {"ok": True, "status": "amended", "position_id": 1}

    router = _InvokeRouter({"amend_position_sltp": _amend})
    monkeypatch.setattr(c, "_invoke", router)
    c.amend_position(1, stop_loss=2390.0, take_profit=None)
    assert seen["stop_loss"] == pytest.approx(2390.0)
    assert seen["take_profit"] == 0.0  # worker treats <=0 as "leave alone"
    amend_calls = [call for call in router.calls if call[0] == "amend_position_sltp"]
    assert amend_calls[0][2] is True  # mutating


# ---------------------------------------------------------------------------
# mutating: close_position
# ---------------------------------------------------------------------------


def test_close_position_resolves_raw_volume_then_closes(monkeypatch):
    c = _client()
    seen = {}

    def _close(payload):
        seen.update(payload)
        return {"ok": True, "status": "closed", "position_id": 62664990, "order_id": 9, "deal_id": 10}

    router = _InvokeRouter(
        {
            "reconcile": {"ok": True, "positions": [dict(_GOLDEN_POSITION)], "orders": [], "deals": []},
            "close": _close,
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    result = c.close_position(62664990)
    assert result["ok"] is True
    assert seen["volume"] == 100  # RAW volume (not oz-converted) — matches close-mode's own scale
    assert seen["position_id"] == 62664990


def test_close_position_not_found_raises(monkeypatch):
    c = _client()
    router = _InvokeRouter({"reconcile": {"ok": True, "positions": [], "orders": [], "deals": []}})
    monkeypatch.setattr(c, "_invoke", router)
    with pytest.raises(McpClientError):
        c.close_position(999)


# ---------------------------------------------------------------------------
# call() dispatch parity
# ---------------------------------------------------------------------------


def test_call_dispatches_known_tool(monkeypatch):
    c = _client()
    router = _InvokeRouter({"health": {"ok": True, "balance": 0, "money_digits": 2, "account_id": DEFAULT_ACCOUNT_ID_PIN}})
    monkeypatch.setattr(c, "_invoke", router)
    result = c.call("get_balance")
    assert result["traderId"] == DEFAULT_ACCOUNT_ID_PIN


def test_call_raises_on_unknown_tool():
    c = _client()
    with pytest.raises(McpClientError):
        c.call("show_notification", {"caption": "x"})


# ---------------------------------------------------------------------------
# transport-failure classification (retry / McpMutationUncertain semantics)
# ---------------------------------------------------------------------------


def test_read_transport_failure_retries_once_then_succeeds(monkeypatch):
    c = _client()
    attempts = {"n": 0}

    def _run_once(mode, payload, timeout_sec):
        attempts["n"] += 1
        if mode == "accounts":
            return _accounts_ok()
        if attempts["n"] <= 2:  # first call for "reconcile" transport-fails once
            return {"ok": False, "status": "worker_error", "message": "boom"}
        return {"ok": True, "positions": [], "orders": [], "deals": []}

    monkeypatch.setattr(c, "_run_worker_once", _run_once)
    monkeypatch.setattr(c, "retry_backoff_sec", 0.0)
    result = c.get_positions()
    assert result == []


def test_read_transport_failure_raises_after_second_attempt(monkeypatch):
    c = _client()

    def _run_once(mode, payload, timeout_sec):
        if mode == "accounts":
            return _accounts_ok()
        return {"ok": False, "status": "worker_error", "message": "boom"}

    monkeypatch.setattr(c, "_run_worker_once", _run_once)
    monkeypatch.setattr(c, "retry_backoff_sec", 0.0)
    with pytest.raises(Dexter3OpenApiTransportError):
        c.get_positions()


def test_mutating_transport_failure_raises_mutation_uncertain_no_retry(monkeypatch):
    c = _client()
    attempts = {"n": 0}

    def _run_once(mode, payload, timeout_sec):
        if mode == "accounts":
            return _accounts_ok()
        if mode == "reconcile":
            return {"ok": True, "positions": [dict(_GOLDEN_POSITION)], "orders": [], "deals": []}
        attempts["n"] += 1
        return {"ok": False, "status": "timeout", "message": "no response"}

    monkeypatch.setattr(c, "_run_worker_once", _run_once)
    with pytest.raises(McpMutationUncertain):
        c.close_position(62664990)
    assert attempts["n"] == 1  # single attempt only — never blind-retry a mutation


def test_tool_level_failure_raises_plain_error_without_retry(monkeypatch):
    c = _client()
    attempts = {"n": 0}

    def _run_once(mode, payload, timeout_sec):
        if mode == "accounts":
            return _accounts_ok()
        attempts["n"] += 1
        return {"ok": False, "status": "symbol_not_found", "message": "symbol not found: FOO"}

    monkeypatch.setattr(c, "_run_worker_once", _run_once)
    with pytest.raises(McpClientError):
        c.get_trendbars("XAUUSD", "m5", 10)
    assert attempts["n"] == 1  # tool-level (definitive) answers are never retried


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_factory_default_returns_local_mcp_client(monkeypatch):
    monkeypatch.delenv("DEXTER3_TRANSPORT", raising=False)
    client = make_client()
    assert isinstance(client, Dexter3McpClient)


def test_factory_explicit_local_mcp(monkeypatch):
    monkeypatch.setenv("DEXTER3_TRANSPORT", "local_mcp")
    client = make_client()
    assert isinstance(client, Dexter3McpClient)


def test_factory_openapi_selects_openapi_client(monkeypatch):
    monkeypatch.setenv("DEXTER3_TRANSPORT", "openapi")
    client = make_client()
    assert isinstance(client, Dexter3OpenApiClient)


def test_factory_unknown_transport_raises(monkeypatch):
    monkeypatch.setenv("DEXTER3_TRANSPORT", "carrier_pigeon")
    with pytest.raises(ValueError):
        make_client()


def test_factory_case_insensitive(monkeypatch):
    monkeypatch.setenv("DEXTER3_TRANSPORT", "OpenAPI")
    client = make_client()
    assert isinstance(client, Dexter3OpenApiClient)


# ---------------------------------------------------------------------------
# daemon mode (DEXTER3_OPENAPI_DAEMON_URL) — added 2026-07-10, gap #4 of
# docs/DEXTER3_VM_MIGRATION_DESIGN.md. When the env var is SET, _invoke's
# transport (_run_worker_once) POSTs {"mode","payload"} to the daemon's
# /call endpoint instead of spawning ops/ctrader_execute_once.py; when it
# is UNSET, behavior is byte-identical to before (every test above this
# section runs without the env var and still passes — that IS the
# regression guard for the subprocess path).
# ---------------------------------------------------------------------------

DAEMON_URL = "http://127.0.0.1:9877"


class _FakeHttpResponse:
    def __init__(self, payload: Any, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = int(status_code)
        self.text = text or ("" if payload is None else str(payload))

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _daemon_client(monkeypatch, **kwargs) -> Dexter3OpenApiClient:
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_URL", DAEMON_URL)
    return Dexter3OpenApiClient(**kwargs)


def test_daemon_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_OPENAPI_DAEMON_URL", raising=False)
    c = _client()
    assert c.daemon_url == ""
    assert c.timeout_sec == DEFAULT_TIMEOUT_SEC  # subprocess default unchanged


def test_daemon_mode_uses_lower_default_timeout(monkeypatch):
    c = _daemon_client(monkeypatch)
    assert c.daemon_url == DAEMON_URL
    # 12.0 (raised from 5.0 on 2026-07-11): reconcile's broker tail exceeded 5s
    # on 15% of live OM ticks (om_lane_read_failed -> OM held blind). Still
    # well under the subprocess default (25s) and the 20s poll cadence.
    assert c.timeout_sec == DEFAULT_DAEMON_TIMEOUT_SEC == 12.0
    assert DEFAULT_DAEMON_TIMEOUT_SEC < DEFAULT_TIMEOUT_SEC  # daemon stays the faster path


def test_daemon_mode_timeout_env_tunable(monkeypatch):
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC", "9.5")
    c = _daemon_client(monkeypatch)
    assert c.timeout_sec == pytest.approx(9.5)


def test_daemon_mode_explicit_timeout_argument_wins(monkeypatch):
    monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC", "9.5")
    c = _daemon_client(monkeypatch, timeout_sec=3.0)
    assert c.timeout_sec == pytest.approx(3.0)


def test_daemon_mode_posts_to_daemon_not_subprocess(monkeypatch):
    """DEXTER3_OPENAPI_DAEMON_URL set -> _run_worker_once must hit the URL
    with the {"mode","payload"} envelope and must NEVER touch the subprocess
    path (worker_path/subprocess.run)."""
    seen: dict[str, Any] = {}

    def _fake_post(url, json=None, timeout=None):  # noqa: A002 - requests kwarg name
        seen["url"] = url
        seen["body"] = json
        seen["timeout"] = timeout
        return _FakeHttpResponse({"ok": True, "status": "connected", "balance": 100000, "money_digits": 2, "account_id": DEFAULT_ACCOUNT_ID_PIN, "environment": "demo"})

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)

    def _explode(*_args, **_kwargs):
        raise AssertionError("subprocess path must not run in daemon mode")

    monkeypatch.setattr(openapi_client_module.subprocess, "run", _explode)

    c = _daemon_client(monkeypatch)
    raw = c._run_worker_once("health", {"account_id": DEFAULT_ACCOUNT_ID_PIN}, 5.0)
    assert raw["ok"] is True
    assert seen["url"] == f"{DAEMON_URL}/call"
    assert seen["body"] == {"mode": "health", "payload": {"account_id": DEFAULT_ACCOUNT_ID_PIN}}
    assert seen["timeout"] == pytest.approx(5.0)


def test_daemon_mode_full_read_parses_same_shapes(monkeypatch):
    """End-to-end through _invoke + normalization: a daemon-served reconcile
    response produces the identical get_positions() output the subprocess
    transport produces (same golden position fixture)."""

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        mode = json["mode"]
        if mode == "accounts":
            return _FakeHttpResponse(_accounts_ok())
        if mode == "reconcile":
            return _FakeHttpResponse({"ok": True, "status": "reconciled", "positions": [dict(_GOLDEN_POSITION)], "orders": [], "deals": []})
        raise AssertionError(f"unexpected mode {mode}")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    [pos] = c.get_positions()
    assert pos["positionId"] == 62664990
    assert pos["tradeSide"] == "BUY"
    assert pos["volume"] == pytest.approx(1.0)
    assert pos["label"] == "dexter3:fable:m5h-v1"


def test_daemon_mode_connection_error_maps_to_transport_failure(monkeypatch):
    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    raw = c._run_worker_once("health", {}, 5.0)
    assert raw["ok"] is False
    assert raw["status"] == "worker_error"  # retryable transport bucket


def test_daemon_mode_timeout_maps_to_timeout_status(monkeypatch):
    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    raw = c._run_worker_once("health", {}, 5.0)
    assert raw["status"] == "timeout"


def test_daemon_mode_http_error_status_maps_to_worker_error(monkeypatch):
    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        return _FakeHttpResponse({"ok": False}, status_code=500, text="internal")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    raw = c._run_worker_once("health", {}, 5.0)
    assert raw["status"] == "worker_error"
    assert "500" in raw["message"]


def test_daemon_mode_disconnected_read_retries_then_raises_transport_error(monkeypatch):
    """The daemon's {"ok":false,"status":"disconnected"} means the broker
    never saw the request — reads retry once (like other transport failures)
    then raise Dexter3OpenApiTransportError, NOT a plain tool error."""
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        calls["n"] += 1
        return _FakeHttpResponse({"ok": False, "status": "disconnected", "message": "no live connection"})

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    monkeypatch.setattr(c, "retry_backoff_sec", 0.0)
    with pytest.raises(Dexter3OpenApiTransportError):
        c._invoke("reconcile", {}, mutating=False)
    assert calls["n"] == 2  # one retry, no more


def test_daemon_mode_disconnected_mutation_raises_mutation_uncertain(monkeypatch):
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        calls["n"] += 1
        return _FakeHttpResponse({"ok": False, "status": "disconnected", "message": "no live connection"})

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    with pytest.raises(McpMutationUncertain):
        c._invoke("execute", {"symbol": "XAUUSD"}, mutating=True)
    assert calls["n"] == 1  # never blind-retry a mutation


def test_daemon_mode_tool_level_failure_never_retried(monkeypatch):
    """A definitive broker answer relayed by the daemon (e.g. rejected)
    must raise plain McpClientError after exactly one attempt."""
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        calls["n"] += 1
        return _FakeHttpResponse({"ok": False, "status": "rejected", "message": "order rejected"})

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    with pytest.raises(McpClientError) as excinfo:
        c._invoke("execute", {"symbol": "XAUUSD"}, mutating=True)
    assert not isinstance(excinfo.value, McpMutationUncertain)
    assert calls["n"] == 1


def test_daemon_mode_non_json_body_maps_to_worker_error(monkeypatch):
    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        return _FakeHttpResponse(ValueError("not json"), status_code=200, text="<html>oops</html>")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    raw = c._run_worker_once("health", {}, 5.0)
    assert raw["status"] == "worker_error"
    assert "non-JSON" in raw["message"]


def test_daemon_mode_get_spot_price_uses_spot_quote_mode(monkeypatch):
    """Daemon mode routes get_spot_price through the daemon-only spot_quote
    (live-cache) mode — NOT capture_market — and parses the same shape."""
    seen_modes: list[str] = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        seen_modes.append(json["mode"])
        if json["mode"] == "accounts":
            return _FakeHttpResponse(_accounts_ok())
        if json["mode"] == "spot_quote":
            assert json["payload"]["symbol"] == "XAUUSD"
            return _FakeHttpResponse({
                "ok": True,
                "status": "spot_quote",
                "source": "live_cache",
                "quote_age_sec": 0.12,
                "spots": [{"bid": 4125.83, "ask": 4126.03, "event_utc": "2026-07-10T03:21:32Z"}],
            })
        raise AssertionError(f"unexpected mode {json['mode']}")

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    quote = c.get_spot_price("XAUUSD")
    assert quote["bid"] == pytest.approx(4125.83)
    assert quote["ask"] == pytest.approx(4126.03)
    assert quote["quote_source"] == "openapi_daemon_spot_cache"
    assert quote["quote_age_sec"] == pytest.approx(0.12)
    assert "capture_market" not in seen_modes
    assert seen_modes == ["accounts", "spot_quote"]


def test_daemon_mode_spot_stale_raises_fail_closed(monkeypatch):
    """spot_stale is a definitive tool-level answer: get_spot_price must
    raise McpClientError (HARD VETO upstream), never return a stale price
    and never retry."""
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        if json["mode"] == "accounts":
            return _FakeHttpResponse(_accounts_ok())
        calls["n"] += 1
        return _FakeHttpResponse({
            "ok": False,
            "status": "spot_stale",
            "message": "no fresh XAUUSD quote within max_age=5.0s (newest cached quote is 42.0s old)",
            "age_sec": 42.0,
        })

    monkeypatch.setattr(openapi_client_module.requests, "post", _fake_post)
    c = _daemon_client(monkeypatch)
    with pytest.raises(McpClientError) as excinfo:
        c.get_spot_price("XAUUSD")
    assert "spot_stale" in str(excinfo.value)
    assert calls["n"] == 1  # definitive answer -> no retry


def test_subprocess_mode_get_spot_price_still_uses_capture_market(monkeypatch):
    """Regression guard: with the daemon env var UNSET, get_spot_price's
    capture_market path is byte-identical to before."""
    monkeypatch.delenv("DEXTER3_OPENAPI_DAEMON_URL", raising=False)
    c = _client()
    router = _InvokeRouter(
        {
            "capture_market": {
                "ok": True,
                "spots": [{"bid": 2400.0, "ask": 2400.2, "event_utc": "t"}],
            }
        }
    )
    monkeypatch.setattr(c, "_invoke", router)
    quote = c.get_spot_price("XAUUSD")
    assert quote["quote_source"] == "openapi_capture_market"
    assert [m for m, _p, _mut in router.calls] == ["accounts", "capture_market"]


def test_daemon_mode_against_real_local_http_server(monkeypatch):
    """Full-stack loopback: a REAL daemon HTTP listener (dexter3.openapi_daemon.
    build_server on an ephemeral port, fake dispatcher — no Twisted, no
    broker) served to a REAL Dexter3OpenApiClient in daemon mode. Proves the
    two halves actually speak the same wire protocol, not just that each
    half matches its own mocks."""
    import threading

    from dexter3.openapi_daemon import build_server

    received: list[tuple[str, dict]] = []

    def _dispatch(mode: str, payload: dict) -> dict:
        received.append((mode, payload))
        if mode == "accounts":
            return _accounts_ok()
        if mode == "health":
            return {"ok": True, "status": "connected", "balance": 100000, "money_digits": 2, "account_id": DEFAULT_ACCOUNT_ID_PIN, "environment": "demo"}
        return {"ok": False, "status": "unknown_mode", "message": mode}

    server = build_server(dispatch_call=_dispatch, health_snapshot=lambda: {"connected": True}, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("DEXTER3_OPENAPI_DAEMON_URL", f"http://127.0.0.1:{port}")
        c = Dexter3OpenApiClient()
        bal = c.get_balance()
        assert bal["balance"] == pytest.approx(1000.00)
        assert bal["traderId"] == DEFAULT_ACCOUNT_ID_PIN
        modes = [m for m, _p in received]
        assert modes == ["accounts", "health"]  # pin first, then the real read
        # account pin id was carried in the payload exactly like subprocess mode
        assert received[1][1]["account_id"] == DEFAULT_ACCOUNT_ID_PIN
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

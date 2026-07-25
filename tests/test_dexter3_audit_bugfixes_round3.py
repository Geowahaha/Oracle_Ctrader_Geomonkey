"""Round-3 regression tests for the 2026-07-25 audit bug fixes.

  1) naked-repair market-closed a HEALTHY position when the amend response was
     merely uncertain (McpMutationUncertain subclasses McpClientError);
  2) an amend the broker REFUSED still reported verified=True, because the
     check only proved the old stop was on the correct SIDE of entry;
  3) a spot-read failure inside PnL enrichment vanished with no marker, while
     silently disabling every exit for that tick.
"""
from __future__ import annotations

from typing import Any

import pytest

from dexter3.executor import verify_entry_snapshot


# --- 2) verify_entry_snapshot must check the stop actually MOVED -----------

def _pos(sl: float, tp: float, side: str = "BUY", vol: float = 1.0) -> dict[str, Any]:
    return {"tradeSide": side, "volumeInUnits": vol, "stopLoss": sl, "takeProfit": tp}


def test_legacy_behaviour_unchanged_without_expectations():
    ok, meta = verify_entry_snapshot(_pos(99.0, 102.0), "buy", 100.0, 1.0)
    assert ok is True
    assert meta["geometry_ok"] is True
    assert "sl_expected" not in meta          # opt-in only


def test_refused_amend_no_longer_reports_verified():
    """THE BUG: the broker refused the widen, so the stop is still the OLD
    tight 99.0 — which is on the correct side of entry, so the old check said
    verified=True and the caller logged success and burned a retry."""
    still_old = _pos(99.0, 102.0)
    ok_old_rule, _ = verify_entry_snapshot(still_old, "buy", 100.0, 1.0)
    assert ok_old_rule is True, "old rule accepted it (this is the defect)"

    ok, meta = verify_entry_snapshot(still_old, "buy", 100.0, 1.0, expect_sl=95.0)
    assert ok is False
    assert meta["sl_matches"] is False
    assert meta["sl_expected"] == 95.0
    assert meta["stop_loss"] == 99.0


def test_applied_amend_verifies():
    ok, meta = verify_entry_snapshot(_pos(95.0, 102.0), "buy", 100.0, 1.0, expect_sl=95.0)
    assert ok is True and meta["sl_matches"] is True


def test_price_tolerance_absorbs_broker_rounding():
    ok, _ = verify_entry_snapshot(_pos(95.02, 102.0), "buy", 100.0, 1.0, expect_sl=95.0)
    assert ok is True
    ok2, _ = verify_entry_snapshot(_pos(95.5, 102.0), "buy", 100.0, 1.0, expect_sl=95.0)
    assert ok2 is False


def test_tp_expectation_checked_independently():
    ok, meta = verify_entry_snapshot(_pos(95.0, 102.0), "buy", 100.0, 1.0, expect_tp=110.0)
    assert ok is False and meta["tp_matches"] is False


def test_sell_side_expectations():
    ok, _ = verify_entry_snapshot(_pos(105.0, 98.0, side="SELL"), "sell", 100.0, 1.0, expect_sl=105.0)
    assert ok is True
    bad, _ = verify_entry_snapshot(_pos(101.0, 98.0, side="SELL"), "sell", 100.0, 1.0, expect_sl=105.0)
    assert bad is False


def test_missing_position_still_short_circuits():
    ok, meta = verify_entry_snapshot(None, "buy", 100.0, 1.0, expect_sl=95.0)
    assert ok is False and meta["reason"] == "position_not_found"


# --- 1) naked repair must not close a healthy position on an UNCERTAIN amend

class _Client:
    """Amend raises McpMutationUncertain but the broker DID apply it."""

    def __init__(self, applied_sl: float, applied_tp: float) -> None:
        self.applied_sl, self.applied_tp = applied_sl, applied_tp
        self.closed: list[int] = []

    def amend_position(self, position_id: int, stop_loss: float, take_profit: float):
        from dexter3.mcp_client import McpMutationUncertain
        raise McpMutationUncertain("response timed out after send")

    def get_positions(self):
        return [{
            "positionId": 7001, "tradeSide": "BUY", "volumeInUnits": 1.0,
            "stopLoss": self.applied_sl, "takeProfit": self.applied_tp,
            "entryPrice": 100.0, "symbol": "XAUUSD",
            "label": "dexter3:test:canary",
        }]

    def close_position(self, position_id: int):
        self.closed.append(int(position_id))
        return {"ok": True}


def _executor_with(client: Any):
    from dexter3 import executor as ex
    obj = ex.Dexter3Executor.__new__(ex.Dexter3Executor)   # bypass __init__/IO
    obj.client = client
    obj._journal = lambda *a, **k: None                     # type: ignore[method-assign]
    return obj


def test_uncertain_amend_that_applied_is_not_closed(monkeypatch):
    monkeypatch.setattr("dexter3.executor.time.sleep", lambda *_: None)
    client = _Client(applied_sl=95.0, applied_tp=110.0)
    out = _executor_with(client)._repair_naked_position(
        "XAUUSD", 7001, "buy", 100.0, 95.0, 110.0, 1.0
    )
    assert out["verified"] is True
    assert out["action"] == "amended"
    assert client.closed == [], "a healthy position must NOT be closed"
    assert "amend_uncertain" in out["verification"]


def test_uncertain_amend_that_did_not_apply_still_closes(monkeypatch):
    """The safety property must survive: if the stop really is absent/wrong,
    the position is still closed rather than left naked."""
    monkeypatch.setattr("dexter3.executor.time.sleep", lambda *_: None)
    client = _Client(applied_sl=0.0, applied_tp=0.0)       # broker applied nothing
    out = _executor_with(client)._repair_naked_position(
        "XAUUSD", 7001, "buy", 100.0, 95.0, 110.0, 1.0
    )
    assert out["verified"] is False
    assert out["action"] == "closed"
    assert client.closed == [7001]


# --- 3) spot failure must leave a marker -----------------------------------

def test_spot_failure_stamps_marker_on_position():
    from dexter3 import openapi_client as oc

    c = oc.Dexter3OpenApiClient.__new__(oc.Dexter3OpenApiClient)

    def _boom(_symbol):
        raise RuntimeError("daemon spot_quote unavailable")

    c.get_spot_price = _boom  # type: ignore[method-assign]
    positions = [{"symbol": "XAUUSD", "tradeSide": "BUY", "entryPrice": 100.0,
                  "volumeInUnits": 1.0, "stopLoss": 95.0, "takeProfit": 110.0}]
    c._enrich_positions_with_live_pnl(positions)   # mutates in place, returns None
    pos = positions[0]
    assert pos.get("pnl_source") == "spot_unavailable"
    assert "daemon spot_quote unavailable" in pos.get("pnl_spot_error", "")
    assert "netProfit" not in pos, "must NOT fabricate a PnL"

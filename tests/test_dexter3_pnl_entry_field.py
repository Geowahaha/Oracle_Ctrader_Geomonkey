"""2026-07-15 P0 regression: the OM's live PnL was found flip-flopping
between a correct entry-based number and a number matching a
take-profit-based miscalculation exactly to the cent, on positions with a
FRESH spot (pnl_spot_age_sec 0.05-0.7s — NOT a staleness bug). Incident
arithmetic (position 652652362, SELL, entry=4030.39, take_profit=4017.22):
a correct reading with ask=4035.7 gave -5.32 (matches broker truth); a
TP-based miscalculation with ask=4031.09 gave EXACTLY -13.87
(= take_profit - ask = 4017.22 - 4031.09), i.e. some computation used
``take_profit`` where ``entry`` belongs.

This module:
  (a) proves, via a REAL protobuf ``ProtoOAPosition`` message (confirmed
      against the installed descriptor — ``price``/``stopLoss``/
      ``takeProfit`` are distinct top-level fields, ``price`` is the
      position's own open/entry price), that
      ``dexter3.openapi_daemon._normalize_position`` (site 1) maps
      ``entry_price`` from ``price``, never from ``takeProfit``;
  (b) proves the full client-side chain (``_normalize_position_for_dexter3``
      + ``_enrich_positions_with_live_pnl``, site 2) computes netProfit
      against that same entry, for both BUY and SELL, with entry/TP
      distances large enough that a TP-based formula would be wildly
      (>$5) different — never a rounding-tolerance-sized discrepancy;
  (c) proves the flip-flop can't survive a fresh enrichment call: a
      position arriving with a PRE-EXISTING TP-based netProfit (as if some
      other/legacy source had stamped it first) is unconditionally
      overwritten by the correct entry-based value every time enrichment
      runs, so "whichever site populated netProfit last" can never win
      with a wrong number on the OpenAPI transport;
  (d) exercises the new invariant guards added at both sites (entry-vs-
      target field-confusion self-consistency, and the client's
      implied-entry back-solve from a computed pnl), confirming they fire
      on a synthetic TP-based value and stay silent on healthy ones.
"""
from __future__ import annotations

import logging

import pytest

from dexter3 import openapi_daemon
from dexter3.openapi_client import (
    DEFAULT_ENTRY_TARGET_SANITY_PRICE,
    DEFAULT_PNL_ENTRY_SANITY_PRICE,
    ENTRY_TARGET_SANITY_ENV_VAR,
    PNL_ENTRY_SANITY_ENV_VAR,
    Dexter3OpenApiClient,
    _USD_POINT_VALUE_PER_UNIT,
    _entry_matches_target as _client_entry_matches_target,
    _pnl_entry_sanity_diff,
    _pnl_implied_entry,
    _warn_if_entry_matches_targets as _client_warn_if_entry_matches_targets,
    _warn_if_pnl_implies_wrong_entry,
    resolve_entry_target_sanity_price,
    resolve_pnl_entry_sanity_price,
)

# Real incident numbers (2026-07-15 forensics, position 652652362, SELL).
_INCIDENT_ENTRY = 4030.39
_INCIDENT_TAKE_PROFIT = 4017.22
_INCIDENT_ASK_CORRECT = 4035.7
_INCIDENT_ASK_WRONG = 4031.09


def test_incident_arithmetic_is_exactly_tp_minus_ask():
    """Locks in the forensic math that motivated this whole module: the
    -13.87 reading is (take_profit - ask), not any staleness artifact."""
    correct = round(_INCIDENT_ENTRY - _INCIDENT_ASK_CORRECT, 2)
    wrong = round(_INCIDENT_TAKE_PROFIT - _INCIDENT_ASK_WRONG, 2)
    assert correct == pytest.approx(-5.31, abs=0.01)
    assert wrong == pytest.approx(-13.87, abs=0.005)


# ---------------------------------------------------------------------------
# (a) daemon-side normalize: real protobuf ProtoOAPosition -> entry_price
# ---------------------------------------------------------------------------


def _build_proto_position(side_enum, position_id: int, entry: float, sl: float, tp: float, volume_raw: int = 100):
    model = openapi_daemon.model
    return model.ProtoOAPosition(
        positionId=position_id,
        tradeData=model.ProtoOATradeData(
            symbolId=41,
            volume=volume_raw,
            tradeSide=side_enum,
            openTimestamp=1783623090337,
            label="dexter3:fable:test",
            comment="c",
        ),
        positionStatus=model.ProtoOAPositionStatus.POSITION_STATUS_OPEN,
        swap=0,
        price=entry,
        stopLoss=sl,
        takeProfit=tp,
        utcLastUpdateTimestamp=0,
        commission=0,
        moneyDigits=2,
    )


@pytest.mark.parametrize(
    "side_name, entry, sl, tp",
    [
        ("SELL", _INCIDENT_ENTRY, 4045.00, _INCIDENT_TAKE_PROFIT),
        ("BUY", 4057.01, 4044.00, 4069.29),
    ],
)
def test_daemon_normalize_position_maps_entry_from_price_not_take_profit(side_name, entry, sl, tp):
    """Site 1 (dexter3/openapi_daemon.py::_normalize_position), driven by a
    REAL protobuf message so the field mapping is checked against the
    installed descriptor, not just against this repo's own dict literals."""
    side_enum = openapi_daemon.model.ProtoOATradeSide.BUY if side_name == "BUY" else openapi_daemon.model.ProtoOATradeSide.SELL
    raw_position = _build_proto_position(side_enum, 555, entry, sl, tp)
    normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert normalized["entry_price"] == pytest.approx(entry)
    assert normalized["stop_loss"] == pytest.approx(sl)
    assert normalized["take_profit"] == pytest.approx(tp)
    # The three fields must be genuinely distinct in this fixture — a test
    # that accidentally reused the same number for entry and take_profit
    # would pass even with the bug this module guards against.
    assert normalized["entry_price"] != pytest.approx(normalized["take_profit"])
    assert normalized["entry_price"] != pytest.approx(normalized["stop_loss"])


# ---------------------------------------------------------------------------
# (b) full chain: daemon normalize -> client normalize -> client enrich
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "side_name, entry, sl, tp, spot_bid, spot_ask",
    [
        ("SELL", _INCIDENT_ENTRY, 4045.00, _INCIDENT_TAKE_PROFIT, _INCIDENT_ASK_CORRECT - 0.05, _INCIDENT_ASK_CORRECT),
        ("BUY", 4057.01, 4044.00, 4069.29, 4068.70, 4068.80),
    ],
)
def test_full_chain_computes_netprofit_from_entry_not_take_profit(
    side_name, entry, sl, tp, spot_bid, spot_ask, monkeypatch
):
    """(b) The full get_positions() pipeline — daemon-shaped normalize dict
    -> client's _normalize_position_for_dexter3 -> _enrich_positions_with_
    live_pnl — must compute netProfit against ENTRY. Chosen entry/TP
    distances (13-15 price units) make a TP-based formula differ by more
    than $5 on a 1oz XAUUSD position — never a rounding-sized gap."""
    side_enum = openapi_daemon.model.ProtoOATradeSide.BUY if side_name == "BUY" else openapi_daemon.model.ProtoOATradeSide.SELL
    raw_position = _build_proto_position(side_enum, 777, entry, sl, tp)
    normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})

    c = Dexter3OpenApiClient()
    dexter3_pos = Dexter3OpenApiClient._normalize_position_for_dexter3(normalized)
    assert dexter3_pos["entryPrice"] == pytest.approx(entry)
    assert dexter3_pos["takeProfit"] == pytest.approx(tp)

    monkeypatch.setattr(c, "get_spot_price", lambda sym: {"bid": spot_bid, "ask": spot_ask, "symbol": sym})
    c._enrich_positions_with_live_pnl([dexter3_pos])

    point_value = _USD_POINT_VALUE_PER_UNIT["XAUUSD"]
    vol_units = dexter3_pos["volume"]
    if side_name == "BUY":
        expected_net = (spot_bid - entry) * vol_units * point_value
        wrong_tp_net = (spot_bid - tp) * vol_units * point_value
    else:
        expected_net = (entry - spot_ask) * vol_units * point_value
        wrong_tp_net = (tp - spot_ask) * vol_units * point_value

    assert dexter3_pos["pnl_source"] == "computed_from_live_spot"
    assert dexter3_pos["netProfit"] == pytest.approx(round(expected_net, 4), abs=1e-3)
    assert abs(wrong_tp_net - expected_net) > 5.0  # unmistakably different, not a rounding gap
    assert dexter3_pos["netProfit"] != pytest.approx(round(wrong_tp_net, 4), abs=1e-3)


# ---------------------------------------------------------------------------
# (c) flip-flop regression: a pre-existing TP-based netProfit never survives
# a fresh enrichment call on the OpenAPI transport
# ---------------------------------------------------------------------------


def test_enrichment_overwrites_a_preexisting_tp_based_netprofit():
    """(c) Simulates 'whichever site populated netProfit that tick wins':
    a position ARRIVES with netProfit already stamped by a TP-based
    formula (as if a different/legacy source had computed it first).
    _enrich_positions_with_live_pnl must unconditionally recompute and
    overwrite it from the position's own entry_price every time it runs —
    proving the flip-flop cannot survive a live OpenAPI-transport tick."""
    c = Dexter3OpenApiClient()
    entry, tp, ask = _INCIDENT_ENTRY, _INCIDENT_TAKE_PROFIT, _INCIDENT_ASK_WRONG
    wrong_preexisting_net = round(tp - ask, 4)  # the incident's -13.87

    pos = {
        "positionId": 652652362,
        "tradeSide": "SELL",
        "entryPrice": entry,
        "takeProfit": tp,
        "stopLoss": 4045.0,
        "volume": 1.0,
        "symbol": "XAUUSD",
        "swap": 0.0,
        "commission": 0.0,
        # Pre-existing WRONG value, as if a different site stamped it first.
        "netProfit": wrong_preexisting_net,
        "pnl_source": "broker_or_legacy",
    }
    assert pos["netProfit"] == pytest.approx(-13.87, abs=0.01)

    import types

    c.get_spot_price = types.MethodType(lambda self, sym: {"bid": ask - 0.1, "ask": ask}, c)
    c._enrich_positions_with_live_pnl([pos])

    expected_net = round((entry - ask) * 1.0 * 1.0, 4)
    assert pos["netProfit"] == pytest.approx(expected_net, abs=1e-3)
    assert pos["netProfit"] != pytest.approx(wrong_preexisting_net, abs=0.5)
    assert pos["pnl_source"] == "computed_from_live_spot"


# ---------------------------------------------------------------------------
# (d) invariant guards: fire on synthetic TP-based values, silent on healthy
# ---------------------------------------------------------------------------


def test_pnl_implied_entry_recovers_take_profit_from_a_tp_based_gross():
    """Feed ``_pnl_implied_entry`` a gross pnl computed the WRONG way (using
    take_profit instead of entry) and confirm it back-solves an implied
    entry near take_profit, not near the position's real entry — proving
    the inversion math the sanity guard relies on is correct."""
    entry, tp, ask = _INCIDENT_ENTRY, _INCIDENT_TAKE_PROFIT, _INCIDENT_ASK_WRONG
    tp_based_gross = (tp - ask) * 1.0 * 1.0  # the SELL formula with tp swapped in for entry
    implied = _pnl_implied_entry("SELL", tp_based_gross, bid=ask - 0.1, ask=ask, vol=1.0, point_value=1.0)
    assert implied == pytest.approx(tp, abs=1e-6)
    assert implied != pytest.approx(entry, abs=0.5)

    diff = _pnl_entry_sanity_diff("SELL", entry, tp_based_gross, bid=ask - 0.1, ask=ask, vol=1.0, point_value=1.0)
    assert diff == pytest.approx(abs(tp - entry), abs=1e-6)
    assert diff > resolve_pnl_entry_sanity_price()


def test_pnl_implied_entry_returns_none_when_uninvertible():
    assert _pnl_implied_entry("BUY", 5.0, bid=10.0, ask=10.1, vol=0.0, point_value=1.0) is None
    assert _pnl_implied_entry("BUY", 5.0, bid=10.0, ask=10.1, vol=1.0, point_value=0.0) is None
    assert _pnl_implied_entry("HOLD", 5.0, bid=10.0, ask=10.1, vol=1.0, point_value=1.0) is None
    assert _pnl_entry_sanity_diff("BUY", 10.0, 5.0, bid=10.0, ask=10.1, vol=0.0, point_value=1.0) is None


def test_warn_if_pnl_implies_wrong_entry_fires_on_synthetic_tp_value(caplog):
    """(d) The client-side guard, wired to _enrich_positions_with_live_pnl,
    must log a warning when a computed gross pnl implies an entry that
    disagrees with the position's stated entryPrice — the exact signature
    of the reported bug."""
    entry, tp, ask = _INCIDENT_ENTRY, _INCIDENT_TAKE_PROFIT, _INCIDENT_ASK_WRONG
    tp_based_gross = (tp - ask) * 1.0 * 1.0
    with caplog.at_level(logging.WARNING, logger="dexter3.openapi_client"):
        _warn_if_pnl_implies_wrong_entry(
            652652362, "SELL", entry, tp_based_gross, bid=ask - 0.1, ask=ask, vol=1.0, point_value=1.0
        )
    assert any("implies entry" in rec.message for rec in caplog.records)
    assert any("652652362" in rec.message for rec in caplog.records)


def test_warn_if_pnl_implies_wrong_entry_silent_on_healthy_position(caplog):
    """Negative control: a correctly-computed gross (using the real entry)
    must never trip the guard."""
    entry, ask, bid = _INCIDENT_ENTRY, _INCIDENT_ASK_CORRECT, _INCIDENT_ASK_CORRECT - 0.1
    correct_gross = (entry - ask) * 1.0 * 1.0
    with caplog.at_level(logging.WARNING, logger="dexter3.openapi_client"):
        _warn_if_pnl_implies_wrong_entry(1, "SELL", entry, correct_gross, bid=bid, ask=ask, vol=1.0, point_value=1.0)
    assert caplog.records == []


def test_warn_if_pnl_implies_wrong_entry_never_raises_on_bad_inputs():
    # vol=0 -> _pnl_implied_entry returns None -> early return, no crash.
    _warn_if_pnl_implies_wrong_entry(1, "SELL", 100.0, 5.0, bid=99.0, ask=100.0, vol=0.0, point_value=1.0)


@pytest.mark.parametrize("entry_matches_fn", [_client_entry_matches_target, openapi_daemon._entry_matches_target])
def test_entry_matches_target_detects_coincidence(entry_matches_fn):
    """(d) The self-consistency half of the guard: entry_price coinciding
    with take_profit/stop_loss within tolerance is flagged; a real,
    separated entry/target pair is not."""
    assert entry_matches_fn(_INCIDENT_TAKE_PROFIT, _INCIDENT_TAKE_PROFIT, 0.01) is True
    assert entry_matches_fn(_INCIDENT_ENTRY, _INCIDENT_TAKE_PROFIT, 0.01) is False
    assert entry_matches_fn(0.0, _INCIDENT_TAKE_PROFIT, 0.01) is False  # no fabricated entry
    assert entry_matches_fn(_INCIDENT_ENTRY, 0.0, 0.01) is False  # no fabricated target


def test_client_warn_if_entry_matches_targets_fires_and_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="dexter3.openapi_client"):
        _client_warn_if_entry_matches_targets(999, _INCIDENT_TAKE_PROFIT, 4045.0, _INCIDENT_TAKE_PROFIT)
    assert any("coincides with takeProfit" in rec.message for rec in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dexter3.openapi_client"):
        _client_warn_if_entry_matches_targets(999, _INCIDENT_ENTRY, 4045.0, _INCIDENT_TAKE_PROFIT)
    assert caplog.records == []


def test_daemon_warn_if_entry_matches_targets_fires_and_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="dexter3_openapi_daemon"):
        openapi_daemon._warn_if_entry_matches_targets(999, _INCIDENT_TAKE_PROFIT, 4045.0, _INCIDENT_TAKE_PROFIT)
    assert any("coincides with take_profit" in rec.message for rec in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dexter3_openapi_daemon"):
        openapi_daemon._warn_if_entry_matches_targets(999, _INCIDENT_ENTRY, 4045.0, _INCIDENT_TAKE_PROFIT)
    assert caplog.records == []


def test_normalize_position_for_dexter3_wires_the_entry_target_guard(caplog):
    """The guard must actually be reachable from the normal normalization
    path, not just callable in isolation."""
    daemon_shaped = {
        "position_id": 42,
        "entry_price": _INCIDENT_TAKE_PROFIT,
        "stop_loss": 4045.0,
        "take_profit": _INCIDENT_TAKE_PROFIT,
        "direction": "short",
        "volume": 100,
        "symbol": "XAUUSD",
    }
    with caplog.at_level(logging.WARNING, logger="dexter3.openapi_client"):
        Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_shaped)
    assert any("coincides with takeProfit" in rec.message for rec in caplog.records)


def test_normalize_position_wires_the_entry_target_guard(caplog):
    raw_position = _build_proto_position(
        openapi_daemon.model.ProtoOATradeSide.SELL, 43, _INCIDENT_TAKE_PROFIT, 4045.0, _INCIDENT_TAKE_PROFIT
    )
    with caplog.at_level(logging.WARNING, logger="dexter3_openapi_daemon"):
        openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert any("coincides with take_profit" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# env-tunability
# ---------------------------------------------------------------------------


def test_pnl_entry_sanity_price_env_override_and_fallback(monkeypatch):
    monkeypatch.delenv(PNL_ENTRY_SANITY_ENV_VAR, raising=False)
    assert resolve_pnl_entry_sanity_price() == DEFAULT_PNL_ENTRY_SANITY_PRICE
    monkeypatch.setenv(PNL_ENTRY_SANITY_ENV_VAR, "0.25")
    assert resolve_pnl_entry_sanity_price() == pytest.approx(0.25)
    monkeypatch.setenv(PNL_ENTRY_SANITY_ENV_VAR, "not-a-number")
    assert resolve_pnl_entry_sanity_price() == DEFAULT_PNL_ENTRY_SANITY_PRICE
    monkeypatch.setenv(PNL_ENTRY_SANITY_ENV_VAR, "-1")
    assert resolve_pnl_entry_sanity_price() == DEFAULT_PNL_ENTRY_SANITY_PRICE


def test_entry_target_sanity_price_env_override_and_fallback(monkeypatch):
    monkeypatch.delenv(ENTRY_TARGET_SANITY_ENV_VAR, raising=False)
    assert resolve_entry_target_sanity_price() == DEFAULT_ENTRY_TARGET_SANITY_PRICE
    monkeypatch.setenv(ENTRY_TARGET_SANITY_ENV_VAR, "0.02")
    assert resolve_entry_target_sanity_price() == pytest.approx(0.02)
    monkeypatch.setenv(ENTRY_TARGET_SANITY_ENV_VAR, "nope")
    assert resolve_entry_target_sanity_price() == DEFAULT_ENTRY_TARGET_SANITY_PRICE


def test_daemon_entry_target_sanity_price_env_override_and_fallback(monkeypatch):
    monkeypatch.delenv(openapi_daemon.ENTRY_TARGET_SANITY_ENV_VAR, raising=False)
    assert openapi_daemon.resolve_entry_target_sanity_price() == openapi_daemon.DEFAULT_ENTRY_TARGET_SANITY_PRICE
    monkeypatch.setenv(openapi_daemon.ENTRY_TARGET_SANITY_ENV_VAR, "0.03")
    assert openapi_daemon.resolve_entry_target_sanity_price() == pytest.approx(0.03)
    monkeypatch.setenv(openapi_daemon.ENTRY_TARGET_SANITY_ENV_VAR, "nope")
    assert openapi_daemon.resolve_entry_target_sanity_price() == openapi_daemon.DEFAULT_ENTRY_TARGET_SANITY_PRICE

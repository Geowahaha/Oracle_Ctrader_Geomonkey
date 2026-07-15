"""2026-07-15 P0 live-money-scaling regression.

Root cause: ``dexter3/openapi_daemon.py::_normalize_position`` read
``swap``/``commission``/``usedMargin`` straight off the raw ProtoOAPosition
WITHOUT dividing by ``scale = 10 ** money_digits`` — while its sibling
``_normalize_deal`` (a few lines below) has always divided every money field
by that same scale. ``money_digits`` was already being captured on the
position dict, just never applied to those three fields.

Proven live on open SHORT position 653082985 (XAUUSD, moneyDigits=2):
raw commission=-12 -> unscaled path fed -12.0 straight into netProfit
instead of -0.12, so with true gross=-5.54 the OM read netProfit=-17.54 —
a ~$12 poison that propagates into basket_live.aggregate_lane's live_r/
peak_r, shadow_runner's daily-governor floating_by_symbol early-loss-stop
math, and executor.close_lane_position's win/loss sign for the empirical
learner. The realized/deals path (get_deals / _normalize_deal) was never
affected — it always divided.

This module:
  (a) pins the exact daemon-side scaling fix for commission/swap/used_margin
      at both moneyDigits=2 and moneyDigits=3;
  (b) proves the full daemon -> client -> _enrich_positions_with_live_pnl
      chain now produces the correct netProfit for the live incident's own
      numbers (entry 4044.71, commission raw -12, digits 2, gross -5.54),
      pinning netProfit == -5.66 and refuting the old -17.54 bug value;
  (c) locks a parity regression against ``_normalize_deal`` so the two
      sibling normalizers can never again silently desync on scaling;
  (d) confirms money_digits/moneyDigits is now carried into the client's
      dexter3-shaped position dict for downstream observability.
"""
from __future__ import annotations

import pytest

from dexter3 import openapi_daemon
from dexter3.openapi_client import Dexter3OpenApiClient, _USD_POINT_VALUE_PER_UNIT

model = openapi_daemon.model


def _build_position(
    *,
    position_id: int,
    side_enum,
    entry: float,
    sl: float,
    tp: float,
    commission_raw: int,
    swap_raw: int,
    used_margin_raw: int,
    money_digits: int,
    volume_raw: int = 100,
):
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
        swap=swap_raw,
        price=entry,
        stopLoss=sl,
        takeProfit=tp,
        utcLastUpdateTimestamp=0,
        commission=commission_raw,
        usedMargin=used_margin_raw,
        moneyDigits=money_digits,
    )


def _build_deal(*, commission_raw: int, close_swap_raw: int, close_commission_raw: int,
                 gross_profit_raw: int, money_digits: int, balance_raw: int = 100000):
    return model.ProtoOADeal(
        dealId=1,
        orderId=1,
        positionId=1,
        volume=100,
        filledVolume=100,
        symbolId=41,
        executionPrice=4044.71,
        tradeSide=model.ProtoOATradeSide.SELL,
        dealStatus=model.ProtoOADealStatus.FILLED,
        commission=commission_raw,
        closePositionDetail=model.ProtoOAClosePositionDetail(
            entryPrice=4050.25,
            grossProfit=gross_profit_raw,
            swap=close_swap_raw,
            commission=close_commission_raw,
            moneyDigits=money_digits,
            closedVolume=100,
            balance=balance_raw,
        ),
    )


# ---------------------------------------------------------------------------
# (a) daemon _normalize_position: exact scaling at moneyDigits=2 and 3
# ---------------------------------------------------------------------------


def test_normalize_position_scales_commission_swap_margin_digits_2():
    """Raw commission=-12, swap=-5, usedMargin=404, moneyDigits=2 must scale
    to EXACTLY commission=-0.12, swap=-0.05, used_margin=4.04 — the same
    scale=10**2 convention ``_normalize_deal`` already applies."""
    raw_position = _build_position(
        position_id=653082985,
        side_enum=model.ProtoOATradeSide.SELL,
        entry=4044.71,
        sl=4060.0,
        tp=4020.0,
        commission_raw=-12,
        swap_raw=-5,
        used_margin_raw=404,
        money_digits=2,
    )
    normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert normalized["commission"] == pytest.approx(-0.12, abs=1e-9)
    assert normalized["swap"] == pytest.approx(-0.05, abs=1e-9)
    assert normalized["used_margin"] == pytest.approx(4.04, abs=1e-9)
    assert normalized["money_digits"] == 2


def test_normalize_position_scales_commission_swap_margin_digits_3():
    """Same raw values but moneyDigits=3 must divide by 1000, not 100 —
    proving the scale is actually driven by money_digits, not hardcoded."""
    raw_position = _build_position(
        position_id=653082986,
        side_enum=model.ProtoOATradeSide.SELL,
        entry=4044.71,
        sl=4060.0,
        tp=4020.0,
        commission_raw=-12,
        swap_raw=-5,
        used_margin_raw=404,
        money_digits=3,
    )
    normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert normalized["commission"] == pytest.approx(-0.012, abs=1e-9)
    assert normalized["swap"] == pytest.approx(-0.005, abs=1e-9)
    assert normalized["used_margin"] == pytest.approx(0.404, abs=1e-9)
    assert normalized["money_digits"] == 3


def test_normalize_position_clamps_out_of_range_money_digits():
    """A malformed/out-of-range moneyDigits must never explode or invert the
    scale — clamp to [0, 8] instead of trusting the wire value blindly."""
    raw_position = _build_position(
        position_id=1,
        side_enum=model.ProtoOATradeSide.BUY,
        entry=100.0,
        sl=90.0,
        tp=110.0,
        commission_raw=-12,
        swap_raw=0,
        used_margin_raw=0,
        money_digits=99,  # out of range -> clamp to 8
    )
    normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert normalized["money_digits"] == 8
    assert normalized["commission"] == pytest.approx(-12 / 10 ** 8, abs=1e-12)


# ---------------------------------------------------------------------------
# (b) full chain: daemon normalize -> client normalize -> client enrich,
# pinned to the live incident's own numbers.
# ---------------------------------------------------------------------------

# Live incident (2026-07-15, open SHORT position 653082985, XAUUSD):
# entry=4044.71, commission raw=-12, moneyDigits=2, true gross=-5.54.
# Before the fix: unscaled commission (-12) fed straight into netProfit ->
# netProfit=-17.54. After the fix: commission scales to -0.12 -> netProfit
# = gross(-5.54) + swap(0) + commission(-0.12) = -5.66.
_INCIDENT_ENTRY = 4044.71
_INCIDENT_ASK = 4050.25  # SHORT closes at ask: (entry - ask) = -5.54
_INCIDENT_BID = 4050.15
_INCIDENT_COMMISSION_RAW = -12
_INCIDENT_GROSS = -5.54
_INCIDENT_NET_CORRECT = -5.66
_INCIDENT_NET_BUG = -17.54  # what the unscaled bug used to produce


def test_full_chain_netprofit_matches_live_incident_after_fix():
    raw_position = _build_position(
        position_id=653082985,
        side_enum=model.ProtoOATradeSide.SELL,
        entry=_INCIDENT_ENTRY,
        sl=4060.0,
        tp=4020.0,
        commission_raw=_INCIDENT_COMMISSION_RAW,
        swap_raw=0,
        used_margin_raw=404,
        money_digits=2,
    )
    daemon_normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    assert daemon_normalized["commission"] == pytest.approx(-0.12, abs=1e-9)

    dexter3_pos = Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_normalized)
    # commission must NOT be re-scaled a second time at the client boundary.
    assert dexter3_pos["commission"] == pytest.approx(-0.12, abs=1e-9)

    c = Dexter3OpenApiClient()
    c.get_spot_price = lambda sym: {"bid": _INCIDENT_BID, "ask": _INCIDENT_ASK, "symbol": sym}
    c._enrich_positions_with_live_pnl([dexter3_pos])

    point_value = _USD_POINT_VALUE_PER_UNIT["XAUUSD"]
    vol_units = dexter3_pos["volume"]
    assert vol_units == pytest.approx(1.0)
    assert point_value == pytest.approx(1.0)

    assert dexter3_pos["grossProfit"] == pytest.approx(_INCIDENT_GROSS, abs=1e-3)
    # The pinned live-incident assertion: netProfit must be gross + scaled
    # commission (+ scaled swap, here 0) — NOT gross plus the raw unscaled
    # commission.
    assert dexter3_pos["netProfit"] == pytest.approx(_INCIDENT_NET_CORRECT, abs=1e-3)
    assert dexter3_pos["netProfit"] != pytest.approx(_INCIDENT_NET_BUG, abs=1.0)
    # Refute the bug formula directly: gross + raw unscaled commission.
    bug_formula_net = _INCIDENT_GROSS + _INCIDENT_COMMISSION_RAW
    assert bug_formula_net == pytest.approx(_INCIDENT_NET_BUG, abs=1e-3)
    assert dexter3_pos["netProfit"] != pytest.approx(bug_formula_net, abs=1.0)


def test_full_chain_netprofit_includes_scaled_swap_too():
    """Regression variant with a non-zero swap, to confirm swap (not just
    commission) is scaled through the whole chain."""
    raw_position = _build_position(
        position_id=653082987,
        side_enum=model.ProtoOATradeSide.SELL,
        entry=_INCIDENT_ENTRY,
        sl=4060.0,
        tp=4020.0,
        commission_raw=_INCIDENT_COMMISSION_RAW,
        swap_raw=-5,  # scales to -0.05
        used_margin_raw=404,
        money_digits=2,
    )
    daemon_normalized = openapi_daemon._normalize_position(raw_position, {41: "XAUUSD"})
    dexter3_pos = Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_normalized)

    c = Dexter3OpenApiClient()
    c.get_spot_price = lambda sym: {"bid": _INCIDENT_BID, "ask": _INCIDENT_ASK, "symbol": sym}
    c._enrich_positions_with_live_pnl([dexter3_pos])

    # gross(-5.54) + swap(-0.05) + commission(-0.12) = -5.71
    assert dexter3_pos["netProfit"] == pytest.approx(-5.71, abs=1e-3)


# ---------------------------------------------------------------------------
# (c) deals-path parity regression: _normalize_deal must keep dividing by the
# same scale, so a future edit can't desync the two sibling normalizers.
# ---------------------------------------------------------------------------


def test_normalize_deal_still_divides_by_money_digits_scale():
    """Regression guard: the deals/realized-PnL path (already correct, must
    NOT be touched by this fix) still scales gross/swap/commission by
    10**moneyDigits exactly like the position path now does."""
    deal = _build_deal(
        commission_raw=-12,
        close_swap_raw=-5,
        close_commission_raw=-12,
        gross_profit_raw=-554,  # -5.54 scaled by 100
        money_digits=2,
    )
    normalized = openapi_daemon._normalize_deal(deal, {41: "XAUUSD"})
    assert normalized["gross_profit_usd"] == pytest.approx(-5.54, abs=1e-9)
    assert normalized["swap_usd"] == pytest.approx(-0.05, abs=1e-9)
    assert normalized["commission_usd"] == pytest.approx(-0.12, abs=1e-9)
    assert normalized["pnl_usd"] == pytest.approx(-5.71, abs=1e-9)
    assert normalized["balance_after_usd"] == pytest.approx(1000.0, abs=1e-9)


# ---------------------------------------------------------------------------
# (d) money_digits/moneyDigits carried into the client's dexter3-shaped dict
# ---------------------------------------------------------------------------


def test_normalize_position_for_dexter3_carries_money_digits():
    daemon_shaped = {
        "position_id": 42,
        "entry_price": 4044.71,
        "stop_loss": 4060.0,
        "take_profit": 4020.0,
        "direction": "short",
        "volume": 100,
        "symbol": "XAUUSD",
        "swap": -0.05,
        "commission": -0.12,
        "used_margin": 4.04,
        "money_digits": 2,
    }
    dexter3_pos = Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_shaped)
    assert dexter3_pos["money_digits"] == 2
    assert dexter3_pos["moneyDigits"] == 2
    # Presence must not depend on the daemon dict actually having the key —
    # default to 2 (the ProtoOAPosition wire default) rather than raising.
    daemon_shaped_no_digits = dict(daemon_shaped)
    del daemon_shaped_no_digits["money_digits"]
    dexter3_pos_default = Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_shaped_no_digits)
    assert dexter3_pos_default["money_digits"] == 2
    assert dexter3_pos_default["moneyDigits"] == 2


def test_normalize_position_for_dexter3_does_not_rescale_swap_commission():
    """The client must pass swap/commission through UNCHANGED — the daemon
    is now the single source of scaling truth. Re-scaling here would
    reintroduce the bug at 1/100x (double-division)."""
    daemon_shaped = {
        "position_id": 42,
        "entry_price": 4044.71,
        "stop_loss": 4060.0,
        "take_profit": 4020.0,
        "direction": "short",
        "volume": 100,
        "symbol": "XAUUSD",
        "swap": -0.05,
        "commission": -0.12,
        "used_margin": 4.04,
        "money_digits": 2,
    }
    dexter3_pos = Dexter3OpenApiClient._normalize_position_for_dexter3(daemon_shaped)
    assert dexter3_pos["swap"] == pytest.approx(-0.05, abs=1e-9)
    assert dexter3_pos["commission"] == pytest.approx(-0.12, abs=1e-9)
    assert dexter3_pos["usedMargin"] == pytest.approx(4.04, abs=1e-9)

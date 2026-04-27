"""Tests for RR-based size tilt (refactor of rr_floor_guard).

Verifies the new behavior: never blocks (default), always writes
raw_scores.rr_bucket_tag, applies multiplier to ctrader_risk_usd_override.
"""
from unittest.mock import MagicMock
import pytest


def _make_executor():
    from execution import ctrader_executor as mod
    # Bypass __init__ heavy work — instantiate via __new__
    inst = mod.CTraderExecutor.__new__(mod.CTraderExecutor)
    return inst


def _payload(*, entry, sl, tp, direction, risk_override=None):
    p = {
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "direction": direction,
        "raw_scores": {},
    }
    if risk_override is not None:
        p["raw_scores"]["ctrader_risk_usd_override"] = risk_override
    return p


def test_rr_full_bucket_no_size_change():
    inst = _make_executor()
    p = _payload(entry=2050, sl=2048, tp=2055, direction="long", risk_override=2.5)
    ok, reason, meta = inst._rr_floor_guard(source="scalp_xauusd:winner", payload=p)
    assert ok is True
    assert reason == ""
    assert meta["bucket"] == "full"
    assert meta["multiplier"] == 1.0
    tag = p["raw_scores"]["rr_bucket_tag"]
    assert tag["bucket"] == "full"
    # No size change at full multiplier
    assert p["raw_scores"]["ctrader_risk_usd_override"] == 2.5


def test_rr_half_bucket_halves_size():
    inst = _make_executor()
    # rr=0.93: TP 0.93x of risk
    p = _payload(entry=2050, sl=2048, tp=2050 + 0.93 * 2, direction="long", risk_override=2.5)
    ok, reason, meta = inst._rr_floor_guard(source="scalp_xauusd:winner", payload=p)
    assert ok is True
    assert reason == ""
    assert meta["bucket"] == "half"
    assert meta["multiplier"] == 0.5
    assert p["raw_scores"]["ctrader_risk_usd_override"] == round(2.5 * 0.5, 4)


def test_rr_mini_bucket_30pct_size():
    inst = _make_executor()
    # rr=0.5
    p = _payload(entry=2050, sl=2048, tp=2050 + 0.5 * 2, direction="long", risk_override=2.5)
    ok, _, meta = inst._rr_floor_guard(source="scalp_xauusd:winner", payload=p)
    assert ok is True
    assert meta["bucket"] == "mini"
    assert meta["multiplier"] == 0.3
    assert p["raw_scores"]["ctrader_risk_usd_override"] == round(2.5 * 0.3, 4)


def test_rr_short_direction_calculated_correctly():
    inst = _make_executor()
    # short rr=2.0 (TP much further than SL)
    p = _payload(entry=2050, sl=2052, tp=2046, direction="short")
    ok, _, meta = inst._rr_floor_guard(source="x", payload=p)
    assert ok is True
    assert meta["bucket"] == "full"
    assert meta["rr"] == 2.0


def test_rr_geometry_invalid_returns_ok():
    inst = _make_executor()
    p = _payload(entry=2050, sl=2050, tp=2055, direction="long")  # zero risk
    ok, _, meta = inst._rr_floor_guard(source="x", payload=p)
    assert ok is True
    assert meta.get("reason") == "geometry_invalid"


def test_rr_no_size_override_does_not_create_one():
    inst = _make_executor()
    p = _payload(entry=2050, sl=2048, tp=2050 + 0.93 * 2, direction="long")  # half bucket
    ok, _, meta = inst._rr_floor_guard(source="x", payload=p)
    assert ok is True
    assert meta["bucket"] == "half"
    # No risk_override existed, so none should be created
    assert "ctrader_risk_usd_override" not in p["raw_scores"]


def test_default_never_hard_blocks_low_rr():
    inst = _make_executor()
    # rr=0.3 — would have been blocked under old gate
    p = _payload(entry=2050, sl=2050 - 1.0, tp=2050 + 0.3, direction="long", risk_override=2.5)
    ok, reason, _ = inst._rr_floor_guard(source="scalp_xauusd:winner", payload=p)
    assert ok is True, f"new gate must never block by default; got reason={reason}"


def test_rr_bucket_tag_always_logged():
    inst = _make_executor()
    p = _payload(entry=2050, sl=2048, tp=2055, direction="long")
    inst._rr_floor_guard(source="x", payload=p)
    assert "rr_bucket_tag" in p["raw_scores"]
    tag = p["raw_scores"]["rr_bucket_tag"]
    assert "rr" in tag and "bucket" in tag and "multiplier_applied" in tag

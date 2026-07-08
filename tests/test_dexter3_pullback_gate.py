"""Pullback-resumption entry selector tests (owner directive 2026-07-08)."""
from __future__ import annotations

from dexter3.edge_buckets import EdgeGateConfig, pullback_resume, pullback_size_mult


def _bars(closes, last_ohlc=None):
    b = [{"open": c, "high": c + 0.5, "low": c - 0.5, "close": c} for c in closes]
    if last_ohlc:
        o, h, l, c = last_ohlc
        b[-1] = {"open": o, "high": h, "low": l, "close": c}
    return b


# 12-bar fixtures (classifier needs >= leg+2 = 10 bars)
_BUY_PB = ([100, 101, 103, 105, 107, 109, 111, 113, 111, 109, 108, 107], (107.0, 109.3, 106.9, 109.0))
_SELL_PB = ([120, 118, 116, 114, 112, 110, 108, 106, 108, 110, 111, 112], (112.0, 112.1, 108.7, 109.0))
_CHASE = ([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111], (110.0, 111.3, 109.9, 111.0))


def test_pullback_resume_buy_setup():
    assert pullback_resume(_bars(*_BUY_PB), "buy") is True


def test_pullback_resume_rejects_chase_no_pullback():
    assert pullback_resume(_bars(*_CHASE), "buy") is False


def test_pullback_resume_rejects_weak_close():
    # leg + pullback but the last bar closes weak (low CLP) -> no resumption
    closes = _BUY_PB[0]
    bars = _bars(closes, last_ohlc=(109.0, 109.5, 105.0, 105.5))  # closes near low
    assert pullback_resume(bars, "buy") is False


def test_pullback_resume_sell_setup():
    assert pullback_resume(_bars(*_SELL_PB), "sell") is True


def test_pullback_resume_none_side_or_short():
    assert pullback_resume([], "buy") is False
    assert pullback_resume(_bars([1, 2, 3]), None) is False


def test_pullback_size_mult_full_on_pullback():
    mult, reason = pullback_size_mult("buy", _bars(*_BUY_PB), EdgeGateConfig())
    assert mult == 1.0
    assert reason["is_pullback"] is True


def test_pullback_size_mult_scout_on_non_pullback():
    mult, reason = pullback_size_mult("buy", _bars(*_CHASE), EdgeGateConfig())
    assert mult == EdgeGateConfig().non_pullback_mult == 0.35
    assert reason["is_pullback"] is False
    assert reason["applied_scout"] is True


def test_pullback_size_mult_disabled_keeps_full_but_classifies():
    cfg = EdgeGateConfig(pullback_enabled=False)
    mult, reason = pullback_size_mult("buy", _bars(*_CHASE), cfg)
    assert mult == 1.0  # disabled -> never downsizes
    assert reason["is_pullback"] is False  # but still classifies
    assert reason["applied_scout"] is False

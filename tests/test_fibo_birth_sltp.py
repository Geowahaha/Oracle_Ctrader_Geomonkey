"""Unit tests for the birth-anchor SL/TP resolver in scanners/fibo_advance.py.

Stays scanner-local. No executor, no XAU direct lane, no routing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from analysis.fibonacci import FibonacciLevels, FiboSignalContext


@pytest.fixture(autouse=True)
def _enable_birth_sltp_flag():
    import config as cfg_mod
    with patch.object(cfg_mod.config, "FIBO_IMPULSE_BIRTH_SLTP_ENABLED", True), \
         patch.object(cfg_mod.config, "FIBO_IMPULSE_BIRTH_SLTP_MIN_CONFIDENCE", 0.60), \
         patch.object(cfg_mod.config, "FIBO_IMPULSE_BIRTH_ENTRY_RATIO", 0.618):
        yield


def _scanner():
    from scanners.fibo_advance import FiboAdvanceScanner
    return FiboAdvanceScanner()


def _bullish_birth_ctx(anchor=100.0, tip=110.0, confidence=0.80) -> FiboSignalContext:
    """A context as if the analyzer just detected a bullish impulse birth."""
    ctx = FiboSignalContext()
    ctx.entry_mode = "early_origin"
    ctx.impulse_birth_detected = True
    ctx.impulse_birth_direction = "bullish"
    ctx.impulse_birth_anchor_price = anchor
    ctx.impulse_birth_confidence = confidence
    rng = tip - anchor
    levels = {r: tip - r * rng for r in (0.0, 0.382, 0.5, 0.618, 0.65, 0.786, 1.0)}
    exts = {r: anchor + r * rng for r in (1.0, 1.272, 1.414, 1.618, 2.0, 2.618)}
    ctx.impulse_birth_fib_levels = FibonacciLevels(
        direction="bullish",
        swing_start=anchor,
        swing_end=tip,
        swing_range=rng,
        levels=levels,
        extensions=exts,
        golden_pocket_low=levels[0.618],
        golden_pocket_high=levels[0.65],
        impulse_strength=confidence,
    )
    return ctx


def _bearish_birth_ctx(anchor=110.0, tip=100.0, confidence=0.80) -> FiboSignalContext:
    ctx = FiboSignalContext()
    ctx.entry_mode = "early_origin"
    ctx.impulse_birth_detected = True
    ctx.impulse_birth_direction = "bearish"
    ctx.impulse_birth_anchor_price = anchor
    ctx.impulse_birth_confidence = confidence
    rng = anchor - tip
    levels = {r: tip + r * rng for r in (0.0, 0.382, 0.5, 0.618, 0.65, 0.786, 1.0)}
    exts = {r: anchor - r * rng for r in (1.0, 1.272, 1.414, 1.618, 2.0, 2.618)}
    ctx.impulse_birth_fib_levels = FibonacciLevels(
        direction="bearish",
        swing_start=anchor,
        swing_end=tip,
        swing_range=rng,
        levels=levels,
        extensions=exts,
        golden_pocket_low=levels[0.618],
        golden_pocket_high=levels[0.65],
        impulse_strength=confidence,
    )
    return ctx


# ---------------------------------------------------------------------------


def test_resolver_activates_on_bullish_early_origin():
    ctx = _bullish_birth_ctx(anchor=100.0, tip=110.0, confidence=0.80)
    ov = _scanner()._resolve_birth_override(ctx, "long", current_price=105.0)
    assert ov is not None
    assert ov["sl_anchor"] == pytest.approx(100.0)
    # Entry is birth 0.618 level = tip - 0.618*(tip-anchor) = 110 - 6.18 = 103.82
    assert ov["entry"] == pytest.approx(103.82, rel=1e-3)
    assert ov["confidence"] == pytest.approx(0.80)


def test_resolver_activates_on_bearish_early_origin():
    ctx = _bearish_birth_ctx(anchor=110.0, tip=100.0, confidence=0.80)
    ov = _scanner()._resolve_birth_override(ctx, "short", current_price=105.0)
    assert ov is not None
    assert ov["sl_anchor"] == pytest.approx(110.0)
    # Entry = tip + 0.618*(anchor-tip) = 100 + 6.18 = 106.18
    assert ov["entry"] == pytest.approx(106.18, rel=1e-3)


def test_resolver_skips_when_mode_not_early_origin():
    ctx = _bullish_birth_ctx()
    ctx.entry_mode = "late_retrace"
    assert _scanner()._resolve_birth_override(ctx, "long", 105.0) is None


def test_resolver_skips_when_direction_mismatch():
    ctx = _bullish_birth_ctx()  # bullish
    # Asking for a short override against a bullish birth → reject.
    assert _scanner()._resolve_birth_override(ctx, "short", 105.0) is None


def test_resolver_skips_below_min_confidence():
    ctx = _bullish_birth_ctx(confidence=0.40)  # under 0.60 floor
    assert _scanner()._resolve_birth_override(ctx, "long", 105.0) is None


def test_resolver_skips_when_birth_fib_missing():
    ctx = _bullish_birth_ctx()
    ctx.impulse_birth_fib_levels = None
    assert _scanner()._resolve_birth_override(ctx, "long", 105.0) is None


def test_resolver_respects_disabled_flag():
    import config as cfg_mod
    ctx = _bullish_birth_ctx()
    with patch.object(cfg_mod.config, "FIBO_IMPULSE_BIRTH_SLTP_ENABLED", False):
        assert _scanner()._resolve_birth_override(ctx, "long", 105.0) is None


def test_resolver_bumps_entry_to_market_when_pullback_missed():
    """If price has already exceeded the 0.618 entry for a long, entry
    snaps to current price instead of waiting."""
    ctx = _bullish_birth_ctx(anchor=100.0, tip=110.0)
    # 0.618 entry = 103.82; current_price well below (still above anchor)
    ov = _scanner()._resolve_birth_override(ctx, "long", current_price=102.0)
    assert ov is not None
    # Current 102 < entry 103.82 → snap to current
    assert ov["entry"] == pytest.approx(102.0)


def test_resolver_bumps_entry_to_market_short_when_price_already_below():
    ctx = _bearish_birth_ctx(anchor=110.0, tip=100.0)
    # 0.618 entry for short = 106.18; current 108 > entry → snap to current
    ov = _scanner()._resolve_birth_override(ctx, "short", current_price=108.0)
    assert ov is not None
    assert ov["entry"] == pytest.approx(108.0)


def test_resolver_rejects_degenerate_birth_fib():
    ctx = _bullish_birth_ctx(anchor=100.0, tip=100.0)  # zero range
    assert _scanner()._resolve_birth_override(ctx, "long", 105.0) is None

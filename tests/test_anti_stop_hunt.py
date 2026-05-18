"""Tests for the Anti-Stop-Hunt SL widener."""
from __future__ import annotations

from analysis.anti_stop_hunt import AntiHuntConfig, widen_sl_for_anti_hunt
from analysis.anti_stop_hunt.widener import _decide


def test_widener_disabled_returns_proposed():
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2301.50, atr=2.0,
        config=AntiHuntConfig(enabled=False),
    )
    assert sl == 2302.0


def test_widener_pushes_short_sl_past_swing_plus_buffer():
    # entry 2300 short, proposed SL 2302 (2pt above), swing 2303, atr 2 → buffer 2
    # candidate = 2303 + 2 = 2305. proposed=2302. max widening = 2 * 2.5 = 5. cap = 2302+5 = 2307.
    # safe = min(max(2302, 2305), 2307) = 2305.
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2303.0, atr=2.0,
        config=AntiHuntConfig(enabled=True, buffer_atr_mult=1.0, max_widening_atr_mult=2.5),
    )
    assert sl == 2305.0


def test_widener_long_pulls_sl_below_swing_minus_buffer():
    # entry 2300 long, proposed SL 2298 (2pt below), swing 2297, atr 2 → buffer 2
    # candidate = 2297 - 2 = 2295. proposed=2298. max widening = 5. cap = 2298 - 5 = 2293.
    # safe = max(min(2298, 2295), 2293) = 2295.
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="long", proposed_sl=2298.0,
        swing_extreme=2297.0, atr=2.0,
        config=AntiHuntConfig(enabled=True),
    )
    assert sl == 2295.0


def test_widener_respects_max_widening_cap():
    # entry 2300 short, proposed SL 2302, swing 2310 (huge gap), atr 2 → buffer 2
    # candidate = 2310 + 2 = 2312. max widening = 5. cap = 2307.
    # safe = min(max(2302, 2312), 2307) = 2307.
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2310.0, atr=2.0,
        config=AntiHuntConfig(enabled=True, buffer_atr_mult=1.0, max_widening_atr_mult=2.5),
    )
    assert sl == 2307.0


def test_widener_no_widening_when_proposed_already_safe():
    # proposed SL is already beyond swing + buffer.
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2310.0,
        swing_extreme=2303.0, atr=2.0,
        config=AntiHuntConfig(enabled=True, buffer_atr_mult=1.0),
    )
    assert sl == 2310.0


def test_widener_handles_zero_atr_gracefully():
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2303.0, atr=0.0,
        config=AntiHuntConfig(enabled=True),
    )
    assert sl == 2302.0


def test_widener_invalid_direction_returns_proposed():
    sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="weird", proposed_sl=2302.0,
        swing_extreme=2303.0, atr=2.0,
        config=AntiHuntConfig(enabled=True),
    )
    assert sl == 2302.0


def test_widener_decision_includes_reason_trail():
    d = _decide(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2303.0, atr=2.0,
        config=AntiHuntConfig(enabled=True),
    )
    assert "anti_hunt" in d.reason
    assert d.widened_by > 0

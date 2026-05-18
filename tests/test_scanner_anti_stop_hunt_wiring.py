"""Integration test: scanner SL widening respects flag + math.

We don't import the full scalp scanner (it pulls heavy market deps). Instead
we replicate the in-scanner code shape and exercise the same widener call,
asserting:
  - When XAU_ANTI_STOP_HUNT_ENABLED is False, stop is unchanged.
  - When True, stop is pushed by buffer_atr_mult * atr.
  - When the widening would exceed 1.5 * max_risk, the change is rejected
    (the scanner falls back to the original, capped stop).
  - The widener never narrows the stop (`_new_risk > risk` guard).
"""
from __future__ import annotations

from analysis.anti_stop_hunt import AntiHuntConfig, widen_sl_for_anti_hunt


def _scanner_like_widening(*, entry: float, direction: str, stop: float, atr: float, max_risk: float, enabled: bool) -> tuple[float, float]:
    """Replicates the in-scanner wiring at scanners/scalping_scanner.py:962-988."""
    risk = abs(entry - stop)
    if enabled:
        cfg = AntiHuntConfig(enabled=True, buffer_atr_mult=1.0, max_widening_atr_mult=2.5)
        widened = widen_sl_for_anti_hunt(
            entry=entry, direction=direction, proposed_sl=stop,
            swing_extreme=stop, atr=atr, config=cfg,
        )
        new_risk = abs(entry - widened)
        if 0 < new_risk <= max_risk * 1.5 and new_risk > risk:
            stop = widened
            risk = new_risk
    return stop, risk


def test_scanner_widening_disabled_leaves_stop_alone():
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4541.5, atr=4.0,
        max_risk=5.0, enabled=False,
    )
    assert stop == 4541.5
    assert risk == 1.5


def test_scanner_widening_pushes_stop_past_swing_plus_atr_for_short():
    # entry=4540, proposed stop=4541.5 (1.5pt above), atr=4 → widener pushes to
    # max(4541.5, 4541.5 + 4*1.0) = 4545.5 (extra 4pt buffer).
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4541.5, atr=4.0,
        max_risk=5.0, enabled=True,
    )
    assert stop == 4545.5
    assert risk == 5.5


def test_scanner_widening_long_pulls_stop_down_past_swing():
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="long", stop=4538.5, atr=4.0,
        max_risk=5.0, enabled=True,
    )
    assert stop == 4534.5
    assert risk == 5.5


def test_scanner_widening_rejects_if_exceeds_max_risk_cap():
    # max_risk=2.0 → cap 1.5x = 3.0. Widener would create 5.5pt risk → reject.
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4541.5, atr=4.0,
        max_risk=2.0, enabled=True,
    )
    # Stop must be unchanged because widened risk (5.5) > 1.5*max_risk (3.0).
    assert stop == 4541.5
    assert risk == 1.5


def test_scanner_widening_always_adds_buffer_when_swing_is_proposed():
    # In the scanner wiring we pass swing_extreme=stop (the scanner places SL
    # just past the swing). The widener treats stop as the swing and always
    # adds buffer_atr_mult × ATR. So a 10pt stop with ATR=2 → 12pt stop.
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4550.0, atr=2.0,
        max_risk=15.0, enabled=True,
    )
    assert stop == 4552.0
    assert risk == 12.0


def test_scanner_widening_respects_max_widening_atr_mult_cap():
    # entry=4540, proposed stop=4541.5, atr=4 → buffer=4, cap=2.5*4=10.
    # widened candidate = swing(4541.5) + buffer(4) = 4545.5. cap = stop+10 = 4551.5.
    # safe = min(max(4541.5, 4545.5), 4551.5) = 4545.5. new_risk=5.5 < 1.5*max_risk(10)=15. OK.
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4541.5, atr=4.0,
        max_risk=10.0, enabled=True,
    )
    assert stop == 4545.5


def test_scanner_widening_zero_atr_safe():
    # ATR=0 → widener returns proposed unchanged; scanner keeps proposed.
    stop, risk = _scanner_like_widening(
        entry=4540.0, direction="short", stop=4541.5, atr=0.0,
        max_risk=5.0, enabled=True,
    )
    assert stop == 4541.5

"""Tests for the MFE Progressive Trail engine.

The 2026-05-18 23:50 incident is the reference case:
- Short pid=621794184, entry 4550.79, original SL 4557.65, risk 6.86pt
- MFE peaked at 4536.88 → 13.91pt favorable = 2.03R
- Expected lock_fraction at 2R tier: 55% → lock 7.65pt → SL @ 4543.14
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from learning.mfe_progressive_trail import (
    MFEProgressiveTrail,
    MFEProgressiveTrailConfig,
    MFETrailDirective,
    MFETrailInputs,
)


_T0 = datetime(2026, 5, 18, 16, 25, 0, tzinfo=timezone.utc)


def _clock_factory(start: datetime):
    state = {"now": start}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def _short_incident_inputs(mfe_r: float, current_sl: float = 4557.65) -> MFETrailInputs:
    return MFETrailInputs(
        position_id=621794184,
        direction="short",
        entry_price=4550.79,
        current_stop_loss=current_sl,
        original_stop_loss=4557.65,
        mfe_r_observed=mfe_r,
    )


def test_trail_disabled_returns_none():
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=False))
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.0))
    assert d is None


def test_trail_does_not_fire_below_min_mfe_r():
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True, min_mfe_r=1.0))
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=0.5))
    assert d is None


def test_trail_locks_30pct_at_1r_tier():
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=1.5))
    assert d is not None
    # tier 1.0 → 30% of MFE 1.5R = 0.45R = 0.45*6.86 = 3.087 pt below entry
    assert d.tier_mfe_r == 1.0
    assert d.tier_lock_fraction == 0.30
    assert round(d.new_stop_loss, 4) == round(4550.79 - 0.45 * 6.86, 4)


def test_trail_locks_55pct_at_2r_tier_matching_incident():
    """The exact incident: 2.03R MFE → 55% lock would have put SL @ ~4543.14."""
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.03))
    assert d is not None
    assert d.tier_mfe_r == 2.0
    assert d.tier_lock_fraction == 0.55
    expected_sl = 4550.79 - (2.03 * 0.55 * 6.86)  # entry - locked_pts
    assert abs(d.new_stop_loss - expected_sl) < 1e-3
    # The trade reversed to 4549.57 — SL would NOT have been hit at this level.
    assert d.new_stop_loss < 4549.57


def test_trail_locks_70pct_at_3r_tier():
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=3.5))
    assert d is not None
    assert d.tier_mfe_r == 3.0
    assert d.tier_lock_fraction == 0.70


def test_trail_locks_85pct_at_4r_tier():
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=4.5))
    assert d is not None
    assert d.tier_mfe_r == 4.0
    assert d.tier_lock_fraction == 0.85


def test_trail_never_widens_existing_protective_sl():
    """If current SL is already tighter (more protective) than the proposed
    trail level, the engine must NOT move SL back outward."""
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    # current_sl is already at 4543 (better than the 2R-tier proposed 4543.14ish).
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.0, current_sl=4540.0))
    assert d is None  # would have moved to ~4543.24, but 4540 is already better


def test_trail_peak_persists_across_calls():
    """A single MFE excursion locks the floor permanently."""
    clock, advance = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True, cooldown_seconds=0.0), clock=clock)
    d1 = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.5))
    assert d1 is not None
    sl_after_peak = d1.new_stop_loss

    # Subsequent tick reports mfe_r=1.0 (price retraced) — peak still 2.5.
    advance(60.0)
    d2 = eng.evaluate(inputs=_short_incident_inputs(mfe_r=1.0, current_sl=sl_after_peak))
    # The proposed SL is still based on peak 2.5R, but it's already at that
    # level → no further tightening needed → engine returns None.
    assert d2 is None
    assert eng.peak_mfe(621794184) == 2.5


def test_trail_cooldown_blocks_back_to_back_amends():
    clock, advance = _clock_factory(_T0)
    eng = MFEProgressiveTrail(
        config=MFEProgressiveTrailConfig(enabled=True, cooldown_seconds=60.0),
        clock=clock,
    )
    d1 = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.0))
    assert d1 is not None
    advance(5.0)
    d2 = eng.evaluate(inputs=_short_incident_inputs(mfe_r=3.0, current_sl=d1.new_stop_loss))
    assert d2 is None  # cooldown blocks
    advance(120.0)
    d3 = eng.evaluate(inputs=_short_incident_inputs(mfe_r=3.0, current_sl=d1.new_stop_loss))
    assert d3 is not None
    assert d3.tier_mfe_r == 3.0


def test_trail_long_direction_pulls_sl_up():
    clock, _ = _clock_factory(_T0)
    eng = MFEProgressiveTrail(config=MFEProgressiveTrailConfig(enabled=True), clock=clock)
    inputs = MFETrailInputs(
        position_id=100, direction="long",
        entry_price=4540.0, current_stop_loss=4533.0,
        original_stop_loss=4533.0, mfe_r_observed=2.5,
    )
    d = eng.evaluate(inputs=inputs)
    # risk = 7; locked_pts = 7 * 2.5 * 0.55 = 9.625 → new SL = 4540 + 9.625 = 4549.625
    assert d is not None
    assert abs(d.new_stop_loss - 4549.625) < 1e-3


def test_trail_source_filter_when_set():
    cfg = MFEProgressiveTrailConfig(enabled=True, allowed_sources_csv="scalp_xauusd:winner")
    eng = MFEProgressiveTrail(config=cfg)
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.0), source="xauusd_scheduled")
    assert d is None  # not in whitelist
    d = eng.evaluate(inputs=_short_incident_inputs(mfe_r=2.0), source="scalp_xauusd:winner")
    assert d is not None

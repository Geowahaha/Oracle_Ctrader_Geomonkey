"""The 2026-07-27 live trail defect, reproduced and fixed.

REAL TRADE (journal, dexter3:vp:canary, vp_lvn_rejection, 07-27T02:38):
    peak_r  = 3.2674      stop = 2.88 pts      ATR ~ 3.82 pts
    giveback_r = 3.0 * 3.82 / 2.88 = 3.98R
    floor_r = 3.2674 - 3.98 = -0.7138
The floor sat BELOW BREAKEVEN for the entire life of the position, so the trail
held a +3.27R winner all the way down and closed it at -0.74R — handing back
4.0R. A trail whose floor can never rise above breakeven is strictly worse than
no trail at all.

The fix caps giveback at a FRACTION OF PEAK — the one exit rule the 2026-07-25
causal path-simulation found beat doing nothing ("trail 50% of peak, arm 1.5R",
the only positive row of 32 tested). It caps the GIVE-BACK; it does not tighten
the arm, because that same study showed every earlier-locking rule loses money.
"""
from __future__ import annotations

import pytest

from dexter3 import vp_lane


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_ARM_R", "2.0")
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_ATR", "3.0")
    monkeypatch.delenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", raising=False)
    yield


# the exact live geometry
PEAK, ATR, STOP = 3.2674, 3.82, 2.88


def test_reproduces_the_live_defect_when_uncapped():
    floor = vp_lane.convex_floor_r(PEAK, ATR, STOP)
    assert floor is not None
    assert floor == pytest.approx(-0.71, abs=0.02), "must reproduce the journal's -0.7138"
    assert floor < 0.0, "the floor was below breakeven — the defect"


def test_cap_lifts_the_floor_above_breakeven(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    floor = vp_lane.convex_floor_r(PEAK, ATR, STOP)
    assert floor == pytest.approx(PEAK * 0.5, abs=1e-6)
    assert floor > 0.0
    # the trade would have banked ~+1.63R instead of -0.74R
    assert floor == pytest.approx(1.6337, abs=0.001)


def test_cap_is_off_by_default():
    """fable/daytrend are inside a frozen measurement — unset must be identical."""
    assert vp_lane.convex_floor_r(PEAK, ATR, STOP) == pytest.approx(-0.71, abs=0.02)


def test_cap_never_widens_a_naturally_tight_giveback(monkeypatch):
    """With a WIDE stop the ATR-based giveback is already small; the cap must
    not loosen it (min, never max)."""
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    wide_stop = 20.0                       # giveback = 3*3.82/20 = 0.573R
    uncapped = PEAK - (3.0 * ATR / wide_stop)
    monkeypatch.delenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", raising=False)
    assert vp_lane.convex_floor_r(PEAK, ATR, wide_stop) == pytest.approx(uncapped, abs=1e-6)
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    assert vp_lane.convex_floor_r(PEAK, ATR, wide_stop) == pytest.approx(uncapped, abs=1e-6)


def test_still_returns_none_before_the_arm_threshold(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    assert vp_lane.convex_floor_r(1.9, ATR, STOP) is None, "must not arm early"
    assert vp_lane.convex_floor_r(2.0, ATR, STOP) is not None


def test_floor_rises_monotonically_with_peak(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    floors = [vp_lane.convex_floor_r(p, ATR, STOP) for p in (2.0, 3.0, 5.0, 10.0)]
    assert all(a < b for a, b in zip(floors, floors[1:])), floors


def test_bad_inputs_still_return_none(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_GIVEBACK_MAX_PEAK_FRAC", "0.5")
    assert vp_lane.convex_floor_r(PEAK, ATR, 0.0) is None


# --- the risk-overrun stamp -------------------------------------------------

def test_risk_overrun_is_stamped_at_the_one_ounce_floor():
    """Live: dtr asked for $1.60 and actually risked $7.16 (4.5x). Every
    governor downsize makes the overrun WORSE, not better."""
    from dexter3.executor import planned_volume_units
    details = {"minVolume": 1.0, "volumeStep": 1.0, "lotSize": 100.0, "pipSize": 0.01}
    vol, meta = planned_volume_units(details, sl_distance=7.16, risk_usd=1.60,
                                     max_volume_units=10.0)
    assert vol == 1.0
    assert meta["min_volume_clamped_up"] is True
    assert meta["risk_overrun_mult"] == pytest.approx(7.16 / 1.60, abs=0.01)


def test_no_overrun_stamp_when_size_is_not_clamped():
    from dexter3.executor import planned_volume_units
    details = {"minVolume": 1.0, "volumeStep": 1.0, "lotSize": 100.0, "pipSize": 0.01}
    _vol, meta = planned_volume_units(details, sl_distance=2.0, risk_usd=20.0,
                                      max_volume_units=100.0)
    assert "risk_overrun_mult" not in meta

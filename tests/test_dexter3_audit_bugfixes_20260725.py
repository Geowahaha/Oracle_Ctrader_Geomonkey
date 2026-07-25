"""Regression tests for the 2026-07-25 audit bug fixes.

Each test pins a CONFIRMED defect found by the whole-project audit so it cannot
silently return. Every fix had to be behaviour-neutral for fable and daytrend,
which are frozen for the SL-floor experiment — the label tests assert that
explicitly.
"""
from __future__ import annotations

import pytest

from dexter3.basket_live import _label_in_family, lane_positions
from dexter3.executor import label_matches_family

# (label, family, expected)
_LABEL_CASES = [
    # THE BUG: a different LANE must not match dpull's family root.
    ("dexter3:dpull-cs:canary", "dexter3:dpull", False),
    # each lane still matches its own root (no lane may regress)
    ("dexter3:dpull:canary", "dexter3:dpull", True),
    ("dexter3:dpull-cs:canary", "dexter3:dpull-cs", True),
    ("dexter3:fable:v1.8-size-the-edge", "dexter3:fable", True),
    ("dexter3:vp:canary", "dexter3:vp", True),
    ("dexter3:dtr:canary", "dexter3:dtr", True),
    ("dexter3:scalp:canary", "dexter3:scalp", True),
    ("dexter3:chf:canary", "dexter3:chf", True),
    # LEGACY: grok separates its version with a hyphen, not a colon.
    ("dexter3:grok-v1.0:scalper", "dexter3:grok", True),
    # exact root, and unrelated labels
    ("dexter3:vp", "dexter3:vp", True),
    ("dexter3:vpx:canary", "dexter3:vp", False),
    ("dexter3:fable2:canary", "dexter3:fable", False),
    ("", "dexter3:vp", False),
    ("dexter3:vp:canary", "", False),
]


@pytest.mark.parametrize("label,family,expected", _LABEL_CASES)
def test_executor_label_family_boundary(label, family, expected):
    assert label_matches_family(label, family) is expected


@pytest.mark.parametrize("label,family,expected", _LABEL_CASES)
def test_basket_live_label_family_boundary(label, family, expected):
    """basket_live duplicates the rule by convention — it MUST agree."""
    assert _label_in_family(label, family) is expected


def test_lane_positions_does_not_scan_a_peer_lane():
    """The live consequence: dpull's basket scan must exclude dpull-cs legs.

    Before the fix, dpull read dpull-cs's open positions as its own — foreign
    legs dragged its basket aggregate_r and its realized-today total, which
    could stop the lane for the day on a PEER's losses and contaminated the
    forward A/B those two lanes existed to run.
    """
    positions = [
        {"label": "dexter3:dpull:canary", "positionId": 1},
        {"label": "dexter3:dpull-cs:canary", "positionId": 2},
    ]
    dpull = lane_positions(positions, "dexter3:dpull")
    assert [p["positionId"] for p in dpull] == [1]

    cs = lane_positions(positions, "dexter3:dpull-cs")
    assert [p["positionId"] for p in cs] == [2]


def test_grok_legacy_position_still_scanned():
    positions = [{"label": "dexter3:grok-v1.0:scalper", "positionId": 9}]
    assert len(lane_positions(positions, "dexter3:grok")) == 1


def test_frozen_lanes_are_behaviour_neutral():
    """fable/daytrend are frozen for the SL-floor experiment — their matching
    must be identical to the pre-fix behaviour."""
    for label, family in (
        ("dexter3:fable:v1.8-size-the-edge", "dexter3:fable"),
        ("dexter3:dtr:canary", "dexter3:dtr"),
    ):
        assert label_matches_family(label, family) is True
        assert _label_in_family(label, family) is True

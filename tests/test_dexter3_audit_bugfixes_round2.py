"""Round-2 regression tests for the 2026-07-25 audit bug fixes.

Covers the three HIGH-severity defects that survived the first pass:
  A) the close-stop lane's R denominator was derived from the WIDENED broker
     SL, so its convex trail could never arm;
  B) the governor's capital-protection close-all was fire-once, so one
     transient failure turned the daily cap into an advisory;
  C) a get_deals outage disabled the daily cap while logging exactly once.
"""
from __future__ import annotations

import dexter3.shadow_runner as sr


# --- A) R denominator on a close-stop lane ---------------------------------

def _pos(entry: float, sl: float, vol: float) -> dict:
    return {"entryPrice": entry, "stopLoss": sl, "volumeInUnits": vol}


def test_r_base_uses_broker_sl_when_no_soft_stop():
    # normal lanes are unchanged: risk = |entry-sl| * vol
    lane = [_pos(4000.0, 3996.0, 2.0)]
    assert sr._lane_actual_risk_usd(lane, 0.5) == 8.0


def test_r_base_uses_soft_stop_when_supplied():
    """THE BUG: dpull-cs sizes to a SOFT 4-pt stop then amends the broker SL
    out to a 14-pt backstop. Deriving R from the broker SL inflates the
    denominator 3.5x, so a genuine +2.0R move reads as 0.57R and the convex
    trail can never arm at ARM_R=2.0."""
    lane = [_pos(4000.0, 3986.0, 1.0)]           # broker SL 14 pts out
    inflated = sr._lane_actual_risk_usd(lane, 0.5)
    corrected = sr._lane_actual_risk_usd(lane, 0.5, soft_stop_pts=4.0)
    assert inflated == 14.0                       # what the old code computed
    assert corrected == 4.0                       # the risk actually run
    # a +$8 move is +2.0R on the real risk, but only 0.57R on the inflated base
    assert round(8.0 / corrected, 2) == 2.0
    assert round(8.0 / inflated, 2) == 0.57


def test_r_base_soft_stop_scales_with_volume():
    lane = [_pos(4000.0, 3986.0, 3.0)]
    assert sr._lane_actual_risk_usd(lane, 0.5, soft_stop_pts=4.0) == 12.0


def test_r_base_falls_back_when_lane_empty():
    assert sr._lane_actual_risk_usd([], 7.5, soft_stop_pts=4.0) == 7.5
    assert sr._lane_actual_risk_usd(None, 7.5) == 7.5


def test_soft_stop_pts_is_zero_when_close_stop_disabled(monkeypatch):
    monkeypatch.delenv("DEXTER3_OM_CONVEX_CLOSE_STOP", raising=False)
    state = {"vp_convex": {"stop_pts": 4.0}}
    assert sr._soft_stop_pts(state) == 0.0        # other lanes unaffected


def test_soft_stop_pts_reads_state_when_close_stop_enabled(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP", "1")
    assert sr._soft_stop_pts({"vp_convex": {"stop_pts": 4.0}}) == 4.0
    # missing/garbage state must degrade to 0 (fall back to broker SL), not raise
    assert sr._soft_stop_pts({}) == 0.0
    assert sr._soft_stop_pts(None) == 0.0
    assert sr._soft_stop_pts({"vp_convex": {"stop_pts": "nope"}}) == 0.0


# --- C) get_deals outage must not silently disable the cap -----------------

class _DeadMcp:
    def get_deals(self, *_a, **_k):
        raise sr.McpClientError("daemon down")


def test_get_deals_outage_relogs_and_flags_blind_cap(monkeypatch):
    """Old behaviour: ONE log line ever, and a fabricated 0.0 realized total
    that made the daily loss cap unable to fire for the rest of the day."""
    sr._LANE_REALIZED_CACHE.clear()
    lines: list[str] = []
    monkeypatch.setattr(sr, "log_line", lambda s: lines.append(s))

    total, pnls = sr._lane_realized_today(_DeadMcp(), "dexter3:test")
    assert (total, pnls) == (0.0, [])
    assert len(lines) == 1
    assert "NO CACHE EVER" in lines[0], "a never-successful read must be escalated"
    assert "daily cap is BLIND" in lines[0]

    # within the re-log interval: no spam
    sr._lane_realized_today(_DeadMcp(), "dexter3:test")
    assert len(lines) == 1

    # past the interval: it warns AGAIN (the old code never did)
    cache = sr._LANE_REALIZED_CACHE["dexter3:test"]
    cache["last_failure_log_epoch"] -= (sr.LANE_REALIZED_FAILURE_RELOG_SEC + 1)
    sr._lane_realized_today(_DeadMcp(), "dexter3:test")
    assert len(lines) == 2, "a persistent outage must keep warning"
    sr._LANE_REALIZED_CACHE.clear()

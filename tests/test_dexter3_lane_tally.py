from datetime import datetime, timezone

from ops.dexter3_lane_tally import tally_deals


def test_tally_deals_excludes_pre_cutoff_and_foreign_labels():
    deals = [
        {"label": "dexter3:fable:m5h-v1", "time": "2026-07-10T16:54:59Z", "netProfit": 8},
        {"label": "dexter3:fable:m5h-v1", "time": "2026-07-10T16:55:00Z", "netProfit": -3},
        {"label": "dexter3:grok-v1.0:scalper", "executionTime": "2026-07-10T17:00:00Z", "netProfit": 4},
        {"label": "foreign", "time": "2026-07-10T17:01:00Z", "netProfit": 99},
    ]
    out = tally_deals(deals, since=datetime(2026, 7, 10, 16, 55, tzinfo=timezone.utc))
    # grok was retired 2026-07-17 and its lane code removed; its historical
    # deals bucket as "retired" rather than as a live lane (2026-07-25).
    assert out == {("fable", "2026-07-10"): [-3.0], ("retired", "2026-07-10"): [4.0]}


def test_every_live_lane_gets_its_own_bucket():
    """2026-07-25 audit fix: _LANE_FAMILIES listed only grok/vp/fable, so dtr,
    dpull, dpull-cs, scalp and chf ALL fell into "other" in the tool this
    project calls broker truth — a structural cause of the cherry-picked PnL
    prose the board later had to retract."""
    day = "2026-07-24"
    deals = [
        {"label": "dexter3:dtr:canary", "time": f"{day}T10:00:00Z", "netProfit": 1},
        {"label": "dexter3:dpull:canary", "time": f"{day}T10:01:00Z", "netProfit": 2},
        {"label": "dexter3:dpull-cs:canary", "time": f"{day}T10:02:00Z", "netProfit": 3},
        {"label": "dexter3:scalp:canary", "time": f"{day}T10:03:00Z", "netProfit": 4},
        {"label": "dexter3:chf:canary", "time": f"{day}T10:04:00Z", "netProfit": 5},
        {"label": "dexter3:vp:canary", "time": f"{day}T10:05:00Z", "netProfit": 6},
        {"label": "dexter3:fable:v1.8-size-the-edge", "time": f"{day}T10:06:00Z", "netProfit": 7},
    ]
    out = tally_deals(deals)
    assert out == {
        ("dtr", day): [1.0],
        ("dpull", day): [2.0],
        ("dpull-cs", day): [3.0],   # NOT bucketed as dpull
        ("scalp", day): [4.0],
        ("chf", day): [5.0],
        ("vp", day): [6.0],
        ("fable", day): [7.0],
    }
    assert not any(lane == "other" for lane, _ in out), "no live lane may fall into 'other'"

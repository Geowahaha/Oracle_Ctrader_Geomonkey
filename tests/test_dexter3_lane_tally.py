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
    assert out == {("fable", "2026-07-10"): [-3.0], ("grok", "2026-07-10"): [4.0]}

from datetime import datetime, timezone

from dexter3.weekly_risk import weekly_close_policy


def _utc(day: int, hour: int, minute: int) -> datetime:
    # 2026-07-06 is Monday; day offsets keep the test calendar explicit.
    return datetime(2026, 7, 6 + day, hour, minute, tzinfo=timezone.utc)


def test_friday_buffer_flattens_and_blocks_entries() -> None:
    assert weekly_close_policy(_utc(4, 20, 29))["block_entries"] is False
    policy = weekly_close_policy(_utc(4, 20, 30))
    assert policy == {"block_entries": True, "flatten": True, "reason": "weekly_close_window"}


def test_closed_weekend_blocks_without_blind_close_retries() -> None:
    assert weekly_close_policy(_utc(5, 12, 0)) == {
        "block_entries": True, "flatten": False, "reason": "weekly_market_closed"
    }
    assert weekly_close_policy(_utc(6, 20, 59))["block_entries"] is True
    assert weekly_close_policy(_utc(6, 21, 0))["block_entries"] is False

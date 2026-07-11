"""Pure weekly-close guard for intraday lanes.

XAUUSD scalps must never be intentionally carried through the broker's
Friday-close gap.  This module knows no broker/client details; callers use
the returned policy to stop entries and flatten only while the market is
expected to remain open.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def weekly_close_policy(
    now: datetime,
    *,
    close_weekday: int = 4,
    close_hour_utc: int = 21,
    buffer_min: int = 30,
    reopen_weekday: int = 6,
    reopen_hour_utc: int = 21,
) -> dict[str, Any]:
    """Return whether an intraday lane must stop entering/flatten.

    Friday 20:30 UTC is the conservative default (30 minutes ahead of the
    usual 21:00 UTC XAU weekly close in DST).  The guard stays entry-blocked
    through Sunday 21:00 UTC.  ``flatten`` is deliberately only true during
    the pre-close window: after closure a close call cannot be relied on, so
    caller leaves the broker SL as the last-resort backstop rather than
    hammering a closed market.
    """
    current = now.astimezone(timezone.utc)
    minute = current.hour * 60 + current.minute
    close_minute = int(close_hour_utc) * 60
    flatten_start = close_minute - max(0, int(buffer_min))
    weekday = current.weekday()
    if weekday == int(close_weekday) and minute >= flatten_start:
        return {"block_entries": True, "flatten": minute < close_minute, "reason": "weekly_close_window"}
    if weekday == 5 or (weekday == int(reopen_weekday) and minute < int(reopen_hour_utc) * 60):
        return {"block_entries": True, "flatten": False, "reason": "weekly_market_closed"}
    return {"block_entries": False, "flatten": False, "reason": "market_session_open"}

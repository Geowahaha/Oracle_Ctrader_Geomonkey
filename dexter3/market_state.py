"""Broker-sourced market open/closed state for the live M5 lanes.

Pure logic — NO broker/client imports. Callers pass the broker-truth signals
they already hold (the freshest completed M5 bar's close epoch, the live spot
quote age, and optionally the broker's ProtoOASymbol ``tradingMode`` mapped to
an ``trading_enabled`` bool) and get back an explicit open/closed verdict plus
the source that decided it.

Why this exists (owner 2026-07-25, cTrader ``‖`` pause observation): the lanes
previously inferred "market closed" only from the hardcoded time window in
``dexter3.weekly_risk.weekly_close_policy`` (magic weekday/hour) plus the
*silent* "no new M5 bars" behaviour — indistinguishable in the logs from "no
setup". That hardcode is wrong on holidays / early closes / DST, and the
silence hid the real state. This module reads the broker's OWN signal instead
(feed freshness now; ``tradingMode``/``schedule`` as they get wired through),
so market-closed becomes an observable, broker-truthed verdict.

Iron-rule design constraints:
  * **fail-OPEN.** Any missing/ambiguous signal => ``open=True``. This is a
    demo account and we never add a hard blocker (``feedback_demo_let_strategies_trade``);
    ``weekly_close_policy`` stays wired as the time-based backstop.
  * **no careless hardcode.** ``trading_enabled`` is computed at the daemon
    boundary by comparing against the library's *named* ``ProtoOATradingMode``
    enum, never a magic int here. The staleness ceiling is env-tunable.
  * ``trading_enabled`` can only ADD a close (broker says disabled); it never
    forces "open" over a stale feed — trading on a lagging feed is the riskier
    error, so a stale feed closes even if the broker flag says enabled.
"""
from __future__ import annotations

from typing import Any, Sequence

# Feed-staleness ceiling: on an OPEN XAU market a completed M5 bar is at most
# ~300s old (one bar period) + minor lateness, and a spot tick is seconds old;
# when the market is paused/closed neither advances, so the age grows without
# bound. 900s (3 missed M5 bars) sits comfortably above normal open-market
# freshness yet far below any real closure gap. Env-tunable, never hardcoded
# into a caller.
DEFAULT_STALE_SEC = 900.0


def _verdict(
    is_open: bool,
    reason: str,
    source: str,
    bar_age_sec: float | None,
    spot_age_sec: float | None,
    trading_enabled: bool | None,
    schedule: Sequence[dict] | None,
) -> dict[str, Any]:
    return {
        "open": bool(is_open),
        "reason": reason,
        "source": source,
        "bar_age_sec": None if bar_age_sec is None else round(float(bar_age_sec), 1),
        "spot_age_sec": None if spot_age_sec is None else round(float(spot_age_sec), 1),
        "trading_enabled": trading_enabled,
        # Telemetry only in v1: the ProtoOAInterval week-second semantics are
        # not gated on until verified against live broker values (surfaced so
        # they CAN be verified). Report only whether the broker sent any.
        "schedule_intervals": 0 if not schedule else len(list(schedule)),
    }


def evaluate_market_state(
    *,
    now_ts: float,
    newest_bar_epoch: float | None,
    spot_age_sec: float | None = None,
    trading_enabled: bool | None = None,
    schedule: Sequence[dict] | None = None,
    stale_sec: float = DEFAULT_STALE_SEC,
) -> dict[str, Any]:
    """Return the broker-sourced market-open verdict.

    Parameters
    ----------
    now_ts : current wall-clock epoch seconds (caller passes it so this stays
        pure/testable — no ``time.time()`` inside).
    newest_bar_epoch : the CLOSE-time epoch of the newest completed M5 bar
        (``None`` if unknown). Bar age = ``now_ts - newest_bar_epoch``.
    spot_age_sec : seconds since the last live spot tick (``None`` if unknown).
    trading_enabled : broker ProtoOASymbol.tradingMode == ENABLED, or ``None``
        when unavailable (subprocess transport / not wired). ``False`` is an
        authoritative closed signal; ``True`` is trusted only with a fresh feed.
    schedule : raw broker interval dicts (telemetry only in v1).
    stale_sec : feed-staleness ceiling in seconds.

    Verdict priority (each step fails OPEN when its signal is absent):
      1. ``trading_enabled is False``            -> closed (broker_trading_mode)
      2. freshest feed age > ``stale_sec``       -> closed (stale_feed)
      3. a fresh feed age exists                 -> open   (fresh_feed)
      4. ``trading_enabled is True`` (no ages)   -> open   (broker_trading_mode)
      5. nothing usable                          -> open   (none, fail-open)
    """
    bar_age: float | None = None
    if newest_bar_epoch is not None:
        try:
            bar_age = max(0.0, float(now_ts) - float(newest_bar_epoch))
        except (TypeError, ValueError):
            bar_age = None

    spot_age: float | None = None
    if spot_age_sec is not None:
        try:
            spot_age = max(0.0, float(spot_age_sec))
        except (TypeError, ValueError):
            spot_age = None

    ages = [a for a in (bar_age, spot_age) if a is not None]
    activity_age = min(ages) if ages else None

    # 1. Broker explicitly says the symbol cannot open a new position.
    if trading_enabled is False:
        return _verdict(False, "broker_trading_disabled", "broker_trading_mode",
                        bar_age, spot_age, trading_enabled, schedule)

    # 2. Feed has not advanced past the staleness ceiling = paused/closed.
    if activity_age is not None and activity_age > float(stale_sec):
        return _verdict(False, f"feed_stale_{int(activity_age)}s>{int(float(stale_sec))}s",
                        "stale_feed", bar_age, spot_age, trading_enabled, schedule)

    # 3. A fresh feed proves the market is live.
    if activity_age is not None:
        return _verdict(True, "feed_fresh", "fresh_feed",
                        bar_age, spot_age, trading_enabled, schedule)

    # 4. No feed age, but the broker flag says enabled.
    if trading_enabled is True:
        return _verdict(True, "broker_trading_enabled", "broker_trading_mode",
                        bar_age, spot_age, trading_enabled, schedule)

    # 5. No usable signal at all -> fail OPEN (never a hard blocker on demo).
    return _verdict(True, "no_signal_fail_open", "none",
                    bar_age, spot_age, trading_enabled, schedule)

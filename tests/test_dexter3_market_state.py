"""Unit tests for dexter3.market_state — broker-sourced open/closed verdict.

Pure logic; every case pins the fail-OPEN and no-force-open-over-stale-feed
invariants the live gate relies on.
"""
from __future__ import annotations

from dexter3.market_state import DEFAULT_STALE_SEC, evaluate_market_state

NOW = 1_700_000_000.0  # fixed epoch; module takes now_ts so tests stay pure


def test_fresh_bar_is_open():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 120.0)
    assert v["open"] is True
    assert v["source"] == "fresh_feed"
    assert v["bar_age_sec"] == 120.0


def test_stale_bar_is_closed():
    # 3h-old bar (>> 900s ceiling) = paused/closed.
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 3 * 3600.0)
    assert v["open"] is False
    assert v["source"] == "stale_feed"
    assert "feed_stale_" in v["reason"]


def test_stale_spot_is_closed_even_without_bar():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=None, spot_age_sec=5000.0)
    assert v["open"] is False
    assert v["source"] == "stale_feed"


def test_fresh_spot_keeps_open_when_bar_unknown():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=None, spot_age_sec=3.0)
    assert v["open"] is True
    assert v["source"] == "fresh_feed"
    assert v["spot_age_sec"] == 3.0


def test_min_age_wins_one_fresh_feed_means_open():
    # Bar looks stale but a spot tick is fresh -> still open (market is live).
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 4000.0, spot_age_sec=2.0)
    assert v["open"] is True
    assert v["source"] == "fresh_feed"


def test_no_signal_fails_open():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=None, spot_age_sec=None)
    assert v["open"] is True
    assert v["source"] == "none"
    assert v["reason"] == "no_signal_fail_open"


def test_broker_disabled_closes_even_with_fresh_feed():
    # tradingMode=DISABLED is authoritative: closed regardless of a fresh bar.
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 30.0, trading_enabled=False)
    assert v["open"] is False
    assert v["source"] == "broker_trading_mode"
    assert v["reason"] == "broker_trading_disabled"


def test_broker_enabled_does_not_force_open_over_stale_feed():
    # The conservative invariant: broker says ENABLED but our feed lags 2h ->
    # closed (never trade on a lagging feed just because the flag says open).
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 7200.0, trading_enabled=True)
    assert v["open"] is False
    assert v["source"] == "stale_feed"


def test_broker_enabled_opens_when_no_feed_age_available():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=None, spot_age_sec=None, trading_enabled=True)
    assert v["open"] is True
    assert v["source"] == "broker_trading_mode"
    assert v["reason"] == "broker_trading_enabled"


def test_env_stale_sec_boundary():
    ceiling = DEFAULT_STALE_SEC
    just_open = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - (ceiling - 1))
    just_closed = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - (ceiling + 1))
    assert just_open["open"] is True
    assert just_closed["open"] is False


def test_custom_stale_sec():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 400.0, stale_sec=300.0)
    assert v["open"] is False  # 400 > custom 300 ceiling


def test_schedule_is_surfaced_as_count_only():
    sched = [{"start": 0, "end": 100}, {"start": 200, "end": 300}]
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch=NOW - 60.0, schedule=sched)
    assert v["schedule_intervals"] == 2
    assert v["open"] is True  # schedule is telemetry-only in v1, does not gate


def test_bad_types_fail_open():
    v = evaluate_market_state(now_ts=NOW, newest_bar_epoch="not-a-number", spot_age_sec="nope")
    assert v["open"] is True
    assert v["source"] == "none"

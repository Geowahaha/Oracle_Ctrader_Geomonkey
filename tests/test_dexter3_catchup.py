"""Catch-up + freshness tests for dexter3.shadow_runner (blueprint: no silent M5 gaps)."""
from __future__ import annotations

import dexter3.shadow_runner as sr


def bars(*ts: str) -> list[dict]:
    return [{"ts": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for t in ts]


def test_first_run_decides_only_newest():
    assert sr.pending_m5_closes({}, "BTCUSD", bars(
        "2026-07-05T08:05:00Z", "2026-07-05T08:10:00Z", "2026-07-05T08:15:00Z"
    )) == [2]


def test_gap_returns_all_missed_in_order():
    state = {"symbols": {"BTCUSD": {"last_m5_close_ts": "2026-07-05T08:10:00Z"}}}
    b = bars(
        "2026-07-05T08:05:00Z", "2026-07-05T08:10:00Z",
        "2026-07-05T08:15:00Z", "2026-07-05T08:20:00Z",
    )
    assert sr.pending_m5_closes(state, "BTCUSD", b) == [2, 3]


def test_no_new_close_returns_empty():
    state = {"symbols": {"BTCUSD": {"last_m5_close_ts": "2026-07-05T08:20:00Z"}}}
    b = bars("2026-07-05T08:15:00Z", "2026-07-05T08:20:00Z")
    assert sr.pending_m5_closes(state, "BTCUSD", b) == []


def test_empty_bars_returns_empty():
    assert sr.pending_m5_closes({}, "XAUUSD", []) == []


def test_catchup_capped_and_keeps_newest():
    state = {"symbols": {"X": {"last_m5_close_ts": "2026-07-05T00:00:00Z"}}}
    b = bars(*[f"2026-07-05T01:{m:02d}:00Z" for m in range(0, 50, 5)])
    pending = sr.pending_m5_closes(state, "X", b, max_catchup=6)
    assert len(pending) == 6
    assert pending[-1] == len(b) - 1
    assert pending == sorted(pending)


def test_completed_by_excludes_forming_context():
    close_epoch = sr._iso_to_epoch("2026-07-05T08:15:00Z") + sr.M5_BAR_SEC  # m5 bar 08:15 closes 08:20
    assert sr._completed_by("2026-07-05T08:00:00Z", close_epoch, 15)  # m15 08:00 closed 08:15
    assert not sr._completed_by("2026-07-05T08:15:00Z", close_epoch, 15)  # m15 08:15 closes 08:30
    assert sr._completed_by("2026-07-05T07:00:00Z", close_epoch, 60)  # h1 07:00 closed 08:00
    assert not sr._completed_by("2026-07-05T08:00:00Z", close_epoch, 60)  # h1 08:00 closes 09:00


def _fake_clock(now_epoch: float):
    """Stand-in for the ``datetime`` class inside shadow_runner: fixed now(),
    real strptime/fromisoformat/fromtimestamp so _iso_to_epoch keeps working."""
    import datetime as _dt

    class FakeClock:
        strptime = _dt.datetime.strptime
        fromisoformat = _dt.datetime.fromisoformat
        fromtimestamp = _dt.datetime.fromtimestamp

        @staticmethod
        def now(tz=None):
            return _dt.datetime.fromtimestamp(now_epoch, tz=_dt.timezone.utc)

    return FakeClock


def test_fetch_fresh_m5_retries_until_expected_bar_arrives(monkeypatch):
    stale = bars("2026-07-05T08:05:00Z", "2026-07-05T08:10:00Z")
    fresh = bars("2026-07-05T08:10:00Z", "2026-07-05T08:15:00Z")
    now_epoch = sr._iso_to_epoch("2026-07-05T08:20:30Z")  # bar 08:15 must exist by now
    FakeClock = _fake_clock(now_epoch)

    calls = {"n": 0}

    class FakeMcp:
        def get_trendbars(self, symbol, period, count):
            calls["n"] += 1
            return list(stale if calls["n"] < 3 else fresh)

    monkeypatch.setattr(sr, "datetime", FakeClock)
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    out = sr.fetch_fresh_m5(FakeMcp(), "BTCUSD", count=2)
    assert out[-1]["ts"] == "2026-07-05T08:15:00Z"
    assert calls["n"] == 3


def test_fetch_fresh_m5_skips_retries_for_idle_market(monkeypatch):
    friday = bars("2026-07-03T20:55:00Z", "2026-07-03T21:00:00Z")  # weekend-stale XAU
    now_epoch = sr._iso_to_epoch("2026-07-05T08:20:30Z")
    FakeClock = _fake_clock(now_epoch)

    calls = {"n": 0}

    class FakeMcp:
        def get_trendbars(self, symbol, period, count):
            calls["n"] += 1
            return list(friday)

    monkeypatch.setattr(sr, "datetime", FakeClock)
    monkeypatch.setattr(sr.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not sleep")))
    out = sr.fetch_fresh_m5(FakeMcp(), "XAUUSD", count=2)
    assert out == friday
    assert calls["n"] == 1  # no retry burn on closed markets

"""Pins dexter3/xaudaily.py — the low-frequency XAU day-open-bias lane
(owner order 2026-08-07). Entry-bar gating, day-open anchor, sidedness,
geometry, off-by-default flag, runner isolation."""
from __future__ import annotations

import pytest

from dexter3 import xaudaily as xd


def _bar(ts, o, h, l, c, v=20.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _day(n=180, start_px=3300.0, step=0.0, day="2026-08-07"):
    """n M5 bars from 00:00Z with CONSTANT true range 2.0 -> ATR14(RMA)=2.0."""
    bars, px = [], start_px
    for i in range(n):
        ts = f"{day}T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"
        o = px
        c = px + step
        bars.append(_bar(ts, o, max(o, c) + (2.0 - abs(step)) / 2,
                         min(o, c) - (2.0 - abs(step)) / 2, c))
        px = c
    return bars


def test_only_the_entry_bar_decides():
    bars = _day(157, step=+0.4)            # last bar opens 13:00Z
    assert bars[-1]["ts"] == "2026-08-07T13:00:00Z"
    assert xd.decide_xaudaily("XAUUSD", bars, 0.3).action == "enter"
    short = _day(156, step=+0.4)           # last bar opens 12:55Z
    d = xd.decide_xaudaily("XAUUSD", short, 0.3)
    assert d.action == "skip" and "not_entry_bar" in d.reasons[0]


def test_flat_vs_day_open_is_a_skip_not_a_coin_flip():
    bars = _day(157, step=0.0)             # close == the 00Z open exactly
    d = xd.decide_xaudaily("XAUUSD", bars, 0.3)
    assert d.action == "skip" and "flat_vs_day_open" in d.reasons[0]


def test_is_entry_bar_matches_hour_and_minute_only():
    assert xd.is_entry_bar("2026-08-07T13:00:00Z", 13) is True
    assert xd.is_entry_bar("2026-08-07T13:05:00Z", 13) is False
    assert xd.is_entry_bar("2026-08-07T14:00:00Z", 13) is False
    assert xd.is_entry_bar("", 13) is False
    assert xd.is_entry_bar("garbage", 13) is False


def test_side_follows_the_day_open_bias():
    up = _day(157, step=+0.4)              # closes far above the 00Z open
    d = xd.decide_xaudaily("XAUUSD", up, 0.3)
    assert d.action == "enter" and d.side == "buy"
    dn = _day(157, step=-0.4)
    d2 = xd.decide_xaudaily("XAUUSD", dn, 0.3)
    assert d2.action == "enter" and d2.side == "sell"


def test_geometry_is_wide_stop_far_target(monkeypatch):
    bars = _day(157, step=+0.4)
    d = xd.decide_xaudaily("XAUUSD", bars, 0.3)
    atr = xd._atr_rma(bars, 14)
    assert atr == pytest.approx(2.0, abs=1e-6)
    assert d.entry - d.sl == pytest.approx(2.0 * atr, abs=1e-3)   # SL 2.0xATR
    assert d.tp - d.entry == pytest.approx(4.0 * atr, abs=1e-3)   # TP 4.0xATR
    monkeypatch.setenv(xd.ENV_SL_ATR, "1.5")
    monkeypatch.setenv(xd.ENV_TP_ATR, "3.0")
    d2 = xd.decide_xaudaily("XAUUSD", bars, 0.3)
    assert d2.entry - d2.sl == pytest.approx(1.5 * atr, abs=1e-3)
    assert d2.tp - d2.entry == pytest.approx(3.0 * atr, abs=1e-3)


def test_missing_day_open_anchor_skips_rather_than_guessing():
    # prefix starts at 09:00Z of the same day -> the 00Z anchor is absent
    bars = _day(157, step=+0.4)[108:]
    assert bars[-1]["ts"] == "2026-08-07T13:00:00Z"
    d = xd.decide_xaudaily("XAUUSD", bars, 0.3)
    assert d.action == "skip" and "no_day_open_anchor" in d.reasons[0]


def test_day_open_price_picks_the_first_bar_of_that_day():
    bars = _day(157, step=+0.4)
    assert xd.day_open_price(bars, "2026-08-07", 0) == pytest.approx(3300.0)
    assert xd.day_open_price(bars, "2026-08-06", 0) is None


def test_entry_hour_is_configurable(monkeypatch):
    monkeypatch.setenv(xd.ENV_ENTRY_HOUR, "14")
    bars = _day(157, step=+0.4)            # 13:00Z bar
    assert xd.decide_xaudaily("XAUUSD", bars, 0.3).action == "skip"
    bars14 = _day(169, step=+0.4)          # 14:00Z bar
    assert bars14[-1]["ts"] == "2026-08-07T14:00:00Z"
    assert xd.decide_xaudaily("XAUUSD", bars14, 0.3).action == "enter"


def test_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert xd.xaudaily_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "xaudaily")
    assert xd.xaudaily_mode_enabled() is True


def test_label_isolation_and_runner_resolvers(monkeypatch):
    from dexter3.executor import label_matches_family

    assert xd.XAUDAILY_LABEL == "dexter3:xaudaily:canary"
    assert label_matches_family(xd.XAUDAILY_LABEL, "dexter3:xaudaily") is True
    assert label_matches_family(xd.XAUDAILY_LABEL, "dexter3:fable") is False
    assert label_matches_family("dexter3:mscalp:canary", "dexter3:xaudaily") is False

    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_MODE", "xaudaily")
    assert sr._active_order_label() == "dexter3:xaudaily:canary"
    assert sr._active_label_family() == "dexter3:xaudaily"
    assert sr._active_state_file().name == "dexter3_xaudaily_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_xaudaily_shadow.log"
    assert sr._xaudaily_producer_enabled() is True
    assert sr._mscalp_producer_enabled() is False
    assert sr._alt_producer_enabled() is True      # needs the deep 340-bar fetch


def test_lane_tally_family():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:xaudaily:canary") == "xaudaily"

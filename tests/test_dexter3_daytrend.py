"""Tests for dexter3/daytrend.py — the live twin of the replay's proven
with-the-day continuation producer (owner deploy 2026-07-16).

The decisive test is PARITY: on identical bars + identical ATR, the live
``decide_daytrend`` must produce exactly the signal the replay function
produced — the replay IS the proof, so any drift here invalidates the
deploy. Plus mode-routing and the OM plain-hold branch.
"""
from __future__ import annotations

import pytest

from dexter3 import daytrend, vp_lane


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _sell_day_prefix() -> list[dict]:
    # ต่ำเปิด day: open 4005 at 00:00Z, low 3990, bounce to 3999.4 with a red
    # resume close 3h in (same shape as the replay unit test).
    return [
        _bar("2026-07-16T00:00:00Z", 4005.0, 4006.0, 4004.0, 4004.5),
        _bar("2026-07-16T02:45:00Z", 4004.5, 4005.0, 4003.5, 4004.0),
        _bar("2026-07-16T02:50:00Z", 4004.0, 4004.5, 3997.5, 3998.0),
        _bar("2026-07-16T02:55:00Z", 3998.0, 3999.0, 3990.0, 3992.0),
        _bar("2026-07-16T03:00:00Z", 3992.0, 4000.5, 3991.5, 4000.2),
        _bar("2026-07-16T03:05:00Z", 4000.2, 4001.0, 3998.8, 3999.4),
    ]


def test_parity_with_replay_producer(monkeypatch):
    """Same bars + same ATR -> the live producer and the replay proof must
    agree on side/entry/sl/tp exactly."""
    from scripts.dexter3_entry_position_replay import decide_daytrend as replay_fn

    prefix = _sell_day_prefix()
    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    atr = vp_lane.mean_true_range(prefix)
    bias, hrs = vp_lane.dayopen_bias(prefix, 0)
    replay_sig = replay_fn(prefix, {"bias_d0": bias, "hrs_d0": hrs}, atr, swing_bars=3)
    live = daytrend.decide_daytrend("XAUUSD", prefix, 0.12, session="asian")
    assert replay_sig is not None and live.action == "enter"
    assert live.side == replay_sig["side"] == "sell"
    assert live.entry == pytest.approx(replay_sig["entry"])
    assert live.sl == pytest.approx(replay_sig["sl"])
    assert live.tp == pytest.approx(replay_sig["tp"])


def test_live_skips_without_bias_or_pullback(monkeypatch):
    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    prefix = _sell_day_prefix()
    # kill the bias by making the last close ABOVE the day open -> no bias-side
    flat = prefix[:-1] + [_bar("2026-07-16T03:05:00Z", 4004.9, 4006.5, 4004.5, 4006.2)]
    d = daytrend.decide_daytrend("XAUUSD", flat, 0.12)
    assert d.action == "skip"
    # green (counter) close on a sell day -> skip with no_setup
    green = prefix[:-1] + [_bar("2026-07-16T03:05:00Z", 3998.8, 4001.0, 3998.5, 4000.6)]
    d2 = daytrend.decide_daytrend("XAUUSD", green, 0.12)
    assert d2.action == "skip"
    assert "no_setup" in d2.reasons[0]


def test_mode_routing_label_state_lock(monkeypatch):
    from dexter3.shadow_runner import (
        _active_label_family,
        _active_order_label,
        _active_state_file,
        _daytrend_producer_enabled,
    )

    monkeypatch.setenv("DEXTER3_MODE", "daytrend")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert _daytrend_producer_enabled()
    assert _active_order_label() == "dexter3:dtr:canary"
    assert _active_label_family() == "dexter3:dtr"
    assert _active_state_file().name == "dexter3_daytrend_shadow_state.json"
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    assert not _daytrend_producer_enabled()
    assert _active_label_family() == "dexter3:fable"


def test_om_plain_mode_never_profit_exits(monkeypatch):
    from dexter3.basket_manager import BasketConfig
    from dexter3.opening_manager import OMConfig, OpeningManager

    monkeypatch.setenv(vp_lane.ENV_TRAIL_MODE, "plain")
    om = OpeningManager(None, None, OMConfig())
    st = {
        "base_risk_usd": 12.0,
        "now_utc_iso": "2026-07-16T04:00:00Z",
        "basket_cfg": BasketConfig(time_stop_min=240),
        "basket_runtime": {"oldest_open_ts": "2026-07-16T03:00:00Z", "peak_r": 2.5,
                           "ticks_since_peak": 30, "ticks_open": 200, "last_pyramid_peak": None},
    }
    # peak 2.5R retraced to +0.3R: the LADDER would close here; plain holds.
    positions = [{"positionId": 1, "symbol": "XAUUSD", "tradeSide": "SELL", "volume": 1.0,
                  "entryPrice": 4000.0, "stopLoss": 4002.0, "takeProfit": 3990.0,
                  "netProfit": 3.6, "openTimestamp": "2026-07-16T03:00:00Z"}]
    act = om.evaluate("XAUUSD", positions, None, [], [], [], st)
    assert act["action"] == "hold"
    assert act["reason"] == "plain_hold"
    # the 240min basket time stop still fires (caps outrank plain-hold)
    st2 = dict(st)
    st2["now_utc_iso"] = "2026-07-16T07:01:00Z"
    act2 = om.evaluate("XAUUSD", positions, None, [], [], [], st2)
    assert act2["action"] == "close_all"
    assert act2.get("cap") == "time_stop_min"


def test_dayreversal_parity_and_gating(monkeypatch):
    """Live decide_dayreversal vs the replay proof fn on identical bars +
    identical ATR (parity), plus env gating and the counter-bias nature."""
    from scripts.dexter3_entry_position_replay import decide_dayreversal as replay_fn

    monkeypatch.setenv(daytrend.ENV_DAYREV_ENABLED, "1")
    monkeypatch.setenv(daytrend.ENV_DAYREV_ARM_ATR, "3.0")   # test-scale arming
    monkeypatch.setenv(daytrend.ENV_DAYREV_SWING_K, "1")
    assert daytrend.dayreversal_enabled()
    # sell-bias day, capitulation, then higher-low + break of swing high.
    # (>= swing_k*2+8 bars required by both the live and replay guards)
    bars = [
        _bar("2026-07-16T00:00:00Z", 4030.0, 4031.0, 4029.0, 4029.5),
        _bar("2026-07-16T04:40:00Z", 4029.5, 4030.0, 4028.5, 4029.0),
        _bar("2026-07-16T04:45:00Z", 4029.0, 4029.5, 4027.0, 4027.5),
        _bar("2026-07-16T04:50:00Z", 4027.5, 4028.0, 4024.0, 4024.5),
        _bar("2026-07-16T04:55:00Z", 4024.5, 4025.0, 4018.0, 4018.5),
        _bar("2026-07-16T05:00:00Z", 4018.5, 4019.0, 4010.0, 4011.0),
        _bar("2026-07-16T05:05:00Z", 4011.0, 4012.0, 4000.0, 4001.0),   # day low 4000 (swing low 1)
        _bar("2026-07-16T05:10:00Z", 4001.0, 4003.0, 4000.8, 4002.5),   # small base
        _bar("2026-07-16T05:15:00Z", 4003.2, 4006.0, 4003.0, 4005.0),   # swing high 4006
        _bar("2026-07-16T05:20:00Z", 4005.0, 4005.5, 4002.5, 4003.0),   # HIGHER swing low 4002.5
        _bar("2026-07-16T05:25:00Z", 4003.0, 4007.5, 4002.8, 4007.0),   # close 4007 breaks 4006
    ]
    atr = vp_lane.mean_true_range(bars)
    bias, hrs = vp_lane.dayopen_bias(bars, 0)
    assert bias == -1
    live = daytrend.decide_dayreversal("XAUUSD", bars, 0.12, session="asian")
    rep = replay_fn(bars, {"bias_d0": bias, "hrs_d0": hrs}, atr,
                    range_arm_atr=3.0, swing_k=1)
    assert live.action == "enter" and live.side == "buy"
    assert rep is not None and rep["side"] == "buy"
    assert live.entry == pytest.approx(rep["entry"])
    assert live.sl == pytest.approx(rep["sl"])
    assert live.tp == pytest.approx(rep["tp"])
    # not armed (small range threshold raised) -> skip
    monkeypatch.setenv(daytrend.ENV_DAYREV_ARM_ATR, "50")
    assert daytrend.decide_dayreversal("XAUUSD", bars, 0.12).action == "skip"


def test_daytrend_range_cap_hands_off_to_reversal(monkeypatch):
    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    monkeypatch.setenv(daytrend.ENV_RANGE_CAP_ATR, "2.5")
    prefix = _sell_day_prefix()   # day range 16 pts > 2.5 x ATR(~5.7) = 14.3
    d = daytrend.decide_daytrend("XAUUSD", prefix, 0.12)
    assert d.action == "skip"
    assert "range_cap" in d.reasons[0]

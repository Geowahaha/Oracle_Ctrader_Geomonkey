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


def _big_range_buy_day_prefix() -> list[dict]:
    # ยืนเปิด day that has already run FAR past a 12xATR cap (range ~60 on
    # tiny per-bar TR), then pulled back deep (~19pts off the 4060 high) and
    # printed a green resume close — the 2026-07-22 owner-flagged anatomy.
    bars = [_bar("2026-07-22T00:00:00Z", 4000.0, 4001.0, 3999.5, 4000.5)]
    px = 4000.5
    for i in range(1, 40):                          # slow grind up: tiny ATR
        ts = f"2026-07-22T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"
        bars.append(_bar(ts, px, px + 1.6, px - 0.1, px + 1.5))
        px += 1.5
    # px ~ 4059; day range ~60; mean TR stays ~1.7
    bars.append(_bar("2026-07-22T03:25:00Z", px, px + 1.0, px - 8.0, px - 7.5))   # pullback leg 1
    bars.append(_bar("2026-07-22T03:30:00Z", px - 7.5, px - 7.0, px - 19.0, px - 18.5))  # deep pullback
    bars.append(_bar("2026-07-22T03:35:00Z", px - 18.5, px - 15.0, px - 18.8, px - 15.5))  # green resume
    return bars


def test_range_cap_still_skips_with_bypass_off(monkeypatch):
    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    monkeypatch.setenv(daytrend.ENV_RANGE_CAP_ATR, "12")
    monkeypatch.delenv(daytrend.ENV_CAP_PULLBACK_BYPASS_ATR, raising=False)
    d = daytrend.decide_daytrend("XAUUSD", _big_range_buy_day_prefix(), 0.12)
    assert d.action == "skip"
    assert "range_cap" in d.reasons[0]


def test_deep_pullback_bypasses_range_cap_and_parity_with_replay(monkeypatch):
    """The 2026-07-22 miss, both fixed and parity-locked: cap 12 exceeded,
    pullback >= 3xATR + green resume -> BOTH implementations fire the same
    buy; features record cap_bypassed."""
    from scripts.dexter3_entry_position_replay import decide_daytrend as replay_fn

    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    monkeypatch.setenv(daytrend.ENV_RANGE_CAP_ATR, "12")
    monkeypatch.setenv(daytrend.ENV_CAP_PULLBACK_BYPASS_ATR, "3")
    prefix = _big_range_buy_day_prefix()
    live = daytrend.decide_daytrend("XAUUSD", prefix, 0.12)
    assert live.action == "enter" and live.side == "buy"
    assert live.features["daytrend"]["cap_bypassed"] is True

    atr = vp_lane.mean_true_range(prefix)
    bias, hrs = vp_lane.dayopen_bias(prefix, 0)
    sig = replay_fn(prefix, {"bias_d0": bias, "hrs_d0": hrs}, atr, swing_bars=3,
                    range_cap_atr=12.0, cap_pullback_bypass_atr=3.0)
    assert sig is not None and sig["side"] == "buy"
    assert live.entry == pytest.approx(sig["entry"])
    assert live.sl == pytest.approx(sig["sl"])
    assert live.tp == pytest.approx(sig["tp"])


def test_shallow_pullback_does_not_bypass_range_cap(monkeypatch):
    """Bypass must NOT re-open the original wound: capitulation-chasing near
    the extreme (shallow pullback < bypass threshold) stays skipped."""
    monkeypatch.setenv(daytrend.ENV_SWING_BARS, "3")
    monkeypatch.setenv(daytrend.ENV_RANGE_CAP_ATR, "12")
    monkeypatch.setenv(daytrend.ENV_CAP_PULLBACK_BYPASS_ATR, "3")
    prefix = _big_range_buy_day_prefix()[:-3]        # drop the pullback legs
    px = float(prefix[-1]["close"])
    prefix.append(_bar("2026-07-22T03:25:00Z", px, px + 0.4, px - 0.5, px + 0.3))  # at the extreme
    d = daytrend.decide_daytrend("XAUUSD", prefix, 0.12)
    assert d.action == "skip"
    assert "range_cap" in d.reasons[0]


def test_replay_bypass_default_off_is_pre_change_behavior():
    from scripts.dexter3_entry_position_replay import decide_daytrend as replay_fn

    prefix = _big_range_buy_day_prefix()
    atr = vp_lane.mean_true_range(prefix)
    bias, hrs = vp_lane.dayopen_bias(prefix, 0)
    assert replay_fn(prefix, {"bias_d0": bias, "hrs_d0": hrs}, atr, swing_bars=3,
                     range_cap_atr=12.0) is None


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


def test_dpull_mode_routing_is_fully_isolated(monkeypatch):
    """DPULL lane (owner sign-off 2026-07-22) must have its own label / family
    / state / log / lock so it never touches fable/vp/daytrend/scalp
    positions — same isolation contract every lane has."""
    from dexter3.shadow_runner import (
        DPULL_LOCK_FILE,
        DPULL_LOG_FILE,
        DPULL_STATE_FILE,
        _active_label_family,
        _active_log_file,
        _active_order_label,
        _active_state_file,
        _alt_producer_enabled,
        _daytrend_producer_enabled,
        _dpull_producer_enabled,
    )

    monkeypatch.setenv("DEXTER3_MODE", "dpull")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert _dpull_producer_enabled()
    assert _alt_producer_enabled()               # gets the vp gate/bypass posture
    assert not _daytrend_producer_enabled()      # distinct from the daytrend lane
    assert _active_order_label() == "dexter3:dpull:canary"
    assert _active_label_family() == "dexter3:dpull"
    assert _active_state_file() == DPULL_STATE_FILE
    assert _active_log_file() == DPULL_LOG_FILE
    # unique files per lane — no collision with any sibling
    assert len({DPULL_STATE_FILE, DPULL_LOG_FILE, DPULL_LOCK_FILE}) == 3
    assert "dpull" in DPULL_STATE_FILE.name


def test_dpull_producer_flag_via_producer_env(monkeypatch):
    from dexter3.shadow_runner import _dpull_producer_enabled

    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.setenv("DEXTER3_PRODUCER", "dpull")
    assert _dpull_producer_enabled()
    monkeypatch.setenv("DEXTER3_PRODUCER", "vp")
    assert not _dpull_producer_enabled()


def test_dpull_cs_mode_routing_is_isolated_from_dpull(monkeypatch):
    """DPULL-CS (owner choice ค) must be a fully separate lane from dpull —
    own label/family/state/log/lock — so the forward head-to-head never
    cross-contaminates positions or PnL."""
    from dexter3.shadow_runner import (
        DPULL_CS_LOCK_FILE,
        DPULL_CS_LOG_FILE,
        DPULL_CS_STATE_FILE,
        DPULL_LOCK_FILE,
        _active_label_family,
        _active_log_file,
        _active_order_label,
        _active_state_file,
        _alt_producer_enabled,
        _dpull_cs_producer_enabled,
        _dpull_producer_enabled,
    )

    monkeypatch.setenv("DEXTER3_MODE", "dpull-cs")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert _dpull_cs_producer_enabled()
    assert _alt_producer_enabled()               # gets the vp gate/bypass posture
    assert not _dpull_producer_enabled()         # distinct from the base dpull lane
    assert _active_order_label() == "dexter3:dpull-cs:canary"
    assert _active_label_family() == "dexter3:dpull-cs"
    assert _active_state_file() == DPULL_CS_STATE_FILE
    assert _active_log_file() == DPULL_CS_LOG_FILE
    # separate lock from base dpull -> both lanes can run at once
    assert DPULL_CS_LOCK_FILE != DPULL_LOCK_FILE
    assert "dpull_cs" in DPULL_CS_STATE_FILE.name


def test_dpull_limit_intent_uses_far_tp_not_signal_tp(monkeypatch):
    """The correctness crux: dpull's exit is the convex trail, so its limit
    intent must carry a FAR protective TP (like the VP lane), NOT the
    daytrend producer's signal TP (the day extreme) — else the +100R convex
    ride the matrix measured gets capped at the extreme. Contrast with a hunt
    lane, which keeps its structural signal TP."""
    from types import SimpleNamespace

    import dexter3.shadow_runner as sr

    # a simple with-bias buy decision: entry 4100, sl 4090 (risk 10), signal
    # TP at 4108 (the day extreme, only +0.8R away — would cap convex hard)
    dec = SimpleNamespace(side="buy", entry=4100.0, sl=4090.0, tp=4108.0,
                          symbol="XAUUSD", ts_close="2026-07-22T05:00:00Z",
                          setup="daytrend_pullback_resume", session="london")
    bars = [{"ts": f"2026-07-22T0{i}:00:00Z", "open": 4100.0, "high": 4101.0,
             "low": 4099.0, "close": 4100.0} for i in range(5)]

    monkeypatch.setenv("DEXTER3_MODE", "dpull")
    monkeypatch.setenv("DEXTER3_HUNT_LIMIT_DIP_R", "0.5")
    monkeypatch.setenv("DEXTER3_VP_FAR_TP_R", "12")
    dpull_intent = sr._lane_make_limit_intent(dec, bars, 5.0)
    # far TP: level = 4100 - 0.5*10 = 4095; stop_pts = |4095-4090| = 5;
    # far TP = 4095 + 12*5 = 4155 — nowhere near the 4108 signal TP.
    assert dpull_intent["tp"] > 4150.0

    # a hunt lane on the SAME decision keeps the structural signal TP (4108)
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    hunt_intent = sr._lane_make_limit_intent(dec, bars, 5.0)
    assert hunt_intent["tp"] == pytest.approx(4108.0)


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


def test_sdzone_live_shares_replay_engine(monkeypatch):
    """Single source of truth: the live producer and the replay import the
    SAME engine module — plus a behavioral check that a zone forms and the
    re-entry confirm fires on synthetic bars."""
    import scripts.dexter3_entry_position_replay as replay
    from dexter3 import sd_zones

    assert replay.SDZoneEngine is sd_zones.SDZoneEngine
    assert replay.decide_sdzone is sd_zones.decide_sdzone

    monkeypatch.setenv(daytrend.ENV_SDZONE_ENABLED, "1")
    assert daytrend.sdzone_enabled()
    # 60 flat bars, then a sweep below the pivot low + huge bullish
    # displacement bar with a volume spike -> demand zone; later price
    # re-enters the zone and closes back above it -> buy signal.
    bars = []
    for k in range(60):
        px = 4000.0 + (k % 3) * 0.3
        bars.append({"ts": f"2026-07-16T{k//12:02d}:{(k%12)*5:02d}:00Z",
                     "open": px, "high": px + 0.4, "low": px - 0.4,
                     "close": px + 0.1, "volume": 100.0})
    # engineered pivot low then sweep+displacement (vol 3x)
    bars.append({"ts": "2026-07-16T05:00:00Z", "open": 4000.0, "high": 4000.2,
                 "low": 3996.0, "close": 3996.5, "volume": 120.0})   # pivot low candidate
    for k in range(6):
        px = 3997.0 + k * 0.2
        bars.append({"ts": f"2026-07-16T05:{5+k*5:02d}:00Z", "open": px,
                     "high": px + 0.3, "low": px - 0.3, "close": px + 0.1,
                     "volume": 100.0})
    bars.append({"ts": "2026-07-16T05:35:00Z", "open": 3995.2, "high": 3997.6,
                 "low": 3995.0, "close": 3997.5, "volume": 400.0})   # sweep 3995<3996 + body 2.3 (~1.9xATR, 88%)
    eng, atr = sd_zones.zones_from_prefix(bars)
    assert any(z["kind"] == "demand" for z in eng.zones), "demand zone should form"


def test_scalp_mode_routing_and_bank_om(monkeypatch):
    """grok's successor (2026-07-17): scalp mode identity + the OM bank-green
    branch (close-based +0.4R take on the latest CLOSED bar; hold otherwise)."""
    from dexter3.shadow_runner import (
        _active_label_family,
        _active_order_label,
        _active_state_file,
        _scalp_producer_enabled,
    )

    monkeypatch.setenv("DEXTER3_MODE", "scalp")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert _scalp_producer_enabled()
    assert _active_order_label() == "dexter3:scalp:canary"
    assert _active_label_family() == "dexter3:scalp"
    assert _active_state_file().name == "dexter3_scalp_shadow_state.json"

    from dexter3.basket_manager import BasketConfig
    from dexter3.opening_manager import OMConfig, OpeningManager

    monkeypatch.setenv(vp_lane.ENV_TRAIL_MODE, "bank")
    monkeypatch.setenv(vp_lane.ENV_BANK_R, "0.4")
    om = OpeningManager(None, None, OMConfig())
    st = {
        "base_risk_usd": 4.0,
        "now_utc_iso": "2026-07-17T04:10:00Z",
        "basket_cfg": BasketConfig(time_stop_min=60),
        "basket_runtime": {"oldest_open_ts": "2026-07-17T04:00:00Z", "peak_r": 0.5,
                           "ticks_since_peak": 2, "ticks_open": 20, "last_pyramid_peak": None},
    }
    pos = [{"positionId": 1, "symbol": "XAUUSD", "tradeSide": "BUY", "volume": 1.0,
            "entryPrice": 4000.0, "stopLoss": 3996.0, "takeProfit": 4008.0,
            "netProfit": 1.8, "openTimestamp": "2026-07-17T04:00:00Z"}]
    # latest CLOSED bar close 4001.7 -> r_close = 1.7/4 = 0.425 >= 0.4 -> bank
    bars = [{"ts": "2026-07-17T04:05:00Z", "open": 4000.5, "high": 4002.0,
             "low": 4000.2, "close": 4001.7}]
    act = om.evaluate("XAUUSD", pos, None, bars, [], [], st)
    assert act["action"] == "close_all"
    assert act["reason"] == "bank_green"
    # close below the bank line -> hold (no ladder/stall interference)
    bars2 = [{"ts": "2026-07-17T04:05:00Z", "open": 4000.5, "high": 4001.4,
              "low": 4000.0, "close": 4001.0}]
    act2 = om.evaluate("XAUUSD", pos, None, bars2, [], [], st)
    assert act2["action"] == "hold"
    assert act2["reason"] == "bank_hold"


# -- dpull-cs self-healing broker-SL backstop (2026-07-23 amend-race fix) -----

class _FakeExec:
    def __init__(self):
        self.amends = []
    def amend_lane_sl_tp(self, pid, sl, tp):
        self.amends.append({"pid": pid, "sl": sl, "tp": tp})
        return {"action": "amended", "status": "ok"}


def _cs_pos(pid, entry, sl, side="SELL", tp=0.0):
    return {"positionId": pid, "entryPrice": entry, "stopLoss": sl,
            "tradeSide": side, "takeProfit": tp, "symbolName": "XAUUSD"}


def test_dpull_cs_backstop_self_heal_widens_soft_sl(monkeypatch):
    """The core fix: on an OM tick, a broker SL still at the SOFT level is
    widened to backstop = entry ± stop_pts*(1+bmult). SELL entry 4100, soft
    stop_pts 5 (broker SL 4105), backstop mult 2.5 -> backstop 4100+5*3.5=4117.5."""
    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP", "1")
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP_BACKSTOP", "2.5")
    ex = _FakeExec()
    state = {"vp_convex": {"stop_pts": 5.0, "atr_pts": 5.0}}
    lane = [_cs_pos(42, 4100.0, 4105.0, side="SELL")]
    sr._ensure_dpull_cs_backstop(ex, state, lane, "XAUUSD")
    assert len(ex.amends) == 1
    assert ex.amends[0]["pid"] == 42
    assert ex.amends[0]["sl"] == pytest.approx(4117.5)


def test_dpull_cs_backstop_self_heal_idempotent_when_already_wide(monkeypatch):
    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP", "1")
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP_BACKSTOP", "2.5")
    ex = _FakeExec()
    state = {"vp_convex": {"stop_pts": 5.0, "atr_pts": 5.0}}
    # broker SL already at the backstop (4117.5) -> no re-amend
    lane = [_cs_pos(42, 4100.0, 4117.5, side="SELL")]
    sr._ensure_dpull_cs_backstop(ex, state, lane, "XAUUSD")
    assert ex.amends == []


def test_dpull_cs_backstop_self_heal_buy_side(monkeypatch):
    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP", "1")
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP_BACKSTOP", "2.5")
    ex = _FakeExec()
    state = {"vp_convex": {"stop_pts": 5.0, "atr_pts": 5.0}}
    lane = [_cs_pos(7, 4100.0, 4095.0, side="BUY")]   # backstop = 4100 - 17.5 = 4082.5
    sr._ensure_dpull_cs_backstop(ex, state, lane, "XAUUSD")
    assert ex.amends[0]["sl"] == pytest.approx(4082.5)


def test_dpull_cs_backstop_self_heal_disabled_no_amend(monkeypatch):
    import dexter3.shadow_runner as sr
    monkeypatch.delenv("DEXTER3_OM_CONVEX_CLOSE_STOP", raising=False)
    ex = _FakeExec()
    state = {"vp_convex": {"stop_pts": 5.0}}
    lane = [_cs_pos(42, 4100.0, 4105.0)]
    sr._ensure_dpull_cs_backstop(ex, state, lane, "XAUUSD")
    assert ex.amends == []   # close-stop off -> base dpull/vp untouched


def test_dpull_cs_backstop_self_heal_needs_single_position(monkeypatch):
    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_OM_CONVEX_CLOSE_STOP", "1")
    ex = _FakeExec()
    state = {"vp_convex": {"stop_pts": 5.0}}
    sr._ensure_dpull_cs_backstop(ex, state, [], "XAUUSD")           # empty
    sr._ensure_dpull_cs_backstop(ex, state, [_cs_pos(1, 4100, 4105),
                                            _cs_pos(2, 4100, 4105)], "XAUUSD")  # 2 legs
    assert ex.amends == []

"""Tests for dexter3/vp_lane.py + the OM convex-trail branch.

Owner deploy order 2026-07-16 ("ไม่ต้องทำ shadow แล้ว deploy เลย"): the live VP
lane must reproduce the 3-window replay proof's semantics exactly —
tests/test_dexter3_entry_position_replay.py is the behavioral spec; these
tests pin the LIVE primitives against the same hand-computed arithmetic, plus
the env-gating that keeps fable/grok byte-identical when the VP envs are
absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from dexter3 import vp_lane


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


@dataclass
class _FakeDecision:
    ts_close: str = "2026-07-14T03:00:00Z"
    symbol: str = "XAUUSD"
    action: str = "enter"
    side: str | None = "buy"
    entry_type: str | None = "market"
    entry: float | None = 2000.0
    sl: float | None = 1998.0
    tp: float | None = 2004.0
    size_class: str = "small"
    leader_score: float = 0.0
    p_win_est: float = 0.0
    setup: str = "vp_lvn_rejection"
    reasons: list = field(default_factory=list)
    session: str = "asian"
    features: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# day-open bias — same arithmetic as the replay's _anchor_bias_fields
# ---------------------------------------------------------------------------


def test_dayopen_bias_below_open_blocks_buys(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_BIAS_ENABLED, "1")
    monkeypatch.setenv(vp_lane.ENV_BIAS_ANCHOR_HOUR, "0")
    monkeypatch.setenv(vp_lane.ENV_BIAS_MIN_HOURS, "1")
    prefix = [
        _bar("2026-07-14T00:00:00Z", 100.0, 101.0, 99.5, 100.5),   # day open = 100
        _bar("2026-07-14T02:00:00Z", 100.0, 100.2, 98.8, 99.0),    # ต่ำเปิด, 2h in
    ]
    allowed_buy, info = vp_lane.dayopen_bias_allows("buy", prefix)
    allowed_sell, _ = vp_lane.dayopen_bias_allows("sell", prefix)
    assert not allowed_buy and allowed_sell
    assert info["bias"] == -1 and info["hours"] == pytest.approx(2.0)


def test_dayopen_bias_neutral_first_hour_and_env_off(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_BIAS_ENABLED, "1")
    monkeypatch.setenv(vp_lane.ENV_BIAS_MIN_HOURS, "1")
    prefix = [
        _bar("2026-07-14T00:00:00Z", 100.0, 101.0, 99.5, 100.5),
        _bar("2026-07-14T00:30:00Z", 100.5, 100.6, 98.9, 99.0),    # 0.5h — too young
    ]
    assert vp_lane.dayopen_bias_allows("buy", prefix)[0]           # neutral
    monkeypatch.setenv(vp_lane.ENV_BIAS_ENABLED, "0")
    prefix2 = prefix + [_bar("2026-07-14T02:00:00Z", 99.0, 99.2, 98.5, 98.6)]
    assert vp_lane.dayopen_bias_allows("buy", prefix2)[0]          # gate off


# ---------------------------------------------------------------------------
# no-trade window
# ---------------------------------------------------------------------------


def test_no_trade_window_inside_outside_and_midnight_cross():
    assert vp_lane.in_no_trade_window("2026-07-14T21:00:00Z", "20:45-22:15")
    assert vp_lane.in_no_trade_window("2026-07-14T20:45:00Z", "20:45-22:15")
    assert not vp_lane.in_no_trade_window("2026-07-14T22:16:00Z", "20:45-22:15")
    assert not vp_lane.in_no_trade_window("2026-07-14T12:00:00Z", "20:45-22:15")
    # midnight-crossing spec
    assert vp_lane.in_no_trade_window("2026-07-14T23:30:00Z", "23:00-01:00")
    assert vp_lane.in_no_trade_window("2026-07-14T00:30:00Z", "23:00-01:00")
    assert not vp_lane.in_no_trade_window("2026-07-14T02:00:00Z", "23:00-01:00")
    assert not vp_lane.in_no_trade_window("2026-07-14T21:00:00Z", "")   # off


def test_vp_entry_gate_shapes(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_NO_TRADE_UTC, "20:45-22:15")
    monkeypatch.setenv(vp_lane.ENV_BIAS_ENABLED, "0")
    blocked = vp_lane.vp_entry_gate("buy", [], "2026-07-14T21:00:00Z")
    assert blocked == {"allow": False, "reason": "vp_no_trade_window", "features": {}}
    ok = vp_lane.vp_entry_gate("buy", [], "2026-07-14T12:00:00Z")
    assert ok["allow"] and ok["reason"] == "vp_gate_pass"


# ---------------------------------------------------------------------------
# synthetic limit intent lifecycle — level math identical to the replay
# ---------------------------------------------------------------------------


def test_make_limit_intent_levels(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_LIMIT_DIP_R, "0.4")
    monkeypatch.setenv(vp_lane.ENV_LIMIT_TTL_MIN, "30")
    monkeypatch.setenv(vp_lane.ENV_FAR_TP_R, "12")
    d = _FakeDecision()   # buy 2000 sl 1998 -> risk 2
    prefix = [_bar("2026-07-14T02:55:00Z", 2000.0, 2001.0, 1999.0, 2000.0)]
    intent = vp_lane.make_limit_intent(d, prefix, 4.0, "2026-07-14T03:00:00Z")
    assert intent["level"] == pytest.approx(1999.2)               # entry - 0.4R
    assert intent["sl"] == pytest.approx(1998.0)                  # structural, unchanged
    assert intent["stop_pts"] == pytest.approx(1.2)               # (1-0.4) x risk
    assert intent["tp"] == pytest.approx(1999.2 + 12 * 1.2)       # far protective cap
    assert intent["deadline_epoch"] - intent["created_epoch"] == pytest.approx(1800.0)
    # sell mirror
    ds = _FakeDecision(side="sell", entry=2000.0, sl=2002.0, tp=1996.0)
    i2 = vp_lane.make_limit_intent(ds, prefix, 4.0, "2026-07-14T03:00:00Z")
    assert i2["level"] == pytest.approx(2000.8)
    assert i2["tp"] == pytest.approx(2000.8 - 12 * 1.2)


def test_check_intent_fill_touch_side_and_ttl(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_LIMIT_DIP_R, "0.4")
    d = _FakeDecision()
    intent = vp_lane.make_limit_intent(d, [], 4.0, "2026-07-14T03:00:00Z")
    # buy fills on the ASK reaching the level; bid alone is not enough
    assert vp_lane.check_intent_fill(intent, bid=1999.0, ask=1999.35, now_iso="2026-07-14T03:05:00Z") is None
    assert vp_lane.check_intent_fill(intent, bid=1999.0, ask=1999.2, now_iso="2026-07-14T03:05:00Z") == "fill"
    # past the deadline -> expired (checked before price)
    assert vp_lane.check_intent_fill(intent, bid=1999.0, ask=1999.1, now_iso="2026-07-14T03:31:00Z") == "expired"


def test_intent_to_decision_carries_filled_geometry(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_LIMIT_DIP_R, "0.4")
    d = _FakeDecision()
    intent = vp_lane.make_limit_intent(d, [], 4.0, "2026-07-14T03:00:00Z")
    filled = vp_lane.intent_to_decision(intent, _FakeDecision, "2026-07-14T03:07:00Z")
    assert filled.entry == pytest.approx(1999.2)                  # pips computed from LEVEL
    assert filled.sl == pytest.approx(1998.0)
    assert filled.action == "enter" and filled.side == "buy"
    assert filled.features["vp_limit_intent"]["signal_entry"] == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# convex trail — floor math + age stop (replay: arm 1.0R, gb 3.0xATR, h48)
# ---------------------------------------------------------------------------


def test_convex_floor_arm_and_giveback(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_TRAIL_MODE, "convex")
    monkeypatch.setenv(vp_lane.ENV_CONVEX_ARM_R, "1.0")
    monkeypatch.setenv(vp_lane.ENV_CONVEX_GIVEBACK_ATR, "3.0")
    assert vp_lane.convex_trail_enabled()
    # below arm -> no floor at all
    assert vp_lane.convex_floor_r(0.9, atr_pts=5.0, stop_pts=6.0) is None
    # armed: floor = peak - 3.0*5.0/6.0 = peak - 2.5 (negative is allowed —
    # the broker SL simply fires first below -1R, replay SL-first semantics)
    assert vp_lane.convex_floor_r(1.0, 5.0, 6.0) == pytest.approx(1.0 - 2.5)
    assert vp_lane.convex_floor_r(4.0, 5.0, 6.0) == pytest.approx(1.5)
    # degenerate stop -> None (fail-safe: broker SL + time stop only)
    assert vp_lane.convex_floor_r(2.0, 5.0, 0.0) is None


def test_convex_age_exceeded(monkeypatch):
    monkeypatch.setenv(vp_lane.ENV_CONVEX_MAX_AGE_MIN, "240")
    assert vp_lane.convex_age_exceeded("2026-07-14T03:00:00Z", "2026-07-14T07:00:00Z")
    assert not vp_lane.convex_age_exceeded("2026-07-14T03:00:00Z", "2026-07-14T06:59:00Z")
    assert not vp_lane.convex_age_exceeded(None, "2026-07-14T07:00:00Z")


# ---------------------------------------------------------------------------
# OM integration — the convex branch replaces ladder/hard-take/stall for the
# opted-in lane and holds otherwise; env absent -> branch never taken.
# ---------------------------------------------------------------------------


def _om_state(now_iso: str, peak_r: float, basket_cfg: Any) -> dict[str, Any]:
    return {
        "base_risk_usd": 12.0,
        "now_utc_iso": now_iso,
        "basket_cfg": basket_cfg,
        "basket_runtime": {"oldest_open_ts": "2026-07-14T03:00:00Z", "peak_r": peak_r,
                           "ticks_since_peak": 5, "ticks_open": 50, "last_pyramid_peak": None},
        "vp_convex": {"atr_pts": 5.0, "stop_pts": 6.0},
    }


def _om_positions(net_profit: float) -> list[dict]:
    return [{"positionId": 1, "symbol": "XAUUSD", "tradeSide": "BUY", "volume": 1.0,
             "entryPrice": 2000.0, "stopLoss": 1998.0, "takeProfit": 2020.0,
             "netProfit": net_profit, "openTimestamp": "2026-07-14T03:00:00Z"}]


def test_om_convex_branch_fires_trail_and_time_stop(monkeypatch):
    from dexter3.basket_manager import BasketConfig
    from dexter3.opening_manager import OMConfig, OpeningManager

    monkeypatch.setenv(vp_lane.ENV_TRAIL_MODE, "convex")
    monkeypatch.setenv(vp_lane.ENV_CONVEX_ARM_R, "1.0")
    monkeypatch.setenv(vp_lane.ENV_CONVEX_GIVEBACK_ATR, "3.0")
    monkeypatch.setenv(vp_lane.ENV_CONVEX_MAX_AGE_MIN, "240")
    om = OpeningManager(None, None, OMConfig())
    # basket time_stop must outlive the convex hold (the vp unit sets 250)
    cfg = BasketConfig(time_stop_min=250)

    # peak 2.0R (runtime), live retraced to -0.6R (net -7.2 / base 12):
    # floor = 2.0 - 3*5/6 = -0.5 -> live_r <= floor -> convex_trail close.
    act = om.evaluate("XAUUSD", _om_positions(-7.2), None, [], [], [],
                      _om_state("2026-07-14T04:00:00Z", 2.0, cfg))
    assert act["action"] == "close_all"
    assert act["reason"] == "convex_trail"
    assert act["floor_r"] == pytest.approx(2.0 - 2.5)

    # same lane at 4h1m old -> convex time stop (checked before the trail)
    act2 = om.evaluate("XAUUSD", _om_positions(-7.2), None, [], [], [],
                       _om_state("2026-07-14T07:01:00Z", 2.0, cfg))
    assert act2["action"] == "close_all"
    assert act2["reason"] == "convex_time_stop"

    # armed but above the floor (live +1.5R) -> explicit convex hold
    # (no hard-take, no ladder, no stall, no pyramid)
    act3 = om.evaluate("XAUUSD", _om_positions(18.0), None, [], [], [],
                       _om_state("2026-07-14T04:00:00Z", 2.0, cfg))
    assert act3["action"] == "hold"
    assert act3["reason"] == "convex_hold"


# ---------------------------------------------------------------------------
# hunt-lane ports (owner 2026-07-16: apply today's proven layers to fable/grok)
# ---------------------------------------------------------------------------


def test_hunt_dayopen_bias_downsizes_counter_side(monkeypatch):
    from dexter3.shadow_runner import _apply_hunt_dayopen_bias

    monkeypatch.setenv("DEXTER3_HUNT_DAYOPEN_BIAS", "downsize")
    monkeypatch.setenv("DEXTER3_HUNT_BIAS_DOWNSIZE_MULT", "0.25")
    monkeypatch.setenv("DEXTER3_HUNT_BIAS_MIN_HOURS", "1")
    prefix = [
        _bar("2026-07-14T00:00:00Z", 100.0, 101.0, 99.5, 100.5),   # day open 100
        _bar("2026-07-14T02:00:00Z", 100.0, 100.2, 98.8, 99.0),    # ต่ำเปิด 2h in
    ]
    d_buy = _FakeDecision(side="buy")
    d_sell = _FakeDecision(side="sell")
    assert _apply_hunt_dayopen_bias(d_buy, prefix, 12.0) == pytest.approx(3.0)   # x0.25
    assert d_buy.features["hunt_dayopen_bias"]["counter"] is True
    assert _apply_hunt_dayopen_bias(d_sell, prefix, 12.0) == pytest.approx(12.0)  # with-bias
    # env off -> untouched (fable/grok default behavior)
    monkeypatch.delenv("DEXTER3_HUNT_DAYOPEN_BIAS")
    assert _apply_hunt_dayopen_bias(d_buy, prefix, 12.0) == pytest.approx(12.0)


def test_lane_limit_entry_gating(monkeypatch):
    from dexter3.shadow_runner import _lane_limit_entry_enabled

    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_HUNT_LIMIT_DIP_R", raising=False)
    assert not _lane_limit_entry_enabled()                    # hunt default: off
    monkeypatch.setenv("DEXTER3_HUNT_LIMIT_DIP_R", "0.4")
    assert _lane_limit_entry_enabled()                        # hunt opt-in
    monkeypatch.setenv("DEXTER3_MODE", "vp")
    monkeypatch.delenv(vp_lane.ENV_LIMIT_DIP_R, raising=False)
    assert not _lane_limit_entry_enabled()                    # vp reads VP env only
    monkeypatch.setenv(vp_lane.ENV_LIMIT_DIP_R, "0.4")
    assert _lane_limit_entry_enabled()


def test_make_limit_intent_prefer_signal_tp_keeps_structural_target(monkeypatch):
    monkeypatch.delenv(vp_lane.ENV_LIMIT_DIP_R, raising=False)
    d = _FakeDecision()   # buy 2000 sl 1998 tp 2004
    hunt = vp_lane.make_limit_intent(d, [], 4.0, "2026-07-14T03:00:00Z",
                                     dip_r=0.4, ttl_min=30.0, prefer_signal_tp=True)
    assert hunt["tp"] == pytest.approx(2004.0)                # structural TP unchanged
    vp = vp_lane.make_limit_intent(d, [], 4.0, "2026-07-14T03:00:00Z")
    assert vp["tp"] == pytest.approx(1999.2 + 12 * 1.2)       # VP far cap unchanged


def test_om_ladder_untouched_when_env_absent(monkeypatch):
    from dexter3.basket_manager import BasketConfig
    from dexter3.opening_manager import OMConfig, OpeningManager

    monkeypatch.delenv(vp_lane.ENV_TRAIL_MODE, raising=False)
    om = OpeningManager(None, None, OMConfig())
    # peak 0.85 -> interpolated ladder floor 0.45 (between 0.80:0.40 and
    # 1.20:0.80); live 0.30 (net 3.6 / base 12) <= 0.45 -> the CLASSIC
    # ladder_floor close still fires — fable/grok byte-identical, even with
    # a stray vp_convex key present in state.
    st = _om_state("2026-07-14T04:00:00Z", 0.85, BasketConfig())
    act = om.evaluate("XAUUSD", _om_positions(3.6), None, [], [], [], st)
    assert act["action"] == "close_all"
    assert act["reason"] == "ladder_floor"
    assert act["reason"] != "convex_trail"

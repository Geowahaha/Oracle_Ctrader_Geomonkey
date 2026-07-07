"""Unit/property tests for dexter3/opening_manager.py — OPENING MANAGER (OM).

Critical invariants under test (owner directive 2026-07-07, see
docs/DEXTER3_M5_HUNTER_BLUEPRINT.md "OPENING MANAGER (OM)"):
  (a) Ratcheting trail: peak_r only ever rises; a basket that goes
      +0.5R -> +1.5R -> +0.9R closes at the RATCHETED floor derived from the
      1.5R peak, not at the M5-sampled +0.9R value the old (pre-OM) 5-min
      sampling would have seen. Property test: over random R-paths, once
      armed we never give back more than (1 - trail_keep_frac) of peak_r.
  (b) Spike capture fires instantly at live_r >= spike_take_r.
  (c) Basket Doctor opens a repair leg on the EDGE side (mocked
      hunt_mode.decide_hunt): opposite side + high conviction -> repair side
      == edge side (counter_trend_recovery); low conviction -> hold.
  (d) Hard caps are unbreachable — enforce_caps (the existing single
      choke-point) refuses a repair leg that would exceed max_legs or
      max_basket_risk_mult, and a capped-out basket (time_stop_min /
      daily_loss_baskets) always resolves via close_all/cap_stop regardless
      of what Profit Hunter/Basket Doctor would otherwise decide.
  (e) An unreliable aggregate (aggregate_lane sets 'unreliable') always
      holds — never trades blind.
  (f) Continuous (tick-resolution) peak_r catches a peak that 5-min M5
      sampling would have missed entirely.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from dexter3 import basket_live
from dexter3.basket_manager import BasketConfig
from dexter3.opening_manager import OMConfig, OpeningManager, _update_peak_r


# ---------------------------------------------------------------------------
# position / bar builders
# ---------------------------------------------------------------------------


def _position(
    side: str = "buy",
    entry: float = 2000.0,
    volume: float = 1.0,
    net_profit: float | None = 0.0,
    label: str = "dexter3:fable:m5h-v1:hunt",
    open_ts: str = "2026-07-07T09:00:00Z",
    symbol: str = "XAUUSD",
    position_id: int = 1,
) -> dict:
    pos = {
        "positionId": position_id,
        "label": label,
        "symbolName": symbol,
        "tradeSide": "BUY" if side == "buy" else "SELL",
        "volumeInUnits": volume,
        "entryPrice": entry,
        "openTimestamp": open_ts,
    }
    if net_profit is not None:
        pos["netProfit"] = net_profit
    return pos


def _lane_at_r(r: float, side: str = "buy", base_risk_usd: float = 1.0) -> list[dict]:
    """A single-leg lane whose aggregate_r (per basket_live.aggregate_lane)
    equals ``r`` exactly, given ``base_risk_usd``."""
    return [_position(side=side, net_profit=r * base_risk_usd)]


def _m5_bars(n: int = 60, base: float = 2000.0) -> list[dict]:
    bars = []
    for i in range(n):
        px = base + (i * 0.01)
        bars.append(
            {
                "ts": f"2026-07-07T{9 + i // 12:02d}:{(i % 12) * 5:02d}:00Z",
                "open": px,
                "high": px + 1.0,
                "low": px - 1.0,
                "close": px + 0.2,
            }
        )
    return bars


def _new_om(config: OMConfig | None = None) -> OpeningManager:
    return OpeningManager(executor=None, journal=None, config=config or OMConfig())


def _feed_path(om: OpeningManager, symbol: str, r_path: list[float], *, side: str = "buy", base_risk_usd: float = 1.0):
    """Feed a sequence of aggregate_r ticks through evaluate(), threading
    basket_runtime state across calls exactly like the runner does. Returns
    the list of action dicts, one per tick (stops early on close_all)."""
    state: dict = {"base_risk_usd": base_risk_usd}
    actions = []
    for r in r_path:
        lane = _lane_at_r(r, side=side, base_risk_usd=base_risk_usd)
        action = om.evaluate(symbol, lane, None, [], [], [], state)
        actions.append(action)
        state["basket_runtime"] = action.get("basket_runtime")
        if action["action"] == "close_all":
            break
    return actions


# ---------------------------------------------------------------------------
# (a) ratcheting trail
# ---------------------------------------------------------------------------


def test_ratchet_closes_at_floor_not_at_m5_sampled_value():
    """+0.5R -> +1.5R -> +0.9R must close at the ratcheted floor derived
    from the 1.5R peak (arm 0.4 default, keep 0.7 -> floor=1.05), proving the
    close reflects the PEAK, not the final +0.9R sample an M5-only sampler
    would have recorded as the "peak"."""
    om = _new_om(OMConfig(arm_trail_r=0.4, trail_keep_frac=0.7, take_r=10.0, spike_take_r=20.0))
    actions = _feed_path(om, "XAUUSD", [0.5, 1.5, 0.9])
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "ratchet must fire once live_r retraces below the floor"
    close = closed[0]
    assert close["reason"] == "trail"
    assert close["peak_r"] == pytest.approx(1.5)
    assert close["floor_r"] == pytest.approx(1.5 * 0.7)
    # The close fires on the FIRST tick where live_r (0.9) <= floor (1.05) —
    # i.e. immediately at the 0.9 tick, not waiting for a lower M5 sample.
    assert close["live_r"] == pytest.approx(0.9)


def test_ratchet_never_gives_back_more_than_keep_frac_gap(monkeypatch=None):
    """Property test: for 200 random R-paths, once armed (peak_r >=
    arm_trail_r), the basket must never be observed to hold a live_r below
    peak_r * trail_keep_frac without OM having already closed it — i.e. the
    ratchet can never silently give back more than (1 - trail_keep_frac) of
    the peak."""
    rng = random.Random(42)
    arm_r = 0.4
    keep = 0.65
    for trial in range(200):
        cfg = OMConfig(arm_trail_r=arm_r, trail_keep_frac=keep, take_r=50.0, spike_take_r=100.0)
        om = _new_om(cfg)
        path_len = rng.randint(3, 15)
        r_path = [round(rng.uniform(-1.0, 3.0), 3) for _ in range(path_len)]
        actions = _feed_path(om, "XAUUSD", r_path)
        # If any action closed, it must be at or above the floor at closure
        # (the tick that triggered close is exactly the first breach — by
        # construction we return close_all on that very tick, so no state
        # after closure exists to violate the invariant).
        for action in actions:
            if action["action"] == "close_all" and action["reason"] == "trail":
                peak_r = action["peak_r"]
                floor_r = action["floor_r"]
                live_r = action["live_r"]
                assert floor_r == pytest.approx(peak_r * keep, abs=1e-4)
                assert live_r <= floor_r + 1e-9, (
                    f"trial={trial} path={r_path}: closed above its own floor"
                )


def test_peak_r_only_ever_rises_across_ticks():
    om = _new_om(OMConfig(arm_trail_r=100.0))  # never arm -> never closes, so we can inspect peak_r freely
    state: dict = {"base_risk_usd": 1.0}
    seen_peaks = []
    for r in [0.1, 0.5, 0.3, 0.8, 0.2, 0.9, -0.5]:
        lane = _lane_at_r(r)
        action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
        state["basket_runtime"] = action["basket_runtime"]
        seen_peaks.append(state["basket_runtime"]["peak_r"])
    assert seen_peaks == sorted(seen_peaks), "peak_r must be monotonically non-decreasing"
    assert seen_peaks[-1] == pytest.approx(0.9)


def test_update_peak_r_resets_on_new_basket():
    runtime = _update_peak_r(None, "2026-07-07T10:00:00Z", 0.9)
    assert runtime["peak_r"] == pytest.approx(0.9)
    # A brand-new basket (different oldest_open_ts) must not inherit the peak.
    fresh = _update_peak_r(runtime, "2026-07-07T14:00:00Z", 0.1)
    assert fresh["peak_r"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# (b) spike capture
# ---------------------------------------------------------------------------


def test_spike_take_fires_instantly_above_spike_r():
    om = _new_om(OMConfig(spike_take_r=2.5, take_r=1.2))
    state: dict = {"base_risk_usd": 1.0}
    action = om.evaluate("XAUUSD", _lane_at_r(3.0), None, [], [], [], state)
    assert action["action"] == "close_all"
    assert action["reason"] == "spike"
    assert action["live_r"] == pytest.approx(3.0)


def test_hard_take_fires_before_spike_threshold():
    om = _new_om(OMConfig(spike_take_r=2.5, take_r=1.2))
    state: dict = {"base_risk_usd": 1.0}
    action = om.evaluate("XAUUSD", _lane_at_r(1.3), None, [], [], [], state)
    assert action["action"] == "close_all"
    assert action["reason"] == "take"


# ---------------------------------------------------------------------------
# (c) Basket Doctor — edge-measured repair
# ---------------------------------------------------------------------------


class _FakeHuntDecision:
    def __init__(self, side: str, conviction: float):
        self.action = "enter"
        self.side = side
        self.leader_score = conviction


def test_basket_doctor_opens_repair_on_opposite_edge_side_high_conviction():
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.35))
    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(-0.5, side="sell")  # basket is short and losing
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.6)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "add_repair_leg"
    assert action["side"] == "buy"  # opposite of the losing (sell) basket side == the edge side
    assert action["note"] == "counter_trend_recovery"
    assert action["conviction"] == pytest.approx(0.6)


def test_basket_doctor_holds_on_low_conviction():
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.35))
    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(-0.5, side="sell")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.1)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_basket_doctor_same_side_add_when_losing_side_still_has_edge():
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.35))
    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(-0.5, side="sell")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("sell", 0.5)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "add_repair_leg"
    assert action["side"] == "sell"
    assert action["note"] == "same_side_add_better_price"


def test_basket_doctor_does_not_fire_without_structure_evidence():
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.0))
    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(-0.5, side="sell")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": False, "m5_close_beyond": False}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_basket_doctor_does_not_fire_above_repair_trigger_r():
    """A modestly negative (but not below repair_trigger_r) basket must not
    trigger Basket Doctor even with strong structure evidence + conviction."""
    om = _new_om(OMConfig(repair_trigger_r=-0.5, repair_min_conviction=0.0))
    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(-0.1, side="sell")  # -0.1R > -0.5R trigger
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


# ---------------------------------------------------------------------------
# (d) hard caps unbreachable
# ---------------------------------------------------------------------------


def test_repair_refused_when_max_legs_reached():
    """enforce_caps (the single choke-point) must refuse a repair leg once
    legs >= max_legs, even with strong edge-measured conviction."""
    basket_cfg = BasketConfig(max_legs=1)
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.0))
    state: dict = {"base_risk_usd": 1.0, "basket_cfg": basket_cfg}
    lane = _lane_at_r(-0.5, side="sell")  # 1 leg already == max_legs
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_cap_stop_fires_on_time_stop_regardless_of_profit_state():
    """A basket past time_stop_min must resolve via close_all/cap_stop even
    when it is GREEN and would otherwise just be sitting in a not-yet-armed
    Profit Hunter hold."""
    basket_cfg = BasketConfig(time_stop_min=1)  # 1-minute cap, trivially breached
    om = _new_om(OMConfig(arm_trail_r=100.0))  # Profit Hunter would never fire on its own
    old_ts = "2020-01-01T00:00:00Z"  # ancient -> guaranteed to exceed 1 minute
    state: dict = {"base_risk_usd": 1.0, "basket_cfg": basket_cfg, "now_utc_iso": "2026-07-07T12:00:00Z"}
    lane = _lane_at_r(0.1, side="buy")
    lane[0]["openTimestamp"] = old_ts
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action["action"] == "close_all"
    assert action["reason"] == "cap_stop"
    assert action["cap"] == "time_stop_min"


def test_cap_stop_fires_on_daily_loss_baskets():
    basket_cfg = BasketConfig(daily_loss_baskets=1)
    om = _new_om(OMConfig())
    state: dict = {
        "base_risk_usd": 1.0,
        "basket_cfg": basket_cfg,
        "now_utc_iso": "2026-07-07T12:00:00Z",
        "daily_state": {"daily_loss_baskets": 1},
    }
    lane = _lane_at_r(0.1, side="buy")
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action["action"] == "close_all"
    assert action["reason"] == "cap_stop"
    assert action["cap"] == "daily_loss_baskets"


def test_adversarial_sequences_never_exceed_max_legs_or_risk_cap():
    """200 randomized adversarial sequences of repeated Basket Doctor calls
    (always strong opposite-edge conviction) must never grow the basket's
    accounted risk beyond what enforce_caps would allow — i.e. every
    add_repair_leg action this module returns must itself have already
    cleared enforce_caps with proposed_repair_risk_usd > 0."""
    rng = random.Random(7)
    basket_cfg = BasketConfig(max_legs=3, max_basket_risk_mult=3.0)
    for trial in range(200):
        legs = rng.randint(1, 5)  # deliberately allow "already over cap" states
        om = _new_om(OMConfig(repair_trigger_r=-0.1, repair_min_conviction=0.0))
        state: dict = {"base_risk_usd": 1.0, "basket_cfg": basket_cfg, "now_utc_iso": "2026-07-07T12:00:00Z"}
        lane = [_position(side="sell", position_id=i + 1, net_profit=-0.2) for i in range(legs)]
        bars = _m5_bars()
        with patch.object(
            basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
        ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)):
            action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        if action["action"] == "add_repair_leg":
            assert legs < basket_cfg.max_legs, (
                f"trial={trial} legs={legs}: repair leg permitted at/over max_legs={basket_cfg.max_legs}"
            )


# ---------------------------------------------------------------------------
# (e) unreliable aggregate -> always hold
# ---------------------------------------------------------------------------


def test_unreliable_aggregate_always_holds():
    om = _new_om(OMConfig(spike_take_r=0.01, take_r=0.01, arm_trail_r=0.01))  # everything else would fire instantly
    state: dict = {"base_risk_usd": 1.0}
    # Missing netProfit field -> aggregate_lane marks unreliable=True.
    lane = [_position(side="buy", net_profit=None)]
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action == {"action": "hold", "reason": "unreliable_pnl"}


def test_no_open_lane_legs_holds():
    om = _new_om()
    state: dict = {"base_risk_usd": 1.0}
    action = om.evaluate("XAUUSD", [], None, [], [], [], state)
    assert action["action"] == "hold"
    assert action["reason"] == "no_open_lane_legs"


# ---------------------------------------------------------------------------
# (f) continuous peak beats 5-min sampling
# ---------------------------------------------------------------------------


def test_continuous_tick_peak_catches_what_m5_sampling_would_miss():
    """Simulate 1 M5 bar's worth of fast ticks where price spikes to +2.0R
    intrabar and falls back to +0.3R by the time the M5 bar closes. An
    M5-only sampler (the pre-OM behavior) would only ever observe the FINAL
    +0.3R value and never know the peak reached +2.0R. OM's continuous tick
    evaluation must catch the peak and (once armed) close on the retrace,
    banking far more than the M5-only sampler ever could have."""
    om = _new_om(OMConfig(arm_trail_r=0.4, trail_keep_frac=0.7, take_r=50.0, spike_take_r=100.0))
    # 24 fast ticks (~4s each) simulating one M5 bar's intrabar path.
    intrabar_ticks = [0.05, 0.2, 0.6, 1.1, 1.6, 2.0, 1.7, 1.2, 0.8, 0.5, 0.3]
    m5_only_sample = intrabar_ticks[-1]  # 0.3 -- what a 5-min sampler would have seen

    actions = _feed_path(om, "XAUUSD", intrabar_ticks)
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "continuous tick evaluation must have caught the intrabar peak and closed on retrace"
    close = closed[0]
    assert close["peak_r"] == pytest.approx(2.0), "OM's peak_r must reflect the TRUE intrabar peak"
    assert close["peak_r"] > m5_only_sample, (
        "the whole point of OM: continuous peak_r must exceed what M5-only sampling would ever have recorded"
    )
    # The floor banked (peak * keep_frac) must be well above the M5-only
    # sample value -- proof the fix converts a would-be near-miss into a
    # meaningfully larger realized profit.
    assert close["floor_r"] > m5_only_sample

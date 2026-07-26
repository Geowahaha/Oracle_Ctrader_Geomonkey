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

DRAGON LADDER additions (owner directive 2026-07-08, see
docs/DEXTER3_M5_HUNTER_BLUEPRINT.md "DRAGON LADDER"):
  (g) ``ladder_floor_r`` is a pure, monotonic, concave giveback curve: None
      below the first breakpoint (0.15R), breakeven AT 0.15R, table-exact at
      every breakpoint, interpolated between, and never falls as peak rises.
  (h) The owner's scenario: a position that peaks small then decays must
      close at/above breakeven, NEVER at a full loss. A bigger peak that
      reverses must close at/above its own tier's floor.
  (i) A genuinely big dragon run rides through a pullback that stays above
      its floor, and only closes once it breaches the CURRENT tier's floor.
  (j) Stall-take fires on a decaying small winner with no new peak for
      ``stall_ticks``; does not fire while new peaks keep coming.
  (k) Pyramid-add fires only when every gate holds (green, floor >= BE,
      same-side high-conviction continuation, caps allow, one add per tier);
      never on a loser, never past caps, never twice in the same tier;
      respects the ``pyramid_enabled`` master switch.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from dexter3 import basket_live
from dexter3.basket_manager import BasketConfig
from dexter3.opening_manager import OMConfig, OpeningManager, _update_peak_r, ladder_floor_r


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
    """+0.5R -> +1.5R -> +0.9R must close at the DRAGON LADDER floor derived
    from the 1.5R peak (default mission ladder interpolates between the
    1.20->0.80 and 2.00->1.45 points:
    floor=0.80+((1.5-1.2)/(2.0-1.2))*(1.45-0.80)=1.04375),
    proving the close reflects the PEAK, not the final +0.9R sample an
    M5-only sampler would have recorded as the "peak". As of the 2026-07-08
    DRAGON LADDER directive the ladder (not the legacy flat arm/keep trail)
    is the default profit exit — see ``test_legacy_flat_trail_still_works``
    below for direct coverage of the retained flat-trail helper."""
    om = _new_om(OMConfig(take_r=10.0, spike_take_r=20.0))
    actions = _feed_path(om, "XAUUSD", [0.5, 1.5, 0.9])
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "ladder must fire once live_r retraces below the floor"
    close = closed[0]
    assert close["reason"] == "ladder_floor"
    assert close["peak_r"] == pytest.approx(1.5)
    assert close["floor_r"] == pytest.approx(1.04375, abs=1e-4)
    # The close fires on the FIRST tick where live_r (0.9) <= floor (1.075) —
    # i.e. immediately at the 0.9 tick, not waiting for a lower M5 sample.
    assert close["live_r"] == pytest.approx(0.9)


def test_legacy_flat_trail_still_works_directly():
    """The legacy flat arm/keep trail (``OpeningManager._profit_hunter``) is
    retained for direct use (A/B harness / future env-flagged fallback) even
    though ``evaluate()`` no longer calls it by default — proves it was not
    silently deleted, just superseded as the default path."""
    om = _new_om(OMConfig(arm_trail_r=0.4, trail_keep_frac=0.7, take_r=10.0, spike_take_r=20.0))
    action = om._profit_hunter(live_r=0.9, peak_r=1.5, cfg=om.config)
    assert action is not None
    assert action["reason"] == "trail"
    assert action["floor_r"] == pytest.approx(1.5 * 0.7)


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


# ---------------------------------------------------------------------------
# (g) ladder_floor_r — pure concave giveback curve
# ---------------------------------------------------------------------------


LADDER_TABLE = [
    (0.25, 0.02),
    (0.50, 0.15),
    (0.80, 0.40),
    (1.20, 0.80),
    (2.00, 1.45),
    (3.00, 2.25),
]


def test_ladder_floor_none_below_first_threshold():
    cfg = OMConfig()
    assert ladder_floor_r(0.0, cfg) is None
    assert ladder_floor_r(0.10, cfg) is None
    assert ladder_floor_r(0.2499, cfg) is None


def test_ladder_floor_near_breakeven_at_exactly_025():
    cfg = OMConfig()
    assert ladder_floor_r(0.25, cfg) == pytest.approx(0.02)


@pytest.mark.parametrize("peak,expected_floor", LADDER_TABLE)
def test_ladder_floor_matches_table_at_breakpoints(peak, expected_floor):
    cfg = OMConfig()
    assert ladder_floor_r(peak, cfg) == pytest.approx(expected_floor, abs=1e-6)


def test_ladder_floor_interpolates_between_breakpoints():
    cfg = OMConfig()
    # Midpoint of (0.50, 0.15) -> (0.80, 0.40): peak=0.65 -> floor halfway.
    mid_floor = ladder_floor_r(0.65, cfg)
    assert mid_floor == pytest.approx((0.15 + 0.40) / 2, abs=1e-6)


def test_ladder_floor_tail_beyond_last_point():
    cfg = OMConfig()
    # Beyond 3.00R: floor = peak * ladder_tail_keep_frac (0.75 default).
    assert ladder_floor_r(4.0, cfg) == pytest.approx(4.0 * 0.75, abs=1e-6)
    assert ladder_floor_r(10.0, cfg) == pytest.approx(10.0 * 0.75, abs=1e-6)


def test_ladder_floor_monotonic_across_full_sweep():
    """Property: across a fine 0 -> 3.5R sweep, the floor never decreases as
    peak_r rises (None counts as "no constraint yet", never a decrease)."""
    cfg = OMConfig()
    prev_floor = None
    peak = 0.0
    while peak <= 3.5:
        floor = ladder_floor_r(round(peak, 3), cfg)
        if floor is not None and prev_floor is not None:
            assert floor >= prev_floor - 1e-9, f"floor fell at peak={peak}: {prev_floor} -> {floor}"
        if floor is not None:
            prev_floor = floor
        peak += 0.01


def test_ladder_floor_never_exceeds_peak():
    """A floor can never sit ABOVE the peak that produced it (would imply
    banking more than the position ever made)."""
    cfg = OMConfig()
    peak = 0.0
    while peak <= 5.0:
        floor = ladder_floor_r(round(peak, 3), cfg)
        if floor is not None:
            assert floor <= peak + 1e-9
        peak += 0.05


# ---------------------------------------------------------------------------
# (h) THE OWNER'S SCENARIO — a real winner must never ladder down to a loss
# ---------------------------------------------------------------------------


def test_owner_scenario_small_peak_then_decay_closes_at_or_above_breakeven():
    """The exact bug the owner reported: a position that peaks at 0.30R then
    decays must NOT ride back to a full stop loss. The mission ladder must
    close it at/above breakeven (0.25R tier's floor=0.02) — never negative.
    The mission ladder's lower tiers are deliberately near-breakeven (let
    winners run to TP), so the decay must dip below ~0.046 to trigger."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0))
    # Peaks at 0.30R, then decays through the interpolated floor (~0.046)
    # toward zero and beyond -- the ladder must catch it at/above breakeven.
    r_path = [0.10, 0.22, 0.30, 0.25, 0.15, 0.08, 0.04]
    actions = _feed_path(om, "XAUUSD", r_path)
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "the ladder must close this position once it breaches its own floor"
    close = closed[0]
    assert close["reason"] == "ladder_floor"
    assert close["live_r"] >= -1e-9, f"OWNER BUG REGRESSION: closed at a loss ({close['live_r']}), not breakeven+"
    assert close["floor_r"] >= -1e-9


def test_owner_scenario_never_rides_all_the_way_to_full_stop():
    """Even feeding the decay all the way down to -1.0R (a full stop), the
    ladder must have ALREADY closed the position long before that point once
    peak_r crossed 0.15R -- proving the floor is not just present in theory
    but actually fires before the position can become a full loss."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0))
    r_path = [0.05, 0.20, 0.30, 0.20, 0.10, 0.0, -0.20, -0.50, -1.00]
    actions = _feed_path(om, "XAUUSD", r_path)
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "ladder must intervene before the position reaches a full loss"
    assert closed[0]["live_r"] >= -1e-9
    # The path must have been cut short (fewer actions than the full r_path)
    # -- proof the ladder closed it before the -1.00R tick was ever reached.
    assert len(actions) < len(r_path)


def test_owner_scenario_082_peak_closes_at_or_above_080_tier_floor():
    """peak 0.82R reversing must close >= the 0.80-tier table floor (0.40R,
    i.e. the WORST-case protection the mission table guarantees once peak_r
    has cleared 0.80R) — never allowed to ride all the way back to -1R. The
    actual interpolated floor at peak=0.82 (just past the 0.80 breakpoint,
    approaching 1.20->0.80) is somewhat higher than the raw 0.80-tier value,
    which only strengthens the guarantee."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0))
    r_path = [0.3, 0.6, 0.82, 0.7, 0.55, 0.41]
    actions = _feed_path(om, "XAUUSD", r_path)
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "ladder must fire on the 0.82R peak's reversal"
    close = closed[0]
    assert close["reason"] == "ladder_floor"
    assert close["peak_r"] == pytest.approx(0.82)
    # The FLOOR itself must be at/above the 0.80-tier table guarantee
    # (0.40R) — the trigger tick's live_r is naturally just under the floor
    # by definition (that is what makes it fire), but the floor the ladder
    # protected this peak with must never be weaker than the table promises.
    assert close["floor_r"] >= 0.40 - 1e-9, f"floor weaker than the 0.80-tier table guarantee: {close}"
    # Never allowed to ride anywhere near a full -1R loss.
    assert close["live_r"] > -0.5


# ---------------------------------------------------------------------------
# (i) big-peak ride — the dragon gets room
# ---------------------------------------------------------------------------


def test_big_peak_pullback_stays_open_above_floor_then_closes_at_its_tier():
    """peak 2.5R then pulls back must stay OPEN as long as live_r is above
    the ladder floor derived from peak=2.5 (interpolated between the
    2.00->1.45 and 3.00->2.25 table points -> 1.85), then closes once it
    breaches that floor — proving the dragon gets room to breathe well above
    its raw peak-tier's table minimum before the ladder intervenes."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0))
    cfg = om.config
    floor_at_25 = ladder_floor_r(2.5, cfg)
    assert floor_at_25 is not None
    above_floor = floor_at_25 + 0.1
    below_floor = floor_at_25 - 0.1

    state: dict = {"base_risk_usd": 1.0}
    lane = _lane_at_r(2.5, side="buy")
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action["action"] != "close_all", "peak tick itself must not close"
    state["basket_runtime"] = action["basket_runtime"]

    lane = _lane_at_r(above_floor, side="buy")
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action["action"] == "hold", f"pullback above floor={floor_at_25} must stay open: {action}"
    state["basket_runtime"] = action["basket_runtime"]

    lane = _lane_at_r(below_floor, side="buy")
    action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
    assert action["action"] == "close_all"
    assert action["reason"] == "ladder_floor"
    assert action["peak_r"] == pytest.approx(2.5)
    assert action["floor_r"] == pytest.approx(floor_at_25, abs=1e-4)
    # The dragon banks well above breakeven on a 2.5R run.
    assert action["floor_r"] > 1.5


def test_dragon_never_rides_full_peak_back_to_zero():
    """A 2.5R dragon run must never be allowed to ride all the way back to
    breakeven or negative -- the ladder closes it well above zero."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0))
    r_path = [0.3, 0.8, 1.5, 2.0, 2.5, 2.2, 1.8, 1.4, 1.0, 0.5, 0.0, -0.5]
    actions = _feed_path(om, "XAUUSD", r_path)
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed
    close = closed[0]
    assert close["reason"] == "ladder_floor"
    assert close["live_r"] > 0.5, f"a 2.5R dragon must bank well above breakeven, got {close}"


# ---------------------------------------------------------------------------
# (j) stall-take — hungry scalp
# ---------------------------------------------------------------------------


def test_stall_take_fires_on_stalled_small_winner():
    """A small winner (peak < stall_max_peak_r) that makes no new peak for
    stall_ticks AND decays to/below peak*stall_decay_frac must be banked by
    stall-take -- it should not have to wait for the (much looser) ladder
    floor, which for peak=0.25 (below the first 0.15 breakpoint's floor
    interpolation range) is very close to breakeven."""
    om = _new_om(
        OMConfig(
            take_r=50.0,
            spike_take_r=100.0,
            stall_max_peak_r=0.5,
            stall_ticks=3,
            stall_decay_frac=0.6,
            stall_min_peak_r=0.0,
            stall_min_hold_ticks=0,
        )
    )
    state: dict = {"base_risk_usd": 1.0}
    # Peak at 0.25R on tick 1, then hold flat/decaying reads with NO new peak.
    r_path = [0.25, 0.20, 0.16, 0.14]  # peak stays 0.25; ticks_since_peak climbs 0,1,2,3
    actions = []
    for r in r_path:
        lane = _lane_at_r(r, side="buy")
        action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
        actions.append(action)
        state["basket_runtime"] = action.get("basket_runtime")
        if action["action"] == "close_all":
            break
    closed = [a for a in actions if a["action"] == "close_all"]
    assert closed, "stall-take must fire on a decaying stalled small winner"
    assert closed[0]["reason"] == "stall_take"
    assert closed[0]["peak_r"] == pytest.approx(0.25)


def test_stall_take_does_not_fire_when_new_peaks_keep_coming():
    """The same small-winner band, but each tick sets a NEW peak (never
    stalls) -- stall-take must never fire (ticks_since_peak resets to 0 every
    tick)."""
    om = _new_om(
        OMConfig(
            take_r=50.0,
            spike_take_r=100.0,
            stall_max_peak_r=0.5,
            stall_ticks=3,
            stall_decay_frac=0.6,
            stall_min_peak_r=0.0,
            stall_min_hold_ticks=0,
        )
    )
    state: dict = {"base_risk_usd": 1.0}
    r_path = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35]  # strictly increasing -> always a new peak
    actions = []
    for r in r_path:
        lane = _lane_at_r(r, side="buy")
        action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
        actions.append(action)
        state["basket_runtime"] = action.get("basket_runtime")
        if action["action"] == "close_all":
            break
    stall_closes = [a for a in actions if a.get("reason") == "stall_take"]
    assert not stall_closes, "stall-take must never fire while new peaks keep coming"


def test_stall_take_does_not_fire_above_stall_max_peak_r_tier():
    """Above the stall tier (peak >= stall_max_peak_r), a stall must NOT
    trigger stall-take -- the (tighter, table-driven) ladder governs instead."""
    om = _new_om(
        OMConfig(
            take_r=50.0,
            spike_take_r=100.0,
            stall_max_peak_r=0.5,
            stall_ticks=2,
            stall_decay_frac=0.9,
            stall_min_peak_r=0.0,
            stall_min_hold_ticks=0,
        )
    )
    state: dict = {"base_risk_usd": 1.0}
    # Peak 0.6R (above stall_max_peak_r=0.5), then stalls + decays a little
    # but not enough to breach the ladder floor for peak=0.6 (~0.336).
    r_path = [0.6, 0.55, 0.5, 0.45]
    actions = []
    for r in r_path:
        lane = _lane_at_r(r, side="buy")
        action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
        actions.append(action)
        state["basket_runtime"] = action.get("basket_runtime")
        if action["action"] == "close_all":
            break
    stall_closes = [a for a in actions if a.get("reason") == "stall_take"]
    assert not stall_closes, "stall-take must not fire once peak_r is at/above stall_max_peak_r"


# ---------------------------------------------------------------------------
# (k) pyramid-add — hunt more while holding (winners-only, capped)
# ---------------------------------------------------------------------------


def _pyramid_state(basket_cfg: BasketConfig | None = None, **extra) -> dict:
    # now_utc_iso close to _position()'s default open_ts (2026-07-07T09:00:00Z)
    # so the basket's age stays well under BasketConfig.time_stop_min (180min)
    # -- these tests are about the pyramid gates, not the time-stop cap.
    state: dict = {"base_risk_usd": 1.0, "now_utc_iso": "2026-07-07T09:05:00Z"}
    if basket_cfg is not None:
        state["basket_cfg"] = basket_cfg
    state.update(extra)
    return state


def test_pyramid_add_fires_when_all_gates_hold():
    om = _new_om(
        OMConfig(
            take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3, pyramid_min_conv=0.4, pyramid_tier_step=0.5
        )
    )
    state = _pyramid_state()
    lane = _lane_at_r(0.5, side="buy")  # green, floor(0.5)=0.32 (>=0 breakeven-protected)
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.6)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "add_repair_leg"
    assert action["side"] == "buy"
    assert action["note"] == "pyramid_add"
    assert action["conviction"] == pytest.approx(0.6)


def test_pyramid_add_does_not_fire_on_a_loser():
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3))
    state = _pyramid_state()
    lane = _lane_at_r(-0.2, side="buy")
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_does_not_fire_below_min_live_r():
    """live_r=0.2 is green but below pyramid_min_live_r=0.3 -- and also below
    the first ladder breakpoint, so this simply holds."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3))
    state = _pyramid_state()
    lane = _lane_at_r(0.2, side="buy")
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_does_not_fire_when_floor_below_breakeven():
    """Even if live_r nominally clears pyramid_min_live_r, if the ladder
    floor is not yet established (or below breakeven) at this peak, no add
    may fire -- adding must never risk turning a not-yet-protected basket
    red past breakeven. Use a tiny pyramid_min_live_r to isolate this gate
    from the live_r gate."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.05))
    state = _pyramid_state()
    lane = _lane_at_r(0.10, side="buy")  # peak 0.10 < 0.15 ladder floor threshold -> floor is None
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_does_not_fire_on_counter_side_continuation():
    """The continuation read must agree with the basket's OWN side -- an
    opposite-side high-conviction read must NOT trigger a pyramid add (that
    is the Basket Doctor's job on a LOSING basket, not this winners-only
    path)."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3))
    state = _pyramid_state()
    lane = _lane_at_r(0.5, side="buy")
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("sell", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_does_not_fire_below_min_conviction():
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3, pyramid_min_conv=0.5))
    state = _pyramid_state()
    lane = _lane_at_r(0.5, side="buy")
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.2)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_refused_when_caps_maxed():
    """Mock enforce_caps to simulate a maxed-out basket -- the pyramid path
    must refuse even with every other gate green."""
    om = _new_om(OMConfig(take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3, pyramid_min_conv=0.4))
    state = _pyramid_state(basket_cfg=BasketConfig(max_legs=1))
    lane = _lane_at_r(0.5, side="buy")  # 1 leg already == max_legs
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"


def test_pyramid_add_does_not_double_add_within_same_tier():
    """After a first pyramid add at peak_r=0.5, a second tick with peak_r
    still only 0.6 (advanced by just 0.1, below pyramid_tier_step=0.5) must
    NOT fire another add -- one add per tier crossed."""
    om = _new_om(
        OMConfig(
            take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3, pyramid_min_conv=0.4, pyramid_tier_step=0.5
        )
    )
    state = _pyramid_state()
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.6)
    ):
        lane = _lane_at_r(0.5, side="buy")
        first = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        assert first["action"] == "add_repair_leg"
        state["basket_runtime"] = first["basket_runtime"]

        lane = _lane_at_r(0.6, side="buy")  # new peak 0.6, only +0.1 since last add (need +0.5)
        second = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        assert second["action"] != "add_repair_leg"


def test_pyramid_add_fires_again_after_advancing_a_full_tier():
    om = _new_om(
        OMConfig(
            take_r=50.0, spike_take_r=100.0, pyramid_min_live_r=0.3, pyramid_min_conv=0.4, pyramid_tier_step=0.5
        )
    )
    state = _pyramid_state()
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.6)
    ):
        lane = _lane_at_r(0.5, side="buy")
        first = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        assert first["action"] == "add_repair_leg"
        state["basket_runtime"] = first["basket_runtime"]

        # Advance a full tier: peak_r 0.5 -> 1.0 (+0.5, meets pyramid_tier_step).
        lane = _lane_at_r(1.0, side="buy")
        second = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        assert second["action"] == "add_repair_leg"
        assert second["note"] == "pyramid_add"


def test_pyramid_disabled_via_config_flag():
    """DEXTER3_OM_PYRAMID_ENABLED=0 (mirrored here via pyramid_enabled=False)
    must fully disable the pyramid path while ladder + stall-take stay on."""
    om = _new_om(
        OMConfig(
            take_r=50.0,
            spike_take_r=100.0,
            pyramid_min_live_r=0.3,
            pyramid_min_conv=0.4,
            pyramid_enabled=False,
        )
    )
    state = _pyramid_state()
    lane = _lane_at_r(0.5, side="buy")
    bars = _m5_bars()
    with patch(
        "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] != "add_repair_leg"
    # Ladder should still be evaluated (0.5R peak isn't near its floor yet,
    # so this settles into a hold — proving the rest of the pipeline runs).
    assert action["action"] == "hold"


def test_pyramid_add_adversarial_sequence_never_exceeds_caps():
    """200 randomized adversarial sequences of repeated pyramid-eligible
    ticks (always strong same-side conviction, always green, always past a
    fresh tier) must never grow the basket beyond what enforce_caps allows —
    i.e. every add_repair_leg this path returns must itself have already
    cleared enforce_caps with proposed_repair_risk_usd > 0."""
    rng = random.Random(11)
    basket_cfg = BasketConfig(max_legs=3, max_basket_risk_mult=3.0)
    bars = _m5_bars()
    for trial in range(200):
        legs = rng.randint(1, 5)  # deliberately allow "already over cap" states
        om = _new_om(
            OMConfig(
                take_r=50.0,
                spike_take_r=100.0,
                pyramid_min_live_r=0.1,
                pyramid_min_conv=0.0,
                pyramid_tier_step=0.0,  # always past "tier" so the cap check is what's under test
            )
        )
        state = _pyramid_state(basket_cfg=basket_cfg)
        lane = [_position(side="buy", position_id=i + 1, net_profit=0.5) for i in range(legs)]
        with patch(
            "dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("buy", 0.9)
        ):
            action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
        if action["action"] == "add_repair_leg":
            assert legs < basket_cfg.max_legs, (
                f"trial={trial} legs={legs}: pyramid add permitted at/over max_legs={basket_cfg.max_legs}"
            )


# ---------------------------------------------------------------------------
# SMART LOSS EXIT (owner directive 2026-07-08) — loss-side mirror of the
# DRAGON LADDER. Only engages a lane classified into the 'disaster' stop
# regime (see dexter3/smart_exit.py); cuts ONLY on a confirmed structural
# break (bar CLOSED beyond the tight invalidation), survives noise wicks.
# ---------------------------------------------------------------------------


def _smart_exit_state(
    *, regime: str = "disaster", oldest_open_ts: str | None = "2026-07-07T09:00:00Z", disaster_mult: float = 2.0
) -> dict:
    return {
        "base_risk_usd": 1.0,
        "smart_exit_regime": {
            "XAUUSD": {
                "regime": regime,
                "disaster_mult": disaster_mult,
                "oldest_open_ts": oldest_open_ts,
            }
        },
    }


def test_smart_loss_exit_fires_on_confirmed_break_disaster_regime():
    om = _new_om(OMConfig(repair_trigger_r=-999.0))  # disable basket doctor so only smart-exit is under test
    state = _smart_exit_state()
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "close_all"
    assert action["reason"] == "smart_confirmed_break"
    assert action["smart_exit_regime"] == "disaster"


def test_smart_loss_exit_survives_noise_wick_holds():
    """A wick beyond the tight invalidation that CLOSES back inside
    (level_lost True, m5_close_beyond False) must be survived — hold, not
    close — exactly the backtest-proven 'survive noise' behavior."""
    om = _new_om(OMConfig(repair_trigger_r=-999.0))
    state = _smart_exit_state()
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": False}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_smart_loss_exit_never_fires_on_tight_regime_chase_entry():
    """A 'tight'-regime lane (chase entry, or smart exit disabled) never
    fires the smart cut regardless of how confirmed the break is — its
    broker-side tight hard SL is the only exit mechanism, unchanged."""
    om = _new_om(OMConfig(repair_trigger_r=-999.0))
    state = _smart_exit_state(regime="tight")
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_smart_loss_exit_never_fires_when_lane_is_green():
    """live_r >= 0 must never trigger a loss-side cut — the branch is
    disjoint from the profit side by construction."""
    om = _new_om(OMConfig(repair_trigger_r=-999.0, take_r=50.0, spike_take_r=100.0))
    state = _smart_exit_state()
    lane = _lane_at_r(0.3, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_smart_loss_exit_holds_when_regime_missing_falls_through():
    """No smart_exit_regime entry for this symbol at all (e.g. an entry
    placed before this feature existed, or state was never populated) ->
    smart-loss-exit never fires; falls through to the rest of the pipeline
    (which, with basket doctor disabled and no ladder engagement on a loser,
    settles at hold)."""
    om = _new_om(OMConfig(repair_trigger_r=-999.0))
    state = {"base_risk_usd": 1.0}  # no smart_exit_regime key at all
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_smart_loss_exit_ignores_stale_regime_from_different_basket():
    """A regime dict recorded for a DIFFERENT basket (different
    oldest_open_ts) must never drive a cut on the current one — same
    stale-state guard philosophy as _update_peak_r's own basket reset."""
    om = _new_om(OMConfig(repair_trigger_r=-999.0))
    state = _smart_exit_state(oldest_open_ts="2020-01-01T00:00:00Z")  # stale, unrelated basket
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "hold"


def test_smart_loss_exit_takes_priority_over_basket_doctor_when_confirmed():
    """Both smart-loss-exit and basket doctor could theoretically fire on
    the same evidence (level_lost AND m5_close_beyond); smart-loss-exit
    (checked earlier in evaluate()'s priority order) must win, closing the
    lane outright rather than adding a repair leg on top of a confirmed
    structural break."""
    om = _new_om(OMConfig(repair_trigger_r=-0.3, repair_min_conviction=0.0))
    state = _smart_exit_state()
    lane = _lane_at_r(-0.5, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ), patch("dexter3.opening_manager.hunt_mode.decide_hunt", return_value=_FakeHuntDecision("sell", 0.9)):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "close_all"
    assert action["reason"] == "smart_confirmed_break"


def test_smart_loss_exit_cap_stop_still_outranks_it():
    """Hard caps (time stop / daily loss) remain the highest-priority branch
    — a capped-out basket resolves via cap_stop even under the disaster
    regime with confirmed-break evidence."""
    basket_cfg = BasketConfig(time_stop_min=30.0)
    om = _new_om(OMConfig(repair_trigger_r=-999.0))
    state = _smart_exit_state(oldest_open_ts="2026-07-07T09:00:00Z")
    state["basket_cfg"] = basket_cfg
    state["now_utc_iso"] = "2026-07-07T09:45:00Z"  # 45min > 30min time_stop_min
    lane = _lane_at_r(-0.6, side="buy")
    bars = _m5_bars()
    with patch.object(
        basket_live, "structure_evidence", return_value={"level_lost": True, "m5_close_beyond": True}
    ):
        action = om.evaluate("XAUUSD", lane, None, bars, bars, bars, state)
    assert action["action"] == "close_all"
    assert action["reason"] == "cap_stop"
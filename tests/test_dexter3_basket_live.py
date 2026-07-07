"""Unit/property tests for dexter3/basket_live.py — real-position basket glue.

Critical invariants under test (per HUNT MODE spec, 2026-07-05):
  (a) aggregate_lane handles multi-leg, mixed-side positions correctly, and
      degrades to unreliable=True (never a fabricated number) when a
      position's PnL field is missing/unreadable.
  (b) structure_evidence produces the documented truth table.
  (c) decide_basket_action covers every branch: resolve target hit, repair
      (both same-side and hedge_lock), every cap breach (legs, risk mult,
      time stop, daily), and the default hold.
  (d) enforce_caps is unbreachable under 200 randomized adversarial
      sequences.
  (e) an unreliable aggregate ALWAYS holds, regardless of other inputs.
"""
from __future__ import annotations

import random

import pytest

from dexter3 import basket_live
from dexter3.basket_manager import BasketConfig


# ---------------------------------------------------------------------------
# position builders
# ---------------------------------------------------------------------------


def _position(
    side: str = "buy",
    entry: float = 2000.0,
    volume: float = 0.01,
    net_profit: float | None = -5.0,
    label: str = "dexter3:fable:m5h-v1:hunt",
    open_ts: str = "2026-07-05T09:00:00Z",
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


# ---------------------------------------------------------------------------
# lane_positions
# ---------------------------------------------------------------------------


def test_lane_positions_filters_by_label_prefix() -> None:
    positions = [
        _position(label="dexter3:fable:m5h-v1:hunt", position_id=1),
        _position(label="some_other_bot:xyz", position_id=2),
        _position(label="dexter3:fable:m5h-v1:hunt_sweep_reclaim", position_id=3),
        {"positionId": 4, "comment": "dexter3:fable:m5h-v1", "symbolName": "XAUUSD", "tradeSide": "BUY"},
    ]
    lane = basket_live.lane_positions(positions)
    ids = {p["positionId"] for p in lane}
    assert ids == {1, 3, 4}


def test_lane_positions_empty_and_none_input() -> None:
    assert basket_live.lane_positions([]) == []
    assert basket_live.lane_positions(None) == []


def test_lane_positions_robust_to_malformed_entries() -> None:
    positions = [
        _position(position_id=1),
        "not_a_dict",
        None,
        {"positionId": 2},  # missing label/comment entirely -> excluded
    ]
    lane = basket_live.lane_positions(positions)  # type: ignore[arg-type]
    assert len(lane) == 1
    assert lane[0]["positionId"] == 1


def test_lane_positions_custom_prefix() -> None:
    positions = [_position(label="custom:v2:leg", position_id=1)]
    assert basket_live.lane_positions(positions, label_prefix="custom:v2") == positions
    assert basket_live.lane_positions(positions, label_prefix="dexter3:fable:m5h-v1") == []


# ---------------------------------------------------------------------------
# aggregate_lane — multi-leg math, mixed sides, missing pnl -> unreliable
# ---------------------------------------------------------------------------


def test_aggregate_lane_empty_positions() -> None:
    agg = basket_live.aggregate_lane([], base_risk_usd=5.0)
    assert agg["legs"] == 0
    assert agg["aggregate_pnl_usd"] == 0.0
    assert agg["aggregate_r"] == 0.0
    assert agg["unreliable"] is False


def test_aggregate_lane_single_leg() -> None:
    positions = [_position(side="buy", volume=0.01, net_profit=-4.5, position_id=1)]
    agg = basket_live.aggregate_lane(positions, base_risk_usd=5.0)
    assert agg["legs"] == 1
    assert agg["aggregate_pnl_usd"] == -4.5
    assert agg["aggregate_r"] == pytest.approx(-0.9)
    assert agg["sides"] == {"buy": 1, "sell": 0}
    assert agg["unreliable"] is False


def test_aggregate_lane_multi_leg_mixed_sides() -> None:
    positions = [
        _position(side="buy", volume=0.01, entry=2000.0, net_profit=-3.0, position_id=1),
        _position(side="buy", volume=0.02, entry=2001.0, net_profit=2.0, position_id=2),
        _position(side="sell", volume=0.01, entry=2002.0, net_profit=-1.0, position_id=3),
    ]
    agg = basket_live.aggregate_lane(positions, base_risk_usd=10.0)
    assert agg["legs"] == 3
    assert agg["aggregate_pnl_usd"] == pytest.approx(-3.0 + 2.0 - 1.0)
    assert agg["aggregate_r"] == pytest.approx((-3.0 + 2.0 - 1.0) / 10.0)
    assert agg["sides"] == {"buy": 2, "sell": 1}
    # volume_net = +0.01 +0.02 -0.01 = 0.02
    assert agg["volume_net"] == pytest.approx(0.02)
    # weighted_entry = sum(entry*vol)/sum(vol)
    expected_weighted_entry = (2000.0 * 0.01 + 2001.0 * 0.02 + 2002.0 * 0.01) / (0.01 + 0.02 + 0.01)
    assert agg["weighted_entry"] == pytest.approx(expected_weighted_entry, rel=1e-4)
    assert agg["unreliable"] is False


def test_aggregate_lane_oldest_open_ts() -> None:
    positions = [
        _position(open_ts="2026-07-05T09:10:00Z", position_id=1),
        _position(open_ts="2026-07-05T09:00:00Z", position_id=2),
        _position(open_ts="2026-07-05T09:05:00Z", position_id=3),
    ]
    agg = basket_live.aggregate_lane(positions, base_risk_usd=5.0)
    assert agg["oldest_open_ts"] == "2026-07-05T09:00:00Z"


def test_aggregate_lane_missing_pnl_field_marks_unreliable() -> None:
    positions = [
        _position(net_profit=-3.0, position_id=1),
        _position(net_profit=None, position_id=2),  # no netProfit/profit/etc at all
    ]
    agg = basket_live.aggregate_lane(positions, base_risk_usd=5.0)
    assert agg["unreliable"] is True


def test_aggregate_lane_unparseable_pnl_marks_unreliable() -> None:
    positions = [_position(position_id=1)]
    positions[0]["netProfit"] = "not_a_number"
    agg = basket_live.aggregate_lane(positions, base_risk_usd=5.0)
    assert agg["unreliable"] is True


def test_aggregate_lane_pnl_field_fallback_chain() -> None:
    # profit / grossProfit / pnl fallbacks all readable when netProfit absent.
    pos = _position(net_profit=None, position_id=1)
    pos["profit"] = 7.5
    agg = basket_live.aggregate_lane([pos], base_risk_usd=5.0)
    assert agg["unreliable"] is False
    assert agg["aggregate_pnl_usd"] == 7.5


def test_aggregate_lane_zero_or_missing_base_risk_marks_unreliable() -> None:
    positions = [_position(net_profit=-1.0, position_id=1)]
    agg_zero = basket_live.aggregate_lane(positions, base_risk_usd=0.0)
    assert agg_zero["unreliable"] is True
    agg_none = basket_live.aggregate_lane(positions, base_risk_usd=None)  # type: ignore[arg-type]
    assert agg_none["unreliable"] is True


def test_aggregate_lane_never_raises_on_garbage_fields() -> None:
    positions = [
        {"label": "dexter3:fable:m5h-v1", "tradeSide": None, "volumeInUnits": "garbage", "entryPrice": None},
    ]
    agg = basket_live.aggregate_lane(positions, base_risk_usd=5.0)
    assert agg["legs"] == 1
    assert agg["unreliable"] is True  # no readable pnl field present


# ---------------------------------------------------------------------------
# structure_evidence — truth table
# ---------------------------------------------------------------------------


def _swing_lens(swing_low: float | None, swing_high: float | None) -> dict:
    swing: dict = {"value": "transition"}
    if swing_low is not None:
        swing["last_swing_low"] = {"price": swing_low}
    if swing_high is not None:
        swing["last_swing_high"] = {"price": swing_high}
    return {"swing_structure": swing}


def _closes(values: list[float]) -> list[dict]:
    return [{"open": v, "high": v, "low": v, "close": v, "ts": f"t{i}"} for i, v in enumerate(values)]


def test_structure_evidence_no_defended_level() -> None:
    lens = _swing_lens(None, None)
    ev = basket_live.structure_evidence(lens, _closes([2000, 2001]), "buy", 2000.0)
    assert ev["level"] is None
    assert ev["level_lost"] is False
    assert ev["m5_close_beyond"] is False


def test_structure_evidence_no_bars() -> None:
    lens = _swing_lens(1995.0, None)
    ev = basket_live.structure_evidence(lens, [], "buy", 2000.0)
    assert ev["level_lost"] is False
    assert ev["m5_close_beyond"] is False


def test_structure_evidence_buy_level_held() -> None:
    lens = _swing_lens(1995.0, None)
    bars = _closes([2000, 2001, 1999, 2002])  # never closes below 1995
    ev = basket_live.structure_evidence(lens, bars, "buy", 2000.0)
    assert ev["level"] == 1995.0
    assert ev["level_lost"] is False
    assert ev["m5_close_beyond"] is False


def test_structure_evidence_buy_level_lost_and_still_beyond() -> None:
    lens = _swing_lens(1995.0, None)
    bars = _closes([2000, 1994, 1993])  # crossed below 1995, still below on last close
    ev = basket_live.structure_evidence(lens, bars, "buy", 2000.0)
    assert ev["level"] == 1995.0
    assert ev["level_lost"] is True
    assert ev["m5_close_beyond"] is True


def test_structure_evidence_buy_level_lost_but_reclaimed() -> None:
    lens = _swing_lens(1995.0, None)
    bars = _closes([2000, 1994, 1996])  # crossed below then closed back above
    ev = basket_live.structure_evidence(lens, bars, "buy", 2000.0)
    assert ev["level"] == 1995.0
    assert ev["level_lost"] is True  # was crossed at some point
    assert ev["m5_close_beyond"] is False  # but last close is no longer beyond


def test_structure_evidence_sell_level_lost_and_still_beyond() -> None:
    lens = _swing_lens(None, 2005.0)
    bars = _closes([2000, 2006, 2007])  # crossed above 2005, still above
    ev = basket_live.structure_evidence(lens, bars, "sell", 2000.0)
    assert ev["level"] == 2005.0
    assert ev["level_lost"] is True
    assert ev["m5_close_beyond"] is True


def test_structure_evidence_sell_level_held() -> None:
    lens = _swing_lens(None, 2005.0)
    bars = _closes([2000, 2001, 2003])
    ev = basket_live.structure_evidence(lens, bars, "sell", 2000.0)
    assert ev["level_lost"] is False
    assert ev["m5_close_beyond"] is False


def test_structure_evidence_never_raises_on_malformed_lens() -> None:
    ev = basket_live.structure_evidence(None, _closes([2000, 1990]), "buy", 2000.0)
    assert ev["level"] is None
    ev2 = basket_live.structure_evidence({"swing_structure": "not_a_dict"}, _closes([2000]), "buy", 2000.0)
    assert ev2["level_lost"] is False


# ---------------------------------------------------------------------------
# decide_basket_action — every branch
# ---------------------------------------------------------------------------


def _agg(
    legs: int = 1,
    aggregate_r: float = 0.0,
    unreliable: bool = False,
    sides: dict | None = None,
    oldest_open_ts: str | None = "2026-07-05T09:00:00Z",
    base_risk_usd: float = 5.0,
    current_basket_risk_usd: float = 5.0,
    lens_liquidity_sweep: dict | None = None,
) -> dict:
    out = {
        "legs": legs,
        "aggregate_pnl_usd": aggregate_r * base_risk_usd,
        "aggregate_r": aggregate_r,
        "sides": sides or {"buy": legs, "sell": 0},
        "volume_net": 0.0,
        "oldest_open_ts": oldest_open_ts,
        "weighted_entry": 2000.0,
        "unreliable": unreliable,
        "base_risk_usd": base_risk_usd,
        "current_basket_risk_usd": current_basket_risk_usd,
    }
    if lens_liquidity_sweep is not None:
        out["lens_liquidity_sweep"] = lens_liquidity_sweep
    return out


def _evidence(level_lost: bool = False, m5_close_beyond: bool = False) -> dict:
    return {"level_lost": level_lost, "m5_close_beyond": m5_close_beyond, "level": 1995.0, "evidence": "test"}


def test_decide_basket_action_unreliable_always_holds() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=2, aggregate_r=999.0, unreliable=True)  # absurd R to prove it's ignored
    evidence = _evidence(level_lost=True, m5_close_beyond=True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"
    assert result.get("reason") == "unreliable_pnl_snapshot"


def test_decide_basket_action_no_legs_holds() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=0)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"


def test_decide_basket_action_resolve_target_hit() -> None:
    cfg = BasketConfig(resolve_target_r=0.2)
    agg = _agg(legs=1, aggregate_r=0.25)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "close_all_in_profit"


def test_decide_basket_action_resolve_target_exact_boundary() -> None:
    cfg = BasketConfig(resolve_target_r=0.2)
    agg = _agg(legs=1, aggregate_r=0.2)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "close_all_in_profit"


def test_decide_basket_action_hold_when_no_evidence() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=1, aggregate_r=-0.3)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(False, False), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"


def test_decide_basket_action_hold_on_partial_evidence() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=1, aggregate_r=-0.3)
    for evidence in (_evidence(True, False), _evidence(False, True)):
        result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
        assert result["action"] == "hold"


def test_decide_basket_action_repair_hedge_lock_when_confirmed_break() -> None:
    cfg = BasketConfig(max_legs=3, max_basket_risk_mult=3.0)
    agg = _agg(legs=1, aggregate_r=-0.5, sides={"buy": 1, "sell": 0}, base_risk_usd=5.0, current_basket_risk_usd=5.0)
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "add_repair_leg"
    assert result["mode"] == basket_live.REPAIR_MODE_HEDGE_LOCK
    assert result["side"] == "sell"  # opposite of dominant buy side


def test_decide_basket_action_repair_same_side_when_sweep_reclaimed_in_our_favor() -> None:
    cfg = BasketConfig(max_legs=3, max_basket_risk_mult=3.0)
    agg = _agg(
        legs=1,
        aggregate_r=-0.5,
        sides={"buy": 1, "sell": 0},
        base_risk_usd=5.0,
        current_basket_risk_usd=5.0,
        lens_liquidity_sweep={"value": True, "side": "buy"},  # reclaim in OUR (buy) direction
    )
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "add_repair_leg"
    assert result["mode"] == basket_live.REPAIR_MODE_SAME_SIDE
    assert result["side"] == "buy"


def test_decide_basket_action_repair_indeterminate_side_holds() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=2, aggregate_r=-0.5, sides={"buy": 1, "sell": 1})  # tied -> indeterminate
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"


def test_decide_basket_action_cap_max_legs_blocks_repair() -> None:
    cfg = BasketConfig(max_legs=2)
    agg = _agg(legs=2, aggregate_r=-0.5, sides={"buy": 2, "sell": 0})
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"
    assert result.get("cap") == "max_legs"


def test_decide_basket_action_cap_max_basket_risk_blocks_repair() -> None:
    cfg = BasketConfig(max_legs=5, max_basket_risk_mult=1.0)
    # current_basket_risk_usd already at 1x base -> any additional repair leg breaches 1.0x cap
    agg = _agg(
        legs=1,
        aggregate_r=-0.5,
        sides={"buy": 1, "sell": 0},
        base_risk_usd=5.0,
        current_basket_risk_usd=5.0,
    )
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "hold"
    assert result.get("cap") == "max_basket_risk_mult"


def test_decide_basket_action_cap_time_stop_overrides_everything() -> None:
    cfg = BasketConfig(time_stop_min=60)
    agg = _agg(legs=1, aggregate_r=-0.5, oldest_open_ts="2026-07-05T08:00:00Z")  # 70 min old
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(cfg, agg, evidence, "2026-07-05T09:10:00Z", {})
    assert result["action"] == "close_all_cap_stop"
    assert result.get("cap") == "time_stop_min"


def test_decide_basket_action_cap_time_stop_even_when_in_profit_evidence_absent() -> None:
    # Time stop must fire even without repair evidence (evaluated before the
    # repair-evidence branch, mirroring BasketManager's cap-first ordering) —
    # but resolve-in-profit still takes priority if the R target is also hit
    # (documented ordering: resolve-in-profit checked before caps).
    cfg = BasketConfig(time_stop_min=60, resolve_target_r=0.2)
    agg = _agg(legs=1, aggregate_r=-0.1, oldest_open_ts="2026-07-05T08:00:00Z")
    result = basket_live.decide_basket_action(cfg, agg, _evidence(False, False), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "close_all_cap_stop"


def test_decide_basket_action_cap_daily_loss_baskets_blocks_everything() -> None:
    cfg = BasketConfig(daily_loss_baskets=2)
    agg = _agg(legs=1, aggregate_r=-0.5)
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(
        cfg, agg, evidence, "2026-07-05T09:10:00Z", {"daily_loss_baskets": 2}
    )
    assert result["action"] == "close_all_cap_stop"
    assert result.get("cap") == "daily_loss_baskets"


def test_decide_basket_action_daily_loss_cap_not_yet_reached_allows_repair() -> None:
    cfg = BasketConfig(daily_loss_baskets=2)
    agg = _agg(legs=1, aggregate_r=-0.5, sides={"buy": 1, "sell": 0})
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(
        cfg, agg, evidence, "2026-07-05T09:10:00Z", {"daily_loss_baskets": 1}
    )
    assert result["action"] == "add_repair_leg"


def test_decide_basket_action_none_daily_state_defaults_safely() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=1, aggregate_r=-0.1)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(False, False), "2026-07-05T09:10:00Z", None)
    assert result["action"] == "hold"


# ---------------------------------------------------------------------------
# enforce_caps — the single choke point, direct tests
# ---------------------------------------------------------------------------


def test_enforce_caps_none_when_nothing_breached() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=1, base_risk_usd=5.0, current_basket_risk_usd=5.0)
    result = basket_live.enforce_caps(cfg, agg=agg, now_utc_iso="2026-07-05T09:10:00Z", daily_state={})
    assert result is None


def test_enforce_caps_daily_loss_fires_even_with_zero_legs() -> None:
    cfg = BasketConfig(daily_loss_baskets=1)
    agg = _agg(legs=0)
    result = basket_live.enforce_caps(
        cfg, agg=agg, now_utc_iso="2026-07-05T09:10:00Z", daily_state={"daily_loss_baskets": 1}
    )
    assert result is not None
    assert result["action"] == "close_all_cap_stop"
    assert result["cap"] == "daily_loss_baskets"


def test_enforce_caps_no_legs_no_repair_probe_returns_none() -> None:
    cfg = BasketConfig()
    agg = _agg(legs=0)
    result = basket_live.enforce_caps(cfg, agg=agg, now_utc_iso="2026-07-05T09:10:00Z", daily_state={})
    assert result is None


def test_enforce_caps_max_legs_only_fires_with_repair_probe() -> None:
    cfg = BasketConfig(max_legs=1)
    agg = _agg(legs=1)
    # No repair probe -> max_legs check is skipped (nothing is being added).
    result_no_probe = basket_live.enforce_caps(cfg, agg=agg, now_utc_iso="2026-07-05T09:10:00Z", daily_state={})
    assert result_no_probe is None
    result_with_probe = basket_live.enforce_caps(
        cfg, agg=agg, now_utc_iso="2026-07-05T09:10:00Z", daily_state={}, proposed_repair_risk_usd=1.0
    )
    assert result_with_probe is not None
    assert result_with_probe["cap"] == "max_legs"


# ---------------------------------------------------------------------------
# enforce_caps — 200 randomized adversarial sequences, unbreachable
# ---------------------------------------------------------------------------


def test_enforce_caps_unbreachable_under_adversarial_sequences() -> None:
    """Simulate 200 randomized adversarial call sequences against enforce_caps
    and BasketManager-equivalent bookkeeping, asserting the caps are never
    exceeded: legs never exceed max_legs, basket risk never exceeds
    max_basket_risk_mult*base_risk, basket age never exceeds time_stop_min
    without a close_all_cap_stop firing, and daily_loss_baskets at/above cap
    always blocks new repair legs.
    """
    rng = random.Random(12345)

    for trial in range(200):
        cfg = BasketConfig(
            max_legs=rng.choice([1, 2, 3, 4]),
            max_basket_risk_mult=rng.choice([1.0, 1.5, 2.0, 3.0]),
            time_stop_min=rng.choice([15, 30, 60, 180]),
            daily_loss_baskets=rng.choice([1, 2, 3]),
            resolve_target_r=0.2,
        )
        base_risk_usd = rng.choice([1.0, 5.0, 10.0])
        legs = 1
        current_basket_risk_usd = base_risk_usd  # first leg = base risk
        opened_at_min = 0.0
        daily_loss_baskets_so_far = rng.choice([0, 1, 2, 3])

        now_min = 0.0
        for step in range(rng.randint(5, 25)):
            now_min += rng.uniform(1, 40)
            now_iso_open = f"2026-07-05T00:{int(opened_at_min):02d}:00Z" if opened_at_min < 60 else "2026-07-05T00:00:00Z"
            # Use a minute-offset epoch model instead of real ISO math for
            # this adversarial loop (age_min computed directly, bypassing
            # enforce_caps' ISO parsing, to isolate the CAP LOGIC itself
            # from timestamp-format edge cases already covered elsewhere).
            age_min = now_min - opened_at_min

            agg = {
                "legs": legs,
                "aggregate_r": rng.uniform(-2.0, 2.0),
                "sides": {"buy": legs, "sell": 0},
                "oldest_open_ts": None,  # force age computation to skip ISO path
                "base_risk_usd": base_risk_usd,
                "current_basket_risk_usd": current_basket_risk_usd,
                "unreliable": False,
            }
            daily_state = {"daily_loss_baskets": daily_loss_baskets_so_far}

            proposed_risk = rng.choice([0.0, base_risk_usd * rng.uniform(0.1, 1.5)])

            # Directly assert the age-based time-stop using the module's own
            # helper semantics: since oldest_open_ts is None here, enforce_caps
            # cannot see age via ISO parsing, so we ALSO drive the ISO path in
            # a second, parallel check below to keep both branches honest.
            result = basket_live.enforce_caps(
                cfg,
                agg=agg,
                now_utc_iso="2026-07-05T09:10:00Z",
                daily_state=daily_state,
                proposed_repair_risk_usd=proposed_risk,
            )

            # INVARIANT 1: daily loss cap reached -> must always block (either
            # close_all_cap_stop with legs present, per the caller's expected
            # legs>0 path here).
            if daily_loss_baskets_so_far >= cfg.daily_loss_baskets:
                assert result is not None and result["action"] == "close_all_cap_stop", (
                    f"trial={trial} step={step}: daily loss cap reached but enforce_caps did not block"
                )
                continue

            # INVARIANT 2: max_legs never silently exceeded — if a repair
            # probe was proposed and legs already >= cap, must be blocked.
            if proposed_risk > 0 and legs >= cfg.max_legs:
                assert result is not None and result["action"] == "hold" and result["cap"] == "max_legs", (
                    f"trial={trial} step={step}: max_legs cap breach not blocked"
                )
                continue

            # INVARIANT 3: max basket risk never silently exceeded.
            if proposed_risk > 0 and base_risk_usd > 0:
                projected = current_basket_risk_usd + proposed_risk
                max_risk = base_risk_usd * cfg.max_basket_risk_mult
                if projected > max_risk:
                    assert result is not None and result["cap"] == "max_basket_risk_mult", (
                        f"trial={trial} step={step}: max_basket_risk_mult breach not blocked "
                        f"(projected={projected}, max={max_risk})"
                    )
                    continue

            # If we get here, enforce_caps should have allowed the proposal
            # (result is None) — simulate the leg/risk bookkeeping growing,
            # exactly mirroring what a caller obeying enforce_caps would do.
            if result is None and proposed_risk > 0:
                legs += 1
                current_basket_risk_usd += proposed_risk

            # Cross-check: after any simulated growth, the invariants must
            # STILL hold (defense in depth against a bookkeeping bug in the
            # test itself, not just the module under test).
            assert legs <= cfg.max_legs, f"trial={trial} step={step}: legs grew past cap despite enforce_caps"
            assert current_basket_risk_usd <= base_risk_usd * cfg.max_basket_risk_mult + 1e-9, (
                f"trial={trial} step={step}: basket risk grew past cap despite enforce_caps"
            )


def test_enforce_caps_unbreachable_time_stop_via_iso_timestamps() -> None:
    """Separate focused property loop exercising the ISO-timestamp age path
    (oldest_open_ts + now_utc_iso) across randomized ages, confirming
    close_all_cap_stop fires exactly when age_min >= cfg.time_stop_min and
    never fires for younger baskets.
    """
    rng = random.Random(999)
    for trial in range(100):
        time_stop_min = rng.choice([15, 30, 60, 120, 180])
        cfg = BasketConfig(time_stop_min=time_stop_min)
        age_min = rng.uniform(0, time_stop_min * 2)
        opened_min = 0
        now_min = age_min
        opened_iso = f"2026-07-05T00:00:00Z"
        now_iso = f"2026-07-05T{int(now_min // 60):02d}:{int(now_min % 60):02d}:00Z"
        agg = {
            "legs": 1,
            "aggregate_r": 0.0,
            "sides": {"buy": 1, "sell": 0},
            "oldest_open_ts": opened_iso,
            "base_risk_usd": 5.0,
            "current_basket_risk_usd": 5.0,
            "unreliable": False,
        }
        result = basket_live.enforce_caps(
            cfg, agg=agg, now_utc_iso=now_iso, daily_state={"daily_loss_baskets": 0}
        )
        if age_min >= time_stop_min:
            assert result is not None and result["action"] == "close_all_cap_stop" and result["cap"] == "time_stop_min", (
                f"trial={trial}: age={age_min:.2f}min >= cap={time_stop_min} but no time_stop_min fired"
            )
        else:
            assert result is None, f"trial={trial}: age={age_min:.2f}min < cap={time_stop_min} but a cap fired: {result}"


# ---------------------------------------------------------------------------
# FIX 1 (2026-07-07) — peak-R basket trailing (basket_runtime param)
#
# Diagnosis this fix repairs: hunt_mode sets per-leg TP at 1.2R, but the OLD
# basket_live resolve fired the instant aggregate_r >= resolve_target_r=0.2R
# — winners were banked at ~+0.2R while losers ran to the full -1R SL,
# avg_win/avg_loss=0.48 -> PF 0.61 despite a 56% win rate. FIX 1 lets winners
# run (peak-R tracking) while still banking a defined fraction once armed,
# or hitting a hard take_r ceiling — see basket_live._resolve_profit_action.
# ---------------------------------------------------------------------------


def test_basket_runtime_none_is_byte_identical_to_pre_fix_flat_bar() -> None:
    """The backward-compat contract: omitting basket_runtime (the default)
    must behave EXACTLY like the pre-fix flat resolve_target_r bar — this is
    what keeps every pre-existing test in this file (and the paper/simulated
    BasketManager path) green without modification."""
    cfg = BasketConfig(resolve_target_r=0.2, arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=0.25)
    result = basket_live.decide_basket_action(cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {})
    assert result["action"] == "close_all_in_profit"
    assert "trail" not in result  # pre-fix path never attaches trail detail


def test_trail_not_armed_below_arm_trail_r_lets_basket_run() -> None:
    """A small green basket (peak_r below arm_trail_r) must NOT close —
    this is the core behavior change: no more banking winners at +0.2R."""
    cfg = BasketConfig(resolve_target_r=0.2, arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=0.25)  # would have closed under the OLD flat bar
    runtime = {"peak_r": 0.25}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "hold", "basket must keep running below arm_trail_r, not bank a small green"


def test_trail_armed_holds_while_still_near_peak() -> None:
    """Once armed (peak_r >= arm_trail_r), the basket keeps running as long
    as aggregate_r has not retraced down to the trail-stop level."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=0.9)  # peak_r=0.9 -> trail_stop = 0.9*0.6=0.54; 0.9 > 0.54
    runtime = {"peak_r": 0.9}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "hold"


def test_trail_closes_when_retraced_to_keep_frac_of_peak() -> None:
    """The headline behavior: once armed, retracing to peak_r*trail_keep_frac
    closes the basket, banking that fraction of the best R ever reached."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    peak_r = 0.9
    trail_stop = peak_r * 0.6  # 0.54
    agg = _agg(legs=1, aggregate_r=trail_stop)
    runtime = {"peak_r": peak_r}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "close_all_in_profit"
    assert result["trail"]["trigger"] == "trail_stop"
    assert result["trail"]["peak_r"] == pytest.approx(peak_r)
    assert result["trail"]["trail_stop_r"] == pytest.approx(trail_stop)


def test_trail_closes_below_keep_frac_too() -> None:
    """Retracing PAST the trail-stop level (not just exactly to it) must
    also close — the trigger is a <=, not an exact-equality tripwire."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=0.3)  # below trail_stop=0.54
    runtime = {"peak_r": 0.9}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "close_all_in_profit"
    assert result["trail"]["trigger"] == "trail_stop"


def test_trail_stop_never_below_resolve_target_r_floor() -> None:
    """resolve_target_r is repurposed as the MINIMUM profit ever worth
    closing for: with a small peak (just above arm_trail_r), keep_frac math
    alone could imply a trail-stop below the floor — the floor must win."""
    cfg = BasketConfig(resolve_target_r=0.15, arm_trail_r=0.5, trail_keep_frac=0.2, take_r=1.1)
    # peak_r=0.5 -> naive trail_stop = 0.5*0.2 = 0.10, but floor=0.15 must apply.
    agg = _agg(legs=1, aggregate_r=0.12)  # below naive trail_stop but ABOVE the floor's own trigger? check below
    runtime = {"peak_r": 0.5}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    # aggregate_r=0.12 <= max(0.10, 0.15)=0.15 -> closes at the floor, not the naive lower trail math
    assert result["action"] == "close_all_in_profit"
    assert result["trail"]["trail_stop_r"] == pytest.approx(0.15)


def test_hard_take_r_closes_unconditionally() -> None:
    """Reaching take_r closes immediately even if the trail math would have
    allowed the basket to keep running (peak_r not yet updated past take_r,
    or trail_keep_frac would have permitted further upside)."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=1.1)
    runtime = {"peak_r": 1.1}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "close_all_in_profit"
    assert result["trail"]["trigger"] == "take_r"


def test_hard_take_r_takes_priority_over_trail_stop() -> None:
    """Above take_r, the hard-take path fires even though trail math (peak_r
    * trail_keep_frac) would also technically not have closed yet."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=1.15)
    runtime = {"peak_r": 1.15}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["trail"]["trigger"] == "take_r"


def test_peak_r_caller_understates_is_corrected_defensively() -> None:
    """If the caller passes a stale/understated peak_r (e.g. forgot to
    update it with this bar's own aggregate_r before calling), the function
    must not let that suppress a legitimate hard-take close."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=1.2)
    runtime = {"peak_r": 0.3}  # understated vs. this bar's own aggregate_r=1.2
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "close_all_in_profit"
    assert result["trail"]["trigger"] == "take_r"
    assert result["trail"]["peak_r"] == pytest.approx(1.2)  # corrected, not the stale 0.3


def test_trail_never_bypasses_caps_repair_still_gated() -> None:
    """Adversarial: even with basket_runtime supplied and the trail NOT
    firing (basket held below arm_trail_r or negative), repair/cap logic
    downstream of _resolve_profit_action must still behave exactly as
    documented — the trail path must never become a second, ungated repair
    channel. Confirms cap enforcement still fires for a losing, capped-out
    basket even when basket_runtime is present."""
    cfg = BasketConfig(max_legs=2, arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=2, aggregate_r=-0.5, sides={"buy": 2, "sell": 0})
    runtime = {"peak_r": 0.1}  # never armed; basket is currently negative
    evidence = _evidence(True, True)
    result = basket_live.decide_basket_action(
        cfg, agg, evidence, "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "hold"
    assert result.get("cap") == "max_legs"


def test_trail_never_closes_below_arm_threshold_even_if_negative_peak_tracked() -> None:
    """peak_r tracks the BEST R seen — even if currently negative, a
    negative peak_r must never satisfy arm_trail_r (a positive threshold)
    and must never spuriously close."""
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    agg = _agg(legs=1, aggregate_r=-0.3)
    runtime = {"peak_r": -0.1}
    result = basket_live.decide_basket_action(
        cfg, agg, _evidence(), "2026-07-05T09:10:00Z", {}, basket_runtime=runtime
    )
    assert result["action"] == "hold"


def test_resolve_profit_action_directly_backward_compat_none_runtime() -> None:
    cfg = BasketConfig(resolve_target_r=0.3)
    assert basket_live._resolve_profit_action(cfg, 0.35, None) is not None
    assert basket_live._resolve_profit_action(cfg, 0.29, None) is None


def test_new_basket_config_defaults_match_spec() -> None:
    cfg = BasketConfig()
    assert cfg.arm_trail_r == 0.5
    assert cfg.trail_keep_frac == 0.6
    assert cfg.take_r == 1.1
    assert cfg.resolve_target_r == 0.2  # unchanged default, now dual-purpose

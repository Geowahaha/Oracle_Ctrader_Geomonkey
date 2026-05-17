"""Tests for the Multi-Route planner + Free-Runner engine."""
from __future__ import annotations

from execution.multi_route import (
    FreeRunnerEngine,
    MultiRouteBasket,
    MultiRoutePlanner,
    MultiRouteSignal,
)
from execution.multi_route.free_runner import FreeRunnerEngineConfig
from execution.multi_route.planner import MultiRoutePlannerConfig, ROUTE_BREAKDOWN, ROUTE_RETEST


def _signal(*, confidence: float = 85.0, route: str = ROUTE_BREAKDOWN, baseline_risk_usd: float = 5.0, free_margin: float = 200.0) -> MultiRouteSignal:
    return MultiRouteSignal(
        signal_run_id="run-x1",
        symbol="XAUUSD",
        direction="short",
        confidence=confidence,
        route=route,
        baseline_risk_usd=baseline_risk_usd,
        market_entry_price=2300.0,
        retest_entry_price=2302.0,
        breakout_entry_price=2295.0,
        stop_loss_price=2308.0,
        take_profit_price=2280.0,
        free_margin_usd=free_margin,
        baseline_leg_margin_usd=50.0,
    )


def test_planner_disabled_returns_ineligible():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=False))
    b = p.plan(signal=_signal())
    assert b.eligible is False
    assert "multi_route_disabled" in b.reasons


def test_planner_three_legs_when_eligible():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=True))
    b = p.plan(signal=_signal(confidence=85.0))
    assert b.eligible
    assert len(b.legs) == 3
    leg_ids = {leg.leg_id for leg in b.legs}
    assert {f"run-x1:probe", f"run-x1:retest", f"run-x1:breakout"} == leg_ids
    # Sum of shares = 1.0 → total_risk == baseline_risk_usd
    assert abs(b.total_risk_usd - 5.0) < 1e-6


def test_planner_low_confidence_falls_back():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=True, min_confidence=80.0))
    b = p.plan(signal=_signal(confidence=70.0))
    assert b.eligible is False
    assert any("confidence_below_min" in r for r in b.reasons)


def test_planner_unsupported_route_falls_back():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=True))
    b = p.plan(signal=_signal(route="harvest_zone_pm_only"))
    assert b.eligible is False
    assert any("route_not_allowed" in r for r in b.reasons)


def test_planner_insufficient_margin_falls_back():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=True, margin_safety_multiplier=3.0))
    b = p.plan(signal=_signal(free_margin=100.0))  # 100 < 3 * 50
    assert b.eligible is False
    assert any("free_margin_insufficient" in r for r in b.reasons)


def test_planner_retest_route_also_allowed():
    p = MultiRoutePlanner(config=MultiRoutePlannerConfig(enabled=True))
    b = p.plan(signal=_signal(route=ROUTE_RETEST))
    assert b.eligible


def test_planner_shares_can_be_tuned():
    cfg = MultiRoutePlannerConfig(enabled=True, probe_share=0.10, retest_share=0.50, breakout_share=0.40)
    p = MultiRoutePlanner(config=cfg)
    b = p.plan(signal=_signal(baseline_risk_usd=10.0))
    risks = {leg.leg_id.split(":")[-1]: leg.risk_usd for leg in b.legs}
    assert risks["probe"] == 1.0
    assert risks["retest"] == 5.0
    assert risks["breakout"] == 4.0


def test_free_runner_disabled_yields_no_directive():
    engine = FreeRunnerEngine(config=FreeRunnerEngineConfig(enabled=False, promote_r=0.8))
    out = engine.on_leg_progress(
        basket_id="b1", leg_id="b1:probe", current_r=1.5,
        basket_legs=[("b1:probe", "long", 2300.0), ("b1:retest", "long", 2298.0)],
        atr_5m=4.0,
    )
    assert out == []


def test_free_runner_promotes_siblings_when_r_threshold_hit():
    engine = FreeRunnerEngine(config=FreeRunnerEngineConfig(enabled=True, promote_r=0.8, be_padding_atr=0.10))
    out = engine.on_leg_progress(
        basket_id="b1", leg_id="b1:probe", current_r=1.0,
        basket_legs=[("b1:probe", "long", 2300.0), ("b1:retest", "long", 2298.0), ("b1:breakout", "long", 2305.0)],
        atr_5m=4.0,
    )
    leg_ids = {d.leg_id for d in out}
    assert leg_ids == {"b1:retest", "b1:breakout"}
    # Longs: new stop = entry - padding (0.10 * 4.0 = 0.4)
    by_id = {d.leg_id: d for d in out}
    assert abs(by_id["b1:retest"].new_stop_loss - (2298.0 - 0.4)) < 1e-6
    assert abs(by_id["b1:breakout"].new_stop_loss - (2305.0 - 0.4)) < 1e-6


def test_free_runner_idempotent_second_tick_yields_nothing():
    engine = FreeRunnerEngine(config=FreeRunnerEngineConfig(enabled=True, promote_r=0.8))
    args = dict(
        basket_id="b1", leg_id="b1:probe", current_r=1.0,
        basket_legs=[("b1:probe", "long", 2300.0), ("b1:retest", "long", 2298.0)],
        atr_5m=4.0,
    )
    first = engine.on_leg_progress(**args)
    second = engine.on_leg_progress(**args)
    assert len(first) == 1
    assert second == []
    assert engine.is_promoted("b1")


def test_free_runner_short_direction_uses_entry_plus_padding():
    engine = FreeRunnerEngine(config=FreeRunnerEngineConfig(enabled=True, promote_r=0.8, be_padding_atr=0.10))
    out = engine.on_leg_progress(
        basket_id="b2", leg_id="b2:probe", current_r=1.0,
        basket_legs=[("b2:probe", "short", 2300.0), ("b2:retest", "short", 2302.0)],
        atr_5m=4.0,
    )
    by_id = {d.leg_id: d for d in out}
    assert abs(by_id["b2:retest"].new_stop_loss - (2302.0 + 0.4)) < 1e-6

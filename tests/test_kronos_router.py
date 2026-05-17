"""Tests for the Kronos-Primary Router."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.kronos_router import KronosForecast, KronosRouter, RoutePlan
from analysis.kronos_router.primary import KronosRouterConfig


def _forecast(*, uncertainty: float = 0.20, direction: str = "short", age_sec: float = 30.0, base: datetime | None = None) -> KronosForecast:
    base = base or datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    return KronosForecast(
        symbol="XAUUSD",
        direction=direction,
        uncertainty=uncertainty,
        first_touch_price=2300.0,
        invalidation_price=2305.0 if direction == "short" else 2295.0,
        target_band_high=2280.0 if direction == "short" else 2320.0,
        created_utc=base - timedelta(seconds=age_sec),
    )


def _technical_plan() -> RoutePlan:
    return RoutePlan(
        entry_price=2301.0, stop_loss=2306.0, take_profit=2285.0,
        entry_type="limit", risk_multiplier=1.0, reason="technical_default",
    )


def _clock_factory(base: datetime):
    state = {"now": base}

    def fn() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return fn, advance


def test_router_disabled_returns_technical():
    cfg = KronosRouterConfig(enabled=False)
    router = KronosRouter(config=cfg)
    decision = router.decide(forecast=_forecast(), bias="short", technical_plan=_technical_plan())
    assert decision.primary == "technical"
    assert "kronos_router_disabled" in decision.reasons


def test_router_kronos_takes_wheel_when_aligned_and_low_uncertainty():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, max_uncertainty=0.30, max_age_sec=120)
    router = KronosRouter(config=cfg, clock=clock)
    decision = router.decide(
        forecast=_forecast(uncertainty=0.20, direction="short", age_sec=10, base=base),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "kronos"
    assert decision.plan is not None
    assert decision.plan.entry_price == 2300.0
    assert decision.plan.stop_loss == 2305.0
    assert decision.plan.take_profit == 2280.0


def test_router_falls_back_when_uncertainty_too_high():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, max_uncertainty=0.30)
    router = KronosRouter(config=cfg, clock=clock)
    decision = router.decide(
        forecast=_forecast(uncertainty=0.45, base=base),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "technical"
    assert any("uncertainty>=" in r for r in decision.reasons)


def test_router_falls_back_when_forecast_disagrees():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True)
    router = KronosRouter(config=cfg, clock=clock)
    decision = router.decide(
        forecast=_forecast(direction="long", base=base),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "technical"
    assert "forecast_disagrees_with_bias" in decision.reasons


def test_router_falls_back_when_forecast_stale():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, max_age_sec=60)
    router = KronosRouter(config=cfg, clock=clock)
    decision = router.decide(
        forecast=_forecast(age_sec=300, base=base),  # 5min old, threshold 60s
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "technical"
    assert any("stale" in r for r in decision.reasons)


def test_router_falls_back_when_daily_share_exceeded():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, daily_share_cap=0.30, daily_total_entries_estimate=10)
    router = KronosRouter(config=cfg, clock=clock)
    # Drive 3 kronos dispatches out of 10 estimated daily entries → share = 3/10 = 0.30.
    for _ in range(3):
        router.record_dispatch(primary="kronos")
    decision = router.decide(
        forecast=_forecast(base=base),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "technical"
    assert "daily_share_cap_exceeded" in decision.reasons


def test_router_falls_back_after_three_failures_in_window():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, failure_lockout_threshold=3, failure_lockout_window_hours=24)
    router = KronosRouter(config=cfg, clock=clock)
    for _ in range(3):
        router.record_outcome(primary="kronos", pnl_usd=-10.0)
    decision = router.decide(
        forecast=_forecast(base=base),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision.primary == "technical"
    assert any("locked_out" in r for r in decision.reasons)
    # After 25 hours, failures evict and lockout clears.
    advance(25 * 3600)
    decision2 = router.decide(
        forecast=_forecast(base=clock()),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision2.primary == "kronos"


def test_router_day_rollover_resets_share():
    base = datetime(2026, 5, 17, 23, 50, 0, tzinfo=timezone.utc)
    clock, advance = _clock_factory(base)
    cfg = KronosRouterConfig(enabled=True, daily_share_cap=0.30, daily_total_entries_estimate=10)
    router = KronosRouter(config=cfg, clock=clock)
    for _ in range(3):
        router.record_dispatch(primary="kronos")
    # Cap hit on day 1.
    decision_day1 = router.decide(
        forecast=_forecast(base=clock()),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision_day1.primary == "technical"
    # Cross midnight → counters reset.
    advance(30 * 60)  # 00:20 UTC next day
    decision_day2 = router.decide(
        forecast=_forecast(base=clock()),
        bias="short",
        technical_plan=_technical_plan(),
    )
    assert decision_day2.primary == "kronos"

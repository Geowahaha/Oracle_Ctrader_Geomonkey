"""Kronos-Primary Router.

Today: technical setup chooses the route, Kronos forecast is a secondary
anchor. This module flips the relationship when (and only when) the forecast
has low uncertainty and agrees with the prevailing bias. In that narrow,
well-defined window Kronos takes the wheel: timing, stop, and target are
derived from `KronosForecast`. Otherwise the existing technical primary path
runs unchanged.

Public surface:

    from analysis.kronos_router import KronosForecast, KronosRouter, RoutePlan

    forecast = KronosForecast(...)
    router = KronosRouter(config=...)
    decision = router.decide(forecast=forecast, bias="short", technical_plan=...)
    if decision.primary == "kronos":
        # follow decision.plan
    else:
        # technical primary path
"""
from __future__ import annotations

from analysis.kronos_router.primary import (
    KronosForecast,
    KronosRouter,
    RouteDecision,
    RoutePlan,
)


__all__ = [
    "KronosForecast",
    "KronosRouter",
    "RouteDecision",
    "RoutePlan",
]

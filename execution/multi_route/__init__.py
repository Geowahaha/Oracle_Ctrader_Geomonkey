"""Parallel Multi-Route Execution Engine.

For high-conviction signals, the engine plans up to three legs sharing one
thesis risk budget: a probe at market, a retest limit, and a breakout stop.
The first leg to reach `R >= free_runner_r` upgrades the rest of the basket
into free runners (stop to BE + small).

Public API:

    from execution.multi_route import MultiRoutePlanner, FreeRunnerEngine
    planner = MultiRoutePlanner(config=...)
    basket = planner.plan(signal=signal)
    if basket.eligible:
        for leg in basket.legs:
            ctrader.place(leg.to_order(), signal_run_id=basket.signal_run_id)
    free_runner = FreeRunnerEngine(...)
    free_runner.on_leg_progress(basket_id, leg_id, current_r=...)
"""
from __future__ import annotations

from execution.multi_route.free_runner import FreeRunnerEngine, FreeRunnerDirective
from execution.multi_route.planner import (
    MultiRouteBasket,
    MultiRouteLeg,
    MultiRoutePlanner,
    MultiRoutePlannerConfig,
    MultiRouteSignal,
)


__all__ = [
    "MultiRoutePlanner",
    "MultiRoutePlannerConfig",
    "MultiRouteSignal",
    "MultiRouteBasket",
    "MultiRouteLeg",
    "FreeRunnerEngine",
    "FreeRunnerDirective",
]

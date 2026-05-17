"""News-as-Edge Engine.

The existing news guard blanks the lane for T1 events (NFP / FOMC / CPI) from
`t-45m` to `t+30m`. This module preserves that behaviour by default but adds a
narrow opportunity layer: when pre-news price action already telegraphs a
surprise direction with high conviction, we ride it on a small probe with a
tight forced-exit window. When pre-news is neutral, the blanket kill remains.

Public surface:

    from analysis.news_edge import NewsEdgeRouter, NewsEvent, PriceContext
    router = NewsEdgeRouter(config=...)
    decision = router.decide(event=event, price_context=price_context, now=now)
    # decision.action in {"kill", "probe_long", "probe_short", "allow"}
"""
from __future__ import annotations

from analysis.news_edge.router import (
    NewsDecision,
    NewsEdgeRouter,
    NewsEdgeRouterConfig,
    NewsEvent,
    PriceContext,
)


__all__ = [
    "NewsEdgeRouter",
    "NewsEdgeRouterConfig",
    "NewsDecision",
    "NewsEvent",
    "PriceContext",
]

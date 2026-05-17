"""Tests for the News-as-Edge router."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.news_edge import NewsEdgeRouter, NewsEvent, PriceContext
from analysis.news_edge.router import NewsEdgeRouterConfig


_T0 = datetime(2026, 6, 5, 12, 30, 0, tzinfo=timezone.utc)


def _event() -> NewsEvent:
    return NewsEvent(
        event_id="nfp-2026-06-05",
        name="NFP",
        symbol_impacted="XAUUSD",
        scheduled_utc=_T0,
    )


def _ctx(*, drift_pct_of_atr: float, vol_ratio: float = 1.0) -> PriceContext:
    atr = 5.0
    return PriceContext(
        close_now=2300.0 + drift_pct_of_atr * atr,
        close_60m_ago=2300.0,
        atr_60m=atr,
        recent_volume_ratio=vol_ratio,
    )


def test_outside_guard_allows():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True))
    now = _T0 - timedelta(hours=2)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=1.0), now=now)
    assert d.action == "allow"


def test_chaos_window_kills_regardless():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True))
    now = _T0 + timedelta(seconds=5)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=1.5), now=now)
    assert d.action == "kill"
    assert "chaos_window" in d.reasons


def test_disabled_engine_keeps_legacy_kill():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=False))
    now = _T0 - timedelta(minutes=10)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=1.5), now=now)
    assert d.action == "kill"
    assert "news_edge_disabled" in d.reasons


def test_opportunity_window_with_long_drift_probes_long():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True, min_drift_atr=0.6))
    now = _T0 - timedelta(minutes=10)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=1.0), now=now)
    assert d.action == "probe_long"
    assert d.drift_atr == 1.0
    assert d.forced_exit_utc is not None


def test_opportunity_window_with_short_drift_probes_short():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True, min_drift_atr=0.6))
    now = _T0 - timedelta(minutes=8)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=-1.2), now=now)
    assert d.action == "probe_short"
    assert d.drift_atr == -1.2


def test_neutral_drift_in_opportunity_window_kills():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True, min_drift_atr=0.6))
    now = _T0 - timedelta(minutes=10)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=0.2), now=now)
    assert d.action == "kill"
    assert any("drift_below_threshold" in r for r in d.reasons)


def test_thin_volume_kills_even_with_strong_drift():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True, min_volume_ratio=0.85))
    now = _T0 - timedelta(minutes=10)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=1.5, vol_ratio=0.50), now=now)
    assert d.action == "kill"
    assert any("volume_too_thin" in r for r in d.reasons)


def test_within_legacy_window_but_outside_opportunity_kills():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True))
    # t-2 minutes is inside legacy guard (-45 to +30) but outside opportunity (-15 to -3).
    now = _T0 - timedelta(minutes=2)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=2.0), now=now)
    assert d.action == "kill"
    assert "outside_opportunity_window" in d.reasons


def test_post_event_window_in_legacy_guard_still_kills():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True))
    now = _T0 + timedelta(minutes=15)
    d = router.decide(event=_event(), price_context=_ctx(drift_pct_of_atr=2.0), now=now)
    assert d.action == "kill"
    assert "outside_opportunity_window" in d.reasons


def test_no_event_returns_allow():
    router = NewsEdgeRouter(config=NewsEdgeRouterConfig(enabled=True))
    d = router.decide(event=None, price_context=None, now=_T0)
    assert d.action == "allow"

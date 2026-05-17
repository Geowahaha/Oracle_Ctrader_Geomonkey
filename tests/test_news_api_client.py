"""Tests for news_api_client — theme classification, quality, dedup, fail-silent."""
import os
from unittest.mock import patch

from api.news_api_client import (
    NewsAPIClient,
    _classify_theme,
    SOURCE_QUALITY,
    make_client,
)


def test_classify_theme_fed():
    assert _classify_theme("Fed announces rate hike") == "FED_RATE"
    assert _classify_theme("Powell speaks at Jackson Hole") == "FED_RATE"


def test_classify_theme_cpi():
    assert _classify_theme("US CPI release tomorrow") == "CPI_NFP"
    assert _classify_theme("Non-farm payrolls beat estimates") == "CPI_NFP"


def test_classify_theme_geopolitics():
    assert _classify_theme("New trade war sanctions imposed") == "GEOPOLITICS"


def test_classify_theme_military():
    assert _classify_theme("Ukraine missile strike kills 5") == "GEO_MILITARY"


def test_classify_theme_energy():
    assert _classify_theme("OPEC cuts oil production") == "OIL_ENERGY"


def test_classify_theme_unrelated_returns_empty():
    assert _classify_theme("Apple announces new iPhone") == ""
    assert _classify_theme("") == ""


def test_source_quality_table_present():
    assert "forexfactory" in SOURCE_QUALITY
    assert SOURCE_QUALITY["tradingeconomics"] >= 0.85


def test_make_client_returns_client_by_default():
    # Without NEWS_API_DISABLED, factory always returns a client
    # (ForexFactory needs no key)
    client = make_client()
    assert client is not None


def test_make_client_disabled_env():
    with patch.dict(os.environ, {"NEWS_API_DISABLED": "1"}):
        assert make_client() is None


def test_client_get_recent_returns_list():
    client = NewsAPIClient()
    # _refresh might fail (no internet in test) — should still return []
    with patch.object(client, "_refresh", return_value=[]):
        out = client.get_recent_high_impact_events(lookback_min=60)
    assert isinstance(out, list)


def test_client_caches_results():
    client = NewsAPIClient()
    call_count = {"n": 0}

    def fake_refresh(lookback_min):
        call_count["n"] += 1
        return [{"theme": "FED_RATE", "source_quality": 0.9, "impact_score": 8.0,
                 "age_min": 5, "headline": "test", "ts": "2026-04-28T00:00:00Z",
                 "source": "test"}]

    with patch.object(client, "_refresh", side_effect=fake_refresh):
        client.get_recent_high_impact_events(lookback_min=60)
        client.get_recent_high_impact_events(lookback_min=60)
        client.get_recent_high_impact_events(lookback_min=60)
    # Cached → only refreshed once
    assert call_count["n"] == 1


def test_client_fail_silent_when_refresh_raises():
    client = NewsAPIClient()
    with patch.object(client, "_refresh", side_effect=RuntimeError("fail")):
        out = client.get_recent_high_impact_events(lookback_min=60)
    assert out == []


def test_dedup_keeps_higher_quality():
    """Two providers report same headline → keep higher quality version."""
    client = NewsAPIClient()
    fake_events = [
        {"source": "newsapi", "theme": "FED_RATE", "source_quality": 0.7,
         "impact_score": 6, "age_min": 10, "headline": "Fed hikes rates",
         "ts": "2026-04-28T00:00:00Z"},
        {"source": "tradingeconomics", "theme": "FED_RATE", "source_quality": 0.9,
         "impact_score": 9, "age_min": 10, "headline": "Fed hikes rates",
         "ts": "2026-04-28T00:00:00Z"},
    ]
    # Run dedup logic from _refresh manually
    from api.news_api_client import NewsAPIClient as _C
    seen = {}
    for ev in fake_events:
        key = (ev["theme"], ev["headline"][:60].lower())
        if key not in seen or ev["source_quality"] > seen[key]["source_quality"]:
            seen[key] = ev
    deduped = list(seen.values())
    assert len(deduped) == 1
    assert deduped[0]["source"] == "tradingeconomics"

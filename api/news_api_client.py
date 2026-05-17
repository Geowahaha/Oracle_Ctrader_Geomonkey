"""News API client — multi-provider with quality scoring.

Implements the interface expected by analysis/shock_detector_v2.py:
    client.get_recent_high_impact_events(lookback_min=90)
        -> list of {timestamp, theme, source_quality, impact_score, age_min, headline}

Providers (any combination, configured via env keys):
    - ForexFactory RSS (NO KEY required, free) ✅ works immediately
    - NewsAPI.org (NEWSAPI_KEY)
    - TradingEconomics (TRADINGECONOMICS_KEY)
    - Finnhub (FINNHUB_KEY) — for company/macro news

Each provider has source_quality 0-1. Caches results 5 min in memory to
respect rate limits. Fail-silent: any provider failure → returns [] for
that source, others still contribute.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib import request as _urlreq, parse as _urlparse

# Provider source quality (used by detector to filter low-quality events)
SOURCE_QUALITY = {
    "forexfactory": 0.85,
    "newsapi": 0.80,
    "tradingeconomics": 0.90,
    "finnhub": 0.82,
}

# Theme keyword maps (used to classify free-text headlines into themes)
_THEME_KEYWORDS = {
    "FED_RATE": ["fed", "powell", "fomc", "rate hike", "rate cut", "interest rate", "ecb"],
    "CPI_NFP": ["cpi", "nfp", "non-farm", "payroll", "inflation", "ppi", "core pce"],
    "GEOPOLITICS": ["sanction", "tariff", "trade war", "summit", "treaty", "election"],
    "GEO_MILITARY": ["war", "missile", "strike", "invasion", "military", "ukraine", "iran"],
    "OIL_ENERGY": ["opec", "oil", "crude", "gas pipeline", "energy crisis", "spr"],
}


def _classify_theme(text: str) -> str:
    t = (text or "").lower()
    for theme, keys in _THEME_KEYWORDS.items():
        for k in keys:
            if k in t:
                return theme
    return ""


def _http_get(url: str, *, timeout: float = 6.0, headers: dict | None = None) -> bytes:
    req = _urlreq.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0 dexter-newsclient"})
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return r.read()


def _age_min_from_iso(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 999.0


# ====================== Provider: ForexFactory RSS ======================

def _ff_impact_score(impact: str) -> float:
    s = (impact or "").lower()
    if "high" in s or "red" in s:
        return 9.0
    if "medium" in s or "orange" in s or "yellow" in s:
        return 5.0
    return 2.0


def _fetch_forexfactory(lookback_min: int) -> list[dict]:
    """ForexFactory RSS feed — completely free, no key needed."""
    out: list[dict] = []
    try:
        # RSS feed of upcoming + recent events
        data = _http_get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=8.0)
        root = ET.fromstring(data)
        for item in root.iter("event"):
            title = (item.findtext("title") or "").strip()
            country = (item.findtext("country") or "").strip().upper()
            impact = (item.findtext("impact") or "").strip()
            date = (item.findtext("date") or "").strip()  # "MM-DD-YYYY"
            time_s = (item.findtext("time") or "").strip()  # "HH:MMam/pm"
            # Parse FF datetime (US/Eastern but treat as UTC for simplicity)
            try:
                ts_dt = datetime.strptime(f"{date} {time_s}", "%m-%d-%Y %I:%M%p")
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60
            except Exception:
                continue
            if age < -lookback_min or age > lookback_min:
                continue
            theme = _classify_theme(title) or ("CPI_NFP" if country == "USD" else "")
            if not theme:
                continue
            out.append({
                "source": "forexfactory",
                "theme": theme,
                "source_quality": SOURCE_QUALITY["forexfactory"],
                "impact_score": _ff_impact_score(impact),
                "age_min": abs(age),
                "headline": f"{country} | {title}",
                "ts": ts_dt.isoformat(),
            })
    except Exception:
        return out
    return out


# ====================== Provider: NewsAPI.org ======================

def _fetch_newsapi(lookback_min: int, api_key: str) -> list[dict]:
    out: list[dict] = []
    try:
        # Search top headlines with finance keywords
        params = {
            "q": "gold OR fed OR fomc OR cpi OR nfp OR powell OR geopolitics OR oil",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 20,
            "apiKey": api_key,
        }
        url = "https://newsapi.org/v2/everything?" + _urlparse.urlencode(params)
        data = _http_get(url, timeout=8.0)
        import json as _json
        payload = _json.loads(data)
        for art in payload.get("articles", []) or []:
            title = (art.get("title") or "").strip()
            published = art.get("publishedAt", "")
            theme = _classify_theme(title + " " + (art.get("description") or ""))
            if not theme:
                continue
            age = _age_min_from_iso(published)
            if age > lookback_min:
                continue
            # Heuristic impact: longer headline + reputable source = higher
            src_name = (art.get("source", {}) or {}).get("name", "")
            reputable = any(s in src_name.lower() for s in
                            ["reuters", "bloomberg", "ap ", "cnbc", "wsj", "ft.com", "marketwatch"])
            impact = 7.0 if reputable else 4.0
            out.append({
                "source": "newsapi",
                "theme": theme,
                "source_quality": SOURCE_QUALITY["newsapi"] * (1.0 if reputable else 0.7),
                "impact_score": impact,
                "age_min": age,
                "headline": title,
                "ts": published,
            })
    except Exception:
        return out
    return out


# ====================== Provider: TradingEconomics ======================

def _fetch_tradingeconomics(lookback_min: int, api_key: str) -> list[dict]:
    out: list[dict] = []
    try:
        url = f"https://api.tradingeconomics.com/calendar?c={api_key}&format=json"
        data = _http_get(url, timeout=8.0)
        import json as _json
        events = _json.loads(data) or []
        for ev in events:
            ts = ev.get("Date", "")
            if not ts:
                continue
            age = _age_min_from_iso(ts)
            if age < -lookback_min or age > lookback_min:
                continue
            event_name = ev.get("Event", "") or ""
            country = (ev.get("Country") or "").upper()
            importance = int(ev.get("Importance", 0) or 0)
            if importance < 2:  # 1=low, 2=med, 3=high
                continue
            theme = _classify_theme(event_name) or ("CPI_NFP" if country == "UNITED STATES" else "")
            if not theme:
                continue
            impact = 9.0 if importance == 3 else 5.0
            out.append({
                "source": "tradingeconomics",
                "theme": theme,
                "source_quality": SOURCE_QUALITY["tradingeconomics"],
                "impact_score": impact,
                "age_min": abs(age),
                "headline": f"{country} | {event_name}",
                "ts": ts,
            })
    except Exception:
        return out
    return out


# ====================== Provider: Finnhub ======================

def _fetch_finnhub(lookback_min: int, api_key: str) -> list[dict]:
    out: list[dict] = []
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
        data = _http_get(url, timeout=8.0)
        import json as _json
        items = _json.loads(data) or []
        for it in items[:30]:
            headline = (it.get("headline") or "").strip()
            ts_unix = float(it.get("datetime", 0) or 0)
            if ts_unix <= 0:
                continue
            age = (time.time() - ts_unix) / 60
            if age > lookback_min:
                continue
            theme = _classify_theme(headline + " " + (it.get("summary") or ""))
            if not theme:
                continue
            out.append({
                "source": "finnhub",
                "theme": theme,
                "source_quality": SOURCE_QUALITY["finnhub"],
                "impact_score": 6.0,
                "age_min": age,
                "headline": headline,
                "ts": datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat(),
            })
    except Exception:
        return out
    return out


# ====================== Aggregate Client ======================

class NewsAPIClient:
    """Aggregates events across all configured providers. 5-min cache."""

    def __init__(self):
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._cache_ttl_sec: float = 300.0

    def _refresh(self, lookback_min: int) -> list[dict]:
        all_events: list[dict] = []

        # 1. ForexFactory (free, always on)
        all_events.extend(_fetch_forexfactory(lookback_min))

        # 2. NewsAPI.org (if key)
        key = os.environ.get("NEWSAPI_KEY", "").strip()
        if key:
            all_events.extend(_fetch_newsapi(lookback_min, key))

        # 3. TradingEconomics (if key)
        te_key = os.environ.get("TRADINGECONOMICS_KEY", "").strip()
        if te_key:
            all_events.extend(_fetch_tradingeconomics(lookback_min, te_key))

        # 4. Finnhub (if key)
        fn_key = os.environ.get("FINNHUB_KEY", "").strip()
        if fn_key:
            all_events.extend(_fetch_finnhub(lookback_min, fn_key))

        # Deduplicate by (theme, headline-prefix) — keep highest quality
        seen: dict[tuple, dict] = {}
        for ev in all_events:
            key = (ev["theme"], (ev.get("headline") or "")[:60].lower())
            if key not in seen or ev["source_quality"] > seen[key]["source_quality"]:
                seen[key] = ev
        return sorted(seen.values(), key=lambda e: e["age_min"])

    def get_recent_high_impact_events(self, lookback_min: int = 90) -> list[dict]:
        """Public interface called by shock_detector_v2."""
        try:
            now = time.time()
            if (now - self._cache_ts) > self._cache_ttl_sec:
                self._cache = self._refresh(lookback_min)
                self._cache_ts = now
            return list(self._cache)
        except Exception:
            return []


def make_client() -> NewsAPIClient | None:
    """Factory. Returns a client if at least ONE provider is available
    (ForexFactory always counts → never returns None unless we want to
    explicitly disable via NEWS_API_DISABLED=1)."""
    if os.environ.get("NEWS_API_DISABLED", "0") in ("1", "true", "True"):
        return None
    return NewsAPIClient()

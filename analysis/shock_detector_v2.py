"""Shock Detector V2 — multi-source confirmation, never hard-blocks.

3 independent layers:
  A) Price-based volatility/range expansion (40%)
  B) News API confirmation (40%) — pluggable, stubs return 0 if no key
  C) Cross-asset correlation (20%) — DXY/VIX/yields, stub for now

Composite score 0-100 with recency decay (30-min half-life). The score is
ONLY consumed by shock_action_tilt — which never blocks, only tilts size.

Per user rule: "โอกาสมาก่อน" — even at score 100, system still trades
(at reduced size). Real opportunities (high reversal_setup score, overlap
confirmed) further override the shock penalty.
"""
from __future__ import annotations

import math
from typing import Any


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _norm(c: Any) -> dict | None:
    if c is None:
        return None
    if isinstance(c, dict):
        h = _f(c.get("high", c.get("h")))
        l = _f(c.get("low", c.get("l")))
        cl = _f(c.get("close", c.get("c")))
        o = _f(c.get("open", c.get("o")))
    else:
        h = _f(getattr(c, "high", None))
        l = _f(getattr(c, "low", None))
        cl = _f(getattr(c, "close", None))
        o = _f(getattr(c, "open", None))
    if h <= 0 or l <= 0 or h < l:
        return None
    return {"open": o, "high": h, "low": l, "close": cl}


def _atr(c: list[dict], p: int) -> float:
    if len(c) < 2:
        return 0.0
    trs = []
    for i in range(1, len(c)):
        h, l = c[i]["high"], c[i]["low"]
        pc = c[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    w = trs[-p:] if len(trs) >= p else trs
    return sum(w) / len(w) if w else 0.0


def get_price_shock_score(m1: Any = None, m5: Any = None) -> dict:
    """Layer A: detect price/volatility shock. Returns {score: 0-100, reasons: []}."""
    out = {"score": 0.0, "reasons": [], "atr_ratio": 0.0, "range_ratio": 0.0}
    norm5: list[dict] = []
    try:
        for c in (m5 or [])[-100:]:
            n = _norm(c)
            if n is not None:
                norm5.append(n)
    except TypeError:
        return out
    if len(norm5) < 30:
        out["reasons"].append("insufficient_m5")
        return out

    # ATR ratio: short window vs long
    atr_short = _atr(norm5[-3:] + [norm5[-1]], 14) if len(norm5) >= 14 else 0.0
    atr_long = _atr(norm5, 50) if len(norm5) >= 50 else _atr(norm5, len(norm5) - 1)
    if atr_long <= 0:
        return out
    atr_ratio = atr_short / atr_long
    out["atr_ratio"] = round(atr_ratio, 3)

    # Range expansion: latest bar range vs avg range
    last_range = norm5[-1]["high"] - norm5[-1]["low"]
    avg_range = sum(c["high"] - c["low"] for c in norm5[-20:-1]) / 19 if len(norm5) >= 20 else 0.0
    range_ratio = last_range / avg_range if avg_range > 0 else 0.0
    out["range_ratio"] = round(range_ratio, 3)

    score = 0.0
    if atr_ratio >= 3.0:
        score += 40.0
        out["reasons"].append(f"atr_spike_{atr_ratio:.1f}x")
    elif atr_ratio >= 2.0:
        score += 25.0
        out["reasons"].append(f"atr_elevated_{atr_ratio:.1f}x")
    elif atr_ratio >= 1.5:
        score += 12.0
        out["reasons"].append(f"atr_mild_{atr_ratio:.1f}x")

    if range_ratio >= 4.0:
        score += 35.0
        out["reasons"].append(f"range_spike_{range_ratio:.1f}x")
    elif range_ratio >= 2.5:
        score += 18.0
        out["reasons"].append(f"range_elevated_{range_ratio:.1f}x")

    # M1 surge: last 3 M1 bars total range vs M5 range
    if m1:
        norm1: list[dict] = []
        try:
            for c in (m1 or [])[-10:]:
                n = _norm(c)
                if n is not None:
                    norm1.append(n)
        except TypeError:
            pass
        if len(norm1) >= 3:
            m1_total = sum(c["high"] - c["low"] for c in norm1[-3:])
            if avg_range > 0 and m1_total / avg_range >= 1.5:
                score += 15.0
                out["reasons"].append("m1_3bar_surge")

    out["score"] = round(min(100.0, max(0.0, score)), 1)
    return out


def get_news_shock_score(news_client: Any = None, themes_whitelist: list[str] | None = None) -> dict:
    """Layer B: news API shock score. Stub returns 0 unless news_client provided.

    news_client interface: client.get_recent_high_impact_events(lookback_min=90)
        returns list of {timestamp, theme, source_quality, impact_score, headline}
    """
    out = {"score": 0.0, "reasons": [], "events": []}
    if news_client is None:
        out["reasons"].append("no_news_client")
        return out
    themes = themes_whitelist or [
        "GEOPOLITICS", "FED_RATE", "OIL_ENERGY", "CPI_NFP", "GEO_MILITARY"
    ]
    try:
        events = news_client.get_recent_high_impact_events(lookback_min=90) or []
    except Exception:
        out["reasons"].append("news_client_error")
        return out

    score = 0.0
    qualified: list[dict] = []
    for ev in events[:20]:
        theme = str(ev.get("theme", "")).upper()
        if theme not in themes:
            continue
        quality = float(ev.get("source_quality", 0.0) or 0.0)
        if quality < 0.75:
            continue
        impact = float(ev.get("impact_score", 0.0) or 0.0)
        age_min = float(ev.get("age_min", 0.0) or 0.0)
        recency = max(0.0, 1.0 - age_min / 90.0)
        contrib = impact * quality * recency
        score += contrib * 5.0
        qualified.append(ev)

    # Require at least 2 qualified high-quality sources for full score
    if len(qualified) < 2:
        score *= 0.5
        out["reasons"].append("only_one_source")

    out["score"] = round(min(100.0, max(0.0, score)), 1)
    out["events"] = qualified[:5]
    return out


def get_cross_asset_shock_score(cross_asset_client: Any = None) -> dict:
    """Layer C: cross-asset correlation. Stub returns 0 unless client provided.

    client interface: client.get_correlation_metrics()
        returns {dxy_change_pct, vix_level, vix_change_pct, yield_change_bp}
    """
    out = {"score": 0.0, "reasons": []}
    if cross_asset_client is None:
        out["reasons"].append("no_cross_asset_client")
        return out
    try:
        m = cross_asset_client.get_correlation_metrics() or {}
    except Exception:
        out["reasons"].append("cross_asset_error")
        return out

    score = 0.0
    dxy = abs(_f(m.get("dxy_change_pct"), 0.0))
    vix = _f(m.get("vix_level"), 0.0)
    vix_chg = _f(m.get("vix_change_pct"), 0.0)
    yield_bp = abs(_f(m.get("yield_change_bp"), 0.0))

    if dxy >= 0.5:
        score += 25.0
        out["reasons"].append(f"dxy_move_{dxy:.2f}pct")
    elif dxy >= 0.25:
        score += 12.0
    if vix >= 25 or vix_chg >= 10:
        score += 30.0
        out["reasons"].append(f"vix_{vix:.1f}_chg_{vix_chg:.1f}pct")
    elif vix >= 20:
        score += 15.0
    if yield_bp >= 10:
        score += 25.0
        out["reasons"].append(f"yield_{yield_bp:.1f}bp")

    out["score"] = round(min(100.0, max(0.0, score)), 1)
    return out


def compute_combined_shock(
    price: dict | None = None,
    news: dict | None = None,
    cross_asset: dict | None = None,
    *,
    price_weight: float = 0.4,
    news_weight: float = 0.4,
    cross_weight: float = 0.2,
) -> dict:
    p = float((price or {}).get("score", 0.0))
    n = float((news or {}).get("score", 0.0))
    c = float((cross_asset or {}).get("score", 0.0))
    composite = p * price_weight + n * news_weight + c * cross_weight
    composite = max(0.0, min(100.0, composite))
    reasons = []
    for d in (price, news, cross_asset):
        if d and isinstance(d.get("reasons"), list):
            reasons.extend(d["reasons"])
    return {
        "score": round(composite, 1),
        "layers": {
            "price": p, "news": n, "cross_asset": c,
        },
        "reasons": reasons[:10],
    }


def decay_score(score: float, age_minutes: float, half_life_min: float = 30.0) -> float:
    """Apply exponential decay. Score halves every half_life_min."""
    if age_minutes <= 0 or half_life_min <= 0:
        return float(score)
    factor = 0.5 ** (age_minutes / half_life_min)
    return round(max(0.0, float(score) * factor), 2)


def auto_recover_check(*, calm_minutes: float, calm_threshold_min: float = 60.0,
                      atr_ratio: float = 0.0) -> bool:
    """Force score=0 if market has been calm > N min and atr_ratio normal."""
    return calm_minutes >= calm_threshold_min and atr_ratio < 1.3

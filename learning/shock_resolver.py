"""Shock state resolver — periodic background refresh.

Reads price (M1+M5) from XAUUSDProvider, computes shock score, caches state
to data/runtime/shock_state_v2.json. Optional news/cross-asset clients
plug in via env-keyed factory.

Auto-recover: if calm > 60 min AND atr_ratio < 1.3 → force score=0.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "runtime" / "shock_state_v2.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"score": 0.0, "tier": "normal", "ts": "", "layers": {}, "reasons": []}


def write_state(state: dict) -> bool:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        return True
    except Exception:
        return False


def _fetch_xau(tf: str, bars: int) -> list[dict]:
    try:
        from market.data_fetcher import XAUUSDProvider
        df = XAUUSDProvider().fetch(timeframe=tf, bars=bars)
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            try:
                out.append({"open": float(row["open"]), "high": float(row["high"]),
                            "low": float(row["low"]), "close": float(row["close"])})
            except Exception:
                continue
        return out
    except Exception:
        return []


def _make_news_client() -> Any | None:
    """Factory: build news client only if env keys present (deferred to next session)."""
    if not os.environ.get("NEWSAPI_KEY"):
        return None
    # Placeholder for future api/news_api_client wrapper
    return None


def _make_cross_asset_client() -> Any | None:
    if not os.environ.get("FRED_API_KEY"):
        return None
    return None


def refresh_state(*, save: bool = True) -> dict:
    """Compute fresh shock state. Returns and optionally saves."""
    from analysis.shock_detector_v2 import (
        get_price_shock_score,
        get_news_shock_score,
        get_cross_asset_shock_score,
        compute_combined_shock,
        auto_recover_check,
    )
    from analysis.shock_action_tilt import get_tier

    m5 = _fetch_xau("5m", 100)
    m1 = _fetch_xau("1m", 30)

    price = get_price_shock_score(m1=m1, m5=m5)
    news = get_news_shock_score(_make_news_client())
    cross = get_cross_asset_shock_score(_make_cross_asset_client())

    weights = {
        "price": float(os.environ.get("SHOCK_V2_PRICE_WEIGHT", "0.4") or 0.4),
        "news": float(os.environ.get("SHOCK_V2_NEWS_WEIGHT", "0.4") or 0.4),
        "cross": float(os.environ.get("SHOCK_V2_CROSS_ASSET_WEIGHT", "0.2") or 0.2),
    }
    combined = compute_combined_shock(
        price=price, news=news, cross_asset=cross,
        price_weight=weights["price"],
        news_weight=weights["news"],
        cross_weight=weights["cross"],
    )

    # Auto-recovery check
    prev = read_state()
    prev_score = float(prev.get("score", 0.0) or 0.0)
    atr_ratio = float(price.get("atr_ratio", 0.0) or 0.0)
    calm_minutes = 0.0
    try:
        if prev.get("ts"):
            prev_ts = datetime.fromisoformat(str(prev["ts"]).replace("Z", "+00:00"))
            calm_minutes = (datetime.now(timezone.utc) - prev_ts).total_seconds() / 60
    except Exception:
        pass
    final_score = combined["score"]
    if combined["score"] < 30 and prev_score < 30 and auto_recover_check(
        calm_minutes=calm_minutes, atr_ratio=atr_ratio
    ):
        final_score = 0.0
        combined["reasons"].insert(0, "auto_recovered_calm")

    tier = get_tier(final_score)
    state = {
        "score": round(final_score, 1),
        "tier": tier["name"],
        "ts": _now_iso(),
        "layers": combined["layers"],
        "reasons": combined["reasons"][:10],
        "weights_used": weights,
        "calm_minutes": round(calm_minutes, 1),
        "atr_ratio": atr_ratio,
    }
    if save:
        write_state(state)
    return state


def get_current_shock(*, max_staleness_min: float = 10.0) -> dict:
    """Read cached state. If older than max_staleness, return zero state."""
    state = read_state()
    try:
        ts = state.get("ts")
        if not ts:
            return {"score": 0.0, "tier": "normal", "stale": True, "reasons": ["no_state_yet"]}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 60
        if age > max_staleness_min:
            return {"score": 0.0, "tier": "normal", "stale": True,
                    "reasons": [f"state_stale_{age:.0f}min"], "score_cached": state.get("score")}
        state["stale"] = False
        return state
    except Exception:
        return {"score": 0.0, "tier": "normal", "stale": True, "reasons": ["read_error"]}

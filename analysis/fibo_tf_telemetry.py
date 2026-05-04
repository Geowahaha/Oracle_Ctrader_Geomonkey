"""Pure helpers for XAU Fibo multi-timeframe telemetry.

These helpers intentionally avoid numpy/pandas so tests and ops scripts can run
in the minimal WSL control environment. They add labels/ids only; they do not
change Fibo trading behavior.
"""
from __future__ import annotations

import hashlib
from typing import Any


_TF_ALIASES = {
    "1M": "M1", "M1": "M1", "1MIN": "M1", "1MINUTE": "M1", "1MINUTES": "M1",
    "5M": "M5", "M5": "M5", "5MIN": "M5", "5MINUTES": "M5",
    "15M": "M15", "M15": "M15", "15MIN": "M15", "15MINUTES": "M15",
    "30M": "M30", "M30": "M30", "30MIN": "M30", "30MINUTES": "M30",
    "1H": "H1", "H1": "H1", "60M": "H1", "1HR": "H1", "1HOUR": "H1",
    "4H": "H4", "H4": "H4", "240M": "H4", "4HOUR": "H4",
    "1D": "D1", "D1": "D1", "DAILY": "D1", "DAY": "D1",
    "1W": "W1", "W1": "W1", "WEEKLY": "W1", "WEEK": "W1",
}


def normalize_tf(tf: Any, default: str = "") -> str:
    raw = str(tf or default or "").strip().upper().replace(" ", "")
    return _TF_ALIASES.get(raw, raw or str(default or ""))


def fibo_ratio_zone(ratio: Any) -> dict:
    """Return first-class zone bucket for a retracement ratio.

    0.89 is treated as 0.886-deep by default per OPUS review until the user
    specifies otherwise.
    """
    try:
        value = float(ratio)
    except Exception:
        return {"ratio_zone": "unknown", "ratio_zone_distance": None, "ratio_zone_center": None}
    zones = [
        ("near_0.50", 0.500, 0.025),
        ("near_0.618", 0.618, 0.018),
        ("0.65_0.70", 0.675, 0.035),
        ("near_0.786", 0.786, 0.030),
        ("0.886_deep_retest", 0.886, 0.030),
    ]
    best = min(zones, key=lambda z: abs(value - z[1]))
    name, center, tol = best
    dist = abs(value - center)
    if dist <= tol:
        return {"ratio_zone": name, "ratio_zone_distance": round(dist, 5), "ratio_zone_center": center}
    return {"ratio_zone": "other", "ratio_zone_distance": round(dist, 5), "ratio_zone_center": center}


def fibo_tf_metadata(*, entry_tf: Any, setup_tf: Any = "", parent_tf: Any = "", source: str = "fibo_xauusd") -> dict:
    tf = normalize_tf(entry_tf, "")
    setup = normalize_tf(setup_tf, tf)
    parent = normalize_tf(parent_tf, setup)
    display = f"fibo_{tf}_xauusd" if tf else "fibo_xauusd"
    return {
        "tf_label": tf,
        "entry_tf": tf,
        "setup_tf": setup,
        "parent_tf": parent,
        "display_source": display,
        "source": str(source or "fibo_xauusd"),
        "source_stable": str(source or "fibo_xauusd"),
        "tf_comment": f"dexter|{display}|XAUUSD" if tf else "dexter|fibo_xauusd|XAUUSD",
    }


def fibo_parent_impulse_id(parent_tf: Any, fib_levels: Any, *, symbol: str = "XAUUSD") -> str:
    tf = normalize_tf(parent_tf, "")
    direction = str(getattr(fib_levels, "direction", "") or "").lower()
    start = round(float(getattr(fib_levels, "swing_start", 0.0) or 0.0), 2)
    end = round(float(getattr(fib_levels, "swing_end", 0.0) or 0.0), 2)
    start_idx = int(getattr(fib_levels, "swing_start_idx", 0) or 0)
    end_idx = int(getattr(fib_levels, "swing_end_idx", 0) or 0)
    payload = f"{str(symbol).upper()}|{tf}|{direction}|{start}|{end}|{start_idx}|{end_idx}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"{tf}:{direction}:{digest}"


def fibo_telemetry_payload(*, entry_tf: Any, setup_tf: Any, parent_tf: Any, fibo_ctx: Any, fib_levels: Any = None, source: str = "fibo_xauusd") -> dict:
    fib = fib_levels if fib_levels is not None else getattr(fibo_ctx, "fib_levels", None)
    ratio = getattr(fibo_ctx, "retracement_depth", None)
    if ratio in (None, 0, 0.0):
        ratio = getattr(fibo_ctx, "nearest_level_ratio", None)
    meta = fibo_tf_metadata(entry_tf=entry_tf, setup_tf=setup_tf, parent_tf=parent_tf, source=source)
    zone = fibo_ratio_zone(ratio)
    parent_id = fibo_parent_impulse_id(meta["parent_tf"], fib, symbol="XAUUSD") if fib is not None else ""
    return {
        **meta,
        **zone,
        "parent_impulse_id": parent_id,
        "retracement_ratio_for_zone": round(float(ratio or 0.0), 5) if ratio is not None else 0.0,
    }

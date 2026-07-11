"""No-lookahead regime features for the volume-profile producer.

This is deliberately measurement-only: it describes the profile context of a
signal without accepting, rejecting, or sizing an order.
"""
from __future__ import annotations

from typing import Any


def profile_regime(bars: list[dict[str, Any]], *, lookback: int = 48) -> dict[str, float | str]:
    """Return causal trend efficiency and relative tick-volume quality."""
    window = list(bars[-lookback:])
    if len(window) < 3:
        return {"state": "unknown", "efficiency": 0.0, "volume_ratio": 0.0}
    closes = [float(b.get("close") or 0.0) for b in window]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0
    vols = [max(0.0, float(b.get("volume") or 0.0)) for b in window]
    baseline = sum(vols[:-1]) / max(1, len(vols) - 1)
    ratio = vols[-1] / baseline if baseline > 0 else 0.0
    state = "directional" if efficiency >= 0.35 else "rotational"
    return {"state": state, "efficiency": round(efficiency, 4), "volume_ratio": round(ratio, 4)}

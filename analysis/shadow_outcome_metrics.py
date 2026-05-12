"""Pure helpers for shadow-trade path excursion metrics."""
from __future__ import annotations

from typing import Iterable


def compute_shadow_path_metrics(
    direction: str,
    *,
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    bars: Iterable[tuple[float, float]],
) -> dict[str, float | str | None]:
    """Resolve a shadow signal and record path MAE/MFE in R units.

    ``bars`` are ``(high, low)`` tuples after the signal.  This mirrors the
    existing conservative shadow resolver: if TP and SL occur in the same coarse
    bar, TP wins.  MAE/MFE are still computed over observed bars up to and
    including the resolving bar so Opus can reject routes with ugly path risk.
    """
    side = str(direction or "").strip().lower()
    entry = float(entry or 0.0)
    sl = float(stop_loss or 0.0)
    tp1 = float(take_profit_1 or 0.0)
    risk = abs(entry - sl)
    if side not in {"long", "buy", "short", "sell"} or entry <= 0.0 or sl <= 0.0 or tp1 <= 0.0 or risk < 1e-6:
        return {"outcome": "expired", "pnl_rr": None, "mae_rr": None, "mfe_rr": None}
    side = "long" if side in {"long", "buy"} else "short"
    outcome = "expired"
    pnl_rr: float | None = None
    max_adverse = 0.0
    max_favorable = 0.0
    saw_bar = False
    for high_raw, low_raw in list(bars or []):
        high = float(high_raw or 0.0)
        low = float(low_raw or 0.0)
        if high <= 0.0 or low <= 0.0:
            continue
        saw_bar = True
        if side == "long":
            max_adverse = max(max_adverse, max(0.0, entry - low))
            max_favorable = max(max_favorable, max(0.0, high - entry))
            tp_hit = high >= tp1
            sl_hit = low <= sl
        else:
            max_adverse = max(max_adverse, max(0.0, high - entry))
            max_favorable = max(max_favorable, max(0.0, entry - low))
            tp_hit = low <= tp1
            sl_hit = high >= sl
        if tp_hit:
            outcome = "tp_hit"
            pnl_rr = round(abs(tp1 - entry) / risk, 4)
            break
        if sl_hit:
            outcome = "sl_hit"
            pnl_rr = -1.0
            break
    if not saw_bar:
        return {"outcome": "expired", "pnl_rr": None, "mae_rr": None, "mfe_rr": None}
    return {
        "outcome": outcome,
        "pnl_rr": pnl_rr,
        "mae_rr": round(max_adverse / risk, 4),
        "mfe_rr": round(max_favorable / risk, 4),
    }

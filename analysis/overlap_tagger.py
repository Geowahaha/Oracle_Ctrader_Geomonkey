"""Live signal tagger — single chokepoint for overlap context.

Called from ctrader_executor.execute_signal() top, tags XAUUSD signals with
overlap zone context + reversal stack score. Currently SHADOW ONLY: size
multiplier = 1.0 (per 30-day BT decision: shadow_only_collect_more).

Once live data shows edge for high-conviction overlaps (≥50 trades), the
multiplier graduates to 1.10 / 1.30 via env flags.

Per project rules:
- Additive only: never modifies entry/sl/tp, never blocks.
- Demo-account safe: size tilt is UP-only and capped at 1.0 today.
- Fail-silent: any exception → no tag, signal passes through unchanged.
"""
from __future__ import annotations

import os
from typing import Any

try:
    from analysis.zone_overlap_detector import (
        detect_zones,
        overlap_at_price,
        compression_score,
        infer_reversal_bias,
    )
    from analysis.reversal_stack import score_reversal, size_tilt_from_score
except Exception:
    detect_zones = None  # type: ignore


def _enabled() -> bool:
    return os.environ.get("XAU_OVERLAP_TAG_ENABLED", "1") not in ("0", "false", "False", "")


def _bool_env(key: str, default: str = "1") -> bool:
    return os.environ.get(key, default) not in ("0", "false", "False", "")


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _fetch_m5(symbol: str, bars: int = 250) -> list[dict]:
    """Best-effort M5 fetch. Returns [] on any failure."""
    try:
        from market.data_fetcher import XAUUSDProvider
        if str(symbol).strip().upper() != "XAUUSD":
            return []
        df = XAUUSDProvider().fetch(timeframe="5m", bars=bars)
        if df is None or df.empty:
            return []
        out: list[dict] = []
        for _, row in df.iterrows():
            try:
                out.append({
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


def tag_signal(signal: Any) -> None:
    """Mutates signal.raw_scores in place with overlap_tag dict. Never raises."""
    try:
        if signal is None or not _enabled() or detect_zones is None:
            return
        symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
        if symbol != "XAUUSD":
            return
        direction = str(getattr(signal, "direction", "") or "").strip().lower()
        if direction not in ("long", "short", "buy", "sell"):
            return
        entry = float(getattr(signal, "entry", 0.0) or 0.0)
        if entry <= 0:
            return

        m5 = _fetch_m5(symbol, bars=250)
        raw = getattr(signal, "raw_scores", None)
        if raw is None:
            raw = {}
            try:
                signal.raw_scores = raw
            except Exception:
                return

        if not m5:
            raw["overlap_tag"] = {"at_overlap_zone": False, "reason": "no_m5_data"}
            return

        zones = detect_zones(m5, width_atr_mult=1.0, max_age_bars=300)
        ov = overlap_at_price(zones, entry, min_quality=0.2)

        if ov is None:
            raw["overlap_tag"] = {
                "at_overlap_zone": False,
                "compression_score": compression_score(m5),
                "reversal_bias": infer_reversal_bias(m5),
                "zones_detected": len(zones),
            }
            return

        bias = infer_reversal_bias(m5)
        comp = compression_score(m5)
        bias_aligned = (
            (bias == "long" and direction in ("long", "buy"))
            or (bias == "short" and direction in ("short", "sell"))
        )

        score = score_reversal(
            overlap=ov,
            direction=direction,
            m1_candles=None,  # M1 not in scope here; live engulfing handled by separate shadow logger
            compression_score=comp,
            overlap_top=ov.get("overlap_top"),
            overlap_bottom=ov.get("overlap_bottom"),
        )
        confirms = score["confirms"]

        # Size tilt — gated by env flag, default 1.0 (shadow only) per 30-day BT
        tilt_4plus = _float_env("XAU_OVERLAP_TILT_CONFIRMS_4PLUS", 1.0)
        tilt_2_3 = _float_env("XAU_OVERLAP_TILT_CONFIRMS_2_3", 1.0)
        if not bias_aligned:
            mult = 1.0
        elif confirms >= 4:
            mult = max(1.0, tilt_4plus)
        elif confirms >= 2:
            mult = max(1.0, tilt_2_3)
        else:
            mult = 1.0

        raw["overlap_tag"] = {
            "at_overlap_zone": True,
            "overlap_quality": ov.get("overlap_quality"),
            "overlap_top": ov.get("overlap_top"),
            "overlap_bottom": ov.get("overlap_bottom"),
            "overlap_mid": ov.get("overlap_mid"),
            "compression_score": comp,
            "reversal_bias": bias,
            "bias_aligned": bias_aligned,
            "reversal_confirms": confirms,
            "size_multiplier_applied": mult,
        }

        # Apply tilt to existing risk override if present and >1.0
        if mult > 1.0:
            existing = float(raw.get("ctrader_risk_usd_override", 0.0) or 0.0)
            if existing > 0:
                raw["ctrader_risk_usd_override"] = round(existing * mult, 4)
    except Exception:
        # Absolute fail-silent guarantee
        pass

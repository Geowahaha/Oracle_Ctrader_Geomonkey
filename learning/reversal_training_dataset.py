"""
learning/reversal_training_dataset.py

Build a reversal-vs-continuation training dataset for XAUUSD from
captured spot/depth market data.

Workflow:
1) Aggregate M1 candles from `ctrader_spot_ticks`.
2) Detect sweep-reversal candidates (same core shape as post-SL detector).
3) Compute microstructure features from trailing spot/depth window.
4) Label each candidate as:
   - reversal_followthrough
   - continuation_followthrough
   - chop
5) Emit dataset + summary for family training.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from analysis.entry_sharpness import compute_entry_sharpness_score
from learning.live_profile_autopilot import _classify_chart_state, summarize_market_capture


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _event_ts_ms(value) -> int:
    raw = _safe_float(value, 0.0)
    if raw <= 0.0:
        return 0
    # Heuristic normalization:
    # - seconds (1e9..1e11)   -> *1000
    # - milliseconds (1e11..) -> keep
    # - microseconds (>1e14)  -> /1000
    if raw > 1e14:
        return int(raw / 1000.0)
    if raw < 1e11:
        return int(raw * 1000.0)
    return int(raw)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(src: Optional[datetime] = None) -> str:
    dt = src if isinstance(src, datetime) else _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minute_iso_from_event_ts(event_ts_ms: int) -> str:
    sec = max(0, int(event_ts_ms // 1000))
    return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Candle:
    minute_bucket: int
    minute_utc: str
    start_event_ts: int
    end_event_ts: int
    open: float
    high: float
    low: float
    close: float
    n_ticks: int

    @property
    def bar_range(self) -> float:
        return max(0.0, float(self.high - self.low))


def _close_position_in_bar(candle: Candle) -> float:
    rng = max(0.0, float(candle.high - candle.low))
    if rng <= 0.0:
        return 0.5
    return (float(candle.close) - float(candle.low)) / rng


def _load_spot_rows(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start_utc: str,
    end_utc: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_ts, event_utc, bid, ask, spread_pct
          FROM ctrader_spot_ticks
         WHERE symbol = ?
           AND event_utc >= ?
           AND event_utc <= ?
         ORDER BY event_ts ASC, id ASC
        """,
        (symbol, start_utc, end_utc),
    ).fetchall()
    out = []
    for row in rows:
        bid = _safe_float(row["bid"], 0.0)
        ask = _safe_float(row["ask"], 0.0)
        if bid <= 0.0 or ask <= 0.0:
            continue
        out.append(
            {
                "event_ts": _event_ts_ms(row["event_ts"]),
                "event_utc": str(row["event_utc"] or ""),
                "bid": bid,
                "ask": ask,
                "spread_pct": _safe_float(row["spread_pct"], 0.0),
            }
        )
    return out


def _load_depth_rows(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start_utc: str,
    end_utc: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_ts, event_utc, side, size, price, level_index
          FROM ctrader_depth_quotes
         WHERE symbol = ?
           AND event_utc >= ?
           AND event_utc <= ?
         ORDER BY event_ts ASC, id ASC
        """,
        (symbol, start_utc, end_utc),
    ).fetchall()
    out = []
    for row in rows:
        event_ts = _event_ts_ms(row["event_ts"])
        side = str(row["side"] or "").strip().lower()
        if event_ts <= 0 or side not in {"bid", "ask"}:
            continue
        out.append(
            {
                "event_ts": event_ts,
                "event_utc": str(row["event_utc"] or ""),
                "side": side,
                "size": _safe_float(row["size"], 0.0),
                "price": _safe_float(row["price"], 0.0),
                "level_index": _safe_int(row["level_index"], 0),
            }
        )
    return out


def _load_targeted_reversal_events(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start_utc: str,
    end_utc: str,
) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT stage, direction, event_key, event_utc, capture_run_id, capture_status
              FROM ctrader_reversal_capture_events
             WHERE symbol = ?
               AND created_utc >= ?
               AND created_utc <= ?
             ORDER BY created_utc ASC, id ASC
            """,
            (symbol, start_utc, end_utc),
        ).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        event_utc = str(row["event_utc"] or "").strip()
        if not event_utc:
            continue
        try:
            event_dt = _parse_utc(event_utc)
        except Exception:
            continue
        out.append(
            {
                "stage": str(row["stage"] or "").strip().lower(),
                "direction": str(row["direction"] or "").strip().lower(),
                "event_key": str(row["event_key"] or "").strip(),
                "event_utc": event_utc,
                "event_ts": int(event_dt.timestamp() * 1000),
                "capture_run_id": str(row["capture_run_id"] or "").strip(),
                "capture_status": str(row["capture_status"] or "").strip().lower(),
            }
        )
    return out


def _build_m1_candles(spot_rows: list[dict]) -> list[Candle]:
    if not spot_rows:
        return []
    candles: list[Candle] = []
    cur_bucket = None
    cur = None
    for row in spot_rows:
        event_ts = _safe_int(row.get("event_ts"), 0)
        if event_ts <= 0:
            continue
        minute_bucket = event_ts // 60000
        mid = (_safe_float(row.get("bid"), 0.0) + _safe_float(row.get("ask"), 0.0)) / 2.0
        if mid <= 0.0:
            continue
        if cur_bucket is None or minute_bucket != cur_bucket:
            if cur is not None:
                candles.append(Candle(**cur))
            cur_bucket = minute_bucket
            cur = {
                "minute_bucket": minute_bucket,
                "minute_utc": _minute_iso_from_event_ts(event_ts),
                "start_event_ts": event_ts,
                "end_event_ts": event_ts,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "n_ticks": 1,
            }
            continue
        cur["end_event_ts"] = event_ts
        cur["high"] = max(cur["high"], mid)
        cur["low"] = min(cur["low"], mid)
        cur["close"] = mid
        cur["n_ticks"] = int(cur["n_ticks"]) + 1
    if cur is not None:
        candles.append(Candle(**cur))
    return candles


def _atr_from_candles(candles: list[Candle], idx: int, *, lookback: int) -> float:
    lo = max(0, idx - max(1, lookback) + 1)
    window = candles[lo : idx + 1]
    if not window:
        return 0.0
    return sum(c.bar_range for c in window) / max(1, len(window))


def detect_live_reversal_zone(
    candles: list[Candle],
    *,
    confirm_min_wick_ratio: float,
    confirm_min_sweep_pips: float,
    atr_bars: int,
    armed_min_wick_ratio: Optional[float] = None,
    armed_min_sweep_pips: Optional[float] = None,
    armed_min_close_pos: float = 0.45,
) -> dict:
    """Return armed/confirmed reversal-zone state from the latest candles.

    `confirmed` follows the stricter two-bar sweep + recovery logic.
    `armed` marks the latest bar when it already has sweep shape and has
    reclaimed enough of the bar to justify a focused capture burst.
    """
    if len(candles) < 2:
        return {"confirmed": False, "armed": False, "stage": "none", "reason": "insufficient_bars"}

    confirm_rows = detect_sweep_reversal_candidates(
        candles,
        min_wick_ratio=confirm_min_wick_ratio,
        min_sweep_pips=confirm_min_sweep_pips,
        atr_bars=atr_bars,
    )
    latest_confirmed = None
    for row in reversed(confirm_rows):
        if int(row.get("idx", -1) or -1) == (len(candles) - 1):
            latest_confirmed = dict(row)
            break
    if latest_confirmed:
        latest_confirmed["confirmed"] = True
        latest_confirmed["armed"] = True
        latest_confirmed["stage"] = "confirmed"
        latest_confirmed["reason"] = "confirmed"
        latest_confirmed["event_key"] = (
            f"confirmed:{latest_confirmed.get('direction','')}:{latest_confirmed.get('event_utc','')}"
        )
        return latest_confirmed

    sweep_bar = candles[-1]
    bar_range = float(sweep_bar.bar_range)
    armed_wick = float(armed_min_wick_ratio if armed_min_wick_ratio is not None else confirm_min_wick_ratio)
    armed_pips = float(armed_min_sweep_pips if armed_min_sweep_pips is not None else confirm_min_sweep_pips)
    if bar_range < armed_pips:
        return {
            "confirmed": False,
            "armed": False,
            "stage": "none",
            "reason": f"range_too_small:{bar_range:.3f}<{armed_pips:.3f}",
        }
    body_bottom = min(sweep_bar.open, sweep_bar.close)
    body_top = max(sweep_bar.open, sweep_bar.close)
    lower_wick = body_bottom - sweep_bar.low
    upper_wick = sweep_bar.high - body_top
    lower_wick_ratio = (lower_wick / bar_range) if bar_range > 0 else 0.0
    upper_wick_ratio = (upper_wick / bar_range) if bar_range > 0 else 0.0
    close_pos = _close_position_in_bar(sweep_bar)
    atr = max(0.0001, _atr_from_candles(candles, len(candles) - 1, lookback=atr_bars))

    if lower_wick_ratio >= armed_wick and close_pos >= max(0.0, min(1.0, armed_min_close_pos)):
        return {
            "confirmed": False,
            "armed": True,
            "stage": "armed",
            "direction": "long",
            "sweep_level": sweep_bar.low,
            "sweep_wick_ratio": round(lower_wick_ratio, 6),
            "current_close": round(float(sweep_bar.close), 6),
            "atr": round(atr, 6),
            "pattern": "sweep_reversal_long_armed",
            "event_utc": str(sweep_bar.minute_utc or ""),
            "event_key": f"armed:long:{str(sweep_bar.minute_utc or '')}",
            "close_position": round(close_pos, 6),
            "reason": "armed_lower_sweep",
        }
    if upper_wick_ratio >= armed_wick and close_pos <= (1.0 - max(0.0, min(1.0, armed_min_close_pos))):
        return {
            "confirmed": False,
            "armed": True,
            "stage": "armed",
            "direction": "short",
            "sweep_level": sweep_bar.high,
            "sweep_wick_ratio": round(upper_wick_ratio, 6),
            "current_close": round(float(sweep_bar.close), 6),
            "atr": round(atr, 6),
            "pattern": "sweep_reversal_short_armed",
            "event_utc": str(sweep_bar.minute_utc or ""),
            "event_key": f"armed:short:{str(sweep_bar.minute_utc or '')}",
            "close_position": round(close_pos, 6),
            "reason": "armed_upper_sweep",
        }
    best = max(lower_wick_ratio, upper_wick_ratio)
    return {
        "confirmed": False,
        "armed": False,
        "stage": "none",
        "close_position": round(close_pos, 6),
        "reason": f"wick_ratio_low:{best:.3f}<{armed_wick:.3f}",
    }


def detect_sweep_reversal_candidates(
    candles: list[Candle],
    *,
    min_wick_ratio: float,
    min_sweep_pips: float,
    atr_bars: int,
) -> list[dict]:
    out: list[dict] = []
    if len(candles) < 2:
        return out
    for idx in range(1, len(candles)):
        sweep_bar = candles[idx - 1]
        recovery_bar = candles[idx]
        bar_range = sweep_bar.bar_range
        if bar_range < float(min_sweep_pips):
            continue
        body_bottom = min(sweep_bar.open, sweep_bar.close)
        body_top = max(sweep_bar.open, sweep_bar.close)
        lower_wick = body_bottom - sweep_bar.low
        upper_wick = sweep_bar.high - body_top
        lower_wick_ratio = (lower_wick / bar_range) if bar_range > 0 else 0.0
        upper_wick_ratio = (upper_wick / bar_range) if bar_range > 0 else 0.0
        atr = max(0.0001, _atr_from_candles(candles, idx, lookback=atr_bars))
        if lower_wick_ratio >= min_wick_ratio and recovery_bar.close > body_top:
            out.append(
                {
                    "idx": idx,
                    "direction": "long",
                    "sweep_level": sweep_bar.low,
                    "sweep_wick_ratio": round(lower_wick_ratio, 6),
                    "entry_price": recovery_bar.close,
                    "atr": atr,
                    "event_ts": recovery_bar.end_event_ts,
                    "event_utc": recovery_bar.minute_utc,
                    "sweep_bar_range": round(bar_range, 6),
                    "reclaim_body_break": round(recovery_bar.close - body_top, 6),
                }
            )
            continue
        if upper_wick_ratio >= min_wick_ratio and recovery_bar.close < body_bottom:
            out.append(
                {
                    "idx": idx,
                    "direction": "short",
                    "sweep_level": sweep_bar.high,
                    "sweep_wick_ratio": round(upper_wick_ratio, 6),
                    "entry_price": recovery_bar.close,
                    "atr": atr,
                    "event_ts": recovery_bar.end_event_ts,
                    "event_utc": recovery_bar.minute_utc,
                    "sweep_bar_range": round(bar_range, 6),
                    "reclaim_body_break": round(body_bottom - recovery_bar.close, 6),
                }
            )
    return out


def evaluate_reversal_template_fit(
    direction: str,
    capture_features: Optional[dict],
    *,
    sharpness: Optional[dict] = None,
    profile: str = "golden_pocket",
    min_score: int = 4,
    min_spots: int = 6,
    min_depth: int = 24,
    allow_unavailable: bool = True,
) -> dict:
    """Score live capture against the learned reversal-followthrough profile.

    The current defaults are intentionally conservative and prioritize the
    strongest separators from the targeted XAU reversal dataset:
    aligned refill shift, bar volume proxy, aligned delta, and lower rejection.
    """
    side = str(direction or "").strip().lower()
    result = {
        "ok": True,
        "applied": False,
        "available": False,
        "profile": str(profile or "golden_pocket"),
        "score": 0,
        "min_score": max(1, int(min_score)),
        "reason": "not_evaluated",
        "reasons": [],
        "missing": [],
    }
    if side not in {"long", "short"}:
        result.update({
            "ok": False,
            "reason": f"invalid_direction:{side or 'unknown'}",
        })
        return result

    feat = dict(capture_features or {})
    if not feat:
        result["reason"] = "capture_unavailable_passthrough" if allow_unavailable else "capture_unavailable"
        result["ok"] = bool(allow_unavailable)
        return result

    spots_count = int(_safe_float(feat.get("spots_count"), 0.0))
    depth_count = int(_safe_float(feat.get("depth_count"), 0.0))
    result.update({
        "spots_count": spots_count,
        "depth_count": depth_count,
    })
    if spots_count < max(1, int(min_spots)) or depth_count < max(1, int(min_depth)):
        result["reason"] = (
            "capture_insufficient_passthrough"
            if allow_unavailable
            else f"capture_insufficient:{spots_count}s_{depth_count}d"
        )
        result["ok"] = bool(allow_unavailable)
        return result

    result["available"] = True
    result["applied"] = True
    sign = 1.0 if side == "long" else -1.0
    delta_proxy = _safe_float(feat.get("delta_proxy"), 0.0)
    refill_shift = _safe_float(feat.get("depth_refill_shift"), 0.0)
    depth_imbalance = _safe_float(feat.get("depth_imbalance"), 0.0)
    bar_volume_proxy = _safe_float(feat.get("bar_volume_proxy"), 0.0)
    tick_up_ratio = _safe_float(feat.get("tick_up_ratio"), 0.5)
    rejection_ratio = _safe_float(feat.get("rejection_ratio"), 0.0)
    spread_expansion = _safe_float(
        feat.get("spread_expansion"),
        _safe_float(feat.get("spread_expansion_ratio"), 1.0),
    )
    sharp = dict(sharpness or {})
    sharpness_score = int(_safe_float(sharp.get("sharpness_score"), 0.0))
    sharpness_band = str(sharp.get("sharpness_band") or "").strip().lower()

    aligned_delta = sign * delta_proxy
    aligned_refill = sign * refill_shift
    aligned_imbalance = sign * depth_imbalance
    aligned_tick_ratio = tick_up_ratio if side == "long" else (1.0 - tick_up_ratio)
    result.update({
        "aligned_delta_proxy": round(aligned_delta, 6),
        "aligned_refill_shift": round(aligned_refill, 6),
        "aligned_depth_imbalance": round(aligned_imbalance, 6),
        "bar_volume_proxy": round(bar_volume_proxy, 6),
        "aligned_tick_ratio": round(aligned_tick_ratio, 6),
        "rejection_ratio": round(rejection_ratio, 6),
        "spread_expansion": round(spread_expansion, 6),
        "sharpness_score": sharpness_score,
        "sharpness_band": sharpness_band,
    })

    if aligned_refill <= -0.02 and aligned_delta <= 0.02:
        result.update({
            "ok": False,
            "reason": f"adverse_refill_delta:{aligned_refill:.3f}/{aligned_delta:.3f}",
            "missing": ["flow_reclaim_not_confirmed"],
        })
        return result

    score = 0
    reasons: list[str] = []
    missing: list[str] = []
    if aligned_refill >= 0.03:
        score += 2
        reasons.append(f"aligned_refill:{aligned_refill:.3f}")
    else:
        missing.append(f"aligned_refill<{0.03:.2f}")
    if aligned_delta >= 0.10:
        score += 1
        reasons.append(f"aligned_delta:{aligned_delta:.3f}")
    else:
        missing.append(f"aligned_delta<{0.10:.2f}")
    if bar_volume_proxy >= 0.25:
        score += 1
        reasons.append(f"bar_volume:{bar_volume_proxy:.3f}")
    else:
        missing.append(f"bar_volume<{0.25:.2f}")
    if aligned_imbalance >= 0.02:
        score += 1
        reasons.append(f"aligned_imbalance:{aligned_imbalance:.3f}")
    else:
        missing.append(f"aligned_imbalance<{0.02:.2f}")
    if aligned_tick_ratio >= 0.53:
        score += 1
        reasons.append(f"tick_ratio:{aligned_tick_ratio:.3f}")
    else:
        missing.append(f"tick_ratio<{0.53:.2f}")
    if rejection_ratio <= 0.34:
        score += 1
        reasons.append(f"rejection_ok:{rejection_ratio:.3f}")
    else:
        missing.append(f"rejection>{0.34:.2f}")
    if spread_expansion <= 1.18:
        score += 1
        reasons.append(f"spread_ok:{spread_expansion:.3f}")
    else:
        missing.append(f"spread>{1.18:.2f}")
    if sharpness_score >= 50 and sharpness_band != "knife":
        score += 1
        reasons.append(f"sharpness:{sharpness_score}:{sharpness_band or 'unknown'}")
    else:
        missing.append(f"sharpness<{50}:{sharpness_band or 'unknown'}")

    result.update({
        "score": int(score),
        "reasons": reasons,
        "missing": missing,
    })
    if score >= result["min_score"]:
        result["reason"] = f"template_ok:{score}/{result['min_score']}"
        return result
    result.update({
        "ok": False,
        "reason": f"template_weak:{score}/{result['min_score']}|missing:{','.join(missing[:3])}",
    })
    return result


def label_followthrough(
    *,
    direction: str,
    entry_price: float,
    atr: float,
    future_highs: list[float],
    future_lows: list[float],
    min_follow_r: float,
    max_adverse_r: float,
) -> dict:
    side = str(direction or "").strip().lower()
    if side not in {"long", "short"}:
        return {"label": "unknown", "favorable_r": 0.0, "adverse_r": 0.0}
    if not future_highs or not future_lows:
        return {"label": "unknown", "favorable_r": 0.0, "adverse_r": 0.0}
    base = max(0.0001, float(atr))
    entry = float(entry_price)
    max_fwd = max(float(x) for x in future_highs)
    min_fwd = min(float(x) for x in future_lows)
    if side == "long":
        favorable = max(0.0, max_fwd - entry)
        adverse = max(0.0, entry - min_fwd)
    else:
        favorable = max(0.0, entry - min_fwd)
        adverse = max(0.0, max_fwd - entry)
    favorable_r = favorable / base
    adverse_r = adverse / base
    if favorable_r >= float(min_follow_r) and adverse_r <= float(max_adverse_r):
        label = "reversal_followthrough"
    elif adverse_r >= float(min_follow_r) and favorable_r <= float(max_adverse_r):
        label = "continuation_followthrough"
    else:
        label = "chop"
    return {
        "label": label,
        "favorable_r": round(favorable_r, 6),
        "adverse_r": round(adverse_r, 6),
        "favorable_abs": round(favorable, 6),
        "adverse_abs": round(adverse, 6),
    }


def _slice_window(rows: list[dict], ts_values: list[int], ts_from: int, ts_to: int) -> list[dict]:
    left = bisect_left(ts_values, int(ts_from))
    right = bisect_right(ts_values, int(ts_to))
    return rows[left:right]


def _filter_candidates_to_targeted_events(candidates: list[dict], targeted_events: list[dict], *, tolerance_sec: int = 90) -> list[dict]:
    if not candidates:
        return []
    if not targeted_events:
        return []
    tol_ms = max(1, int(tolerance_sec)) * 1000
    out = []
    for cand in list(candidates or []):
        c_ts = _safe_int(cand.get("event_ts"), 0)
        c_dir = str(cand.get("direction") or "").strip().lower()
        for evt in list(targeted_events or []):
            e_ts = _safe_int(evt.get("event_ts"), 0)
            e_dir = str(evt.get("direction") or "").strip().lower()
            if e_dir and c_dir and e_dir != c_dir:
                continue
            if abs(c_ts - e_ts) <= tol_ms:
                tagged = dict(cand)
                tagged["targeted_event_key"] = str(evt.get("event_key") or "")
                tagged["targeted_stage"] = str(evt.get("stage") or "")
                tagged["targeted_capture_run_id"] = str(evt.get("capture_run_id") or "")
                out.append(tagged)
                break
    return out


def _family_hint(
    *,
    label: str,
    state_label: str,
    sharpness_band: str,
    aligned_delta_proxy: float,
    aligned_refill_shift: float,
    rejection_ratio: float,
) -> str:
    state = str(state_label or "").strip().lower()
    band = str(sharpness_band or "").strip().lower()
    if label == "reversal_followthrough":
        if state == "reversal_exhaustion" or rejection_ratio >= 0.42:
            return "xau_scalp_range_repair"
        if band in {"sharp", "normal"}:
            return "xau_scalp_failed_fade_follow_stop"
        return "xau_scalp_tick_depth_filter"
    if label == "continuation_followthrough":
        if aligned_delta_proxy >= 0.08 and aligned_refill_shift >= 0.02:
            return "xau_scalp_microtrend_follow_up"
        return "xau_scalp_tick_depth_filter"
    return "skip_or_reduce_risk"


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(__import__("statistics").median(values))


def _aggregate_summary(rows: list[dict]) -> dict:
    counts = Counter(str(row.get("label") or "") for row in rows)
    fields = [
        "sweep_wick_ratio",
        "sweep_bar_range",
        "reclaim_body_break",
        "favorable_r",
        "adverse_r",
        "delta_proxy",
        "aligned_delta_proxy",
        "depth_imbalance",
        "aligned_depth_imbalance",
        "depth_refill_shift",
        "aligned_refill_shift",
        "rejection_ratio",
        "bar_volume_proxy",
        "spread_expansion",
        "sharpness_score",
        "continuation_bias",
    ]
    by_label: dict[str, dict] = {}
    for label in sorted(counts.keys()):
        subset = [row for row in rows if str(row.get("label") or "") == label]
        feat = {}
        for key in fields:
            vals = [_safe_float(item.get(key), 0.0) for item in subset]
            feat[key] = {
                "mean": round(_avg(vals), 6),
                "median": round(_median(vals), 6),
            }
        by_label[label] = {
            "count": len(subset),
            "features": feat,
        }
    rev = by_label.get("reversal_followthrough", {}).get("features", {})
    cont = by_label.get("continuation_followthrough", {}).get("features", {})
    deltas = {}
    if rev and cont:
        for key in fields:
            deltas[key] = round(
                _safe_float(rev.get(key, {}).get("mean"), 0.0)
                - _safe_float(cont.get(key, {}).get("mean"), 0.0),
                6,
            )
    family_matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        fam = str(row.get("family_hint") or "unknown")
        lbl = str(row.get("label") or "unknown")
        family_matrix[fam][lbl] += 1
    family_matrix_out = {
        fam: dict(sorted(lbls.items(), key=lambda item: (-int(item[1]), item[0])))
        for fam, lbls in sorted(family_matrix.items(), key=lambda item: item[0])
    }
    return {
        "total_candidates": len(rows),
        "labels": dict(counts),
        "by_label": by_label,
        "delta_reversal_minus_continuation": deltas,
        "family_hint_matrix": family_matrix_out,
    }


def build_reversal_training_dataset(
    *,
    db_path: Path,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    lookback_sec: int,
    forward_bars: int,
    atr_bars: int,
    min_wick_ratio: float,
    min_sweep_pips: float,
    min_follow_r: float,
    max_adverse_r: float,
    targeted_only: bool = False,
) -> dict:
    start_iso = _iso_utc(start_utc)
    end_iso = _iso_utc(end_utc)
    if not db_path.exists():
        return {
            "ok": False,
            "error": "db_not_found",
            "db_path": str(db_path),
        }
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        spot_rows = _load_spot_rows(conn, symbol=symbol, start_utc=start_iso, end_utc=end_iso)
        depth_rows = _load_depth_rows(conn, symbol=symbol, start_utc=start_iso, end_utc=end_iso)
        targeted_events = _load_targeted_reversal_events(conn, symbol=symbol, start_utc=start_iso, end_utc=end_iso) if targeted_only else []
    candles = _build_m1_candles(spot_rows)
    candidates = detect_sweep_reversal_candidates(
        candles,
        min_wick_ratio=min_wick_ratio,
        min_sweep_pips=min_sweep_pips,
        atr_bars=atr_bars,
    )
    if targeted_only:
        candidates = _filter_candidates_to_targeted_events(candidates, targeted_events, tolerance_sec=90)
    spot_ts = [_safe_int(row.get("event_ts"), 0) for row in spot_rows]
    depth_ts = [_safe_int(row.get("event_ts"), 0) for row in depth_rows]
    rows: list[dict] = []
    lb_ms = max(30, int(lookback_sec)) * 1000
    for cand in candidates:
        idx = _safe_int(cand.get("idx"), -1)
        if idx < 0 or (idx + forward_bars) >= len(candles):
            continue
        direction = str(cand.get("direction") or "")
        entry_price = _safe_float(cand.get("entry_price"), 0.0)
        atr = max(0.0001, _safe_float(cand.get("atr"), 0.0))
        if entry_price <= 0.0:
            continue
        future = candles[idx + 1 : idx + 1 + max(1, forward_bars)]
        lab = label_followthrough(
            direction=direction,
            entry_price=entry_price,
            atr=atr,
            future_highs=[c.high for c in future],
            future_lows=[c.low for c in future],
            min_follow_r=min_follow_r,
            max_adverse_r=max_adverse_r,
        )
        event_ts = _safe_int(cand.get("event_ts"), 0)
        spot_window = _slice_window(spot_rows, spot_ts, event_ts - lb_ms, event_ts)
        depth_window = _slice_window(depth_rows, depth_ts, event_ts - lb_ms, event_ts)
        features = summarize_market_capture(spot_window, depth_window) if spot_window else {}
        sharpness = compute_entry_sharpness_score(features, direction) if features else {}
        chart_state = _classify_chart_state(
            direction,
            {"pattern": f"sweep_reversal_{direction}"},
            capture_features=features,
        ) if features else {}
        sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
        delta_proxy = _safe_float(features.get("delta_proxy"), 0.0)
        depth_imbalance = _safe_float(features.get("depth_imbalance"), 0.0)
        refill_shift = _safe_float(features.get("depth_refill_shift"), 0.0)
        rejection = _safe_float(features.get("rejection_ratio"), 0.0)
        state_label = str(chart_state.get("state_label") or "")
        sharpness_band = str(sharpness.get("sharpness_band") or "")
        row = {
            "event_utc": str(cand.get("event_utc") or ""),
            "direction": direction,
            "label": str(lab.get("label") or "unknown"),
            "family_hint": _family_hint(
                label=str(lab.get("label") or ""),
                state_label=state_label,
                sharpness_band=sharpness_band,
                aligned_delta_proxy=(sign * delta_proxy),
                aligned_refill_shift=(sign * refill_shift),
                rejection_ratio=rejection,
            ),
            "forward_bars": int(forward_bars),
            "lookback_sec": int(lookback_sec),
            "sweep_wick_ratio": round(_safe_float(cand.get("sweep_wick_ratio"), 0.0), 6),
            "sweep_bar_range": round(_safe_float(cand.get("sweep_bar_range"), 0.0), 6),
            "reclaim_body_break": round(_safe_float(cand.get("reclaim_body_break"), 0.0), 6),
            "entry_price": round(entry_price, 5),
            "sweep_level": round(_safe_float(cand.get("sweep_level"), 0.0), 5),
            "atr": round(atr, 6),
            "favorable_r": _safe_float(lab.get("favorable_r"), 0.0),
            "adverse_r": _safe_float(lab.get("adverse_r"), 0.0),
            "favorable_abs": _safe_float(lab.get("favorable_abs"), 0.0),
            "adverse_abs": _safe_float(lab.get("adverse_abs"), 0.0),
            "state_label": state_label,
            "day_type": str(chart_state.get("day_type") or ""),
            "continuation_bias": round(_safe_float(chart_state.get("continuation_bias"), 0.0), 6),
            "delta_proxy": round(delta_proxy, 6),
            "aligned_delta_proxy": round(sign * delta_proxy, 6),
            "depth_imbalance": round(depth_imbalance, 6),
            "aligned_depth_imbalance": round(sign * depth_imbalance, 6),
            "depth_refill_shift": round(refill_shift, 6),
            "aligned_refill_shift": round(sign * refill_shift, 6),
            "rejection_ratio": round(rejection, 6),
            "bar_volume_proxy": round(_safe_float(features.get("bar_volume_proxy"), 0.0), 6),
            "spread_expansion": round(_safe_float(features.get("spread_expansion"), 0.0), 6),
            "spread_avg_pct": round(_safe_float(features.get("spread_avg_pct"), 0.0), 8),
            "tick_up_ratio": round(_safe_float(features.get("tick_up_ratio"), 0.0), 6),
            "sharpness_score": int(_safe_float(sharpness.get("sharpness_score"), 0.0)),
            "sharpness_band": sharpness_band,
            "momentum_quality": round(_safe_float(sharpness.get("momentum_quality"), 0.0), 6),
            "flow_persistence": round(_safe_float(sharpness.get("flow_persistence"), 0.0), 6),
            "absorption_quality": round(_safe_float(sharpness.get("absorption_quality"), 0.0), 6),
            "price_stability": round(_safe_float(sharpness.get("price_stability"), 0.0), 6),
            "positioning_quality": round(_safe_float(sharpness.get("positioning_quality"), 0.0), 6),
            "sharpness_reasons": list(sharpness.get("sharpness_reasons") or []),
            "spots_count": int(_safe_float(features.get("spots_count"), 0)),
            "depth_count": int(_safe_float(features.get("depth_count"), 0)),
            "targeted_event_key": str(cand.get("targeted_event_key") or ""),
            "targeted_stage": str(cand.get("targeted_stage") or ""),
            "targeted_capture_run_id": str(cand.get("targeted_capture_run_id") or ""),
        }
        rows.append(row)
    summary = _aggregate_summary(rows)
    total_minutes = max(1.0, (end_utc - start_utc).total_seconds() / 60.0)
    coverage_ratio = float(len(candles)) / total_minutes
    warnings: list[str] = []
    if coverage_ratio < 0.35:
        warnings.append("sparse_tick_coverage_for_window")
    if targeted_only and not targeted_events:
        warnings.append("no_targeted_reversal_events_for_window")
    return {
        "ok": True,
        "generated_at": _iso_utc(),
        "db_path": str(db_path),
        "symbol": symbol,
        "window": {
            "start_utc": start_iso,
            "end_utc": end_iso,
        },
        "params": {
            "lookback_sec": int(lookback_sec),
            "forward_bars": int(forward_bars),
            "atr_bars": int(atr_bars),
            "min_wick_ratio": float(min_wick_ratio),
            "min_sweep_pips": float(min_sweep_pips),
            "min_follow_r": float(min_follow_r),
            "max_adverse_r": float(max_adverse_r),
            "targeted_only": bool(targeted_only),
        },
        "source_counts": {
            "spot_rows": len(spot_rows),
            "depth_rows": len(depth_rows),
            "candles": len(candles),
            "expected_minutes": int(round(total_minutes)),
            "coverage_ratio": round(coverage_ratio, 4),
            "targeted_events": len(targeted_events) if targeted_only else 0,
            "candidates": len(candidates),
            "labeled_rows": len(rows),
        },
        "warnings": warnings,
        "summary": summary,
        "rows": rows,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build XAU reversal-vs-continuation dataset from spot/depth capture."
    )
    parser.add_argument("--db-path", default="data/ctrader_openapi.db")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--start-utc", default="")
    parser.add_argument("--end-utc", default="")
    parser.add_argument("--lookback-sec", type=int, default=240)
    parser.add_argument("--forward-bars", type=int, default=20)
    parser.add_argument("--atr-bars", type=int, default=10)
    # Mining defaults are intentionally looser than live post-SL gate so
    # we can gather enough training samples from sparse capture windows.
    parser.add_argument("--min-wick-ratio", type=float, default=0.35)
    parser.add_argument("--min-sweep-pips", type=float, default=0.5)
    parser.add_argument("--min-follow-r", type=float, default=1.0)
    parser.add_argument("--max-adverse-r", type=float, default=0.6)
    parser.add_argument("--targeted-only", action="store_true")
    parser.add_argument("--output", default="")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    now_utc = _utc_now()
    start_utc = (
        _parse_utc(args.start_utc)
        if str(args.start_utc or "").strip()
        else (now_utc - timedelta(days=2))
    )
    end_utc = (
        _parse_utc(args.end_utc)
        if str(args.end_utc or "").strip()
        else now_utc
    )
    if end_utc <= start_utc:
        print("error: end_utc must be greater than start_utc")
        return 2
    report = build_reversal_training_dataset(
        db_path=Path(args.db_path),
        symbol=str(args.symbol or "XAUUSD").strip().upper(),
        start_utc=start_utc,
        end_utc=end_utc,
        lookback_sec=max(30, int(args.lookback_sec)),
        forward_bars=max(1, int(args.forward_bars)),
        atr_bars=max(2, int(args.atr_bars)),
        min_wick_ratio=max(0.0, float(args.min_wick_ratio)),
        min_sweep_pips=max(0.1, float(args.min_sweep_pips)),
        min_follow_r=max(0.1, float(args.min_follow_r)),
        max_adverse_r=max(0.0, float(args.max_adverse_r)),
        targeted_only=bool(args.targeted_only),
    )
    if not bool(report.get("ok")):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    output_path = str(args.output or "").strip()
    if not output_path:
        stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
        output_path = str(Path("artifacts") / f"xau_reversal_dataset_{stamp}.json")
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = dict(report.get("summary") or {})
    labels = dict(summary.get("labels") or {})
    print(
        f"ok symbol={report.get('symbol')} rows={report.get('source_counts', {}).get('labeled_rows', 0)} "
        f"labels={labels} output={out_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

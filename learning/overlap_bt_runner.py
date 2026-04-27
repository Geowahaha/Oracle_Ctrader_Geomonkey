"""Overlap-tag historical backtest — token-efficient.

Walks XAUUSD M5 history (~30+ days) from candle_data*.db, runs the zone
overlap detector at each step, scores reversal confirmations with available
historical features, forward-tests each overlap-hit signal against ATR-based
SL/TP, then writes a single summary JSON to artifacts/.

Run:
    python -m learning.overlap_bt_runner

Output: artifacts/overlap_bt_summary.json (single ~50-line file)

Token efficiency: stdout silent unless --verbose. Claude reads only the
summary JSON.

Per project rules: pure read-only analysis. Does not touch live state, env,
or running services.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.zone_overlap_detector import (  # noqa: E402
    detect_zones,
    overlap_at_price,
    compression_score,
    infer_reversal_bias,
)
from analysis.m1_reversal_confirmation import detect_engulfing  # noqa: E402

DBS = [
    ROOT / "backtest" / "candle_data__local.db",
    ROOT / "backtest" / "candle_data.db",
]


def load_candles(symbol: str, tf: str) -> list[dict]:
    """Union-load candles across known DBs, dedupe by ts."""
    seen: dict[str, dict] = {}
    for db in DBS:
        if not db.exists():
            continue
        con = sqlite3.connect(str(db))
        try:
            cur = con.cursor()
            for ts, o, h, l, c, v in cur.execute(
                "SELECT ts, open, high, low, close, COALESCE(volume,0) "
                "FROM candles WHERE symbol=? AND tf=? ORDER BY ts",
                (symbol, tf),
            ):
                seen[ts] = {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        except Exception:
            pass
        finally:
            con.close()
    rows = list(seen.values())
    rows.sort(key=lambda r: r["ts"])
    return rows


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("+00:00", "+00:00"))
    except Exception:
        return None


def session_of(ts: str) -> str:
    dt = parse_ts(ts)
    if dt is None:
        return "unknown"
    h = dt.hour
    if 0 <= h < 7:
        return "asian"
    if 7 <= h < 13:
        return "london"
    if 13 <= h < 21:
        return "new_york"
    return "asian"


def h1_dema_slope_at(h1: list[dict], idx: int, period: int = 14) -> float:
    if idx < period * 2 or idx >= len(h1):
        return 0.0
    closes = [c["close"] for c in h1[: idx + 1]]
    # quick EMA
    k = 2 / (period + 1)
    ema1 = closes[0]
    for c in closes[1:]:
        ema1 = c * k + ema1 * (1 - k)
    # slope: last close vs ema diff sign+magnitude normalized
    return (closes[-1] - ema1) / max(abs(ema1), 1e-9)


def failed_sweep_wick(candle: dict, zone_top: float, zone_bot: float, direction: str) -> bool:
    """Bullish: pierces zone_bot with low but closes above it. Mirror for bearish."""
    if direction == "long":
        return candle["low"] < zone_bot and candle["close"] > zone_bot
    if direction == "short":
        return candle["high"] > zone_top and candle["close"] < zone_top
    return False


def m1_window_at(m1: list[dict], target_ts: str) -> list[dict]:
    """Return last ~5 M1 candles up to target_ts (M5 close moment)."""
    if not m1:
        return []
    # binary-ish search via lower_bound
    lo, hi = 0, len(m1) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if m1[mid]["ts"] <= target_ts:
            lo = mid
        else:
            hi = mid - 1
    return m1[max(0, lo - 4): lo + 1]


def atr_simple(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    win = trs[-period:] if len(trs) >= period else trs
    return sum(win) / len(win) if win else 0.0


def forward_test(
    m5: list[dict], i: int, direction: str, entry: float, sl: float, tp: float, max_bars: int
) -> tuple[float, str]:
    """Return (realized_R, exit_reason). RR base = (tp-entry)/(entry-sl)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0, "bad_risk"
    rr_target = abs(tp - entry) / risk
    end = min(len(m5), i + 1 + max_bars)
    for j in range(i + 1, end):
        h, l = m5[j]["high"], m5[j]["low"]
        if direction == "long":
            if l <= sl:
                return -1.0, "sl"
            if h >= tp:
                return rr_target, "tp"
        else:
            if h >= sl:
                return -1.0, "sl"
            if l <= tp:
                return rr_target, "tp"
    # mark-to-market end of window
    last = m5[end - 1]["close"] if end > i + 1 else entry
    move = (last - entry) if direction == "long" else (entry - last)
    return round(move / risk, 3), "timeout"


def bucket(confirms: int, has_overlap: bool) -> str:
    if not has_overlap:
        return "no_overlap"
    if confirms >= 4:
        return "overlap_confirms_4_plus"
    if confirms >= 2:
        return "overlap_confirms_2_3"
    return "overlap_confirms_0_1"


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "wr": 0.0, "exp_R": 0.0, "avg_R": 0.0}
    n = len(rows)
    wins = sum(1 for r in rows if r["realized_R"] > 0)
    avg = sum(r["realized_R"] for r in rows) / n
    return {
        "n": n,
        "wr": round(wins / n, 3),
        "exp_R": round(avg, 3),
        "avg_R": round(avg, 3),
    }


def run(verbose: bool = False) -> dict:
    m5 = load_candles("XAUUSD", "5m")
    m1 = load_candles("XAUUSD", "1m")
    h1 = load_candles("XAUUSD", "1h")
    if verbose:
        print(f"loaded M5={len(m5)} M1={len(m1)} H1={len(h1)}", file=sys.stderr)
    if len(m5) < 250:
        return {"error": "insufficient M5 data", "m5_bars": len(m5)}

    # Build H1 lookup index by ts
    h1_ts_to_idx = {c["ts"]: i for i, c in enumerate(h1)}

    rows: list[dict] = []
    forward_window_bars = 24  # 2h on M5
    step = 3  # evaluate every 15 min of M5

    for i in range(200, len(m5) - forward_window_bars, step):
        window = m5[max(0, i - 200): i + 1]
        zones = detect_zones(window, width_atr_mult=1.0, max_age_bars=300)
        if not zones:
            continue
        cur = m5[i]
        price = cur["close"]
        ov = overlap_at_price(zones, price, min_quality=0.2)
        if ov is None:
            continue
        bias = infer_reversal_bias(window)
        if bias not in ("long", "short"):
            continue

        # historical confirms (4 available)
        confirms = 0
        m1w = m1_window_at(m1, cur["ts"])
        eng = detect_engulfing(bias, m1w, entry_price=price) if m1w else {"confirmed": None}
        if eng.get("confirmed") is True:
            confirms += 1
        cs = compression_score(window)
        if 0 < cs < 0.7:
            confirms += 1
        # H1 slope
        h1_idx = None
        cur_dt = parse_ts(cur["ts"])
        if cur_dt:
            # find latest h1 ts <= cur_dt
            for ts_candidate in (
                cur["ts"][:13] + ":00:00+00:00",
                cur["ts"][:13] + ":00:00",
            ):
                if ts_candidate in h1_ts_to_idx:
                    h1_idx = h1_ts_to_idx[ts_candidate]
                    break
        slope = h1_dema_slope_at(h1, h1_idx) if h1_idx else 0.0
        if (bias == "long" and slope >= -0.001) or (bias == "short" and slope <= 0.001):
            confirms += 1
        if failed_sweep_wick(cur, ov["overlap_top"], ov["overlap_bottom"], bias):
            confirms += 1

        # forward test
        atr = atr_simple(window, period=14)
        if atr <= 0:
            continue
        if bias == "long":
            sl = price - 1.5 * atr
            tp = price + 3.0 * atr
        else:
            sl = price + 1.5 * atr
            tp = price - 3.0 * atr
        rR, reason = forward_test(m5, i, bias, price, sl, tp, forward_window_bars)

        rows.append({
            "ts": cur["ts"],
            "session": session_of(cur["ts"]),
            "bias": bias,
            "confirms": confirms,
            "overlap_quality": ov["overlap_quality"],
            "compression": cs,
            "realized_R": rR,
            "exit": reason,
        })

    # Reference rows: random sample of non-overlap signals for baseline
    baseline: list[dict] = []
    for i in range(200, len(m5) - forward_window_bars, step * 4):
        cur = m5[i]
        window = m5[max(0, i - 200): i + 1]
        zones = detect_zones(window, width_atr_mult=1.0, max_age_bars=300)
        if zones and overlap_at_price(zones, cur["close"], min_quality=0.2) is not None:
            continue
        bias = infer_reversal_bias(window)
        if bias not in ("long", "short"):
            continue
        atr = atr_simple(window, period=14)
        if atr <= 0:
            continue
        if bias == "long":
            sl = cur["close"] - 1.5 * atr; tp = cur["close"] + 3.0 * atr
        else:
            sl = cur["close"] + 1.5 * atr; tp = cur["close"] - 3.0 * atr
        rR, reason = forward_test(m5, i, bias, cur["close"], sl, tp, forward_window_bars)
        baseline.append({"realized_R": rR, "session": session_of(cur["ts"])})

    # Buckets
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[bucket(r["confirms"], True)].append(r)
    buckets["no_overlap"] = baseline

    # By session for overlap-confirmed-4+
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["confirms"] >= 4:
            by_session[r["session"]].append(r)

    summary = {
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "data_coverage": {
            "m5_bars": len(m5),
            "m5_first_ts": m5[0]["ts"] if m5 else None,
            "m5_last_ts": m5[-1]["ts"] if m5 else None,
            "days_approx": round(len(m5) * 5 / 60 / 24, 1),
            "m1_bars": len(m1),
            "h1_bars": len(h1),
        },
        "method": {
            "step_bars_m5": step,
            "forward_window_bars_m5": forward_window_bars,
            "forward_minutes": forward_window_bars * 5,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
            "rr_target": 2.0,
            "confirms_max_historical": 4,
            "confirms_layers_used": ["m1_engulfing", "compression", "h1_dema_slope", "failed_sweep_wick"],
        },
        "buckets": {k: aggregate(v) for k, v in buckets.items()},
        "overlap_4plus_by_session": {s: aggregate(v) for s, v in by_session.items()},
        "size_tilt_recommendation": {},
        "decision": "",
    }

    base = summary["buckets"].get("no_overlap", {})
    p4 = summary["buckets"].get("overlap_confirms_4_plus", {})
    p23 = summary["buckets"].get("overlap_confirms_2_3", {})

    base_R = base.get("exp_R", 0.0)
    rec: dict[str, float] = {}
    if p4.get("n", 0) >= 20 and p4.get("exp_R", 0.0) > base_R + 0.30:
        rec["confirms_4_plus"] = 1.30
    elif p4.get("n", 0) >= 20 and p4.get("exp_R", 0.0) > base_R + 0.15:
        rec["confirms_4_plus"] = 1.15
    else:
        rec["confirms_4_plus"] = 1.0

    if p23.get("n", 0) >= 30 and p23.get("exp_R", 0.0) > base_R + 0.20:
        rec["confirms_2_3"] = 1.10
    else:
        rec["confirms_2_3"] = 1.0

    summary["size_tilt_recommendation"] = rec

    if rec.get("confirms_4_plus", 1.0) >= 1.30:
        summary["decision"] = "ship_full_tilt"
    elif rec.get("confirms_4_plus", 1.0) >= 1.15:
        summary["decision"] = "ship_conservative_tilt"
    else:
        summary["decision"] = "shadow_only_collect_more"

    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--out", default=str(ROOT / "artifacts" / "overlap_bt_summary.json"))
    args = p.parse_args()

    summary = run(verbose=args.verbose)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.verbose:
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

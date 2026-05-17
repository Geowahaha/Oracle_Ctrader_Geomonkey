#!/usr/bin/env python3
"""P3 acceptance analytics for Fibo multi-timeframe shadow candidates.

Reads xau_shadow_journal rows produced by P2 (block_reason='fibo_mtf_shadow')
and reports expectancy/profit-factor/outlier-trim metrics by TF and ratio zone.
No policy is applied automatically; this script only emits evidence for later OPUS
review and one-TF demo promotion decisions.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Iterable


DEFAULT_MIN_TRADES = 30
DEFAULT_MIN_PF = 1.3
DEFAULT_MAX_MISMATCH = 0.10


def _json_obj(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_shadow_rows(db_path: Path, days: int = 30, limit: int = 5000) -> list[dict]:
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, signal_utc, symbol, direction, confidence, entry, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3, block_reason,
                   raw_scores_json, shadow_outcome, resolved_utc, shadow_pnl_rr
              FROM xau_shadow_journal
             WHERE block_reason = 'fibo_mtf_shadow'
               AND signal_utc >= datetime('now', ?)
             ORDER BY signal_utc DESC
             LIMIT ?
            """,
            (f"-{max(1, int(days))} days", int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        raw = _json_obj(row["raw_scores_json"] or "{}")
        out.append({**dict(row), "raw": raw})
    return out


def _trim_top_outliers(values: list[float], n: int = 2) -> list[float]:
    if len(values) <= n:
        return list(values)
    indexed = sorted(enumerate(values), key=lambda item: item[1], reverse=True)
    drop = {idx for idx, _ in indexed[:n]}
    return [v for idx, v in enumerate(values) if idx not in drop]


def _profit_factor(values: list[float]) -> float | None:
    gross_win = sum(v for v in values if v > 0)
    gross_loss = abs(sum(v for v in values if v < 0))
    if gross_loss <= 0:
        return None if gross_win <= 0 else math.inf
    return gross_win / gross_loss


def _mismatch_rate(rows: list[dict]) -> float:
    total = 0
    mismatch = 0
    for row in rows:
        raw = dict(row.get("raw") or {})
        expected = str(raw.get("requested_direction") or raw.get("signal_direction") or row.get("direction") or "").lower()
        observed = str(raw.get("deal_direction") or raw.get("closed_deal_direction") or "").lower()
        if not expected or not observed:
            continue
        total += 1
        if expected != observed:
            mismatch += 1
    return (mismatch / total) if total else 0.0


def summarize_group(rows: list[dict], *, min_trades: int = DEFAULT_MIN_TRADES, min_pf: float = DEFAULT_MIN_PF, max_mismatch: float = DEFAULT_MAX_MISMATCH) -> dict:
    resolved = [r for r in rows if r.get("shadow_pnl_rr") is not None]
    values = [float(r.get("shadow_pnl_rr") or 0.0) for r in resolved]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    trimmed = _trim_top_outliers(values, 2)
    pf = _profit_factor(values)
    trimmed_pf = _profit_factor(trimmed)
    mismatch = _mismatch_rate(rows)
    expectancy = mean(values) if values else 0.0
    trimmed_expectancy = mean(trimmed) if trimmed else 0.0
    eligible = bool(
        len(values) >= int(min_trades)
        and trimmed_expectancy > 0.0
        and (trimmed_pf is None or trimmed_pf >= float(min_pf))
        and mismatch <= float(max_mismatch)
    )
    blockers = []
    if len(values) < int(min_trades):
        blockers.append(f"n<{int(min_trades)}")
    if trimmed_expectancy <= 0.0:
        blockers.append("trimmed_expectancy<=0")
    if trimmed_pf is not None and trimmed_pf < float(min_pf):
        blockers.append(f"trimmed_pf<{float(min_pf):.2f}")
    if mismatch > float(max_mismatch):
        blockers.append(f"direction_mismatch>{float(max_mismatch):.0%}")
    return {
        "candidates": len(rows),
        "resolved": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(values), 4) if values else 0.0,
        "expectancy_R": round(expectancy, 4),
        "expectancy_R_trim_top2": round(trimmed_expectancy, 4),
        "profit_factor": None if pf is None else ("inf" if math.isinf(pf) else round(pf, 4)),
        "profit_factor_trim_top2": None if trimmed_pf is None else ("inf" if math.isinf(trimmed_pf) else round(trimmed_pf, 4)),
        "direction_mismatch_rate": round(mismatch, 4),
        "eligible_for_one_tf_demo_review": eligible,
        "promotion_blockers": blockers,
    }


def summarize_rows(rows: Iterable[dict], *, min_trades: int = DEFAULT_MIN_TRADES, min_pf: float = DEFAULT_MIN_PF, max_mismatch: float = DEFAULT_MAX_MISMATCH) -> dict:
    rows = list(rows or [])
    by_tf = defaultdict(list)
    by_zone = defaultdict(list)
    by_tf_zone = defaultdict(list)
    for row in rows:
        raw = dict(row.get("raw") or _json_obj(row.get("raw_scores_json", "{}")))
        row["raw"] = raw
        tf = str(raw.get("tf_label") or raw.get("entry_tf") or "unknown")
        zone = str(raw.get("ratio_zone") or "unknown")
        by_tf[tf].append(row)
        by_zone[zone].append(row)
        by_tf_zone[f"{tf}|{zone}"].append(row)
    return {
        "summary": summarize_group(rows, min_trades=min_trades, min_pf=min_pf, max_mismatch=max_mismatch),
        "by_tf": {k: summarize_group(v, min_trades=min_trades, min_pf=min_pf, max_mismatch=max_mismatch) for k, v in sorted(by_tf.items())},
        "by_ratio_zone": {k: summarize_group(v, min_trades=min_trades, min_pf=min_pf, max_mismatch=max_mismatch) for k, v in sorted(by_zone.items())},
        "by_tf_ratio_zone": {k: summarize_group(v, min_trades=min_trades, min_pf=min_pf, max_mismatch=max_mismatch) for k, v in sorted(by_tf_zone.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    ap.add_argument("--min-profit-factor", type=float, default=DEFAULT_MIN_PF)
    ap.add_argument("--max-direction-mismatch-rate", type=float, default=DEFAULT_MAX_MISMATCH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = load_shadow_rows(Path(args.db), days=args.days, limit=args.limit)
    report = summarize_rows(rows, min_trades=args.min_trades, min_pf=args.min_profit_factor, max_mismatch=args.max_direction_mismatch_rate)
    report["db_path"] = str(Path(args.db))
    report["days"] = int(args.days)
    report["acceptance"] = {
        "min_trades": int(args.min_trades),
        "min_profit_factor_trim_top2": float(args.min_profit_factor),
        "max_direction_mismatch_rate": float(args.max_direction_mismatch_rate),
        "note": "Review only; no live promotion is applied by this script.",
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Fibo MTF shadow analytics db={report['db_path']} days={report['days']}")
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
        print("by_tf:")
        for tf, row in report["by_tf"].items():
            print(f"  {tf}: {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

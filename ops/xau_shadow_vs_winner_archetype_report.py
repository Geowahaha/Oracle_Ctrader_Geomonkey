#!/usr/bin/env python3
"""Compare Fibo MTF shadow rows against historical XAU winner archetypes.

Read-only evidence tool. It does not trade, route, promote, or mutate DB state.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Iterable

from ops.fibo_mtf_planner_shadow_report import load_rows as load_shadow_rows
from ops.xau_winner_archetype_report import (
    build_report as build_winner_report,
    classify_risk_geometry,
    load_positions,
    session_bucket,
)


def _float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _risk_row_from_shadow(row: dict) -> dict:
    tp = row.get("take_profit_1")
    if tp is None:
        tp = row.get("take_profit")
    return {
        "entry_price": row.get("entry"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": tp,
    }


def _session_from_shadow(row: dict) -> str:
    key = str(row.get("session_key") or "")
    if ":" in key:
        return key.rsplit(":", 1)[-1]
    dt = row.get("signal_dt")
    if dt is not None:
        return session_bucket(dt.isoformat())
    return "unknown"


def classify_shadow_candidate(row: dict) -> dict:
    """Normalize a Fibo MTF shadow row into archetype comparison features."""
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    anchor_source = str(raw.get("execution_anchor_source") or "unavailable")
    live_plan = bool(raw.get("execution_anchor_is_live_plan") or False)
    item = dict(row)
    item["session"] = _session_from_shadow(row)
    item["risk_geometry"] = classify_risk_geometry(_risk_row_from_shadow(row))
    item["anchor_source"] = anchor_source
    item["anchor_usable"] = anchor_source not in {"", "unavailable", "none"}
    item["live_plan"] = live_plan
    item["shadow_rr"] = _float(row.get("shadow_pnl_rr"))
    return item


def _summarize_shadow(rows: Iterable[dict]) -> dict:
    items = list(rows or [])
    rr = [float(r["shadow_rr"]) for r in items if r.get("shadow_rr") is not None]
    return {
        "decisions": len(items),
        "resolved": len(rr),
        "expectancy_R": round(mean(rr), 4) if rr else 0.0,
        "anchor_usable_rate": round(sum(1 for r in items if r.get("anchor_usable")) / len(items), 4) if items else 0.0,
    }


def _historical_lookup(historical_rows: list[dict]) -> dict:
    report = build_winner_report(historical_rows, top_n=10)
    return {
        "report": report,
        "by_session": report.get("by_session", {}),
        "by_risk_geometry": report.get("by_risk_geometry", {}),
    }


def _net(stats: dict | None) -> float:
    return float((stats or {}).get("net_pnl_usd") or 0.0)


def _match_bucket(row: dict, lookup: dict) -> str:
    session_net = _net(lookup.get("by_session", {}).get(row.get("session")))
    risk_net = _net(lookup.get("by_risk_geometry", {}).get(row.get("risk_geometry")))
    if session_net > 0 and risk_net >= 0 and row.get("anchor_usable"):
        return "resembles_winner_archetype"
    if session_net < 0 or risk_net < 0 or not row.get("anchor_usable"):
        return "resembles_loser_archetype"
    return "mixed_or_unknown"


def _verdict(summary: dict, by_match: dict) -> dict:
    blockers: list[str] = []
    if float(summary.get("anchor_usable_rate") or 0.0) <= 0.50:
        blockers.append("anchor_coverage<0.50")
    loser_like = int((by_match.get("resembles_loser_archetype") or {}).get("decisions") or 0)
    winner_like = int((by_match.get("resembles_winner_archetype") or {}).get("decisions") or 0)
    if loser_like > winner_like:
        blockers.append("loser_archetype_dominates")
    if blockers:
        action = "fix_before_counting_days"
    elif winner_like > 0:
        action = "continue_shadow_collection_with_archetype_watch"
    else:
        action = "insufficient_signal"
    return {"action": action, "blockers": blockers}


def build_comparison(shadow_rows: Iterable[dict], historical_rows: Iterable[dict]) -> dict:
    """Compare shadow rows to historical winner/loser buckets."""
    shadow = [classify_shadow_candidate(r) for r in shadow_rows]
    lookup = _historical_lookup(list(historical_rows))
    by_match: dict[str, list[dict]] = defaultdict(list)
    for row in shadow:
        bucket = _match_bucket(row, lookup)
        row["match_bucket"] = bucket
        by_match[bucket].append(row)
    by_match_summary = {k: _summarize_shadow(v) for k, v in sorted(by_match.items())}
    summary = _summarize_shadow(shadow)
    return {
        "summary": {
            "shadow_decisions": summary["decisions"],
            "shadow_resolved": summary["resolved"],
            "shadow_expectancy_R": summary["expectancy_R"],
            "anchor_usable_rate": summary["anchor_usable_rate"],
        },
        "by_match_bucket": by_match_summary,
        "historical_actionable_findings": lookup["report"].get("actionable_findings", []),
        "verdict": _verdict(summary, by_match_summary),
        "notes": [
            "Read-only comparison; never routes live orders.",
            "Winner-like means the row matches historically positive session and risk geometry with usable shadow anchor.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--history-days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    shadow = load_shadow_rows(db, days=args.days, limit=args.limit)
    history = load_positions(db, symbol=args.symbol, days=args.history_days, limit=args.limit)
    report = build_comparison(shadow, history)
    report["db_path"] = str(db)
    report["symbol"] = args.symbol
    report["days"] = int(args.days)
    report["history_days"] = int(args.history_days)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"XAU shadow-vs-winner-archetype report db={db} symbol={args.symbol}")
        print(json.dumps(report["summary"], indent=2))
        print("verdict", report["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

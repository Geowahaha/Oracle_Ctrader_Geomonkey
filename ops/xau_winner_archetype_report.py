#!/usr/bin/env python3
"""Read-only XAU winner archetype report.

This tool mines historical cTrader positions to identify winner/loser
archetypes that can guide shadow-only Fibo MTF evidence design.  It never
changes trading state and intentionally uses ctrader_positions.direction as the
opening-direction source of truth; ctrader_deals.direction can represent close
side and must not drive strategy direction analysis by itself.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

FAMILY_RE = re.compile(r"(?:dexter[:|]XAUUSD[:|])?([a-z0-9_]+)(?::[^|]*)?", re.IGNORECASE)
KNOWN_FAMILIES = (
    "fibo_xauusd",
    "xauusd_scheduled",
    "scalp_xauusd",
    "reclaim",
    "xau_reclaim",
    "fibo_advance",
    "fibo_mtf",
)


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite database in immutable read-only mode."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration_minutes(start: str | None, end: str | None) -> float | None:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() / 60.0)


def session_bucket(value: str | None) -> str:
    """Return coarse UTC trading session bucket."""
    dt = _parse_dt(value)
    if dt is None:
        return "unknown"
    hour = dt.hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 21:
        return "ny"
    return "post_ny"


def classify_family(label: str | None, comment: str | None) -> str:
    """Extract Dexter strategy family from label/comment."""
    text = " ".join(part for part in (label or "", comment or "") if part)
    lowered = text.lower()
    for family in KNOWN_FAMILIES:
        if family in lowered:
            return family
    for part in re.split(r"[|:\s]+", lowered):
        if part in {"dexter", "xauusd", "xau", "canary", "winner", "manual", ""}:
            continue
        if part.startswith("xau") or part.endswith("xauusd"):
            return part
    return "unknown"


def _target_distance_r(row: dict) -> float | None:
    entry = _float(row.get("entry_price"))
    stop = _float(row.get("stop_loss"))
    tp = _float(row.get("take_profit"))
    if entry is None or stop is None or tp is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return abs(tp - entry) / risk


def _float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def classify_archetype(row: dict) -> str:
    """Classify a position into deterministic winner/loser archetypes."""
    pnl = _float(row.get("pnl_usd")) or 0.0
    if pnl <= 0.0:
        return "loser"
    entry = _float(row.get("entry_price"))
    stop = _float(row.get("stop_loss"))
    risk_price = abs(entry - stop) if entry is not None and stop is not None else None
    target_r = _target_distance_r(row)
    duration = _duration_minutes(row.get("first_seen_utc"), row.get("close_utc") or row.get("last_seen_utc"))

    if risk_price is not None and risk_price < 1.0:
        return "false_good_or_ugly_winner"
    if target_r is not None and target_r >= 5.0:
        if duration is not None and duration >= 240:
            return "runner_winner"
        return "wide_target_winner"
    if target_r is not None and target_r >= 2.0:
        return "clean_fast_winner"
    return "false_good_or_ugly_winner"


def _summarize(rows: Iterable[dict]) -> dict:
    items = list(rows or [])
    pnls = [float(r.get("pnl_usd") or 0.0) for r in items]
    winners = [p for p in pnls if p > 0.0]
    losers = [p for p in pnls if p < 0.0]
    return {
        "positions": len(items),
        "winners": len(winners),
        "losers": len(losers),
        "winrate": round(len(winners) / len(items), 4) if items else 0.0,
        "avg_pnl_usd": round(mean(pnls), 4) if pnls else 0.0,
        "gross_win_usd": round(sum(winners), 2),
        "gross_loss_usd": round(sum(losers), 2),
        "net_pnl_usd": round(sum(pnls), 2),
    }


def _enrich(row: dict) -> dict:
    item = dict(row)
    item["pnl_usd"] = round(float(item.get("pnl_usd") or 0.0), 4)
    item["family"] = classify_family(str(item.get("label") or ""), str(item.get("comment") or ""))
    item["session"] = session_bucket(item.get("first_seen_utc"))
    item["duration_minutes"] = _duration_minutes(item.get("first_seen_utc"), item.get("close_utc") or item.get("last_seen_utc"))
    item["target_distance_R"] = _target_distance_r(item)
    item["archetype"] = classify_archetype(item)
    return item


def load_positions(db_path: Path, symbol: str = "XAUUSD", days: int = 90, limit: int = 5000) -> list[dict]:
    """Load position-level XAU outcomes using positions.direction as truth."""
    conn = connect_ro(db_path)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace("+00:00", "Z")
        rows = conn.execute(
            """
            WITH pos_pnl AS (
                SELECT p.position_id,
                       p.symbol,
                       p.broker_symbol,
                       p.direction,
                       p.volume,
                       p.entry_price,
                       p.stop_loss,
                       p.take_profit,
                       p.first_seen_utc,
                       p.last_seen_utc,
                       p.label,
                       p.comment,
                       SUM(COALESCE(d.pnl_usd, 0.0)) AS pnl_usd,
                       COUNT(d.deal_id) AS deal_count,
                       MAX(d.execution_utc) AS close_utc
                  FROM ctrader_positions p
                  LEFT JOIN ctrader_deals d ON d.position_id = p.position_id
                 WHERE (p.symbol LIKE ? OR p.broker_symbol LIKE ?)
                   AND (p.first_seen_utc = '' OR p.first_seen_utc >= ?)
                 GROUP BY p.position_id
            )
            SELECT *
              FROM pos_pnl
             ORDER BY COALESCE(close_utc, last_seen_utc, first_seen_utc) DESC
             LIMIT ?
            """,
            (f"%{symbol}%", f"%{symbol}%", cutoff, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [_enrich(dict(row)) for row in rows]


def build_report(rows: Iterable[dict], top_n: int = 15) -> dict:
    """Build deterministic winner archetype report from position rows."""
    enriched = [_enrich(r) if "family" not in r else dict(r) for r in rows]
    by_family: dict[str, list[dict]] = defaultdict(list)
    by_session: dict[str, list[dict]] = defaultdict(list)
    by_archetype: dict[str, list[dict]] = defaultdict(list)
    by_direction: dict[str, list[dict]] = defaultdict(list)
    for row in enriched:
        by_family[str(row.get("family") or "unknown")].append(row)
        by_session[str(row.get("session") or "unknown")].append(row)
        by_archetype[str(row.get("archetype") or "unknown")].append(row)
        by_direction[str(row.get("direction") or "unknown")].append(row)

    top_winners = sorted((r for r in enriched if float(r.get("pnl_usd") or 0.0) > 0.0), key=lambda r: float(r.get("pnl_usd") or 0.0), reverse=True)[:top_n]
    top_losers = sorted((r for r in enriched if float(r.get("pnl_usd") or 0.0) < 0.0), key=lambda r: float(r.get("pnl_usd") or 0.0))[:top_n]
    return {
        "summary": _summarize(enriched),
        "by_family": {k: _summarize(v) for k, v in sorted(by_family.items())},
        "by_session": {k: _summarize(v) for k, v in sorted(by_session.items())},
        "by_archetype": {k: _summarize(v) for k, v in sorted(by_archetype.items())},
        "by_direction": {k: _summarize(v) for k, v in sorted(by_direction.items())},
        "top_winners": top_winners,
        "top_losers": top_losers,
        "notes": [
            "Read-only historical archetype mining; do not use for live routing without separate Opus review.",
            "Direction truth comes from ctrader_positions.direction, not ctrader_deals.direction alone.",
            "Archetypes are deterministic first-pass buckets; MAE/MFE candle reconstruction is a separate next step.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_positions(Path(args.db), symbol=args.symbol, days=args.days, limit=args.limit)
    report = build_report(rows, top_n=args.top)
    report["db_path"] = str(Path(args.db))
    report["symbol"] = args.symbol
    report["days"] = int(args.days)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        summary = report["summary"]
        print(f"XAU winner archetype report db={report['db_path']} symbol={args.symbol} days={args.days}")
        print(
            "positions={positions} winners={winners} losers={losers} winrate={winrate} net_pnl_usd={net_pnl_usd}".format(
                **summary
            )
        )
        print("\nBy family:")
        for family, item in report["by_family"].items():
            print(f"  {family}: n={item['positions']} winrate={item['winrate']} net={item['net_pnl_usd']}")
        print("\nTop winners:")
        for row in report["top_winners"][: args.top]:
            print(
                f"  {row['position_id']} {row.get('family')} {row.get('direction')} "
                f"pnl={row.get('pnl_usd')} archetype={row.get('archetype')} first={row.get('first_seen_utc')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

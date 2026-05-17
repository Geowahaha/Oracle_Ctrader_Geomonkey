#!/usr/bin/env python3
"""Mine historical XAU mistakes for hidden shadow-strategy diamonds.

Read-only Phase A tool. It looks for systematic losing events where the next
opposite-side trade quickly captured profit. This is evidence for a future
shadow-only stop-hunt inversion family, not a live trading path.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

from ops.xau_winner_archetype_report import classify_family, session_bucket


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _direction(value) -> str:
    text = str(value or "").lower()
    if "buy" in text or "long" in text:
        return "long"
    if "sell" in text or "short" in text:
        return "short"
    return text or "unknown"


def _opposite(direction: str) -> str:
    return "short" if direction == "long" else "long" if direction == "short" else "unknown"


def _enrich(row: dict) -> dict:
    item = dict(row)
    item["pnl_usd"] = float(item.get("pnl_usd") or item.get("pnl") or 0.0)
    item["direction"] = _direction(item.get("direction"))
    item["family"] = item.get("family") or classify_family(str(item.get("label") or ""), str(item.get("comment") or ""))
    item["open_dt"] = _parse_dt(item.get("first_seen_utc"))
    item["close_dt"] = _parse_dt(item.get("close_utc")) or _parse_dt(item.get("last_seen_utc"))
    item["session"] = session_bucket(item.get("first_seen_utc"))
    return item


def load_positions(db_path: Path, symbol: str = "XAUUSD", days: int = 90, limit: int = 8000) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace("+00:00", "Z")
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(
            """
            WITH pos_pnl AS (
                SELECT p.position_id,
                       p.symbol,
                       p.broker_symbol,
                       p.direction,
                       p.entry_price,
                       p.stop_loss,
                       p.take_profit,
                       p.label,
                       p.comment,
                       p.source,
                       p.first_seen_utc,
                       p.last_seen_utc,
                       SUM(COALESCE(d.pnl_usd, 0.0)) AS pnl_usd,
                       MAX(d.execution_utc) AS close_utc,
                       COUNT(d.deal_id) AS deal_count
                  FROM ctrader_positions p
                  LEFT JOIN ctrader_deals d ON d.position_id = p.position_id
                 WHERE (p.symbol LIKE ? OR p.broker_symbol LIKE ?)
                   AND (p.first_seen_utc = '' OR p.first_seen_utc >= ?)
                 GROUP BY p.position_id
            )
            SELECT * FROM pos_pnl
             WHERE first_seen_utc IS NOT NULL AND first_seen_utc != ''
             ORDER BY first_seen_utc ASC
             LIMIT ?
            """,
            (f"%{symbol}%", f"%{symbol}%", cutoff, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [_enrich(dict(r)) for r in rows]


def _summarize_pnls(values: list[float]) -> dict:
    wins = [v for v in values if v > 0.0]
    losses = [v for v in values if v < 0.0]
    return {
        "samples": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(values), 4) if values else 0.0,
        "avg_pnl_usd": round(mean(values), 4) if values else 0.0,
        "inversion_net_usd": round(sum(values), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
    }


def _bucket_key(loss: dict, bucket_level: str) -> tuple:
    family = str(loss.get("family") or "unknown")
    session = str(loss.get("session") or "unknown")
    direction = str(loss.get("direction") or "unknown")
    if bucket_level == "family_session_dir":
        return family, session, direction
    if bucket_level == "family_dir":
        return family, direction
    if bucket_level == "session_dir":
        return session, direction
    if bucket_level == "family_session":
        return family, session
    if bucket_level == "family":
        return (family,)
    if bucket_level == "direction":
        return (direction,)
    return family, session, direction


def _cluster_from_events(bucket_level: str, key: tuple, events: list[dict], max_minutes: int, min_samples: int) -> dict | None:
    pnls = [float(e["followup"].get("pnl_usd") or 0.0) for e in events]
    if len(pnls) < int(min_samples):
        return None
    summary = _summarize_pnls(pnls)
    first_loss = events[0]["loss"]
    lost_direction = str(first_loss.get("direction") or "unknown")
    theoretical_direction = _opposite(lost_direction)
    cluster_key = "|".join(str(v) for v in key)
    return {
        "cluster_id": f"stophunt_inversion|{cluster_key}|{max_minutes}m",
        "bucket_level": bucket_level,
        "bucket_key": list(key),
        "source_family_that_lost": str(first_loss.get("family") or "unknown"),
        "session": str(first_loss.get("session") or "unknown"),
        "lost_direction": lost_direction,
        "theoretical_direction": theoretical_direction,
        "window_minutes": int(max_minutes),
        **summary,
        "phase_a_shadow_candidate": bool(summary["samples"] >= min_samples and summary["inversion_net_usd"] > 0 and summary["profit_factor"] >= 1.2),
        "example_position_ids": [e["loss"].get("position_id") for e in events[:5]],
    }


def mine_stophunt_inversions(
    rows: Iterable[dict],
    max_minutes: int = 30,
    min_samples: int = 10,
    bucket_level: str = "family_session_dir",
) -> list[dict]:
    """Find losing-position buckets followed by opposite-side trades inside max_minutes."""
    items = sorted((_enrich(r) for r in rows), key=lambda r: r.get("open_dt") or datetime.min.replace(tzinfo=timezone.utc))
    buckets: dict[tuple, list[dict]] = {}
    for idx, loss in enumerate(items[:-1]):
        if loss.get("pnl_usd", 0.0) >= 0.0 or not loss.get("close_dt"):
            continue
        loss_dir = str(loss.get("direction") or "unknown")
        for nxt in items[idx + 1 : idx + 8]:
            if not nxt.get("open_dt"):
                continue
            gap_min = (nxt["open_dt"] - loss["close_dt"]).total_seconds() / 60.0
            if gap_min < 0:
                continue
            if gap_min > max_minutes:
                break
            if str(nxt.get("direction") or "") == _opposite(loss_dir):
                key = _bucket_key(loss, bucket_level)
                buckets.setdefault(key, []).append({"loss": loss, "followup": nxt, "gap_minutes": round(gap_min, 2)})
                break

    clusters: list[dict] = []
    for key, events in buckets.items():
        cluster = _cluster_from_events(bucket_level, key, events, max_minutes, min_samples)
        if cluster is not None:
            clusters.append(cluster)
    return sorted(clusters, key=lambda c: (c["phase_a_shadow_candidate"], c["inversion_net_usd"], c["samples"]), reverse=True)


def mine_multilevel_stophunt_inversions(rows: Iterable[dict], max_minutes: int = 30, min_samples: int = 10) -> list[dict]:
    """Mine exact and broader buckets so sparse exact samples do not hide real edges."""
    clusters: list[dict] = []
    for level in ("family_session_dir", "family_dir", "session_dir", "family_session", "family", "direction"):
        clusters.extend(mine_stophunt_inversions(rows, max_minutes=max_minutes, min_samples=min_samples, bucket_level=level))
    return sorted(clusters, key=lambda c: (c["phase_a_shadow_candidate"], c["inversion_net_usd"], c["samples"]), reverse=True)


def build_report(rows: Iterable[dict], min_samples: int = 10) -> dict:
    items = [_enrich(r) for r in rows]
    clusters: list[dict] = []
    for window in (15, 30, 60):
        clusters.extend(mine_multilevel_stophunt_inversions(items, max_minutes=window, min_samples=min_samples))
    seen = set()
    unique = []
    for c in clusters:
        if c["cluster_id"] in seen:
            continue
        seen.add(c["cluster_id"])
        unique.append(c)
    diamonds = [c for c in unique if c.get("phase_a_shadow_candidate")]
    return {
        "summary": {
            "positions": len(items),
            "losses": sum(1 for r in items if r.get("pnl_usd", 0.0) < 0.0),
            "winners": sum(1 for r in items if r.get("pnl_usd", 0.0) > 0.0),
            "diamonds_found": len(diamonds),
        },
        "diamonds": diamonds[:20],
        "all_clusters": unique[:50],
        "strategy_scaffold": {
            "family": "xau_stophunt_inversion_shadow",
            "phase": "A_observe_only",
            "live_enabled": False,
            "execution_enabled": False,
            "entry_rule": "After a qualified historical-loser family stops out, observe opposite-side continuation inside the mined time window.",
            "promotion_gate": "No live dispatch until walk-forward per-month validation, >=40 events per bucket, PF>=1.4, WR>=0.58, Opus review.",
        },
        "notes": [
            "Read-only report. Hidden edge is stop-hunt inversion after systematic losing clusters.",
            "Direction truth uses ctrader_positions.direction, not deal close direction.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=8000)
    ap.add_argument("--min-samples", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = load_positions(Path(args.db), symbol=args.symbol, days=args.days, limit=args.limit)
    report = build_report(rows, min_samples=args.min_samples)
    report["db_path"] = str(Path(args.db))
    report["symbol"] = args.symbol
    report["days"] = int(args.days)
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json:
        print(text)
    else:
        print(f"XAU mistake diamond miner db={args.db} symbol={args.symbol} days={args.days}")
        print(json.dumps(report["summary"], indent=2))
        print("Top diamonds:")
        for d in report["diamonds"][:10]:
            print(f"  {d['cluster_id']} samples={d['samples']} net={d['inversion_net_usd']} pf={d['profit_factor']} wr={d['winrate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

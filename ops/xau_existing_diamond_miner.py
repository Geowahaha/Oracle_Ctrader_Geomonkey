#!/usr/bin/env python3
"""Mine XAU strategies that are already profitable diamonds.

This is complementary to mistake mining: it finds buckets that are already
profitable with enough sample support, then emits shadow-only promotion
candidates. No order dispatch.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable

from ops.xau_mistake_diamond_miner import _direction, _parse_dt
from ops.xau_winner_archetype_report import classify_family, classify_risk_geometry, session_bucket


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _month(value) -> str:
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m") if dt else "unknown"


def _enrich(row: dict) -> dict:
    item = dict(row)
    item["pnl_usd"] = float(item.get("pnl_usd") or item.get("pnl") or 0.0)
    item["family"] = item.get("family") or classify_family(str(item.get("label") or ""), str(item.get("comment") or ""))
    item["direction"] = _direction(item.get("direction"))
    item["session"] = item.get("session") or session_bucket(item.get("first_seen_utc"))
    item["risk_geometry"] = item.get("risk_geometry") or classify_risk_geometry(item)
    item["month"] = item.get("month") or _month(item.get("first_seen_utc"))
    return item


def load_positions(db_path: Path, symbol: str = "XAUUSD", limit: int = 10000) -> list[dict]:
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
                 GROUP BY p.position_id
            )
            SELECT * FROM pos_pnl
             WHERE first_seen_utc IS NOT NULL AND first_seen_utc != ''
             ORDER BY first_seen_utc ASC
             LIMIT ?
            """,
            (f"%{symbol}%", f"%{symbol}%", int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [_enrich(dict(r)) for r in rows]


def _stats(rows: list[dict]) -> dict:
    pnls = [float(r.get("pnl_usd") or 0.0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    months = sorted({str(r.get("month") or "unknown") for r in rows})
    monthly = {}
    for m in months:
        mp = [float(r.get("pnl_usd") or 0.0) for r in rows if str(r.get("month")) == m]
        monthly[m] = round(sum(mp), 2)
    positive_months = sum(1 for v in monthly.values() if v > 0)
    return {
        "samples": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(rows), 4) if rows else 0.0,
        "net_usd": round(sum(pnls), 2),
        "avg_pnl_usd": round(mean(pnls), 4) if pnls else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else (999.0 if gross_win else 0.0),
        "months": len(months),
        "positive_months": positive_months,
        "monthly_net_usd": monthly,
    }


def _bucket_specs() -> list[tuple[str, Callable[[dict], tuple]]]:
    return [
        ("family_dir_rr", lambda r: (r["family"], r["direction"], r["risk_geometry"])),
        ("family_rr", lambda r: (r["family"], r["risk_geometry"])),
        ("family_session_dir", lambda r: (r["family"], r["session"], r["direction"])),
        ("family_session_rr", lambda r: (r["family"], r["session"], r["risk_geometry"])),
        ("session_rr_dir", lambda r: (r["session"], r["risk_geometry"], r["direction"])),
        ("family_dir", lambda r: (r["family"], r["direction"])),
        ("family", lambda r: (r["family"],)),
    ]


def mine_existing_diamonds(
    rows: Iterable[dict],
    min_samples: int = 40,
    min_pf: float = 1.4,
    min_net: float = 50.0,
    min_positive_months: int = 1,
) -> list[dict]:
    items = [_enrich(r) for r in rows]
    diamonds: list[dict] = []
    for level, key_fn in _bucket_specs():
        buckets: dict[tuple, list[dict]] = {}
        for item in items:
            key = tuple(str(v) for v in key_fn(item))
            if "unknown" in key:
                continue
            buckets.setdefault(key, []).append(item)
        for key, bucket_rows in buckets.items():
            if len(bucket_rows) < int(min_samples):
                continue
            stats = _stats(bucket_rows)
            promote = bool(
                stats["samples"] >= min_samples
                and stats["net_usd"] >= min_net
                and stats["profit_factor"] >= min_pf
                and stats["positive_months"] >= min_positive_months
            )
            if not promote:
                continue
            diamonds.append(
                {
                    "cluster_id": f"existing_diamond|{level}|" + "|".join(key),
                    "bucket_level": level,
                    "bucket_key": list(key),
                    **stats,
                    "shadow_promote_candidate": True,
                    "execution_enabled": False,
                    "live_enabled": False,
                    "example_position_ids": [r.get("position_id") for r in bucket_rows[:8]],
                }
            )
    return sorted(diamonds, key=lambda d: (d["net_usd"], d["profit_factor"], d["samples"]), reverse=True)


def select_shadow_promotions(diamonds: Iterable[dict]) -> list[dict]:
    """Select only robust existing diamonds for shadow-only forward walk promotion."""
    promotions: list[dict] = []
    for diamond in diamonds:
        key = list(diamond.get("bucket_key") or [])
        is_scheduled_sub1r_long = (
            diamond.get("bucket_level") == "family_dir_rr"
            and len(key) == 3
            and key[0] == "xauusd_scheduled"
            and key[1] == "long"
            and key[2] == "sub_1r_target"
        )
        if not is_scheduled_sub1r_long:
            continue
        if int(diamond.get("samples") or 0) < 40:
            continue
        if float(diamond.get("profit_factor") or 0.0) < 1.8:
            continue
        if float(diamond.get("winrate") or 0.0) < 0.60:
            continue
        if int(diamond.get("positive_months") or 0) < 3:
            continue
        promoted = dict(diamond)
        promoted.update(
            {
                "promotion_phase": "shadow_only_forward_walk",
                "promoted_family": "xau_existing_diamond_shadow",
                "archetype": "xauusd_scheduled_sub1r_long",
                "execution_enabled": False,
                "live_enabled": False,
                "risk_usd": 0.0,
                "promotion_reason": "Only existing diamond with >=40 samples, PF>=1.8, WR>=0.60, and 3 positive months.",
            }
        )
        promotions.append(promoted)
    return sorted(promotions, key=lambda d: (d["net_usd"], d["profit_factor"]), reverse=True)


def build_report(rows: Iterable[dict], min_samples: int = 40, min_pf: float = 1.4, min_net: float = 50.0) -> dict:
    items = [_enrich(r) for r in rows]
    diamonds = mine_existing_diamonds(items, min_samples=min_samples, min_pf=min_pf, min_net=min_net)
    shadow_promotions = select_shadow_promotions(diamonds)
    return {
        "summary": {
            "positions": len(items),
            "net_usd": round(sum(float(r.get("pnl_usd") or 0.0) for r in items), 2),
            "diamonds_found": len(diamonds),
            "shadow_promotions": len(shadow_promotions),
            "min_samples": int(min_samples),
            "min_pf": float(min_pf),
            "min_net": float(min_net),
        },
        "diamonds": diamonds[:50],
        "shadow_promotions": shadow_promotions,
        "promotion_scaffold": {
            "candidate_family": "xau_existing_diamond_shadow",
            "phase": "shadow_promotion_candidate",
            "execution_enabled": False,
            "live_enabled": False,
            "use": "Boost or clone only already-profitable historical XAU buckets; never promote losers.",
            "promotion_gate": "Opus review + forward shadow proof + no overlap with live risk limits.",
        },
        "notes": [
            "Read-only mining; no order dispatch.",
            "Diamonds are already profitable historical buckets, not mistake inversions.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/ctrader_openapi.db")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--min-samples", type=int, default=40)
    parser.add_argument("--min-pf", type=float, default=1.4)
    parser.add_argument("--min-net", type=float, default=50.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = load_positions(Path(args.db), symbol=args.symbol, limit=args.limit)
    report = build_report(rows, min_samples=args.min_samples, min_pf=args.min_pf, min_net=args.min_net)
    report["created_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json:
        print(text)
    else:
        print(f"xau_existing_diamond_miner diamonds={report['summary']['diamonds_found']} positions={report['summary']['positions']}")
        for d in report["diamonds"][:10]:
            print(f"  {d['cluster_id']} samples={d['samples']} net={d['net_usd']} pf={d['profit_factor']} wr={d['winrate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Mine predictive all-order edges from real cTrader position/deal history.

The report is read-only. It turns every closed order into historical priors,
walks the buckets forward by month, and emits canary-use recommendations only;
it never dispatches orders.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable

from ops.xau_mistake_diamond_miner import _direction, _parse_dt
from ops.xau_winner_archetype_report import classify_family, classify_risk_geometry, session_bucket


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite in immutable read-only mode for live VM-safe diagnostics."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _month(value: object) -> str:
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m") if dt else "unknown"


def _weekday(value: object) -> str:
    dt = _parse_dt(value)
    return WEEKDAYS[dt.weekday()] if dt else "unknown"


def _hour_bucket(value: object) -> str:
    dt = _parse_dt(value)
    return f"h{dt.hour:02d}" if dt else "unknown"


def _norm_symbol(value: object) -> str:
    token = str(value or "").strip().upper()
    return token or "UNKNOWN"


def _enrich(row: dict) -> dict:
    item = dict(row)
    item["symbol"] = _norm_symbol(item.get("symbol") or item.get("broker_symbol"))
    item["pnl_usd"] = float(item.get("pnl_usd") or item.get("pnl") or 0.0)
    item["family"] = item.get("family") or classify_family(str(item.get("label") or ""), str(item.get("comment") or ""))
    item["direction"] = _direction(item.get("direction"))
    item["session"] = item.get("session") or session_bucket(item.get("first_seen_utc"))
    item["risk_geometry"] = item.get("risk_geometry") or classify_risk_geometry(item)
    item["month"] = item.get("month") or _month(item.get("first_seen_utc"))
    item["weekday"] = item.get("weekday") or _weekday(item.get("first_seen_utc"))
    item["hour_bucket"] = item.get("hour_bucket") or _hour_bucket(item.get("first_seen_utc"))
    return item


def load_orders(db_path: Path, *, symbol: str = "", limit: int = 50000, closed_only: bool = True) -> list[dict]:
    """Load all positions with summed deal PnL from real cTrader storage."""
    conn = connect_ro(db_path)
    symbol_filter = str(symbol or "").strip().upper()
    if symbol_filter:
        where_symbol = "AND (UPPER(p.symbol) LIKE ? OR UPPER(p.broker_symbol) LIKE ?)"
        params: list[object] = [f"%{symbol_filter}%", f"%{symbol_filter}%"]
    else:
        where_symbol = ""
        params = []
    closed_filter = "AND COALESCE(p.is_open, 0) = 0" if closed_only else ""
    try:
        rows = conn.execute(
            f"""
            WITH pos_pnl AS (
                SELECT p.position_id,
                       p.symbol,
                       p.broker_symbol,
                       p.direction,
                       p.volume,
                       p.entry_price,
                       p.stop_loss,
                       p.take_profit,
                       p.label,
                       p.comment,
                       p.source,
                       p.lane,
                       p.is_open,
                       p.status,
                       p.first_seen_utc,
                       p.last_seen_utc,
                       SUM(COALESCE(d.pnl_usd, 0.0)) AS pnl_usd,
                       SUM(CASE WHEN COALESCE(d.pnl_usd, 0.0) > 0 THEN COALESCE(d.pnl_usd, 0.0) ELSE 0.0 END) AS gross_win_usd,
                       SUM(CASE WHEN COALESCE(d.pnl_usd, 0.0) < 0 THEN COALESCE(d.pnl_usd, 0.0) ELSE 0.0 END) AS gross_loss_usd,
                       MAX(d.execution_utc) AS close_utc,
                       COUNT(d.deal_id) AS deal_count
                  FROM ctrader_positions p
                  LEFT JOIN ctrader_deals d ON d.position_id = p.position_id
                 WHERE p.first_seen_utc IS NOT NULL
                   AND p.first_seen_utc != ''
                   {where_symbol}
                   {closed_filter}
                 GROUP BY p.position_id
            )
            SELECT * FROM pos_pnl
             ORDER BY first_seen_utc ASC
             LIMIT ?
            """,
            [*params, int(limit)],
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
    for month in months:
        monthly[month] = round(sum(float(r.get("pnl_usd") or 0.0) for r in rows if str(r.get("month")) == month), 2)
    positive_months = sum(1 for value in monthly.values() if value > 0)
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
        ("symbol_family_dir_rr", lambda r: (r["symbol"], r["family"], r["direction"], r["risk_geometry"])),
        ("symbol_family_session_dir", lambda r: (r["symbol"], r["family"], r["session"], r["direction"])),
        ("symbol_family_dir", lambda r: (r["symbol"], r["family"], r["direction"])),
        ("symbol_family_rr", lambda r: (r["symbol"], r["family"], r["risk_geometry"])),
        ("symbol_session_dir", lambda r: (r["symbol"], r["session"], r["direction"])),
        ("symbol_family", lambda r: (r["symbol"], r["family"])),
        ("symbol_dir", lambda r: (r["symbol"], r["direction"])),
    ]


def _split_train_forward(rows: list[dict]) -> tuple[list[dict], list[dict], str]:
    months = sorted({str(r.get("month") or "unknown") for r in rows if str(r.get("month") or "unknown") != "unknown"})
    if len(months) < 2:
        return list(rows), list(rows), months[-1] if months else "unknown"
    forward_month = months[-1]
    train = [r for r in rows if str(r.get("month")) != forward_month]
    forward = [r for r in rows if str(r.get("month")) == forward_month]
    return train, forward, forward_month


def _edge_type(stats: dict, forward: dict, min_wr: float, min_pf: float) -> str:
    wr_ok = float(stats.get("winrate") or 0.0) >= min_wr and float(forward.get("winrate") or 0.0) >= min_wr
    pf_ok = float(stats.get("profit_factor") or 0.0) >= min_pf and float(forward.get("profit_factor") or 0.0) >= min_pf
    net_ok = float(stats.get("net_usd") or 0.0) > 0 and float(forward.get("net_usd") or 0.0) > 0
    if wr_ok and pf_ok and net_ok:
        return "max_wr_and_profit"
    if pf_ok and net_ok:
        return "max_profit"
    if wr_ok and net_ok:
        return "max_wr"
    return "watch_only"


def mine_predictive_edges(
    rows: Iterable[dict],
    *,
    min_samples: int = 40,
    min_pf: float = 1.4,
    min_wr: float = 0.55,
    min_forward_samples: int = 8,
) -> list[dict]:
    """Mine forward-supported predictive buckets from all orders."""
    items = [_enrich(r) for r in rows]
    edges: list[dict] = []
    for level, key_fn in _bucket_specs():
        buckets: dict[tuple, list[dict]] = defaultdict(list)
        for item in items:
            key = tuple(str(v) for v in key_fn(item))
            if "unknown" in {k.lower() for k in key}:
                continue
            buckets[key].append(item)
        for key, bucket_rows in buckets.items():
            if len(bucket_rows) < int(min_samples):
                continue
            stats = _stats(bucket_rows)
            if stats["net_usd"] <= 0:
                continue
            train_rows, forward_rows, forward_month = _split_train_forward(bucket_rows)
            train_stats = _stats(train_rows)
            forward_stats = _stats(forward_rows)
            if forward_stats["samples"] < min_forward_samples:
                continue
            edge_type = _edge_type(stats, forward_stats, min_wr, min_pf)
            if edge_type == "watch_only":
                continue
            if train_stats["net_usd"] <= 0 or forward_stats["net_usd"] <= 0:
                continue
            score = (
                float(forward_stats["net_usd"])
                * max(0.5, float(forward_stats["profit_factor"]))
                * max(0.5, float(forward_stats["winrate"]) + 0.25)
            )
            edges.append(
                {
                    "cluster_id": f"predictive_edge|{level}|" + "|".join(key),
                    "bucket_level": level,
                    "bucket_key": list(key),
                    **stats,
                    "train": train_stats,
                    "forward": forward_stats,
                    "forward_month": forward_month,
                    "edge_type": edge_type,
                    "predictive_score": round(score, 4),
                    "execution_enabled": False,
                    "live_enabled": False,
                    "example_position_ids": [r.get("position_id") for r in bucket_rows[:10]],
                }
            )
    return sorted(edges, key=lambda d: (d["edge_type"] == "max_wr_and_profit", d["predictive_score"], d["net_usd"]), reverse=True)


def select_live_uses(edges: Iterable[dict]) -> list[dict]:
    """Translate edges into safe canary recommendations, never direct dispatch."""
    uses: list[dict] = []
    for edge in edges:
        key = list(edge.get("bucket_key") or [])
        symbol = str(key[0] if key else "").upper()
        if not symbol:
            continue
        forward = dict(edge.get("forward") or {})
        base_ok = (
            int(edge.get("samples") or 0) >= 40
            and int(forward.get("samples") or 0) >= 8
            and float(edge.get("net_usd") or 0.0) > 0
            and float(forward.get("net_usd") or 0.0) > 0
            and float(edge.get("profit_factor") or 0.0) >= 1.4
            and float(forward.get("profit_factor") or 0.0) >= 1.2
        )
        wr_ok = float(edge.get("winrate") or 0.0) >= 0.55 and float(forward.get("winrate") or 0.0) >= 0.50
        max_profit_ok = str(edge.get("edge_type") or "") == "max_profit" and float(forward.get("profit_factor") or 0.0) >= 1.4
        if not base_ok or not (wr_ok or max_profit_ok):
            continue
        base_risk = 0.5 if symbol == "XAUUSD" else 0.35 if symbol == "ETHUSD" else 0.65 if symbol == "BTCUSD" else 0.25
        max_risk = base_risk if wr_ok else round(base_risk * 0.5, 4)
        action = "canary_allow_or_boost_existing_lane" if wr_ok else "canary_exit_or_micro_risk_only"
        uses.append(
            {
                "edge_cluster_id": edge.get("cluster_id"),
                "symbol": symbol,
                "bucket_level": edge.get("bucket_level"),
                "bucket_key": key,
                "edge_type": edge.get("edge_type"),
                "recommended_action": action,
                "execution_enabled": False,
                "live_enabled": False,
                "max_risk_usd": max_risk,
                "reason": "Forward-supported DB edge; consume only through existing canary executor/risk governor, with reduced risk for profit-only low-WR edges.",
            }
        )
    return uses[:20]


def build_edge_report(rows: Iterable[dict], *, min_samples: int = 40, min_pf: float = 1.4, min_wr: float = 0.55) -> dict:
    items = [_enrich(r) for r in rows]
    edges = mine_predictive_edges(items, min_samples=min_samples, min_pf=min_pf, min_wr=min_wr)
    uses = select_live_uses(edges)
    return {
        "summary": {
            "positions": len(items),
            "net_usd": round(sum(float(r.get("pnl_usd") or 0.0) for r in items), 2),
            "symbols": sorted({_norm_symbol(r.get("symbol")) for r in items}),
            "predictive_edges": len(edges),
            "live_use_candidates": len(uses),
            "min_samples": int(min_samples),
            "min_pf": float(min_pf),
            "min_wr": float(min_wr),
        },
        "top_edges": edges[:80],
        "live_use_candidates": uses,
        "prediction_contract": {
            "data_source": "ctrader_positions_joined_deals",
            "method": "monthly walk-forward bucket priors from real closed positions",
            "prediction": "next matching setup inherits the bucket prior until invalidated by forward results",
            "execution_enabled": False,
            "live_enabled": False,
            "promotion_rule": "Only existing canary lanes may consume this as allow/boost; no new direct dispatch.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/ctrader_openapi.db")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--limit", type=int, default=50000)
    parser.add_argument("--min-samples", type=int, default=40)
    parser.add_argument("--min-pf", type=float, default=1.4)
    parser.add_argument("--min-wr", type=float, default=0.55)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = load_orders(Path(args.db), symbol=args.symbol, limit=args.limit)
    report = build_edge_report(rows, min_samples=args.min_samples, min_pf=args.min_pf, min_wr=args.min_wr)
    report["created_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json:
        print(text)
    else:
        s = report["summary"]
        print(
            "all_order_edge_predictor "
            f"positions={s['positions']} edges={s['predictive_edges']} live_uses={s['live_use_candidates']} net={s['net_usd']}"
        )
        for edge in report["top_edges"][:12]:
            print(
                f"  {edge['cluster_id']} samples={edge['samples']} net={edge['net_usd']} "
                f"pf={edge['profit_factor']} wr={edge['winrate']} fwd={edge['forward']['net_usd']} type={edge['edge_type']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

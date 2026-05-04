#!/usr/bin/env python3
"""Read-only Fibo direction diagnostics for cTrader journal/deal semantics.

Uses SQLite immutable URI by default so it remains safe on the live DB copy.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


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


def run(db_path: Path, limit: int = 80) -> dict:
    conn = connect_ro(db_path)
    rows = conn.execute(
        """
        SELECT d.deal_id, d.journal_id, d.execution_utc, d.source AS deal_source,
               d.lane, d.direction AS deal_direction, d.symbol, d.broker_symbol,
               COALESCE(d.pnl_usd, d.gross_profit_usd, 0.0) AS pnl_usd,
               d.execution_price, d.volume, d.signal_run_no,
               j.created_utc AS journal_created_utc, j.direction AS journal_direction,
               j.status AS journal_status, j.message AS journal_message,
               j.request_json, j.response_json
          FROM ctrader_deals d
          LEFT JOIN execution_journal j ON j.id = d.journal_id
         WHERE (UPPER(COALESCE(d.symbol,'')) LIKE '%XAU%' OR UPPER(COALESCE(d.broker_symbol,'')) LIKE '%XAU%')
           AND LOWER(COALESCE(d.source,'') || '|' || COALESCE(d.lane,'')) LIKE '%fibo%'
           AND ABS(COALESCE(d.pnl_usd, d.gross_profit_usd, 0.0)) > 0.0001
         ORDER BY d.execution_utc DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    findings = []
    mismatch = 0
    status_counts = Counter()
    for row in rows:
        req = _json_obj(row["request_json"] or "{}")
        resp = _json_obj(row["response_json"] or "{}")
        req_dir = str(req.get("direction") or row["journal_direction"] or "").lower()
        deal_dir = str(row["deal_direction"] or "").lower()
        same = bool(req_dir and deal_dir and req_dir == deal_dir)
        if req_dir and deal_dir and not same:
            mismatch += 1
        status_counts[str(row["journal_status"] or "")] += 1
        findings.append(
            {
                "deal_id": row["deal_id"],
                "journal_id": row["journal_id"],
                "execution_utc": row["execution_utc"],
                "request_direction": req_dir,
                "deal_direction": deal_dir,
                "direction_same": same,
                "pnl_usd": round(float(row["pnl_usd"] or 0.0), 2),
                "entry": req.get("entry"),
                "execution_price": row["execution_price"],
                "label": req.get("label") or resp.get("label") or "",
                "comment": req.get("comment") or resp.get("comment") or "",
                "source": row["deal_source"],
                "journal_status": row["journal_status"],
                "journal_message": row["journal_message"],
            }
        )
    conn.close()
    total = len(findings)
    return {
        "db_path": str(db_path),
        "rows": total,
        "direction_mismatch_count": mismatch,
        "direction_mismatch_rate": round(mismatch / total, 4) if total else 0.0,
        "journal_status_counts": dict(status_counts),
        "interpretation": "If most profitable closed-deal rows have request_direction opposite deal_direction, ctrader_deals.direction is likely close-side/deal-side, while execution_journal.request_json.direction is signal entry direction.",
        "findings": findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = run(Path(args.db), limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"DB: {report['db_path']}")
        print(f"rows={report['rows']} mismatch={report['direction_mismatch_count']} rate={report['direction_mismatch_rate']:.1%}")
        print(f"status_counts={report['journal_status_counts']}")
        print(report["interpretation"])
        for row in report["findings"][: min(20, len(report["findings"]))]:
            print(
                f"{row['execution_utc']} jid={row['journal_id']} req={row['request_direction']} "
                f"deal={row['deal_direction']} same={row['direction_same']} pnl={row['pnl_usd']} "
                f"comment={row['comment']} msg={row['journal_message']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

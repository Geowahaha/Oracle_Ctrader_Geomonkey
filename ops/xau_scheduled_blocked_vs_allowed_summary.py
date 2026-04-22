"""
ops/xau_scheduled_blocked_vs_allowed_summary.py

Side-by-side summary for scheduled XAU lanes in execution_journal:
- blocked late-entry rows (no_chase / trap_guard)
- allowed rows (scheduled entries that passed pre-dispatch and were journaled)
- reason buckets
- status buckets
- outcome / pnl where ctrader_deals can be joined by journal_id

Usage:
    python ops/xau_scheduled_blocked_vs_allowed_summary.py
    python ops/xau_scheduled_blocked_vs_allowed_summary.py --hours 24
    python ops/xau_scheduled_blocked_vs_allowed_summary.py --format json
    python ops/xau_scheduled_blocked_vs_allowed_summary.py --db /opt/dexter_pro/data/ctrader_openapi.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("data/ctrader_openapi.db")
SCHEDULED_SOURCES = {"xauusd_scheduled:canary", "xauusd_scheduled:winner"}
LATE_REASONS = {"xau_scheduled_no_chase_block", "xau_scheduled_trap_guard_block"}
ALLOWED_STATUSES = {"accepted", "executed", "placed", "filled", "ok", "dry_run"}


def _safe_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _iter_execution_rows(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT id, created_utc, source, status, request_json, response_json, execution_meta_json, message
        FROM execution_journal
        WHERE datetime(created_utc) >= datetime('now', ?)
          AND LOWER(COALESCE(source, '')) LIKE 'xauusd_scheduled:%'
        ORDER BY id DESC
    """
    return list(cur.execute(query, (f"-{max(1, int(hours))} hour",)).fetchall())


def _load_deal_map(conn: sqlite3.Connection, hours: int) -> dict[int, list[dict[str, Any]]]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT journal_id, execution_utc, source, symbol, direction, pnl_usd, outcome, has_close_detail
        FROM ctrader_deals
        WHERE datetime(execution_utc) >= datetime('now', ?)
          AND UPPER(COALESCE(symbol, '')) IN ('XAUUSD', 'GOLD', 'XAU')
    """
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in cur.execute(query, (f"-{max(1, int(hours))} hour",)).fetchall():
        journal_id = int(row["journal_id"] or 0)
        if journal_id <= 0:
            continue
        out[journal_id].append(
            {
                "execution_utc": str(row["execution_utc"] or ""),
                "source": str(row["source"] or "").strip().lower(),
                "symbol": str(row["symbol"] or "").strip().upper(),
                "direction": str(row["direction"] or "").strip().lower(),
                "pnl_usd": _safe_float(row["pnl_usd"]),
                "outcome": row["outcome"],
                "has_close_detail": int(row["has_close_detail"] or 0),
            }
        )
    return out


def _normalize_reason(response: dict[str, Any], meta: dict[str, Any]) -> str:
    reason = str(meta.get("pre_dispatch_reason") or response.get("reason") or "").strip().lower()
    if reason in LATE_REASONS:
        return reason
    late_meta = dict(meta.get("xau_scheduled_late_entry_block") or {})
    reason = str(late_meta.get("reason") or reason).strip().lower()
    return reason


def _bucket_row(row: sqlite3.Row, deal_map: dict[int, list[dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
    request = _safe_json(str(row["request_json"] or ""))
    response = _safe_json(str(row["response_json"] or ""))
    meta = _safe_json(str(row["execution_meta_json"] or ""))
    source = str(row["source"] or request.get("source") or "").strip().lower()
    status = str(row["status"] or "").strip().lower()
    reason = _normalize_reason(response, meta)
    row_id = int(row["id"] or 0)
    deals = list(deal_map.get(row_id) or [])
    pnl_values = [float(d["pnl_usd"]) for d in deals if d.get("pnl_usd") is not None]
    outcomes = [d.get("outcome") for d in deals]
    category = "allowed"
    if reason in LATE_REASONS:
        category = "blocked"
    elif bool(meta.get("pre_dispatch_audit")) and status == "filtered":
        category = "other_filtered"
    item = {
        "id": row_id,
        "created_utc": str(row["created_utc"] or ""),
        "source": source,
        "status": status,
        "reason": reason,
        "message": str(row["message"] or ""),
        "direction": str(request.get("direction") or "").strip().lower(),
        "entry_type": str(request.get("entry_type") or "").strip().lower(),
        "confidence": request.get("confidence"),
        "signal_run_id": str(request.get("signal_run_id") or ""),
        "signal_run_no": int(request.get("signal_run_no") or 0),
        "deal_count": len(deals),
        "deal_outcomes": outcomes,
        "deal_pnl_sum": round(sum(pnl_values), 4) if pnl_values else None,
        "late_entry_flag": bool((request.get("raw_scores") or {}).get("xau_scheduled_late_entry_blocked")),
    }
    return category, item


def _summarize_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    pnl_sum = 0.0
    pnl_count = 0
    hourly_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        status_counts[str(row.get("status") or "(none)")] += 1
        reason = str(row.get("reason") or "")
        if reason:
            reason_counts[reason] += 1
        source_counts[str(row.get("source") or "(unknown)")] += 1
        for outcome in list(row.get("deal_outcomes") or []):
            outcome_counts[str(outcome)] += 1
        pnl = row.get("deal_pnl_sum")
        if pnl is not None:
            pnl_sum += float(pnl)
            pnl_count += 1
        created_utc = str(row.get("created_utc") or "")
        hour_bucket = created_utc[:13] + ":00Z" if len(created_utc) >= 13 else "unknown"
        hourly_counts[hour_bucket][str(row.get("source") or "(unknown)")] += 1
    return {
        "count": len(rows),
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts),
        "source_counts": dict(source_counts),
        "outcome_counts": dict(outcome_counts),
        "deal_pnl_sum": round(pnl_sum, 4) if pnl_count else None,
        "deal_pnl_avg": round(pnl_sum / pnl_count, 4) if pnl_count else None,
        "hourly_source_counts": {bucket: dict(counter) for bucket, counter in sorted(hourly_counts.items())},
        "recent_examples": rows[:20],
    }


def build_summary(exec_rows: list[sqlite3.Row], deal_map: dict[int, list[dict[str, Any]]], *, hours: int) -> dict[str, Any]:
    blocked: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    other_filtered: list[dict[str, Any]] = []
    all_statuses: Counter[str] = Counter()
    for row in exec_rows:
        category, item = _bucket_row(row, deal_map)
        all_statuses[str(item.get("status") or "(none)")] += 1
        if category == "blocked":
            blocked.append(item)
        elif category == "allowed":
            allowed.append(item)
        else:
            other_filtered.append(item)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": int(hours),
        "scheduled_sources": sorted(SCHEDULED_SOURCES),
        "execution_journal_rows": len(exec_rows),
        "all_status_counts": dict(all_statuses),
        "blocked_late_entry": _summarize_bucket(blocked),
        "allowed_rows": _summarize_bucket(allowed),
        "other_filtered_rows": _summarize_bucket(other_filtered),
    }


def print_text(summary: dict[str, Any]) -> None:
    print("Scheduled XAU blocked vs allowed summary")
    print(f"window_hours: {summary['window_hours']}")
    print(f"execution_journal_rows: {summary['execution_journal_rows']}")
    print("all_status_counts:")
    for key, value in sorted(dict(summary.get("all_status_counts") or {}).items()):
        print(f"  - {key}: {value}")
    for bucket_name in ("blocked_late_entry", "allowed_rows", "other_filtered_rows"):
        bucket = dict(summary.get(bucket_name) or {})
        print(f"\n[{bucket_name}]")
        print(f"count: {bucket.get('count', 0)}")
        for label in ("status_counts", "reason_counts", "source_counts", "outcome_counts"):
            print(f"{label}:")
            for key, value in sorted(dict(bucket.get(label) or {}).items()):
                print(f"  - {key}: {value}")
        print(f"deal_pnl_sum: {bucket.get('deal_pnl_sum')}")
        print(f"deal_pnl_avg: {bucket.get('deal_pnl_avg')}")
        print("recent_examples:")
        for row in list(bucket.get("recent_examples") or [])[:8]:
            print(
                "  - {created_utc} | {source} | status={status} | reason={reason} | dir={direction} | etype={entry_type} | pnl={deal_pnl_sum}".format(
                    **row
                )
            )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare scheduled XAU blocked vs allowed rows.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="Path to ctrader_openapi.db")
    p.add_argument("--hours", type=int, default=72, help="Lookback window in hours")
    p.add_argument("--format", choices=["text", "json"], default="text")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": f"cannot open DB: {exc}", "db": str(db_path)}))
        return 1
    try:
        exec_rows = _iter_execution_rows(conn, args.hours)
        deal_map = _load_deal_map(conn, args.hours)
        summary = build_summary(exec_rows, deal_map, hours=args.hours)
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

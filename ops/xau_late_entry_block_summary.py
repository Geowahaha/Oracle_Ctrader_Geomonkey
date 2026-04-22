"""
ops/xau_late_entry_block_summary.py — summarize scheduled XAU late-entry blocks
recorded via pre-dispatch audit telemetry.

Usage:
    python ops/xau_late_entry_block_summary.py
    python ops/xau_late_entry_block_summary.py --db /opt/dexter_pro/data/ctrader_openapi.db
    python ops/xau_late_entry_block_summary.py --hours 24 --format json
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
LATE_REASONS = {"xau_scheduled_no_chase_block", "xau_scheduled_trap_guard_block"}


def _safe_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _iter_rows(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT id, created_utc, source, request_json, response_json, execution_meta_json
        FROM execution_journal
        WHERE datetime(created_utc) >= datetime('now', ?)
        ORDER BY id DESC
    """
    return list(cur.execute(query, (f"-{max(1, int(hours))} hour",)).fetchall())


def build_summary(rows: list[sqlite3.Row], *, hours: int) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    hourly_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        response = _safe_json(str(row["response_json"] or ""))
        execution_meta = _safe_json(str(row["execution_meta_json"] or ""))
        request = _safe_json(str(row["request_json"] or ""))
        reason = str(
            execution_meta.get("pre_dispatch_reason")
            or response.get("reason")
            or ""
        ).strip().lower()
        if reason not in LATE_REASONS:
            late_meta = dict(execution_meta.get("xau_scheduled_late_entry_block") or {})
            reason = str(late_meta.get("reason") or reason).strip().lower()
        if reason not in LATE_REASONS:
            continue
        source = str(row["source"] or request.get("source") or "").strip().lower()
        created_utc = str(row["created_utc"] or "")
        hour_bucket = created_utc[:13] + ":00Z" if len(created_utc) >= 13 else "unknown"
        symbol = str(request.get("symbol") or "").strip().upper()
        direction = str(request.get("direction") or "").strip().lower()
        entry_type = str(request.get("entry_type") or "").strip().lower()
        confidence = request.get("confidence")
        raw_scores = dict(request.get("raw_scores") or {})
        matched.append(
            {
                "id": int(row["id"] or 0),
                "created_utc": created_utc,
                "source": source,
                "reason": reason,
                "symbol": symbol,
                "direction": direction,
                "entry_type": entry_type,
                "confidence": confidence,
                "signal_run_id": str(request.get("signal_run_id") or ""),
                "late_entry_flag": bool(raw_scores.get("xau_scheduled_late_entry_blocked")),
            }
        )
        reason_counts[reason] += 1
        source_counts[source or "(unknown)"] += 1
        hourly_counts[hour_bucket][reason] += 1

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": int(hours),
        "total_late_entry_blocks": len(matched),
        "reason_counts": dict(reason_counts),
        "source_counts": dict(source_counts),
        "hourly_reason_counts": {bucket: dict(counter) for bucket, counter in sorted(hourly_counts.items())},
        "recent_examples": matched[:20],
    }


def print_text(summary: dict[str, Any]) -> None:
    print("XAU scheduled late-entry block summary")
    print(f"window_hours: {summary['window_hours']}")
    print(f"total_late_entry_blocks: {summary['total_late_entry_blocks']}")
    print("reason_counts:")
    for key, value in sorted(dict(summary.get("reason_counts") or {}).items()):
        print(f"  - {key}: {value}")
    print("source_counts:")
    for key, value in sorted(dict(summary.get("source_counts") or {}).items()):
        print(f"  - {key}: {value}")
    print("recent_examples:")
    for row in list(summary.get("recent_examples") or [])[:10]:
        print(
            "  - {created_utc} | {source} | {reason} | {direction} | {entry_type} | conf={confidence}".format(
                **row
            )
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Summarize scheduled XAU late-entry block telemetry.")
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
        rows = _iter_rows(conn, args.hours)
        summary = build_summary(rows, hours=args.hours)
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

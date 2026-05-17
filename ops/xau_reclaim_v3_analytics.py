#!/usr/bin/env python3
"""Evidence-only analytics for XAU Reclaim/Staircase V3 telemetry.

Reads execution_journal rows whose request/execution metadata contains
raw_scores.xau_reclaim_v3 and summarizes whether the live/demo feature is only
recording metadata, actively identifying reclaim setups, bypassing confidence
floors, or changing risk/confidence. This script is read-only and never applies
policy.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Any


DEFAULT_DB = "data/ctrader_openapi.db"


def _json_obj(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _meta_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("raw_scores")
    if isinstance(raw, Mapping) and isinstance(raw.get("xau_reclaim_v3"), Mapping):
        return dict(raw.get("xau_reclaim_v3") or {})
    direct = payload.get("xau_reclaim_v3")
    if isinstance(direct, Mapping):
        return dict(direct or {})
    return {}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(db_path: Path, *, days: int = 7, limit: int = 5000) -> list[dict[str, Any]]:
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, created_utc, source, lane, symbol, direction, confidence,
                   entry, stop_loss, take_profit, entry_type, status, message,
                   request_json, execution_meta_json
              FROM execution_journal
             WHERE created_utc >= datetime('now', ?)
               AND (
                    coalesce(request_json, '') like '%xau_reclaim_v3%'
                 OR coalesce(execution_meta_json, '') like '%xau_reclaim_v3%'
               )
             ORDER BY created_utc DESC
             LIMIT ?
            """,
            (f"-{max(1, int(days))} days", int(limit)),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        request = _json_obj(item.pop("request_json", "{}"))
        exec_meta = _json_obj(item.pop("execution_meta_json", "{}"))
        reclaim = _meta_from_payload(request) or _meta_from_payload(exec_meta)
        item["xau_reclaim_v3"] = reclaim
        item["signal_run_id"] = request.get("signal_run_id") or exec_meta.get("signal_run_id") or ""
        out.append(item)
    return out


def _bucket_key(row: Mapping[str, Any], *fields: str) -> str:
    parts = []
    meta = dict(row.get("xau_reclaim_v3") or {})
    for field in fields:
        if field.startswith("meta."):
            parts.append(str(meta.get(field.split(".", 1)[1], "")))
        else:
            parts.append(str(row.get(field, "")))
    return "|".join(parts)


def summarize_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(r) for r in rows or []]
    by_source = Counter(_bucket_key(r, "source") for r in rows)
    by_status = Counter(_bucket_key(r, "status") for r in rows)
    by_phase = Counter(_bucket_key(r, "meta.phase") for r in rows)
    by_reason = Counter(_bucket_key(r, "meta.reason") for r in rows)
    by_active = Counter(str(bool((r.get("xau_reclaim_v3") or {}).get("active"))) for r in rows)
    by_bypass = Counter(str(bool((r.get("xau_reclaim_v3") or {}).get("bypass_conf_below"))) for r in rows)
    by_winner_override = Counter(str(bool((r.get("xau_reclaim_v3") or {}).get("winner_partial_override"))) for r in rows)
    by_shadow = Counter(str(bool((r.get("xau_reclaim_v3") or {}).get("shadow_only"))) for r in rows)

    scores = [float((r.get("xau_reclaim_v3") or {}).get("score") or 0.0) for r in rows]
    planned_rr = [float((r.get("xau_reclaim_v3") or {}).get("planned_rr") or 0.0) for r in rows]
    confidence_bonus = [float((r.get("xau_reclaim_v3") or {}).get("confidence_bonus") or 0.0) for r in rows]
    risk_mult = [float((r.get("xau_reclaim_v3") or {}).get("risk_mult") or 1.0) for r in rows]

    active_rows = [r for r in rows if bool((r.get("xau_reclaim_v3") or {}).get("active"))]
    live_effect_rows = [
        r for r in rows
        if bool((r.get("xau_reclaim_v3") or {}).get("active"))
        and not bool((r.get("xau_reclaim_v3") or {}).get("shadow_only"))
        and (
            bool((r.get("xau_reclaim_v3") or {}).get("bypass_conf_below"))
            or bool((r.get("xau_reclaim_v3") or {}).get("winner_partial_override"))
            or float((r.get("xau_reclaim_v3") or {}).get("confidence_bonus") or 0.0) > 0.0
            or float((r.get("xau_reclaim_v3") or {}).get("risk_mult") or 1.0) > 1.0001
        )
    ]

    def top(counter: Counter, n: int = 20) -> list[dict[str, Any]]:
        return [{"key": key, "n": value} for key, value in counter.most_common(n)]

    return {
        "rows": len(rows),
        "active_rows": len(active_rows),
        "possible_live_effect_rows": len(live_effect_rows),
        "active_rate": round(len(active_rows) / len(rows), 4) if rows else 0.0,
        "score_max": round(max(scores), 3) if scores else 0.0,
        "score_avg": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "planned_rr_min": round(min(planned_rr), 4) if planned_rr else 0.0,
        "planned_rr_avg": round(sum(planned_rr) / len(planned_rr), 4) if planned_rr else 0.0,
        "confidence_bonus_max": round(max(confidence_bonus), 3) if confidence_bonus else 0.0,
        "risk_mult_max": round(max(risk_mult), 4) if risk_mult else 1.0,
        "by_source": top(by_source),
        "by_status": top(by_status),
        "by_phase": top(by_phase),
        "by_reason": top(by_reason),
        "by_active": top(by_active),
        "by_shadow_only": top(by_shadow),
        "by_bypass_conf_below": top(by_bypass),
        "by_winner_partial_override": top(by_winner_override),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_rows(Path(args.db), days=args.days, limit=args.limit)
    report = {
        "db_path": str(Path(args.db)),
        "days": int(args.days),
        "summary": summarize_rows(rows),
        "samples": rows[: max(0, int(args.samples))],
        "note": "Read-only evidence. Do not infer live edge until active rows have outcomes.",
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"XAU Reclaim V3 telemetry analytics db={report['db_path']} days={report['days']}")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2, default=str))
        if report["samples"]:
            print("samples:")
            for row in report["samples"]:
                meta = row.get("xau_reclaim_v3") or {}
                print(
                    f"  id={row.get('id')} utc={row.get('created_utc')} source={row.get('source')} "
                    f"dir={row.get('direction')} conf={row.get('confidence')} status={row.get('status')} "
                    f"active={meta.get('active')} score={meta.get('score')} phase={meta.get('phase')} "
                    f"reason={meta.get('reason')} msg={row.get('message')}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

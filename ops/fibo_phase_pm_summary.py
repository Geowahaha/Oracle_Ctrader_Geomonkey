#!/usr/bin/env python3
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


def _safe_json(text):
    try:
        obj = json.loads(text or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _bucket(items, key):
    c = Counter()
    for item in items:
        c[str(item.get(key) or "unknown")] += 1
    return dict(sorted(c.items()))


def main():
    ap = argparse.ArgumentParser(description="Summarize fibo phase position-manager telemetry from execution_journal")
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    db_path = Path(args.db)
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, source, symbol, status, execution_meta_json, created_utc
        FROM execution_journal
        WHERE created_utc >= datetime('now', ?)
          AND lower(coalesce(source, '')) LIKE 'fibo%'
        ORDER BY id DESC
        """,
        (f"-{int(args.hours)} hours",),
    ).fetchall()
    conn.close()

    audits = []
    for row in rows:
        meta = _safe_json(row["execution_meta_json"])
        for item in list(meta.get("position_manager_audit") or []):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().lower()
            if not (action.startswith("fibo_") or action.startswith("xau_profit_extension") or action == "xau_momentum_exhaustion_lock"):
                continue
            details = dict(item.get("details") or {})
            audits.append(
                {
                    "journal_id": int(row["id"] or 0),
                    "source": str(row["source"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "status": str(row["status"] or ""),
                    "action": action,
                    "phase": str(details.get("phase") or "unknown"),
                    "winner_profile": bool(details.get("winner_profile")),
                    "giveback_r_buffer": _safe_float(details.get("giveback_r_buffer"), -1.0),
                    "locked_r": _safe_float(details.get("locked_r"), -1.0),
                    "target_r": _safe_float(details.get("target_r"), -1.0),
                    "partial_close_pct": _safe_float(details.get("partial_close_pct"), -1.0),
                    "partial_close_volume": _safe_float(details.get("partial_close_volume"), -1.0),
                    "trigger": str(details.get("trigger") or ""),
                }
            )

    metrics = {
        "rows": len(rows),
        "audits": len(audits),
        "actions": _bucket(audits, "action"),
        "phases": _bucket(audits, "phase"),
        "winner_profile": {
            "true": sum(1 for x in audits if x.get("winner_profile")),
            "false": sum(1 for x in audits if not x.get("winner_profile")),
        },
    }

    by_phase = defaultdict(list)
    for item in audits:
        by_phase[item["phase"]].append(item)

    phase_metrics = {}
    for phase, items in by_phase.items():
        givebacks = [x["giveback_r_buffer"] for x in items if x["giveback_r_buffer"] >= 0]
        locked = [x["locked_r"] for x in items if x["locked_r"] >= 0]
        targets = [x["target_r"] for x in items if x["target_r"] >= 0]
        partials = [x["partial_close_pct"] for x in items if x["partial_close_pct"] >= 0]
        phase_metrics[phase] = {
            "count": len(items),
            "avg_giveback_r_buffer": round(mean(givebacks), 4) if givebacks else None,
            "avg_locked_r": round(mean(locked), 4) if locked else None,
            "avg_target_r": round(mean(targets), 4) if targets else None,
            "avg_partial_close_pct": round(mean(partials), 4) if partials else None,
            "actions": _bucket(items, "action"),
        }
    metrics["phase_metrics"] = dict(sorted(phase_metrics.items()))

    if args.format == "json":
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return

    print(f"Fibo PM telemetry summary | rows={metrics['rows']} audits={metrics['audits']}")
    print("actions:")
    for k, v in metrics["actions"].items():
        print(f"  {k}: {v}")
    print("phases:")
    for k, v in metrics["phases"].items():
        print(f"  {k}: {v}")
    print("winner_profile:")
    for k, v in metrics["winner_profile"].items():
        print(f"  {k}: {v}")
    print("phase_metrics:")
    for phase, info in metrics["phase_metrics"].items():
        print(f"  {phase}: count={info['count']} giveback_r={info['avg_giveback_r_buffer']} locked_r={info['avg_locked_r']} target_r={info['avg_target_r']} partial_pct={info['avg_partial_close_pct']}")


if __name__ == "__main__":
    main()

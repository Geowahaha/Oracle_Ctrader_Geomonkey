#!/usr/bin/env python3
"""Shadow evidence report for the Fibo MTF trade planner.

Reads xau_shadow_journal rows produced by the reviewed scheduler path:
block_reason='fibo_mtf_planner:<route>'.  This is evidence-only tooling for
Opus/micro-live review; it never promotes or changes trading behavior.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

PROMOTION_ROUTE = "probe"
MIN_PROBE_DECISIONS = 30
MIN_CALENDAR_DAYS = 14
MIN_SESSIONS = 2
MIN_WINRATE = 0.50
MIN_EXPECTANCY_R = 0.40
MAX_MAE_R = 3.50
MIN_REAL_ANCHOR_RATE = 0.95


def _json_obj(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_route(row: dict, raw: dict) -> str:
    block_reason = str(row.get("block_reason") or "")
    if block_reason.startswith("fibo_mtf_planner:"):
        return block_reason.split(":", 1)[1] or "unknown"
    return str(raw.get("fibo_mtf_route") or "unknown")


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


def _session_key(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    hour = dt.hour
    if 0 <= hour < 7:
        bucket = "asia"
    elif 7 <= hour < 13:
        bucket = "london"
    elif 13 <= hour < 21:
        bucket = "ny"
    else:
        bucket = "post_ny"
    return f"{dt.date()}:{bucket}"


def _has_real_anchor(raw: dict) -> bool:
    keys = (
        "execution_swing_low",
        "execution_swing_high",
        "local_swing_low",
        "local_swing_high",
        "recent_swing_low",
        "recent_swing_high",
    )
    for key in keys:
        try:
            if float(raw.get(key) or 0.0) > 0.0:
                return True
        except Exception:
            continue
    planner = raw.get("fibo_mtf_trade_planner") if isinstance(raw.get("fibo_mtf_trade_planner"), dict) else {}
    trade_plan = planner.get("trade_plan") if isinstance(planner.get("trade_plan"), dict) else {}
    metadata = trade_plan.get("metadata") if isinstance(trade_plan.get("metadata"), dict) else {}
    for key in ("execution_anchor", "anchor_source", "stop_anchor_source"):
        if metadata.get(key):
            return True
    return False


def _mae_r(row: dict, raw: dict) -> float | None:
    for key in ("shadow_mae_rr", "mae_r", "simulated_mae_r", "max_adverse_excursion_r"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return abs(float(value))
        except Exception:
            continue
    value = row.get("shadow_mae_rr")
    if value is not None:
        try:
            return abs(float(value))
        except Exception:
            return None
    return None


def load_rows(db_path: Path, days: int = 30, limit: int = 5000) -> list[dict]:
    conn = connect_ro(db_path)
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(xau_shadow_journal)").fetchall()}
        optional_cols = [name for name in ("shadow_mae_rr", "shadow_mfe_rr") if name in cols]
        optional_select = (", " + ", ".join(optional_cols)) if optional_cols else ""
        rows = conn.execute(
            f"""
            SELECT id, signal_utc, symbol, direction, confidence, entry, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3, block_reason,
                   raw_scores_json, shadow_outcome, resolved_utc, shadow_pnl_rr
                   {optional_select}
              FROM xau_shadow_journal
             WHERE block_reason LIKE 'fibo_mtf_planner:%'
               AND signal_utc >= datetime('now', ?)
             ORDER BY signal_utc DESC
             LIMIT ?
            """,
            (f"-{max(1, int(days))} days", int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out: list[dict] = []
    for row in rows:
        raw = _json_obj(row["raw_scores_json"])
        item = dict(row)
        item["raw"] = raw
        item["route"] = _parse_route(item, raw)
        item["tf"] = str(raw.get("tf_label") or raw.get("entry_tf") or "unknown")
        item["ratio_zone"] = str(raw.get("ratio_zone") or "unknown")
        item["signal_dt"] = _parse_dt(item.get("signal_utc"))
        item["session_key"] = _session_key(item["signal_dt"])
        item["has_real_anchor"] = _has_real_anchor(raw)
        item["mae_r"] = _mae_r(item, raw)
        out.append(item)
    return out


def summarize_group(rows: Iterable[dict]) -> dict:
    rows = list(rows or [])
    resolved = [r for r in rows if r.get("shadow_pnl_rr") is not None]
    rr_values = [float(r.get("shadow_pnl_rr") or 0.0) for r in resolved]
    wins = [v for v in rr_values if v > 0]
    maes = [float(r["mae_r"]) for r in rows if r.get("mae_r") is not None]
    dates = sorted({r["signal_dt"].date().isoformat() for r in rows if r.get("signal_dt")})
    first_dt = min((r["signal_dt"] for r in rows if r.get("signal_dt")), default=None)
    last_dt = max((r["signal_dt"] for r in rows if r.get("signal_dt")), default=None)
    anchor_rate = (sum(1 for r in rows if r.get("has_real_anchor")) / len(rows)) if rows else 0.0
    sessions = sorted({str(r.get("session_key") or "unknown") for r in rows if r.get("session_key")})
    return {
        "decisions": len(rows),
        "resolved": len(resolved),
        "first_signal_utc": first_dt.isoformat().replace("+00:00", "Z") if first_dt else None,
        "last_signal_utc": last_dt.isoformat().replace("+00:00", "Z") if last_dt else None,
        "calendar_days": len(dates),
        "sessions": len([s for s in sessions if s != "unknown"]),
        "winrate": round(len(wins) / len(rr_values), 4) if rr_values else 0.0,
        "expectancy_R": round(mean(rr_values), 4) if rr_values else 0.0,
        "max_mae_R": round(max(maes), 4) if maes else None,
        "real_anchor_rate": round(anchor_rate, 4),
    }


def summarize_rows(rows: Iterable[dict], *, ignore_calendar_days: bool | None = None, gate_mode: str = "opus_standard") -> dict:
    rows = list(rows or [])
    by_route: dict[str, list[dict]] = defaultdict(list)
    by_route_tf: dict[str, list[dict]] = defaultdict(list)
    by_route_zone: dict[str, list[dict]] = defaultdict(list)
    by_reclaim_setup: dict[str, list[dict]] = defaultdict(list)
    by_route_reclaim_setup: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        route = str(row.get("route") or "unknown")
        tf = str(row.get("tf") or "unknown")
        zone = str(row.get("ratio_zone") or "unknown")
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        reclaim_setup = str(raw.get("fibo_reclaim_setup") or row.get("fibo_reclaim_setup") or "unknown")
        by_route[route].append(row)
        by_route_tf[f"{route}|{tf}"].append(row)
        by_route_zone[f"{route}|{zone}"].append(row)
        by_reclaim_setup[reclaim_setup].append(row)
        by_route_reclaim_setup[f"{route}|{reclaim_setup}"].append(row)
    report = {
        "summary": summarize_group(rows),
        "by_route": {k: summarize_group(v) for k, v in sorted(by_route.items())},
        "by_route_tf": {k: summarize_group(v) for k, v in sorted(by_route_tf.items())},
        "by_route_ratio_zone": {k: summarize_group(v) for k, v in sorted(by_route_zone.items())},
        "by_reclaim_setup": {k: summarize_group(v) for k, v in sorted(by_reclaim_setup.items())},
        "by_route_reclaim_setup": {k: summarize_group(v) for k, v in sorted(by_route_reclaim_setup.items())},
    }
    if ignore_calendar_days is None:
        ignore_calendar_days = str(os.getenv("FIBO_MTF_MICRO_LIVE_IGNORE_CALENDAR_DAYS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if ignore_calendar_days and gate_mode == "opus_standard":
        gate_mode = "demo_accelerated"
    report["micro_live_probe_gate"] = evaluate_probe_gate(
        report["by_route"].get(PROMOTION_ROUTE, summarize_group([])),
        ignore_calendar_days=bool(ignore_calendar_days),
        gate_mode=gate_mode,
    )
    return report


def evaluate_probe_gate(probe: dict, *, ignore_calendar_days: bool = False, gate_mode: str = "opus_standard") -> dict:
    blockers: list[str] = []
    if int(probe.get("decisions") or 0) < MIN_PROBE_DECISIONS:
        blockers.append(f"probe_decisions<{MIN_PROBE_DECISIONS}")
    effective_min_calendar_days = 1 if ignore_calendar_days else MIN_CALENDAR_DAYS
    if int(probe.get("calendar_days") or 0) < effective_min_calendar_days:
        blockers.append(f"calendar_days<{effective_min_calendar_days}")
    if int(probe.get("sessions") or 0) < MIN_SESSIONS:
        blockers.append(f"sessions<{MIN_SESSIONS}")
    if int(probe.get("resolved") or 0) < MIN_PROBE_DECISIONS:
        blockers.append(f"resolved_probe_outcomes<{MIN_PROBE_DECISIONS}")
    if float(probe.get("winrate") or 0.0) < MIN_WINRATE:
        blockers.append(f"winrate<{MIN_WINRATE:.0%}")
    if float(probe.get("expectancy_R") or 0.0) < MIN_EXPECTANCY_R:
        blockers.append(f"expectancy_R<{MIN_EXPECTANCY_R:.2f}")
    max_mae = probe.get("max_mae_R")
    if max_mae is None:
        blockers.append("mae_R_missing")
    elif float(max_mae) > MAX_MAE_R:
        blockers.append(f"max_mae_R>{MAX_MAE_R:.1f}")
    if float(probe.get("real_anchor_rate") or 0.0) < MIN_REAL_ANCHOR_RATE:
        blockers.append(f"real_anchor_rate<{MIN_REAL_ANCHOR_RATE:.0%}")
    return {
        "route": PROMOTION_ROUTE,
        "gate_mode": str(gate_mode or "opus_standard"),
        "calendar_days_ignored": bool(ignore_calendar_days),
        "eligible_for_opus_micro_live_review": not blockers,
        "blockers": blockers,
        "requirements": {
            "min_probe_decisions": MIN_PROBE_DECISIONS,
            "min_calendar_days": effective_min_calendar_days,
            "min_sessions": MIN_SESSIONS,
            "min_winrate": MIN_WINRATE,
            "min_expectancy_R": MIN_EXPECTANCY_R,
            "max_mae_R": MAX_MAE_R,
            "min_real_anchor_rate": MIN_REAL_ANCHOR_RATE,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = load_rows(Path(args.db), days=args.days, limit=args.limit)
    report = summarize_rows(rows)
    report["db_path"] = str(Path(args.db))
    report["days"] = int(args.days)
    report["note"] = "Evidence only. Keep Fibo MTF planner shadow-only until Opus approves live promotion."
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Fibo MTF planner shadow report db={report['db_path']} days={report['days']}")
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
        print("by_route:")
        for route, row in report["by_route"].items():
            print(f"  {route}: {row}")
        print("micro_live_probe_gate:")
        print(json.dumps(report["micro_live_probe_gate"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

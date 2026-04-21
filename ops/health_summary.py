"""
ops/health_summary.py — Smallest useful live summary for the health gate
+ XAU suppression shadow pipeline.

Purpose: one CLI that answers, in a single glance:
  1. Is DB health healthy / warning / critical?
  2. Is cTrader auth healthy?
  3. How many health-gate warn/block decisions fired recently (XAU scope)?
  4. Is the XAU suppression shadow actually recording events?

Design invariants:
- Read-only. No writes to any DB, runtime state, or log.
- Never imports live-trading modules.
- stdlib only.
- Counts are XAU-scope for now — that is where gate blocks matter most,
  and the shadow JSONL is the source of truth. A later enhancement can
  add a symbol-agnostic gate events log without reworking this script.

Usage:
    python ops/health_summary.py
    python ops/health_summary.py --window-hours 24
    python ops/health_summary.py --format json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_HEALTH = "data/runtime/db_health_state.json"
DEFAULT_AUTH_STATE = "data/runtime/ctrader_token_state.json"
DEFAULT_SHADOW_GLOB = "data/runtime/xau_conf_suppression_shadow*.jsonl"
DEFAULT_WINDOW_HOURS = 24.0
STALE_WARN_MIN = 120.0  # match health_gate.py default


def _parse_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _age_min(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60.0


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Component readers
# ---------------------------------------------------------------------------


def read_db_health(path: str, now: datetime) -> dict:
    raw = _safe_load_json(path) or {}
    ts = _parse_utc(raw.get("timestamp_utc"))
    age = _age_min(ts, now)
    status = str(raw.get("overall_status") or "").lower() or "unknown"
    stale = age is not None and age > STALE_WARN_MIN
    issues = raw.get("issues") or []
    return {
        "path": path,
        "present": bool(raw),
        "status": status,
        "age_min": age,
        "stale": stale,
        "issues": issues if isinstance(issues, list) else [],
        "timestamp_utc": raw.get("timestamp_utc"),
    }


def read_auth_health(path: str, now: datetime) -> dict:
    raw = _safe_load_json(path) or {}
    saved = _parse_utc(raw.get("saved_utc")) or _parse_utc(raw.get("last_refresh_utc"))
    age = _age_min(saved, now)
    consecutive_fail = int(raw.get("consecutive_failures") or 0)
    stale = age is not None and age > STALE_WARN_MIN

    # Coarse classification mirroring infra/auth_health (without importing it)
    if not raw:
        status = "unknown"
    elif consecutive_fail >= 3:
        status = "critical"
    elif consecutive_fail >= 1 or stale:
        status = "warning"
    else:
        status = "healthy"

    return {
        "path": path,
        "present": bool(raw),
        "status": status,
        "age_min": age,
        "stale": stale,
        "consecutive_failures": consecutive_fail,
        "refresh_count": int(raw.get("refresh_count") or 0),
        "last_refresh_utc": raw.get("last_refresh_utc"),
        "saved_utc": raw.get("saved_utc"),
    }


def read_shadow_activity(glob_pattern: str, window_h: float, now: datetime) -> dict:
    """Scan shadow JSONL(s): file stats + gate-related decision counts."""
    files = sorted(glob.glob(glob_pattern))
    total_events = 0
    window_events = 0
    gate_warn = 0
    gate_block = 0
    rejected_total = 0
    executed_total = 0
    arrived_total = 0
    last_ts: Optional[datetime] = None
    first_ts_in_window: Optional[datetime] = None
    total_bytes = 0
    window_cutoff = now - timedelta(hours=window_h)

    for fp in files:
        p = Path(fp)
        try:
            total_bytes += p.stat().st_size
        except OSError:
            pass
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total_events += 1
                    ts = _parse_utc(rec.get("ts_utc"))
                    if ts is not None:
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                        if ts >= window_cutoff:
                            window_events += 1
                            if first_ts_in_window is None or ts < first_ts_in_window:
                                first_ts_in_window = ts
                            decision = str(rec.get("decision") or "").lower()
                            reason = str(rec.get("reason") or "").lower()
                            if decision == "health_warn":
                                gate_warn += 1
                            if decision == "rejected":
                                rejected_total += 1
                                if reason.startswith("health_gate_blocked"):
                                    gate_block += 1
                            if decision == "executed":
                                executed_total += 1
                            if decision == "arrived":
                                arrived_total += 1
        except OSError:
            continue

    return {
        "files_matched": len(files),
        "total_events": total_events,
        "total_bytes": total_bytes,
        "last_event_utc": last_ts.isoformat() if last_ts else None,
        "last_event_age_min": _age_min(last_ts, now),
        "window_hours": window_h,
        "window_events": window_events,
        "window_arrived": arrived_total,
        "window_executed": executed_total,
        "window_rejected": rejected_total,
        "window_gate_warn": gate_warn,
        "window_gate_block": gate_block,
        "active": window_events > 0,
    }


# ---------------------------------------------------------------------------
# Overall classification
# ---------------------------------------------------------------------------


def _badge(status: str) -> str:
    s = (status or "").lower()
    return {
        "healthy": "OK ",
        "warning": "WARN",
        "critical": "CRIT",
        "stale": "STAL",
        "unknown": "??? ",
    }.get(s, "??? ")


def classify_overall(db: dict, auth: dict, shadow: dict) -> str:
    for comp in (db, auth):
        if comp["status"] == "critical":
            return "critical"
    for comp in (db, auth):
        if comp["status"] == "warning" or comp.get("stale"):
            return "warning"
    if not shadow["active"] and shadow["files_matched"] == 0:
        return "warning"
    return "healthy"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_text(db: dict, auth: dict, shadow: dict, overall: str, now: datetime) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"Dexter Pro health summary  [{overall.upper()}]  {now.isoformat(timespec='seconds')}")
    lines.append("=" * 70)

    age_s = lambda a: "n/a" if a is None else f"{a:6.1f} min"

    lines.append(f"  DB health         [{_badge(db['status'])}] status={db['status']:<8s}  "
                 f"age={age_s(db['age_min'])}  stale={db['stale']}")
    if db["issues"]:
        for i in db["issues"][:4]:
            lines.append(f"      issue: {i}")

    lines.append(f"  cTrader auth      [{_badge(auth['status'])}] status={auth['status']:<8s}  "
                 f"age={age_s(auth['age_min'])}  consecutive_fails={auth['consecutive_failures']}  "
                 f"refreshes={auth['refresh_count']}")

    sh_label = "OK  " if shadow["active"] else ("WARN" if shadow["files_matched"] else "MISS")
    lines.append(f"  XAU shadow log    [{sh_label}] files={shadow['files_matched']}  "
                 f"events={shadow['total_events']}  bytes={shadow['total_bytes']}")
    lines.append(f"      last_event_age   : {age_s(shadow['last_event_age_min'])}")
    lines.append(f"      window ({int(shadow['window_hours'])}h) activity :")
    lines.append(f"          arrived={shadow['window_arrived']}  executed={shadow['window_executed']}  "
                 f"rejected={shadow['window_rejected']}")
    lines.append(f"          gate_warn={shadow['window_gate_warn']}  "
                 f"gate_block={shadow['window_gate_block']}")

    if not shadow["active"]:
        if shadow["files_matched"] == 0:
            lines.append("      NOTE: no shadow JSONL files on disk yet.")
        else:
            lines.append("      NOTE: files exist but no events in window — XAU may be idle "
                         "or shadow flag is off.")

    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Health + suppression shadow summary.")
    p.add_argument("--db-health", default=DEFAULT_DB_HEALTH)
    p.add_argument("--auth-state", default=DEFAULT_AUTH_STATE)
    p.add_argument("--shadow-glob", default=DEFAULT_SHADOW_GLOB)
    p.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    p.add_argument("--format", choices=("text", "json"), default="text")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)

    db = read_db_health(args.db_health, now)
    auth = read_auth_health(args.auth_state, now)
    shadow = read_shadow_activity(args.shadow_glob, float(args.window_hours), now)
    overall = classify_overall(db, auth, shadow)

    if args.format == "json":
        out = {
            "generated_utc": now.isoformat(),
            "overall": overall,
            "db_health": db,
            "auth_health": auth,
            "shadow_activity": shadow,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_text(db, auth, shadow, overall, now))

    # Exit code: 0 healthy/warning, 2 critical. Makes it usable as a cron
    # status check without gating on warnings.
    return 2 if overall == "critical" else 0


if __name__ == "__main__":
    sys.exit(main())

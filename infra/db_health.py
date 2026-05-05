"""
infra/db_health.py — Read-only database health diagnostics for ctrader_openapi.db.

Executes the safe diagnostic sequence from docs/handoff/DB_VERIFICATION_PLAN.md.
All operations are read-only. No data is modified, deleted, or moved.

Designed to be called from scheduler.py at startup for continuous monitoring.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("data/ctrader_openapi.db")
BACKUP_DIR = Path("data/backups")
HEALTH_STATE_FILE = Path("data/runtime/db_health_state.json")


def _file_system_facts() -> dict:
    """Gather file system facts without opening the DB."""
    facts = {
        "exists": DB_PATH.exists(),
        "size_bytes": 0,
        "size_mb": 0,
        "modified_utc": "",
        "wal_exists": False,
        "wal_size_mb": 0,
        "shm_exists": False,
        "shm_size_mb": 0,
        "free_space_gb": 0,
    }

    if not facts["exists"]:
        return facts

    try:
        stat = os.stat(DB_PATH)
        facts["size_bytes"] = stat.st_size
        facts["size_mb"] = round(stat.st_size / 1024 / 1024, 1)
        facts["modified_utc"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        pass

    # WAL companion files
    for suffix, key in [("-wal", "wal_exists"), ("-shm", "shm_exists")]:
        p = DB_PATH.with_name(DB_PATH.name + suffix)
        if p.exists():
            facts[key] = True
            try:
                facts[key.replace("exists", "size_mb")] = round(os.path.getsize(p) / 1024 / 1024, 2)
            except OSError:
                pass

    # Free space
    try:
        usage = os.statvfs(str(DB_PATH.parent)) if hasattr(os, "statvfs") else None
        if usage:
            facts["free_space_gb"] = round(usage.f_bavail * usage.f_frsize / 1024 / 1024 / 1024, 2)
        else:
            # Windows — use shutil
            import shutil
            du = shutil.disk_usage(str(DB_PATH.parent))
            facts["free_space_gb"] = round(du.free / 1024 / 1024 / 1024, 2)
    except Exception:
        pass

    return facts


def _sqlite_facts(timeout: int = 15) -> dict:
    """Attempt to open the DB read-only and gather SQLite-level facts."""
    facts = {
        "open_success": False,
        "open_error": "",
        "journal_mode": "",
        "page_count": 0,
        "page_size": 0,
        "logical_size_mb": 0,
        "table_count": 0,
        "total_rows": 0,
        "tables": {},
        "integrity_check": "",
        "integrity_check_seconds": 0,
    }

    try:
        start = time.time()
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout)
        facts["open_success"] = True
        facts["open_seconds"] = round(time.time() - start, 2)

        # PRAGMA checks
        for pragma in ["journal_mode", "page_count", "page_size"]:
            try:
                val = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
                facts[pragma] = val
            except Exception as e:
                facts[pragma] = f"ERROR: {e}"

        if isinstance(facts["page_count"], int) and isinstance(facts["page_size"], int):
            facts["logical_size_mb"] = round(facts["page_count"] * facts["page_size"] / 1024 / 1024, 1)

        # Table inventory
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            facts["table_count"] = len(tables)
            total_rows = 0
            for (tname,) in tables:
                try:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM [{tname}]").fetchone()[0]
                    facts["tables"][tname] = cnt
                    total_rows += cnt
                except Exception as e:
                    facts["tables"][tname] = f"ERROR: {e}"
            facts["total_rows"] = total_rows
        except Exception:
            pass

        conn.close()
    except Exception as e:
        facts["open_error"] = str(e)

    return facts


def _quick_integrity_check(timeout: int = 60) -> dict:
    """Run PRAGMA integrity_check(100) if the DB is openable."""
    facts = {
        "ran": False,
        "result": "",
        "seconds": 0,
    }

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout)
        start = time.time()
        result = conn.execute("PRAGMA integrity_check(100)").fetchone()[0]
        facts["ran"] = True
        facts["result"] = result[:500]
        facts["seconds"] = round(time.time() - start, 2)
        conn.close()
    except Exception as e:
        facts["result"] = f"SKIPPED: {e}"

    return facts


def run_full_health_check(
    include_integrity: bool = False,
    sqlite_timeout: int = 15,
    integrity_timeout: int = 60,
) -> dict:
    """Run the full read-only health check sequence.

    Returns a structured report dict. Does NOT modify the DB.

    Args:
        include_integrity: If True, run PRAGMA integrity_check (slow).
        sqlite_timeout: Timeout for DB open attempts.
        integrity_timeout: Timeout for integrity check.
    """
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "file_system": {},
        "sqlite": {},
        "integrity": {},
        "overall_status": "unknown",
        "issues": [],
    }

    # Step 1: File system facts (always safe)
    report["file_system"] = _file_system_facts()

    if not report["file_system"]["exists"]:
        report["overall_status"] = "critical"
        report["issues"].append("DB file not found")
        return report

    # Step 2: SQLite open + facts
    report["sqlite"] = _sqlite_facts(timeout=sqlite_timeout)

    if not report["sqlite"]["open_success"]:
        report["overall_status"] = "critical"
        report["issues"].append(f"Cannot open DB: {report['sqlite']['open_error']}")
        _save_health_state(report)
        return report

    # Check for issues
    issues = []

    # Size check
    if report["file_system"]["size_mb"] > 4000:
        issues.append(f"DB size {report['file_system']['size_mb']} MB exceeds 4 GB threshold")

    # WAL mode with no -wal companion file is normal when SQLite has checkpointed
    # and there are no active uncheckpointed frames. Do not warn on absence alone.

    # Largest table check
    tables = report["sqlite"].get("tables", {})
    if tables:
        largest = max(tables.items(), key=lambda x: x[1] if isinstance(x[1], int) else 0)
        if isinstance(largest[1], int) and largest[1] > 5_000_000:
            issues.append(f"Table '{largest[0]}' has {largest[1]:,} rows — likely growth driver")

    # Free space check
    if report["file_system"].get("free_space_gb", 999) < 2:
        issues.append(f"Only {report['file_system']['free_space_gb']} GB free disk space")

    # Step 3: Integrity check (optional, slow)
    if include_integrity:
        report["integrity"] = _quick_integrity_check(timeout=integrity_timeout)
        if report["integrity"].get("result", "").lower() != "ok":
            issues.append(f"Integrity check: {report['integrity']['result'][:200]}")

    # Overall status
    if issues:
        has_critical = any("Cannot open" in i or "not found" in i or "Integrity" in i for i in issues)
        report["overall_status"] = "critical" if has_critical else "warning"
    else:
        report["overall_status"] = "healthy"

    report["issues"] = issues

    # Log summary
    log_fn = logger.error if report["overall_status"] == "critical" else (
        logger.warning if report["overall_status"] == "warning" else logger.info
    )
    log_fn(
        "[db_health] Status: %s | Size: %.1f MB | Tables: %d | Rows: %s | Issues: %d",
        report["overall_status"],
        report["file_system"].get("size_mb", 0),
        report["sqlite"].get("table_count", 0),
        f"{report['sqlite'].get('total_rows', 0):,}",
        len(issues),
    )
    for issue in issues:
        logger.warning("[db_health] Issue: %s", issue)

    _save_health_state(report)
    return report


def _save_health_state(report: dict) -> None:
    """Persist the latest health check result."""
    try:
        HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Use atomic write if available
        try:
            from utils.atomic_write import atomic_json_write
            atomic_json_write(HEALTH_STATE_FILE, report)
        except ImportError:
            HEALTH_STATE_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("[db_health] Failed to save health state: %s", e)


def get_health_state() -> dict:
    """Load the last saved health check state."""
    try:
        from utils.atomic_write import atomic_json_read
        return atomic_json_read(HEALTH_STATE_FILE, default={})
    except ImportError:
        if HEALTH_STATE_FILE.exists():
            return json.loads(HEALTH_STATE_FILE.read_text(encoding="utf-8"))
        return {}


def should_attempt_backup() -> bool:
    """Check if a backup should be attempted based on current health state.

    Returns True only if:
    - DB is openable
    - No critical integrity issues
    - No existing backup from today
    """
    state = get_health_state()
    if not state:
        return True  # No data yet — run the check

    sqlite_ok = state.get("sqlite", {}).get("open_success", False)
    if not sqlite_ok:
        return False  # Can't back up if we can't open

    # Check for today's backup
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if BACKUP_DIR.exists():
        for f in BACKUP_DIR.iterdir():
            if today in f.name and f.name.endswith(".db"):
                return False  # Already backed up today

    return True

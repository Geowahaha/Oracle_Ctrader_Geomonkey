#!/usr/bin/env python3
import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TABLES = [
    ("ctrader_depth_quotes", "event_utc"),
    ("ctrader_spot_ticks", "event_utc"),
    ("ctrader_capture_runs", "created_utc"),
]


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def ro_conn(db: Path):
    uri = f"file:{db.resolve()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rw_conn(db: Path):
    conn = sqlite3.connect(str(db.resolve()))
    conn.row_factory = sqlite3.Row
    return conn


def count_old(cur, table: str, time_col: str, days: int) -> int:
    return int(cur.execute(
        f"SELECT COUNT(*) FROM {qident(table)} WHERE datetime({qident(time_col)}) < datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchone()[0])


def count_all(cur, table: str) -> int:
    return int(cur.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0])


def ensure_archive_table(src_cur, arc_cur, table: str):
    exists = arc_cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if exists:
        return
    schema_row = src_cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not schema_row or not schema_row[0]:
        raise RuntimeError(f"missing schema for table {table}")
    arc_cur.execute(schema_row[0])


def dry_run(db: Path, days: int):
    conn = ro_conn(db)
    cur = conn.cursor()
    out = []
    for table, time_col in TABLES:
        total = count_all(cur, table)
        old = count_old(cur, table, time_col, days)
        out.append({
            "table": table,
            "time_col": time_col,
            "total_rows": total,
            "rows_older_than_cutoff": old,
            "pct_older_than_cutoff": round((old / total), 4) if total else 0.0,
        })
    conn.close()
    return out


def apply_retention(db: Path, archive_db: Path, days: int, confirm_stop_service: bool, batch_size: int):
    if int(days) < 1:
        raise SystemExit("Refusing retention with --days < 1")
    if int(batch_size) < 1000:
        raise SystemExit("Refusing --batch-size < 1000")
    if not confirm_stop_service:
        raise SystemExit("Refusing --apply without --confirm-stop-service")
    backup = db.with_name(db.name + f".backup-before-retention-{iso_now()}")
    shutil.copy2(db, backup)
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    wal_backup = None
    shm_backup = None
    if wal.exists():
        wal_backup = wal.with_name(wal.name + f".backup-before-retention-{iso_now()}")
        shutil.copy2(wal, wal_backup)
    if shm.exists():
        shm_backup = shm.with_name(shm.name + f".backup-before-retention-{iso_now()}")
        shutil.copy2(shm, shm_backup)

    src = rw_conn(db)
    arc = rw_conn(archive_db)
    src_cur = src.cursor()
    arc_cur = arc.cursor()
    arc_cur.execute("CREATE TABLE IF NOT EXISTS retention_runs (run_utc TEXT, source_db TEXT, cutoff_days INTEGER, table_name TEXT, moved_rows INTEGER)")

    moved = []
    for table, time_col in TABLES:
        ensure_archive_table(src_cur, arc_cur, table)
        moved_rows = 0
        while True:
            rows = src_cur.execute(
                f"SELECT rowid AS __rowid__, * FROM {qident(table)} WHERE datetime({qident(time_col)}) < datetime('now', ?) ORDER BY rowid LIMIT ?",
                (f"-{int(days)} days", int(batch_size)),
            ).fetchall()
            if not rows:
                break
            cols = [d[0] for d in src_cur.description if d[0] != '__rowid__']
            col_sql = ", ".join(qident(c) for c in cols)
            placeholders = ", ".join(["?"] * len(cols))
            arc_cur.executemany(
                f"INSERT INTO {qident(table)} ({col_sql}) VALUES ({placeholders})",
                [tuple(row[c] for c in cols) for row in rows],
            )
            rowids = [int(row['__rowid__']) for row in rows]
            src_cur.executemany(
                f"DELETE FROM {qident(table)} WHERE rowid=?",
                [(rid,) for rid in rowids],
            )
            batch_moved = len(rowids)
            moved_rows += batch_moved
            arc.commit()
            src.commit()
        moved.append({"table": table, "moved_rows": moved_rows})
        arc_cur.execute(
            "INSERT INTO retention_runs(run_utc, source_db, cutoff_days, table_name, moved_rows) VALUES (?, ?, ?, ?, ?)",
            (iso_now(), str(db), int(days), table, moved_rows),
        )
        arc.commit()

    src_cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src.commit()
    src_cur.execute("VACUUM")
    src.commit()
    src.close()
    arc.close()

    return {
        "backup_db": str(backup),
        "backup_wal": str(wal_backup) if wal_backup else None,
        "backup_shm": str(shm_backup) if shm_backup else None,
        "archive_db": str(archive_db),
        "moved": moved,
    }


def main():
    ap = argparse.ArgumentParser(description="Dry-run/apply retention for large ctrader history tables")
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--archive-db", default="data/archive/ctrader_openapi_archive.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=50000)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm-stop-service", action="store_true")
    args = ap.parse_args()

    if int(args.days) < 1:
        raise SystemExit("--days must be >= 1")
    if int(args.batch_size) < 1000:
        raise SystemExit("--batch-size must be >= 1000")

    db = Path(args.db)
    archive_db = Path(args.archive_db)
    archive_db.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "db": str(db),
        "archive_db": str(archive_db),
        "days": int(args.days),
        "batch_size": int(args.batch_size),
        "dry_run": not bool(args.apply),
        "tables": dry_run(db, args.days),
    }
    if args.apply:
        result["apply"] = apply_retention(db, archive_db, args.days, args.confirm_stop_service, args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

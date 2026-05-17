# DB Verification Plan — ctrader_openapi.db

> Created: 2026-04-20
> Owner: Hermes
> Status: IN PROGRESS — read-only diagnostics only
> Constraint: NO destructive operations until backup is verified

---

## Confirmed Facts

These are verified by direct observation during the 2026-04-20 diagnostic.

| Fact | Evidence |
|------|----------|
| File exists at `data/ctrader_openapi.db` | os.path.exists = True |
| File size: 5,382 MB (5.26 GB) | os.stat.st_size |
| Read-only open succeeds | sqlite3.connect with mode=ro returned connection |
| Journal mode reports "wal" | PRAGMA journal_mode returned "wal" |
| 14 tables exist | SELECT from sqlite_master |
| Total rows: ~8.9 million | Sum of row counts across tables |
| Largest table: ctrader_depth_quotes (8,002,293 rows) | Direct COUNT query |
| Other 11 DB files in data/ total ~142 MB, healthy | File system check |
| Python sqlite3 operations time out at 300s | Script timeout observed |
| No -wal companion file exists | os.path.exists for ctrader_openapi.db-wal = False |
| No -shm companion file exists | os.path.exists for ctrader_openapi.db-shm = False |
| No -journal file exists | os.path.exists for ctrader_openapi.db-journal = False |

### Table Inventory

| Table | Rows | Notes |
|-------|------|-------|
| ctrader_depth_quotes | 8,002,293 | **88% of all rows.** Likely growth driver. |
| ctrader_spot_ticks | 852,561 | Tick data |
| ctrader_deals | 3,964 | Trade records |
| execution_journal | 4,743 | Execution log |
| xau_family_canary_gate_journal | 4,721 | Canary gate decisions |
| ctrader_capture_runs | 12,781 | Capture sessions |
| xau_shadow_journal | 786 | Shadow trades |
| ctrader_positions | 1,511 | Position records |
| ctrader_orders | 805 | Order records |
| stream_status | 1 | Stream metadata |
| stream_trendbars | 1 | Stream metadata |
| sqlite_sequence | 6 | Autoincrement state |
| stream_executions | 0 | Empty |
| stream_margin | 0 | Empty |

---

## Likely Issues

These are strongly suggested by evidence but not yet fully confirmed.

### L1: WAL files missing despite WAL mode reported

**Evidence:** PRAGMA journal_mode returned "wal" but no -wal or -shm files exist on disk.

**Possible explanations:**
1. WAL was checkpointed and files were deleted (normal, but unusual for a 5.4 GB active DB)
2. DB was opened in a mode that reported WAL but the files are stored elsewhere
3. Files were manually deleted while DB was closed
4. Filesystem layer (WSL/Windows mount) is not showing the files

**Risk if WAL files were deleted while DB was open:**
- Uncommitted transactions may be lost
- DB may be in an inconsistent state
- This would explain the extreme query latency

**Next diagnostic:** Open DB in RW mode, check if WAL files are created. If they appear, the previous state was a clean checkpoint. If not, there may be a filesystem issue.

### L2: Extreme query latency

**Evidence:** Python sqlite3 operations time out at 300 seconds. Read-only open succeeds but queries hang.

**Possible explanations:**
1. 5.4 GB file on a slow mount (WSL accessing Windows NTFS via /mnt/d/)
2. Lock contention — another process may hold a lock on the DB
3. Missing WAL files causing SQLite to use rollback journal mode internally
4. Filesystem cache pressure from the large file

**Risk:** If live trading is writing to this DB, the latency means trade journal entries may be buffered or lost.

**Next diagnostic:** Check if any other process has the DB open. Check disk I/O metrics.

### L3: ctrader_depth_quotes growth

**Evidence:** 8 million rows in a single table, 88% of all data.

**Possible explanations:**
1. Depth quotes are accumulated without retention policy
2. This table is not needed for live trading decisions
3. Archival of this table alone would reduce DB size by ~80%

**Risk:** Unbounded growth will continue. Even if the DB is healthy now, it will degrade.

**Next diagnostic:** Check what queries read from this table. Determine if it's on the live trading path.

---

## Unverified Assumptions

These are assumptions in the current plan that need verification before relying on them.

| Assumption | Status | How to Verify |
|------------|--------|---------------|
| DB is corrupted | **UNVERIFIED** | Read-only open works. Need quick_check. |
| Live trading writes to this DB | UNVERIFIED | Search code for sqlite3.connect to this path |
| No other process has the DB open | UNVERIFIED | Check for lock files, running processes |
| WAL mode was working before | UNVERIFIED | Check if -wal file existed historically |
| The DB can be backed up successfully | UNVERIFIED | Attempt sqlite3 .backup command |
| VACUUM would help | UNVERIFIED | Need page_count vs actual file size analysis |
| ctrader_depth_quotes can be archived safely | UNVERIFIED | Check if any live query reads from it |
| RW operations will hang | UNVERIFIED | Only tested with 10s timeout — may need longer |

---

## Safe Read-Only Diagnostic Sequence

**Rule: Every step below is read-only. No data is modified, deleted, or moved.**

### Step 1: File system analysis (COMPLETE)

```
Check: file size, modification time, companion files
Result: See Confirmed Facts above
Status: DONE
```

### Step 2: Process lock check

```
Check: Is any other process holding the DB open?
Command (from WSL):
  lsof data/ctrader_openapi.db 2>/dev/null
  fuser data/ctrader_openapi.db 2>/dev/null
Risk: None (read-only)
```

### Step 3: Extended read-only diagnostic

```
Open DB in mode=ro with 60s timeout.
Run:
  PRAGMA journal_mode          → confirm WAL
  PRAGMA page_count            → confirm page count
  PRAGMA page_size             → confirm page size
  PRAGMA integrity_check(1)    → quick integrity (first 100 pages)
  PRAGMA foreign_key_check     → FK violations
  SELECT COUNT(*) per table    → confirm row counts
Risk: May time out. If it does, that's a diagnostic result (confirms latency issue).
```

### Step 4: Code path analysis

```
Search code for all sqlite3.connect calls that target ctrader_openapi.db.
Identify: which modules read/write, how often, what timeout they use.
Command (from WSL):
  grep -rn "ctrader_openapi" --include="*.py" | grep -i "connect\|db_path\|database"
Risk: None (read-only search)
```

### Step 5: WAL file recreation test

```
Open DB in RW mode for exactly 10 seconds.
Check if -wal and -shm files appear.
If they appear: previous state was a clean checkpoint (good).
If they don't: filesystem issue (needs investigation).
Close connection immediately after check.
Risk: LOW — brief RW open, no writes performed.
```

### Step 6: Backup attempt

```
Attempt: sqlite3 .backup command via Python API.
Method: Use sqlite3.Connection.backup() target method.
Target: data/backups/ctrader_openapi_YYYYMMDD.db
Timeout: 600s (10 minutes — the file is 5.4 GB)
Risk: None — .backup is read-only on source.
```

---

## Backup-First Procedure

**Rule: NO repair, VACUUM, or archival happens until a verified backup exists.**

### Step B1: Create backup directory

```
mkdir -p data/backups
Risk: None
```

### Step B2: Attempt Python API backup

```python
import sqlite3
from datetime import datetime

src = sqlite3.connect("file:data/ctrader_openapi.db?mode=ro", uri=True, timeout=60)
dst_path = f"data/backups/ctrader_openapi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
dst = sqlite3.connect(dst_path)

src.backup(dst)  # This is the safe, read-only-on-source method

dst.close()
src.close()
```

### Step B3: Verify backup integrity

```python
bak = sqlite3.connect(dst_path, timeout=30)
result = bak.execute("PRAGMA integrity_check").fetchone()[0]
# Should return "ok" if backup is clean
bak.close()
```

### Step B4: If Python backup fails, try file copy

```bash
# From WSL — direct file copy (less safe but may work)
cp data/ctrader_openapi.db data/backups/ctrader_openapi_<timestamp>.db
# Also copy WAL if it exists
cp data/ctrader_openapi.db-wal data/backups/ 2>/dev/null
cp data/ctrader_openapi.db-shm data/backups/ 2>/dev/null
```

### Step B5: If file copy fails, try from Windows

```powershell
# From PowerShell — may have better access to NTFS
Copy-Item D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed\data\ctrader_openapi.db D:\dexter_pro_v3_fixed\dexter_pro_v3_fixed\data\backups\
```

---

## Recovery Decision Tree

```
START
  │
  ├─ Can read-only open succeed?
  │   ├─ NO → DB is unreadable. Try file copy from Windows.
  │   │       If file copy fails → DB may need professional recovery.
  │   │       STOP. Do not attempt sqlite3 operations.
  │   │
  │   └─ YES → Continue
  │
  ├─ Can integrity_check complete?
  │   ├─ TIMEOUT (>60s) → Latency issue confirmed. Try backup anyway.
  │   │
  │   ├─ FAILS (returns errors) → DB has corruption.
  │   │   ├─ Can .backup complete? → YES: backup is the recovery. Use it.
  │   │   └─ Can .backup complete? → NO: try .recover command.
  │   │
  │   └─ OK → DB is structurally sound. Latency is likely size/IO related.
  │
  ├─ Can .backup complete?
  │   ├─ YES → Backup is verified. Safe to proceed with:
  │   │   ├─ VACUUM on original (after backup verified)
  │   │   ├─ Archival of old records
  │   │   └─ WAL re-enable if needed
  │   │
  │   └─ NO → Try file copy. If that fails, do NOT proceed.
  │
  └─ After backup verified:
      ├─ Is ctrader_depth_quotes needed for live trading?
      │   ├─ NO → Archive all rows > 30 days. Expected recovery: ~4 GB.
      │   └─ YES → Archive all rows > 7 days. Expected recovery: ~2 GB.
      │
      ├─ Run VACUUM (requires exclusive lock — do during market close)
      │
      └─ Enable WAL mode if not already active
          ├─ PRAGMA journal_mode=WAL
          ├─ PRAGMA synchronous=NORMAL
          └─ PRAGMA cache_size=-64000
```

---

## What Must Be Confirmed Before Any Repair/Archive Action

**ALL of the following must be TRUE before any mutation is allowed:**

| # | Confirmation Required | How | Status |
|---|----------------------|-----|--------|
| 1 | A verified backup exists | .backup completes, integrity_check returns "ok" | NOT CONFIRMED |
| 2 | Backup is stored outside the data/ directory | Copy to a different drive or cloud | NOT CONFIRMED |
| 3 | No live trading is writing to the DB | Stop scheduler or confirm dry-run mode | NOT CONFIRMED |
| 4 | The DB can complete a PRAGMA quick_check | Read-only diagnostic | NOT CONFIRMED |
| 5 | The latency source is identified | Process check + I/O analysis | NOT CONFIRMED |
| 6 | ctrader_depth_quotes archival safety is confirmed | Code search shows no live queries | NOT CONFIRMED |
| 7 | VACUUM is scheduled during market close | Sunday 00:00-06:00 UTC window | NOT CONFIRMED |
| 8 | Rollback plan exists | Backup can be restored if VACUUM fails | NOT CONFIRMED |

**Current status: 0/8 confirmed. DO NOT proceed with any repair or archival.**

---

## Next Actions (Hermes)

1. Run Step 2 (process lock check) — quick, no risk
2. Run Step 4 (code path analysis) — find all DB consumers
3. Attempt Step B1+B2 (create backup dir + Python backup) — the critical test
4. If backup succeeds: mark confirmation #1 as TRUE, proceed to Step 3 (integrity check)
5. If backup fails: try Step B4 (file copy) and Step B5 (Windows copy)
6. Report results to handoff. Update ACTION_BACKLOG.md status.

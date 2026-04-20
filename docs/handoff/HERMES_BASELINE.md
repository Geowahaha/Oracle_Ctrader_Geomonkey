# Hermes — Baseline & Ownership

> Last updated: 2026-04-20
> Role: Architectural hardening, production reliability, observability
> Complements: Opus (execution truth, live intelligence)

---

## Hermes Ownership Areas

```
REFACTOR MAP          → How to restructure without breaking live trading
CONFIG HARDENING      → Prevent silent misconfiguration
LOGGING/OBSERVABILITY → Make everything visible and traceable
SCHEDULER SPLIT       → Decompose the 13.7K-line god class
DB ARCHIVAL           → Keep databases healthy and bounded
THREAD MODEL          → Prevent race conditions and state corruption
PRODUCTION OPS        → Make the system safe to operate and debug
```

**Hermes does NOT own:**
- Evaluating whether trading logic is profitable
- Changing confidence thresholds or risk parameters
- Deciding which strategies to run
- Modifying execution logic
- Classifying modules as fake-smart (reports to Opus)

---

## Architectural Hardening Priorities

### P0 — Before anything else

1. **ctrader_openapi.db health check** — 5.4 GB, I/O errors observed. Verify reads AND writes work. If corrupted, recover before any other work.

2. **Atomic state writes** — All runtime JSON files (trading_manager_state.json, etc.) must use write-to-.tmp-then-rename pattern. Prevents corruption on crash.

3. **r_peak persistence verification** — Startup check that r_peak exists in trading_manager_state.json. If missing, reconstruct from trade history.

4. **TRAILING_STRUCT enforcement telemetry** — Log trailing state for every open position. Alert when intended trailing differs from actual.

5. **Token/auth health monitoring** — Log token expiry, refresh success/failure. Alert on refresh failure or <5min to expiry.

### P1 — This week

6. **Opportunity suppression tracker** — Log every gate rejection. Build rejection funnel. Enable replay analysis: "would rejected signals have won?"

7. **Gate ROI attribution** — Record every gate decision + trade outcome. Build per-gate win rate and PnL attribution.

8. **Startup config assertions** — Detect dangerous combinations: autotrade+dry_run, trailing without position manager, suppression risk from stacked high thresholds.

9. **WAL mode on ctrader_openapi.db** — Enable Write-Ahead Logging for concurrent read/write safety.

### P2 — Week 2-4

10. **Structured logging** — Replace ad-hoc logger.info with structlog JSON output. Add signal correlation IDs.

11. **Scheduler heartbeat** — Detect hung main loop. Alert if no heartbeat in 60s.

12. **DB archival** — Backup strategy first, then retention policy. Never archive during market hours.

13. **Scheduler split Phase 1** — Extract report generators (~3,000 lines) from scheduler.py.

---

## Scheduler Split Direction

```
CURRENT:  scheduler.py — 13,679 lines, 1 class, 120+ methods

TARGET:   scheduler/
            __init__.py      ~2,000 lines (loop, orchestration, scan loops)
            routing.py       ~3,000 lines (signal dispatch engine)
            state.py         ~500 lines  (shared state with accessors)
            guards/          ~2,500 lines (all guard logic)
            families/        ~4,000 lines (all family builders)
            reports/         ~3,000 lines (all report generators)
            scan_loops.py    ~1,000 lines (scan orchestration)

SEQUENCE:
  Phase 1: reports/       (lowest risk — read-only, no state mutation)
  Phase 2: families/      (medium risk — modify signals, interact with state)
  Phase 3: guards/        (medium risk — called during routing hot path)
  Phase 4: routing.py     (highest risk — execution hot path)
  Phase 5: scan_loops.py  (low risk — orchestration only)

CONSTRAINT: Do not extract methods that route through modules
            Opus hasn't validated as causal yet.
```

---

## Config Hardening Direction

```
CURRENT:  1,903 attributes, 1,864 unique env vars
          validate() checks only 3 of them
          923 getattr(config, "X") calls vs 88 direct config.X
          No type checking, no range validation, no cross-field validation

TARGET:   config/
            __init__.py       (backward-compatible facade)
            schema.py         (Pydantic models with types + ranges)
            validators.py     (cross-field + startup assertions)
            groups/           (ai.py, ctrader.py, mt5.py, families.py, etc.)

MIGRATION:
  Step 1: Add schema.py as shadow validator (no existing code changes)
  Step 2: Add startup validation — log warnings, don't fail
  Step 3: Add startup assertions for dangerous combinations
  Step 4: Migrate getattr() calls to direct access, one module at a time
  Step 5: Grouped access: cfg.ctrader.risk_usd_per_trade
```

---

## Observability Direction

```
STRUCTURED LOGGING:
  - structlog with JSON output
  - Signal correlation IDs (signal_run_id) propagated through entire lifecycle
  - Consistent event taxonomy: signal.*, gate.*, execution.*, position.*, learning.*, scheduler.*

KEY TELEMETRY (Opus-specific):
  - Opportunity suppression tracker (per-gate rejection rates + replay)
  - Gate ROI attribution (per-gate win rate + PnL)
  - TRAILING_STRUCT enforcement gap detection
  - r_peak persistence verification at startup
  - Token/auth refresh health

KEY METRICS:
  - signals_generated_total, signals_executed_total
  - signal_rejection_rate (per gate)
  - execution_success_rate
  - scheduler_loop_heartbeat
  - daily_pnl, active_positions
  - ctrader_db_size_bytes
  - neural_model_last_train timestamp

ALERTS:
  P0: r_peak.missing, trailing.gap_detected, auth.refresh_failure, db.io_error
  P1: scheduler.hung, db.size_critical, opportunity_suppression.extreme
  P2: gate.decision_summary (daily), auth.state_summary (daily)
```

---

## DB Archival Direction

```
CURRENT:  ctrader_openapi.db — 5.4 GB, I/O errors observed
          No archival, no retention, no VACUUM strategy

STRATEGY:
  HOT:   Last 30 days — fast queries, in main DB
  WARM:  30-180 days — slower queries, in main DB
  COLD:  >180 days — separate archive DB

APPROACH:
  1. Health check → enable WAL → separate read/write connections
  2. Weekly archival: copy records > 30 days to archive DB
  3. Verify count → delete from main → VACUUM
  4. Daily backup to data/backups/
  5. Alert at 4 GB, archive at 3 GB

RULES:
  - Never archive during market hours (Sunday 00:00-06:00 UTC only)
  - Verify before delete
  - Archive DBs have identical schema (no transformations)
  - VACUUM only after archival, only during low-activity windows
```

---

## Thread Model Cleanup Direction

```
CURRENT:
  - 3 locks for 13.7K lines
  - 40+ self._* variables written without synchronization
  - 1 background thread (neural mission) spawned from scheduler
  - No atomic writes for JSON state
  - No heartbeat for main loop

PLAN:
  Phase 1: Document concurrency contract (state.py)
  Phase 2: Replace neural mission thread with queue-based approach
  Phase 3: Separate read/write SQLite connections
  Phase 4: Add notification queue (prevent Telegram I/O blocking)
  Phase 5: Audit all self._* access, add locks where needed

ATOMIC WRITES (apply everywhere):
  - Write JSON to .tmp file
  - Rename .tmp to real file (atomic on Windows and Linux)
  - On read: check for .tmp (interrupted write), recover if possible
  - Apply to: trading_manager_state.json, neural_gate_canary_policy.json, etc.
```

---

## How Hermes Complements Opus

```
OPUS FINDS:                         HERMES BUILDS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"r_peak doesn't persist"       →   atomic writes + startup verification + alert

"TRAILING_STRUCT not enforced" →   enforcement telemetry + gap detection alert

"Confidence is synthetic"      →   gate_roi attribution + rejection funnel

"Fake-smart module"            →   flag for removal, do NOT refactor

"Token refresh too weak"       →   auth health telemetry + expiry alert

"Silent failure risks"         →   structured logging + heartbeat + startup checks

"Opportunity suppressed"       →   suppression tracker + replay analysis

"Runtime state unreliable"     →   atomic writes + recovery + reconstruction
```

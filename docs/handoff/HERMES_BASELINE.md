# Hermes Baseline

**Last updated:** 2026-04-20
**Role:** Senior software architect, maintainability reviewer, systems-hardening engineer, production-operations planner
**Branch:** `deploy-xau-family-canary`

---

## Hermes Ownership Areas

| Area | Scope | Does NOT include |
|------|-------|-----------------|
| Refactor map | How to restructure code without breaking live trading | Deciding what to remove (that's Opus) |
| Config hardening | Preventing silent misconfiguration | Changing trading thresholds |
| Logging / monitoring / observability | Making problems visible | Interpreting what the logs mean for trading |
| Scheduler split | Decomposing the 13.7K-line god class | Changing scan logic or routing behavior |
| DB archival / retention / backup safety | Keeping databases healthy | Deciding what data is trading-relevant |
| Thread model cleanup | Preventing race conditions | Changing execution logic |
| Production maintainability | Safe deployment, rollback, health checks | Trading strategy decisions |

---

## Architectural Hardening Priorities

### Codebase metrics (measured)
- **Total Python:** ~285K lines across 200+ files
- **scheduler.py:** 13,679 lines, 1 class, 120+ methods — the critical bottleneck
- **config.py:** 1,903 attributes, 1,864 unique env vars, validate() checks only 3
- **learning/:** 29,675 lines across 26 modules
- **execution/:** 13,153 lines across 7 modules
- **ctrader_executor.py:** 7,378 lines mixing DB access (221 lines), order management (1,700 refs), subprocess execution, and source gating
- **live_profile_autopilot.py:** 7,642 lines — largest single module, mixed causal and potentially decorative logic
- **Tests:** 29,499 lines across 60 files
- **ctrader_openapi.db:** 5.4 GB — returning I/O errors, no archival policy

### Config access patterns (measured)
- `getattr(config, "X")` in scheduler.py: **923 calls**
- `config.X` direct access in scheduler.py: **88 calls**
- Ratio: **10:1 defensive to direct** — the codebase has learned to distrust config

### Thread model (measured)
- **3 locks** for 13.7K lines of code with 35+ scheduled jobs + async threads
- **40+ self._* instance variables** written by multiple methods without synchronization
- **1 async thread** spawned (`_run_neural_mission_cycle_async`)
- **Main loop:** daemon thread, no heartbeat monitoring

---

## Scheduler Split Direction

### Current state
`scheduler.py` is a 13,679-line God Class containing:
- Signal helpers (~2,000 lines)
- Family canary builders (~4,000 lines)
- Guard methods (~2,500 lines)
- Report generators (~3,000 lines)
- Scan loop methods (~1,000 lines)
- Execution dispatchers (~1,000 lines)
- Setup, start, stop (~200 lines)

### Target structure
```
scheduler/
  __init__.py        ← Core class (2,000 lines): start/stop/loop/scan orchestration
  state.py           ← Shared state management with locks
  routing.py         ← Signal dispatch engine
  guards/            ← All guard/check logic
  families/          ← All family canary builders
  reports/           ← All report generators
  scan_loops.py      ← Scan orchestration methods
```

### Sequence (constrained by Opus findings)
1. **Phase 1:** Extract report generators (LOWEST risk — read-only, don't affect trading)
2. **Phase 2:** Extract family builders (MEDIUM risk — modify signals but don't execute)
3. **Phase 3:** Extract guards (MEDIUM risk — but WAIT for Opus to validate which guards are causal)
4. **Phase 4:** Extract routing (HIGH risk — execution hot path, needs full test coverage first)
5. **Phase 5:** Decompose ctrader_executor.py (WAIT — Opus still evaluating execution wiring)
6. **Phase 6:** Decompose live_profile_autopilot.py (HOLD — pending Opus verdict on causal status)

### Constraint
> Do NOT decompose any module Opus identifies as non-causal. Flag for removal instead of investing in refactoring.

---

## Config Hardening Direction

### Current state
- 1,903 flat attributes from `os.getenv()`
- No type checking, no range validation, no cross-field validation
- `validate()` checks only 3 keys (AI key, Telegram token, Telegram chat ID)
- Dangerous combinations (autotrade+dry_run) go undetected

### Target architecture
```
config/
  __init__.py        ← Config class (backward-compatible facade)
  schema.py          ← Pydantic model for validation
  validators.py      ← Cross-field assertions + startup checks
  groups/            ← Organized by subsystem (ctrader, mt5, scanning, etc.)
```

### Key additions (Opus-informed)
- r_peak persistence verification at startup
- TRAILING_STRUCT + position manager consistency check
- Token/auth timeout ordering check
- Opportunity suppression risk detection (high confidence + high sharpness thresholds)
- Neural brain model staleness check

### Migration approach
1. Add Pydantic schema as "shadow validator" alongside existing config (no behavior change)
2. Add startup assertions that log warnings (no failures)
3. Migrate `getattr()` calls to direct access one module at a time
4. Only then consider grouped access (`cfg.ctrader.risk_usd_per_trade`)

---

## Observability Direction

### What to build (priority order)

**P0 — Opus-critical visibility:**
1. **r_peak health check** — verify at startup, alert if missing
2. **TRAILING_STRUCT enforcement visibility** — log gap between intended and actual trailing
3. **Token/auth health monitoring** — alert on refresh failure, approaching expiry
4. **Atomic JSON writes** — prevent state corruption on crash

**P1 — Production reliability:**
5. **Structured logging** (structlog with JSON output)
6. **Signal correlation IDs** — trace every signal from scanner to execution
7. **Per-gate rejection telemetry** — who blocks what and why
8. **Scheduler heartbeat** — detect hung main loop
9. **Gate ROI attribution** — prove which gates add value (Opus's "per-gate live ROI proof")

**P2 — Operational insight:**
10. **Rejection funnel dashboard** — visual gate-by-gate pass/fail rates
11. **Opportunity suppression tracker** — "would rejected signals have been profitable?"
12. **DB health monitoring** — size, I/O errors, WAL mode status
13. **Config drift report** — diff current .env.local against .env.example

### Event taxonomy
```
signal.received / signal.rejected / signal.routed / signal.executed / signal.failed
gate.decision / gate.outcome / gate.rejected (with reason)
execution.order_placed / order_filled / order_rejected / order_cancelled
position.opened / position.closed / position.defended / position.breakeven
learning.outcome_recorded / model_updated / profile_adjusted
scheduler.heartbeat / job_start / job_end / job_error
config.warning / config.assertion_failed
auth.token_state / auth.token_expiring_soon / auth.refresh_failure
trailing.gap_detected
r_peak.missing_at_startup / r_peak.loaded
```

---

## DB Archival Direction

### Current state
- `ctrader_openapi.db`: 5.4 GB, returning I/O errors
- No archival policy, no retention, no VACUUM schedule
- All trade records since inception in one file

### Target policy
- **HOT (0-30 days):** Active queries, <50ms response
- **WARM (30-180 days):** Historical learning, <500ms response
- **COLD (>180 days):** Archive file, rarely accessed

### Technical approach
1. Enable WAL mode + separate read/write connections
2. Weekly archival: copy records >30 days to archive DB, then delete from main
3. VACUUM after archival, only during Sunday 00:00-06:00 UTC
4. Daily backup to `data/backups/`
5. Alert at 4 GB, archive at 3 GB

### Immediate action
Run health check before any archival. The I/O error observed during review may indicate corruption, not just growth.

---

## Thread Model Cleanup Direction

### Current state
- 3 locks for 13.7K lines
- 40+ unprotected self._* variables
- 1 async thread (neural mission)
- No heartbeat, no atomic writes, no queue-based patterns

### Target state
1. Centralized `SchedulerState` class with RLock
2. Neural mission: queue-based instead of thread-spawned
3. All JSON state writes: atomic (write-to-temp-then-rename)
4. SQLite: WAL mode + separate read/write connections
5. Notifications: queue-based to prevent Telegram I/O blocking
6. Main loop: heartbeat monitoring with watchdog

### Where locks are necessary
| State | Current | Needed |
|-------|---------|--------|
| `_neural_mission_thread` | None | Lock or atomic flag |
| `_mt5_repeat_guard_state` | Protected ✓ | Keep |
| `_signal_trace_seq` | Protected ✓ | Keep |
| `neural_brain.npz` | None | File lock or atomic write |
| All `data/runtime/*.json` | None | Atomic write pattern |

### Where queue-based design is better
1. Neural mission training
2. Telegram notifications
3. DB writes for reports (batch writer thread)

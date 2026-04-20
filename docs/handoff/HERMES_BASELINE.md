# Hermes — Baseline & Ownership

> **Last updated:** 2026-04-20
> **Role:** Architecture, infrastructure, observability, production reliability.
> **Mode:** Delta — complement Opus without overlapping.

---

## Hermes Ownership Areas

```
HERMES OWNS:                          OPUS OWNS (do not touch):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Refactor map                           Execution truth
Config hardening                       Confidence logic
Logging / monitoring                   Fake-smart detection
Scheduler split                        Opportunity capture
DB archival / backup                   Winner protection
Thread model cleanup                   Live policy behavior
Production maintainability             TRAILING_STRUCT enforcement
                                       Per-gate ROI evaluation
```

**Core rule:** Opus identifies the problem. Hermes builds the instrumentation to make it visible. Opus proposes the fix. Hermes builds the infrastructure to deploy it safely.

---

## Architectural Hardening Priorities

### Current state (quantified):

| Metric | Value | Risk |
|--------|-------|------|
| scheduler.py | 13,679 lines, 1 class, 120+ methods | Single point of failure |
| config.py | 1,903 attributes, 1,864 unique env vars | validate() checks only 3 |
| getattr(config) ratio | 923 calls vs 88 direct refs (10:1) | Typo = silent fallback |
| Boolean config flags | 401 | Dangerous combos undetected |
| Locks in scheduler | 3 locks for 13.7K lines | Race conditions possible |
| ctrader_openapi.db | 5.4 GB, I/O errors observed | May be failing silently |
| Total Python | ~285K lines (excluding .claude worktree) | Large surface area |

### Hardening sequence:

1. **Immediate:** DB health, atomic writes, r_peak verification, trailing visibility
2. **This week:** Structured logging, config assertions, opportunity tracker
3. **Weeks 2-4:** Scheduler extraction (reports → families → guards)
4. **Weeks 5+:** Execution module decomposition, config migration

---

## Scheduler Split Direction

### Current structure:
```
scheduler.py (13,679 lines)
  ├── Signal helpers (~2,000 lines)
  ├── Family canary builders (~4,000 lines)
  ├── Guard methods (~2,500 lines)
  ├── Report generators (~3,000 lines)
  ├── Scan loops (~1,000 lines)
  ├── Execution dispatchers (~1,000 lines)
  └── Core loop + setup (~500 lines)
```

### Target structure:
```
scheduler/
  __init__.py          ← Core loop, setup, scan loops (~2,000 lines)
  state.py             ← Shared state management (~500 lines)
  routing.py           ← Signal dispatch engine (~3,000 lines)
  guards/              ← All guard logic (~2,500 lines across modules)
  families/            ← All family builders (~4,000 lines across modules)
  reports/             ← All report generators (~3,000 lines across modules)
```

### Migration sequence:
1. **Phase 1 (Week 2):** Extract report generators — lowest risk, read-only operations
2. **Phase 2 (Week 3):** Extract family builders — medium risk, modify signal objects
3. **Phase 3 (Week 3-4):** Extract guard logic — medium risk, called in hot path
4. **Phase 4 (Week 5):** Extract routing logic — highest risk, execution hot path
5. **Phase 5 (Week 6):** Extract scan loops — medium risk

### Constraints:
- Do NOT extract methods that route through modules Opus hasn't validated as causal
- Every extraction must have a "behavior snapshot" test before and after
- Never refactor during market hours — weekends or feature flags only
- The scheduler singleton (`scheduler = DexterScheduler()`) must remain importable from `scheduler`

---

## Config Hardening Direction

### Current state:
- 1,903 attributes set via `os.getenv()` with no type checking
- `validate()` checks only 3 keys (AI key, Telegram token, Telegram chat ID)
- No range validation, no cross-field validation, no startup assertions
- 923 `getattr(config, "X")` calls in scheduler.py alone — defensive coding because config is untrusted

### Target architecture:
```
config/
  __init__.py       ← Config class (backward-compatible facade)
  schema.py         ← Pydantic model for all config groups
  validators.py     ← Cross-field validation + startup assertions
  groups/           ← Organized by subsystem (~15 group files)
```

### Migration sequence:
1. **Week 1:** Add `schema.py` as shadow validator (no code changes)
2. **Week 2:** Add startup validation pass (log warnings, don't fail)
3. **Week 3:** Add startup assertions for dangerous combinations
4. **Week 4-6:** Migrate `getattr()` calls to direct `config.X` access
5. **Week 6-8:** Optional: grouped access (`cfg.ctrader.risk_usd`)

### Dangerous combinations to assert:
- `AUTOTRADE_ENABLED=1` + `DRY_RUN=0` → log WARNING with account ID
- Both MT5 + cTrader enabled + combined risk > daily limit → block startup
- `SCALPING_ENABLED=1` + no execution backend → log WARNING
- `TRAILING_STRUCT_ENABLED=1` + `POSITION_MANAGER_DISABLED` → log WARNING (Opus finding)
- AI provider missing + `SIGNAL_FEEDBACK_ENABLED=1` → log WARNING

---

## Observability Direction

### Structured logging:
- Adopt structlog with JSON output
- Signal correlation IDs propagated through entire lifecycle
- Consistent event taxonomy: `signal.received`, `gate.rejected`, `execution.order_placed`, etc.

### Opus-specific telemetry (highest priority):
1. **Opportunity suppression tracker** — per-gate rejection rates + replay of "would rejected signals have won?"
2. **TRAILING_STRUCT enforcement visibility** — log gap between intended and actual trailing stops
3. **r_peak persistence verification** — startup check + reconstruction from history
4. **Token/auth health monitoring** — refresh failures, expiry countdown
5. **Gate ROI attribution** — per-gate outcome tracking for live ROI measurement

### Alerts (P0):
- `r_peak.missing_at_startup` → Telegram URGENT
- `trailing.gap_detected` → Telegram URGENT
- `auth.refresh_failure` → Telegram URGENT
- `scheduler.hung` → Telegram
- `db.io_error` → Telegram
- `opportunity_suppression.extreme` → Telegram + "verify gate ROI"

---

## DB Archival Direction

### Current state:
- `ctrader_openapi.db`: 5.4 GB, I/O errors observed
- No archival, no retention, no VACUUM strategy
- All trade records since inception in one file

### Target:
- **Hot:** Last 30 days — fast queries, <50ms
- **Warm:** 30-180 days — slower queries, <500ms
- **Cold:** >180 days — archive DB, rarely accessed

### Sequence:
1. **Immediate:** Health check — verify reads AND writes work
2. **Week 1:** Enable WAL mode, separate read/write connections, daily backup
3. **Week 2:** Implement archival of records > 30 days
4. **Ongoing:** Monitor size, alert at 4 GB, VACUUM after archival

---

## Thread Model Cleanup Direction

### Current state:
- 3 locks (`_neural_mission_cycle_lock`, `_mt5_repeat_guard_lock`, `_signal_trace_lock`)
- 40+ `self._*` state variables written by multiple methods
- 1 async thread (neural mission) spawned from main loop
- Daemon thread main loop — no watchdog

### Key risks:
- `self._neural_mission_thread` written without lock
- `self._mt5_repeat_guard_state` persisted to JSON without atomic writes
- `neural_brain.npz` written by async thread, read by main thread — no file lock
- `self.running` flag read/written without synchronization

### Sequence:
1. **Week 1:** Document concurrency contract, add `state.py` with centralized access
2. **Week 2:** Replace neural mission thread with queue-based approach, atomic JSON writes
3. **Week 3:** Separate read/write SQLite connections, WAL mode on all databases
4. **Week 4:** Add notification queue, heartbeat monitoring
5. **Week 5:** Audit all `self._*` access, add locks where needed

---

## What Hermes Needs From Opus

| Need | Why |
|------|-----|
| Causal audit of learning/ modules | To know what to refactor vs. flag for removal |
| r_peak persistence verification | To know if startup verification needs reconstruction logic |
| TRAILING_STRUCT enforcement status | To know if enforcement gap is at caller or callee level |
| Per-gate ROI evaluation | To know which guards to keep vs. simplify |
| Confidence calibration verdict | To know if confidence logic needs hardening or replacement |

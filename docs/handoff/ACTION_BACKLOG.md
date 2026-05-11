# Action Backlog

> Last updated: 2026-05-11 (delta: Fibo MTF shadow leak closed; route planner wired shadow-only; Opus 4.7 approved shadow deploy with micro-live promotion gates)
> Format: Prioritized, owner-assigned, dependency-tracked
> Update this file after every session. Move items between P-levels as needed.

---

## P0 — Immediate (before next trading session)

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P0-0 | DB verification — read-only diagnostic + backup-first (see DB_VERIFICATION_PLAN.md) | Hermes | **DONE** — health check module created, first diagnostic run | — | Read-only diagnostics. No mutation. Must complete before P1-4/P1-9/P2-4. |
| P0-1 | Add atomic_json_write utility + apply to trading_manager_state.json | Hermes | **DONE** — utils/atomic_write.py created and tested | — | No behavior change. Drop-in replacement. |
| P0-2 | Add r_peak persistence verification at startup | Hermes | TODO | P0-1 | Reads state file. Logs warning if missing. No trading logic change. r_peak confirmed ABSENT from trading_manager_state.json. |
| P0-3 | Add token/auth refresh health monitoring | Hermes | **DONE** — infra/auth_health.py created, first check shows CRITICAL (142h since last refresh) | — | Additive logging only. No auth logic change. |
| P0-4 | TRAILING_STRUCT enforcement | — | **DONE** | — | Fixed in commit 720b8f0. Verified in code: elif branch reading sl_floor_r + amend_position_sltp in ctrader_executor.py. |
| P0-5 | Verify r_peak is actually written during trade recording | Opus | TODO | — | Code review only. Trace r_peak through trade lifecycle. |
| P0-6 | Trace TRAILING_STRUCT from definition to execution caller | Opus | **DONE** | — | Fixed in 720b8f0. Opus should verify fix completeness. |
| P0-7 | Fibo MTF shadow-to-live leak invariant | Hermes + Opus | **DONE** — invariant + scheduler shadow-only planner wiring, Opus approved shadow deploy | — | Do not promote FIBO_MTF_SHADOW directly; future live requires separate non-shadow adapter. |
| P0-8 | Fibo MTF planner shadow evidence collection | Hermes | TODO | P0-7 | Deploy/restart after tests; verify xau_shadow_journal block_reason `fibo_mtf_planner:<route>` and route counts. |
| P0-9 | Fibo MTF micro-live probe promotion decision | Opus | BLOCKED | P0-8 | Need ≥30 shadow probe decisions, ≥14 days, expectancy ≥ +0.4R, conflict/news/broker context, explicit env kill switch. |

---

## P1 — This Week

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P1-1 | Add opportunity suppression tracker (per-gate rejection logging) | Hermes | TODO | — | Additive. No existing code changes. |
| P1-2 | Add gate ROI attribution data collection | Hermes | TODO | P1-1 | Additive. Records decisions + outcomes. |
| P1-3 | Add startup config assertions (dangerous combinations) | Hermes | TODO | — | Additive. Logs warnings at startup. |
| P1-4 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO | P0-0 | Only after DB health verified. Single PRAGMA command. |
| P1-5 | Causal audit of all learning/ modules | Opus | TODO | — | Code review. Classify each module: real / decorative / uncertain. |
| P1-6 | Verify r_peak reconstruction from trade history | Opus | TODO | P0-5 | Code review. Can neural_brain reconstruct it? |
| P1-7 | Add scheduler heartbeat monitoring | Hermes | TODO | — | Additive. Counter in main loop + alert. |
| P1-8 | Implement DB backup (daily copy to data/backups/) | Hermes | TODO | P0-0 | Additive script. No existing code changes. Must complete DB verification first. |
| P1-9 | Verify TRAILING_STRUCT fix works in live | Opus | TODO | — | Confirm commit 720b8f0 fix produces correct trailing in live trading. |

---

## P2 — Week 2-4

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P2-1 | Set up structured logging (structlog, JSON output) | Hermes | TODO | — | Replace logging setup. All existing log calls still work. |
| P2-2 | Add signal correlation IDs to all signal lifecycle logs | Hermes | TODO | P2-1 | Propagate signal_run_id through contextvars. |
| P2-3 | Extract report generators from scheduler.py → scheduler/reports/ | Hermes | TODO | P2-1 | Phase 1 split. Move methods, add imports. Zero logic change. |
| P2-4 | Implement DB archival (records > 30 days → archive DB) | Hermes | TODO | P1-8 | New script. Runs Sunday 00:00 UTC. Must complete DB verification first. |
| P2-5 | Build rejection funnel dashboard report | Hermes | TODO | P1-1 | Consumes suppression tracker data. Additive report. |
| P2-6 | Evaluate confidence calibration against actual outcomes | Opus | TODO | P1-2 | Analysis. Uses gate ROI data. |
| P2-7 | Identify and flag fake-smart modules for removal | Opus | TODO | P1-5 | Output: definitive list of non-causal modules. |
| P2-8 | Apply atomic writes to all runtime JSON files | Hermes | TODO | P0-2 | Extend pattern to neural_gate, strategy_lab, trading_team state. |

---

## P3 — After Opus completes live-trading audit

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P3-1 | Extract family builders from scheduler.py → scheduler/families/ | Hermes | TODO | P2-3 | Phase 2 split. After Opus validates family logic is causal. |
| P3-2 | Extract guard logic from scheduler.py → scheduler/guards/ | Hermes | TODO | P2-3, P2-7 | Phase 3 split. Only extract guards Opus confirms are causal. |
| P3-3 | Extract routing logic from scheduler.py → scheduler/routing.py | Hermes | TODO | P3-1, P3-2 | Phase 4 split. Highest risk. Full integration test required. |
| P3-4 | Fix r_peak persistence (if broken) | Opus | TODO | P0-5, P1-6 | Code change. Depends on whether persistence works. |
| P3-5 | Decompose live_profile_autopilot.py | Hermes | TODO | P2-7 | Phase 6 split. ONLY if Opus confirms parts are causal. |
| P3-7 | Migrate config to Pydantic validation | Hermes | TODO | P3-1 | After scheduler split reduces config access surface. |
| P3-8 | Remove fake-smart modules flagged by Opus | Reviewer | TODO | P2-7 | Surgical removal. Full test suite must pass. |
| P3-9 | Separate read/write SQLite connections in ctrader_executor.py | Hermes | TODO | P3-3 | After routing extraction reduces coupling. |

---

## Completed

| ID | Item | Owner | Date | Notes |
|----|------|-------|------|-------|
| — | Initial architecture review (Opus) | Opus | 2026-04-20 | Baseline established |
| — | Initial architecture review (Hermes) | Hermes | 2026-04-20 | Hardening plan created |
| — | Multi-expert handoff structure created | Hermes | 2026-04-20 | 5 coordination files |
| P0-4 | TRAILING_STRUCT enforcement wiring gap | Repo Owner | 2026-04-20 | Fixed in commit 720b8f0. elif branch reading sl_floor_r + amend_position_sltp. |
| P0-7 | TRAILING_STRUCT wiring trace | Opus | 2026-04-20 | Confirmed fixed in 720b8f0. |
| P0-0a | DB read-only diagnostic (first pass) | Hermes | 2026-04-20 | Read-only open works. 14 tables, 8.9M rows. No WAL companion files. Extreme latency. Full plan in DB_VERIFICATION_PLAN.md. |
| P0-0b | DB health check module + second diagnostic | Hermes | 2026-04-20 | infra/db_health.py created. DB openable (warning: 5.4 GB, depth_quotes 8M rows). WAL files appear/disappear (live system checkpointing). |
| P0-1 | Atomic write utility | Hermes | 2026-04-20 | utils/atomic_write.py created. atomic_json_write, atomic_json_read, atomic_json_update. 14 runtime JSON files registered. Functional test passed. |
| P0-3 | Token/auth health monitoring | Hermes | 2026-04-20 | infra/auth_health.py created. First check: CRITICAL — token last refreshed 142h ago. |
| P0-7 | Fibo MTF shadow leak + planner foundation | Hermes + Opus | 2026-05-11 | Commits in progress: invariant blocks shadow dispatch, `FiboMtfTradePlanner` routes observe/probe/base_live/runner_add, scheduler stores route metadata shadow-only; Opus 4.7 approved shadow deploy and defined micro-live gates. |

---

## Dependency Graph (simplified)

```
P0-0 (DB verification)                    ← NEW TOP PRIORITY
  └─→ P1-4 (WAL mode)
  └─→ P1-8 (DB backup)
       └─→ P2-4 (DB archival)

P0-1 (atomic writes)
  └─→ P0-2 (r_peak verification)
  └─→ P2-8 (all runtime JSON)

P1-1 (suppression tracker)
  └─→ P1-2 (gate ROI attribution)
       └─→ P2-5 (rejection funnel)
       └─→ P2-6 (confidence calibration)

P1-5 (causal audit)
  └─→ P2-7 (flag fake-smart)
       └─→ P3-8 (remove fake-smart)
       └─→ P3-5 (decompose autopilot)

P2-3 (extract reports — Phase 1)
  └─→ P3-1 (extract families — Phase 2)
       └─→ P3-2 (extract guards — Phase 3)
            └─→ P3-3 (extract routing — Phase 4)

TRAILING_STRUCT: FIXED (720b8f0) — no blocking dependencies
```

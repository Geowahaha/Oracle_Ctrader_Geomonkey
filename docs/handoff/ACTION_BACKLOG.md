# Action Backlog

> Last updated: 2026-04-20
> Format: Prioritized, owner-assigned, dependency-tracked
> Update this file after every session. Move items between P-levels as needed.

---

## P0 — Immediate (before next trading session)

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P0-1 | Run ctrader_openapi.db health check (reads + writes + WAL status) | Hermes | TODO | — | Standalone. No code changes. |
| P0-2 | Add atomic_json_write utility + apply to trading_manager_state.json | Hermes | TODO | — | No behavior change. Drop-in replacement. |
| P0-3 | Add r_peak persistence verification at startup | Hermes | TODO | P0-2 | Reads state file. Logs warning if missing. No trading logic change. |
| P0-4 | Add TRAILING_STRUCT enforcement visibility logging | Hermes | TODO | — | Additive logging only. No execution logic change. |
| P0-5 | Add token/auth refresh health monitoring | Hermes | TODO | — | Additive logging only. No auth logic change. |
| P0-6 | Verify r_peak is actually written during trade recording | Opus | TODO | — | Code review only. Trace r_peak through trade lifecycle. |
| P0-7 | Trace TRAILING_STRUCT from definition to execution caller | Opus | TODO | — | Code review only. Find the wiring gap. |

---

## P1 — This Week

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P1-1 | Add opportunity suppression tracker (per-gate rejection logging) | Hermes | TODO | — | Additive. No existing code changes. |
| P1-2 | Add gate ROI attribution data collection | Hermes | TODO | P1-1 | Additive. Records decisions + outcomes. |
| P1-3 | Add startup config assertions (dangerous combinations) | Hermes | TODO | — | Additive. Logs warnings at startup. |
| P1-4 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO | P0-1 | Only if health check passes. Single PRAGMA command. |
| P1-5 | Causal audit of all learning/ modules | Opus | TODO | — | Code review. Classify each module: real / decorative / uncertain. |
| P1-6 | Verify TRAILING_STRUCT wiring end-to-end | Opus | TODO | P0-7 | Code review. Identify exact gap location. |
| P1-7 | Verify r_peak reconstruction from trade history | Opus | TODO | P0-6 | Code review. Can neural_brain reconstruct it? |
| P1-8 | Add scheduler heartbeat monitoring | Hermes | TODO | — | Additive. Counter in main loop + alert. |
| P1-9 | Implement DB backup (daily copy to data/backups/) | Hermes | TODO | P0-1 | Additive script. No existing code changes. |

---

## P2 — Week 2-4

| ID | Item | Owner | Status | Depends On | Safe Sequencing |
|----|------|-------|--------|------------|-----------------|
| P2-1 | Set up structured logging (structlog, JSON output) | Hermes | TODO | — | Replace logging setup. All existing log calls still work. |
| P2-2 | Add signal correlation IDs to all signal lifecycle logs | Hermes | TODO | P2-1 | Propagate signal_run_id through contextvars. |
| P2-3 | Extract report generators from scheduler.py → scheduler/reports/ | Hermes | TODO | P2-1 | Phase 1 split. Move methods, add imports. Zero logic change. |
| P2-4 | Implement DB archival (records > 30 days → archive DB) | Hermes | TODO | P1-9 | New script. Runs Sunday 00:00 UTC. |
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
| P3-4 | Close TRAILING_STRUCT wiring gap | Opus | TODO | P1-6 | Code change. Depends on exact gap location. |
| P3-5 | Fix r_peak persistence (if broken) | Opus | TODO | P0-6, P1-7 | Code change. Depends on whether persistence works. |
| P3-6 | Decompose live_profile_autopilot.py | Hermes | TODO | P2-7 | Phase 6 split. ONLY if Opus confirms parts are causal. |
| P3-7 | Migrate config to Pydantic validation | Hermes | TODO | P3-1 | After scheduler split reduces config access surface. |
| P3-8 | Remove fake-smart modules flagged by Opus | Reviewer | TODO | P2-7 | Surgical removal. Full test suite must pass. |
| P3-9 | Separate read/write SQLite connections in ctrader_executor.py | Hermes | TODO | P3-3 | After routing extraction reduces coupling. |

---

## Completed

| ID | Item | Owner | Date | Notes |
|----|------|-------|------|-------|
| — | Initial architecture review (Opus) | Opus | 2026-04-20 | Baseline established |
| — | Initial architecture review (Hermes) | Hermes | 2026-04-20 | Hardening plan created |
| — | Multi-expert handoff structure created | Hermes | 2026-04-20 | This file + 4 others |

---

## Dependency Graph (simplified)

```
P0-1 (DB health check)
  └─→ P1-4 (WAL mode)
  └─→ P1-9 (DB backup)
       └─→ P2-4 (DB archival)

P0-2 (atomic writes)
  └─→ P0-3 (r_peak verification)
  └─→ P2-8 (all runtime JSON)

P1-1 (suppression tracker)
  └─→ P1-2 (gate ROI attribution)
       └─→ P2-5 (rejection funnel)
       └─→ P2-6 (confidence calibration)

P1-5 (causal audit)
  └─→ P2-7 (flag fake-smart)
       └─→ P3-8 (remove fake-smart)
       └─→ P3-6 (decompose autopilot)

P2-3 (extract reports — Phase 1)
  └─→ P3-1 (extract families — Phase 2)
       └─→ P3-2 (extract guards — Phase 3)
            └─→ P3-3 (extract routing — Phase 4)
```

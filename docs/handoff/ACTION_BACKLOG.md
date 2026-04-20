# Action Backlog

> **Sequenced for safe execution.** Owner per item. Dependencies noted.
> Do not skip ahead — dependencies are real.

---

## P0 — Immediate (before any refactoring)

| # | Action | Owner | Status | Depends On | Safe Sequencing |
|---|--------|-------|--------|------------|-----------------|
| 0 | Run `ctrader_openapi.db` health check (reads + writes) | Hermes | TODO | Nothing | Do first — if DB is broken, everything else is compromised |
| 1 | Add `atomic_json_write()` utility | Hermes | TODO | Nothing | Independent — can do in parallel with #0 |
| 2 | Apply atomic writes to `trading_manager_state.json` | Hermes | TODO | #1 | After utility exists |
| 3 | Verify r_peak persistence across restart | Opus | TODO | #2 | After atomic writes — test with restart simulation |
| 4 | Add r_peak startup verification + alert | Hermes | TODO | #3 | After Opus confirms current mechanism |
| 5 | Verify TRAILING_STRUCT enforcement at execution level | Opus | TODO | Nothing | Independent — can start now |
| 6 | Add TRAILING_STRUCT enforcement visibility logging | Hermes | TODO | #5 | After Opus confirms where the gap is |
| 7 | Add token/auth refresh health monitoring | Hermes | TODO | Nothing | Independent — can start now |
| 8 | Add startup config assertions (r_peak, trailing, suppression risk) | Hermes | TODO | #3, #5 | After Opus findings on r_peak and trailing |

## P1 — This week

| # | Action | Owner | Status | Depends On | Safe Sequencing |
|---|--------|-------|--------|------------|-----------------|
| 9 | Causal audit: classify all learning/ modules (real vs decorative) | Opus | TODO | Nothing | Start now — blocks Phase 6 refactor |
| 10 | Add opportunity suppression tracker (per-gate rejection logging) | Hermes | TODO | Nothing | Independent |
| 11 | Add gate ROI attribution data collection | Hermes | TODO | #10 | After tracker exists |
| 12 | Enable WAL mode on `ctrader_openapi.db` | Hermes | TODO | #0 (health check) | After confirming DB is healthy |
| 13 | Set up structured logging (structlog + JSON) | Hermes | TODO | Nothing | Independent — but do before adding more logging |
| 14 | Add scheduler heartbeat monitoring | Hermes | TODO | #13 | After structured logging |
| 15 | Apply atomic writes to all runtime state files | Hermes | TODO | #1 | After utility exists |

## P2 — Weeks 2-4

| # | Action | Owner | Status | Depends On | Safe Sequencing |
|---|--------|-------|--------|------------|-----------------|
| 16 | Extract report generators from scheduler.py (Phase 1) | Hermes | TODO | #13, #14 | After logging + heartbeat in place |
| 17 | Implement DB daily backup | Hermes | TODO | #12 | After WAL mode enabled |
| 18 | Build rejection funnel dashboard report | Hermes | TODO | #10, #11 | After tracker + attribution |
| 19 | Fix confidence logic gaps (per Opus findings) | Opus | TODO | #9, #11 | After causal audit + gate attribution data |
| 20 | Extract family builders from scheduler.py (Phase 2) | Hermes | TODO | #16 | After Phase 1 extraction proven safe |
| 21 | Extract guard logic from scheduler.py (Phase 3) | Hermes | TODO | #9, #20 | After Opus validates which guards are causal |
| 22 | Implement DB archival (records > 30 days) | Hermes | TODO | #17 | After backups are running |
| 23 | Identify and remove/deprioritize fake-smart modules | Opus | TODO | #9 | After causal audit |
| 24 | Add per-gate live ROI reporting | Hermes | TODO | #11 | After attribution data collection |

## P3 — Weeks 5+ (after Opus completes live-trading audit)

| # | Action | Owner | Status | Depends On | Safe Sequencing |
|---|--------|-------|--------|------------|-----------------|
| 25 | Extract routing logic from scheduler.py (Phase 4) | Hermes | TODO | #21 | After guards extracted |
| 26 | Decompose live_profile_autopilot.py (if causal per Opus) | Hermes | TODO | #9 | After causal audit — skip if decorative |
| 27 | Decompose ctrader_executor.py (separate DB from execution) | Hermes | TODO | #25 | After routing extracted |
| 28 | Migrate config to Pydantic validation | Hermes | TODO | #8 | After startup assertions stable |
| 29 | Separate read/write SQLite connections in ctrader_executor | Hermes | TODO | #27 | After executor decomposition |
| 30 | Replace neural mission thread with queue-based approach | Hermes | TODO | #14 | After heartbeat monitoring |
| 31 | Add notification queue (Telegram I/O non-blocking) | Hermes | TODO | #13 | After structured logging |
| 32 | VACUUM strategy (weekly during low-activity windows) | Hermes | TODO | #22 | After archival running |

---

## Dependency Graph (simplified)

```
#0 (DB health)
 └─ #12 (WAL mode)
    └─ #17 (daily backup)
       └─ #22 (archival)
          └─ #32 (VACUUM)

#1 (atomic write utility)
 ├─ #2 (trading_manager_state)
 │  └─ #3 (r_peak verification — Opus)
 │     └─ #4 (r_peak startup alert)
 │        └─ #8 (config assertions)
 └─ #15 (all runtime state files)

#9 (causal audit — Opus)
 ├─ #19 (confidence fix — Opus)
 ├─ #21 (guard extraction — Hermes, depends on Opus verdict)
 ├─ #23 (remove fake-smart — Opus)
 └─ #26 (autopilot decomposition — Hermes)

#10 (opportunity tracker)
 └─ #11 (gate attribution)
    ├─ #18 (rejection funnel dashboard)
    └─ #24 (per-gate ROI reporting)

#13 (structured logging)
 ├─ #14 (heartbeat monitoring)
 │  └─ #16 (report extraction)
 │     └─ #20 (family extraction)
 │        └─ #21 (guard extraction)
 │           └─ #25 (routing extraction)
 └─ #31 (notification queue)

#5 (trailing verification — Opus)
 └─ #6 (trailing visibility logging)
```

---

## Blocked Items (waiting on Opus)

| Item | Blocked By | What Opus Needs to Provide |
|------|------------|---------------------------|
| #3 r_peak verification | Opus audit | Does r_peak persist? Where? What format? |
| #5 TRAILING_STRUCT verification | Opus audit | Where is the enforcement gap? Caller or callee? |
| #9 Causal audit | Opus analysis | Classification of each learning/ module |
| #19 Confidence fix | Opus analysis + #11 | Which confidence paths are synthetic? |
| #23 Remove fake-smart | Opus analysis | Which modules to remove? |

## Blocked Items (waiting on Hermes)

| Item | Blocked By | What Hermes Needs to Provide |
|------|------------|------------------------------|
| #10 opportunity tracker | Hermes implementation | Structured logging + signal correlation IDs |
| #11 gate attribution | #10 | Tracker infrastructure in place |
| #16 report extraction | #13, #14 | Logging + heartbeat before splitting |
| #28 config migration | #8 | Startup assertions stable first |

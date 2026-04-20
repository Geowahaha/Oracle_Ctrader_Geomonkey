# Multi-Expert Handoff & Coordination

> **Purpose:** Durable coordination structure for Opus, Hermes, and reviewer/integrator.
> **Created:** 2026-04-20
> **Branch:** deploy-xau-family-canary
> **Repo:** Oracle_Ctrader_Geomonkey

---

## Project Mission

Build and operate a self-improving, multi-strategy AI day-trading system that:

- Runs multiple strategy families in parallel (swarm model) on XAUUSD, BTCUSD, ETHUSD
- Executes via cTrader OpenAPI and MT5 with real money
- Learns from live outcomes via neural brain + winner logic + autopilot
- Protects capital through layered guards, active position defense, and circuit breakers
- Compounds edge by promoting winning families and demoting losing ones

**Trading priorities (non-negotiable):**
1. Let profits run in trend
2. Secure profit efficiently in sideways/chop
3. Do NOT over-block good trades
4. Preserve capital — asymmetric R:R minimum 1:3

---

## Current Live Status

- **Branch:** deploy-xau-family-canary (active development, deployed to VM)
- **Mode:** swarm_support_all — all XAU families active for broader data collection
- **Brokers:** cTrader OpenAPI (primary) + MT5 (secondary, via RPyC)
- **Symbols:** XAUUSD (primary), BTCUSD, ETHUSD
- **Database:** ctrader_openapi.db at 5.4 GB — I/O errors observed during review (P0)
- **Config:** 1,864 unique env vars in .env.local, 401 boolean flags
- **Scheduler:** 13,679-line god class, 35+ scheduled jobs, daemon thread
- **Tests:** 60 test files, 29,500 lines

---

## Confirmed Truths

These are accepted as baseline. Do not re-audit unless contradicted by new code evidence.

### Strong (real causal value)
- NeuralBrain is a genuine outcome-linked learning component
- HermesLoop is a real reinforcement-style modifier loop
- V4 WinnerProtection architecture is directionally correct
- Entry sharpness scoring (8 microstructure features) is substantial
- Active position defense (tighten stop, close early, lock profit) is real
- Canary system with family promotion/demotion is sound
- Multi-agent conductor (Risk/Perf/Regime/Opt agents) is genuine

### Weak / Uncertain (needs validation)
- Some decorative AI/library prior logic — not on live decision path
- LLM/research paths not on live trading decision path
- Auto-calibration paths may not persist or materially affect live outcomes
- Gate stacks may not be validated by per-gate live ROI attribution
- Confidence construction is synthetic / weakly calibrated
- TRAILING_STRUCT enforcement has a wiring gap (intent exists, execution incomplete)
- r_peak persistence across restarts is not verified

### Known Issues (confirmed)
- ctrader_openapi.db returning I/O errors — may be corrupted
- No atomic state writes — crash can corrupt runtime JSON
- Token/auth refresh failure handling too weak
- Scheduler has only 3 locks for 40+ shared state variables
- No structured logging, no correlation IDs, no heartbeat monitoring
- 923 getattr(config) calls vs 88 direct config.X — config system is distrusted

---

## Ownership Split

### Opus — Execution Truth & Live Intelligence

**Owns:**
- Execution wiring verification — does the system actually place trades correctly?
- Confidence logic — is confidence construction real or synthetic?
- Fake-smart detection — which modules look smart but add no causal value?
- Opportunity capture vs over-blocking — are strong trades being suppressed?
- Winner protection / active defense — is it wired correctly end-to-end?
- Per-gate ROI attribution — which gates actually add value?
- TRAILING_STRUCT enforcement — close the wiring gap
- r_peak persistence — ensure it survives restarts

**Does NOT own:**
- Scheduler decomposition
- Config architecture
- Logging framework
- DB archival strategy
- Thread safety refactoring
- General code organization

### Hermes — Architecture & Production Hardening

**Owns:**
- Refactor map — how to restructure without breaking live trading
- Config hardening — prevent silent misconfiguration
- Logging / monitoring / observability — make everything visible
- Scheduler split — decompose the 13.7K-line god class
- DB archival / retention / backup safety
- Thread model cleanup / shared-state safety
- Production maintainability — make the system safe to operate
- Atomic state writes — prevent corruption on crash
- Startup verification — verify critical state survived restarts

**Does NOT own:**
- Evaluating whether a confidence adjustment is "real"
- Deciding which trading strategies are profitable
- Modifying execution logic
- Changing risk parameters
- Evaluating fake-smart modules (reports findings to Opus)

### Reviewer / Integrator — Coordination & Quality

**Owns:**
- Merging expert outputs into actionable plans
- Verifying no-overlap between Opus and Hermes work
- Sequencing changes for live safety
- Final review before any merge to main
- Maintaining ACTION_BACKLOG.md
- Updating NEXT_PROMPTS.md after each session

---

## No-Overlap Rules

```
RULE 1: Opus evaluates causal value. Hermes instruments what Opus finds.
        Opus: "This module is fake-smart."
        Hermes: "OK. I will NOT refactor it. I will flag it for removal
                 and remove it from scheduled jobs."

RULE 2: Opus proposes fixes. Hermes builds infrastructure for fixes.
        Opus: "TRAILING_STRUCT is not enforced at execution level."
        Hermes: "I will add trailing_enforcement telemetry and gap detection
                 so the fix is visible and verifiable."

RULE 3: Neither expert modifies the other's domain without explicit approval.
        Opus does not propose scheduler splits.
        Hermes does not propose confidence threshold changes.

RULE 4: When in doubt, ask the reviewer/integrator before acting.
```

---

## Current Priorities

### P0 — Immediate (this session or next)

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 0 | Run ctrader_openapi.db health check | Hermes | TODO | I/O errors observed — may already be broken |
| 1 | Add atomic_json_write to trading_manager_state.json | Hermes | TODO | r_peak corruption risk |
| 2 | Verify r_peak persistence at startup | Hermes | TODO | Winner-protection unreliable without it |
| 3 | Add TRAILING_STRUCT enforcement visibility | Hermes | TODO | Opus identified wiring gap |
| 4 | Add token/auth refresh health monitoring | Hermes | TODO | Opus: failure handling too weak |

### P1 — This Week

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 5 | Add opportunity suppression tracker | Hermes | TODO | Per-gate rejection logging |
| 6 | Add gate ROI attribution data collection | Hermes | TODO | Opus needs this to validate gates |
| 7 | Add startup config assertions | Hermes | TODO | Dangerous combo detection |
| 8 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO | Concurrent read/write safety |
| 9 | Causal audit of all learning/ modules | Opus | TODO | Classify real vs fake-smart |
| 10 | Verify TRAILING_STRUCT wiring end-to-end | Opus | TODO | Close the enforcement gap |
| 11 | Verify r_peak reconstruction from history | Opus | TODO | Fallback if persistence fails |

### P2 — Week 2-4

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 12 | Extract report generators from scheduler.py | Hermes | TODO | Phase 1 split — lowest risk |
| 13 | Set up structured logging (structlog) | Hermes | TODO | JSON output + correlation IDs |
| 14 | Add scheduler heartbeat monitoring | Hermes | TODO | Detect hung scheduler |
| 15 | Implement DB archival (backup first) | Hermes | TODO | No deletion until health verified |
| 16 | Evaluate confidence calibration | Opus | TODO | Is confidence construction fixable? |
| 17 | Identify and flag fake-smart modules | Opus | TODO | For removal, not refactoring |

### P3 — After Opus completes live-trading audit

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 18 | Extract family builders from scheduler.py | Hermes | TODO | Phase 2 split |
| 19 | Extract guard logic from scheduler.py | Hermes | TODO | Phase 3 — after Opus validates causal guards |
| 20 | Extract routing logic from scheduler.py | Hermes | TODO | Phase 4 — execution hot path |
| 21 | Decompose live_profile_autopilot.py | Hermes | TODO | Phase 6 — only if Opus confirms causal parts |
| 22 | Migrate config to Pydantic validation | Hermes | TODO | After all config keys validated |

---

## Open Risks

1. **ctrader_openapi.db may already be corrupted.** I/O errors observed. Every trade journal write may be failing silently. Verify before anything else.

2. **r_peak may not survive restart.** If true, winner-protection and emergency logic are unreliable after every restart until a new trade provides data.

3. **TRAILING_STRUCT is not fully enforced.** Structural trailing intent exists in code but is not wired to execution at the caller level. Positions may not be defended as designed.

4. **Confidence is synthetic.** High-confidence blocking exists in important paths, but confidence construction may not be calibrated to actual win rates.

5. **No atomic writes.** A crash or power loss during state persistence corrupts JSON files. No recovery mechanism exists.

6. **Scheduler has no heartbeat.** If the main loop hangs (e.g., blocked on DB I/O), the system appears "running" but is not scanning.

---

## Next Questions to Each Expert

### To Opus
1. Which learning/ modules are confirmed causal vs decorative? Need a definitive list.
2. Is r_peak actually persisted? If not, can it be reconstructed from ctrader_openapi.db?
3. Is TRAILING_STRUCT enforcement a single missing call, or a systemic wiring issue?
4. What is the actual confidence construction formula? Is it calibrated to outcomes?
5. Which gates in the signal routing pipeline have proven ROI? Which don't?

### To Hermes
1. Is the ctrader_openapi.db health check clean? If not, what's the recovery plan?
2. Are atomic writes in place for all runtime JSON state files?
3. Is the scheduler heartbeat monitoring active?
4. Has the DB archival plan been executed? What's the current backup status?
5. Is structured logging deployed? Can we trace a signal lifecycle end-to-end?

### To Reviewer/Integrator
1. Are Opus and Hermes outputs sequenced safely? Any conflicts?
2. Is the action backlog up to date?
3. Are there any P0 items that have been sitting too long?
4. Has the trading mission been preserved in all proposed changes?

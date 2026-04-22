# Multi-Expert Handoff & Coordination

> **Purpose:** Durable coordination structure for Opus, Hermes, and reviewer/integrator.
> **Created:** 2026-04-20
> **Branch:** deploy-xau-family-canary
> **Repo:** Oracle_Ctrader_Geomonkey

---

## Current Team Execution Snapshot

### Mission Lock
- Maximize real trading opportunity
- Let profits run in trend
- Secure profit efficiently in sideways/chop
- Do not over-block good trades
- Improve trustworthiness for live money
- Avoid overlap between experts

---

### Current Operating Model
This project is currently being advanced through a strict multi-expert workflow:

- **Opus** owns:
  - execution truth
  - confidence logic
  - fake-smart cleanup
  - live trading behavior
  - opportunity capture vs over-blocking
  - policy correctness

- **Hermes** owns:
  - refactor map
  - config hardening
  - logging / monitoring / observability
  - scheduler split
  - DB archival / verification support
  - thread model cleanup
  - production maintainability

- **Reviewer / Integrator** owns:
  - contradiction detection
  - overlap prevention
  - final priority order
  - action-plan merging
  - next-prompt control

---

### Current Priority State
#### Opus — current focus
- Implement `r_peak` persistence path (truth now defined — see A2 below)
- Wire graded degrade policy into routing/execution (DB/auth health → risk multiplier)
- Commit explicit config keys for regime-break guard (config discoverability follow-up)

#### Hermes — current focus
- Deliver health-status accessor plumbing for Opus's graded degrade wiring
- Startup verification support for `r_peak` once Opus lands the write path
- Periodic monitoring (auth 15min, DB 60min, heartbeat 60s)
- Structured event/log schema for future warning/escalation

---

### Current Critical Facts
- `TRAILING_STRUCT` wiring gap is resolved (commit 720b8f0)
- `r_peak` write-path truth is now defined by Opus (executor writes on increase, runtime JSON is crash-safe mirror, executor cache is live truth, cTrader feed is authoritative for existence, DB is fallback/reconstruction only)
- `r_peak` persistence is now waiting on implementation, not truth discovery
- DB health is elevated as a top structural risk (5.4 GB, extreme latency)
- Atomic writes delivered and integrated into key runtime state writers
- DB health + auth/token health checks integrated into scheduler startup
- Regime-break circuit breaker on LOOSEN_XAU_CANARY is functionally shipped; config-key discoverability may need follow-up
- Graded degrade policy is the intended direction for DB/auth health response (not warn-only forever), but not fully wired yet

---

### No-Overlap Rules
- Opus must not lead scheduler/config/DB/thread refactors
- Hermes must not lead confidence logic, execution truth, or live XAU behavior changes
- If a task changes live trading behavior, it belongs to **Opus**
- If a task improves safety, observability, or maintainability without changing trading behavior, it belongs to **Hermes**
- If a task affects both, split it into two steps and assign accordingly

---

### Immediate Coordination Order
#### NOW
- Opus: implement `r_peak` persistence path (truth defined, implementation pending)
- Opus: wire graded degrade policy into routing/execution
- Hermes: deliver health-status accessor plumbing (non-blocking, for Opus to consume)
- Hermes: periodic monitoring + structured event schema

#### NEXT
- Opus: commit explicit config keys for regime-break guard (config discoverability)
- Hermes: startup assertions / config safety scaffolding
- Hermes: r_peak startup verification support (once Opus lands write path)

#### LATER
- Opus: confidence-cap / high-confidence suppression via safe route
- Opus: causal audit of learning/ modules, fake-smart flagging
- Hermes: scheduler split, config hardening, DB archival, thread cleanup

---

### Important Constraint
Do not restart broad audits from scratch.
Use:
- `OPUS_BASELINE.md`
- `HERMES_BASELINE.md`
- `ACTION_BACKLOG.md`
as the working memory spine for all future sessions.

### Immediate Delta Update
- `TRAILING_STRUCT` execution wiring is resolved (commit 720b8f0).
- Hermes has integrated:
  - atomic writes into key runtime state writers (token_manager, hermes_loop, trading_manager_agent, live_profile_autopilot)
  - DB health check into scheduler startup
  - auth/token health check into scheduler startup
- `r_peak` write-path truth is now defined by Opus:
  - executor writes when r_peak increases
  - runtime JSON is the crash-safe mirror
  - executor cache is the live in-process truth
  - cTrader position feed is authoritative for open/closed existence
  - DB is fallback/reconstruction source only
- `r_peak` persistence is now waiting on **implementation**, not truth discovery.
- Graded degrade policy is the intended direction for DB/auth health response (not warn-only forever). Not fully wired yet.
- DB health remains an elevated production risk.
- Current baton:
  - **Opus:** implement r_peak persistence path + wire graded degrade policy
  - **Hermes:** deliver health accessor plumbing, periodic monitoring, startup verification, structured event schema
  - **Reviewer:** confirm no overlap, update priority order
- **2026-04-22 live delta (Codex):**
  - Commit `323342e` is live on the Oracle VM branch `deploy-xau-family-canary`.
  - XAU post-SL sweep logic now starts targeted cTrader reversal-zone capture in two stages: `armed` for early sweep/reclaim zones and `confirmed` for actual reversal confirmation.
  - New DB table `ctrader_reversal_capture_events` stores tagged capture metadata so dataset mining can ignore normal traffic and learn only reversal windows.
  - `learning/reversal_training_dataset.py` now exposes a reusable reversal-template fit scorer derived from the targeted dataset separators (`aligned_refill_shift`, `aligned_delta_proxy`, `bar_volume_proxy`, lower `rejection_ratio`).
  - `scanners/fibo_advance.py` now applies that reversal-template confirmation only in/near Fib `0.618/0.650` before allowing golden-pocket entries; current default is capture-aware but non-strict if live capture is missing.
  - Verification at rollout: local `py_compile`, reversal dataset tests, new Fib template tests, and full Fib regression suite all passed; VM service restarted `active`.
  - Residual VM warning after restart: `infra.auth_health` reports stale token age. Treat as an existing ops issue, not a regression from this bundle.



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
- **Database:** ctrader_openapi.db at 5.4 GB — slow I/O (300s timeouts on queries), no WAL companion files despite WAL mode reported, read-only open confirmed working (2026-04-20 diagnostic)
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
- r_peak persistence implementation is pending (truth defined, not yet wired)

### Resolved (fixed, verified in code)
- TRAILING_STRUCT enforcement wiring gap — FIXED in commit 720b8f0. The `elif` branch reading `sl_floor_r` and calling `amend_position_sltp` is present in ctrader_executor.py.

### Known Issues (confirmed)
- ctrader_openapi.db: 5.4 GB, extreme query latency (300s timeouts), no WAL companion files, read-only open works but RW operations may hang. Full diagnostic in docs/handoff/DB_VERIFICATION_PLAN.md.
- Atomic writes delivered for key writers but not yet applied to all 17 runtime JSON files
- Token last refreshed 142h+ (as of 2026-04-20) — health monitoring now catches this
- Scheduler has only 3 locks for 40+ shared state variables
- No structured logging, no correlation IDs, no heartbeat monitoring yet
- 923 getattr(config) calls vs 88 direct config.X — config system is distrusted
- r_peak implementation pending (truth defined by Opus, write path not yet wired)
- Graded degrade policy not yet wired (DB/auth health is warn-only for now)

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
- r_peak persistence — implement the write path (truth now defined)
- Graded degrade policy — wire DB/auth health into routing/execution risk multiplier

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

### P0 — Immediate (before next trading session)

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 0 | DB verification — ctrader_openapi.db (read-only diagnostic, backup-first) | Hermes | **DONE** | Health check module created, first diagnostic run. See DB_VERIFICATION_PLAN.md. |
| 1 | Atomic state writes — key runtime JSON writers | Hermes | **DONE** | utils/atomic_write.py created + integrated into token_manager, hermes_loop, trading_manager_agent, live_profile_autopilot. |
| 2 | r_peak persistence — implement write path | Opus | **TODO** | Truth defined: executor writes on increase, runtime JSON is mirror. Implementation pending. |
| 3 | Token/auth health monitoring | Hermes | **DONE** | infra/auth_health.py created, integrated into scheduler startup. |
| 4 | TRAILING_STRUCT enforcement | — | **DONE** | Fixed in commit 720b8f0. Verified in code. |
| 5 | Graded degrade policy — wire DB/auth health into routing | Opus | **TODO** | live_trading_allowed() and risk_multiplier() behavior. Depends on Hermes accessor plumbing. |
| 6 | Health accessor plumbing — non-blocking DB/auth status for execution path | Hermes | **TODO** | Thin accessor so Opus can wire degrade policy. |

### P1 — This Week

| # | Item | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 5 | Add opportunity suppression tracker | Hermes | TODO | Per-gate rejection logging |
| 6 | Add gate ROI attribution data collection | Hermes | TODO | Opus needs this to validate gates |
| 7 | Add startup config assertions | Hermes | TODO | Dangerous combo detection |
| 8 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO | Only after DB health verified |
| 9 | Causal audit of all learning/ modules | Opus | TODO | Classify real vs fake-smart |
| 10 | Verify r_peak reconstruction from history | Opus | TODO | Fallback if persistence fails |
| 11 | Add scheduler heartbeat monitoring | Hermes | TODO | Detect hung scheduler |

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

1. **ctrader_openapi.db has extreme latency.** 5.4 GB, queries time out at 300s, no WAL companion files despite WAL mode being reported. Read-only open confirmed working but RW operations may hang. This is the #1 production risk. Full diagnostic plan in DB_VERIFICATION_PLAN.md.

2. **r_peak does not survive restart.** Confirmed absent from trading_manager_state.json. Winner-protection and emergency logic are unreliable after every restart until new trade data arrives.

3. **No atomic writes.** A crash or power loss during state persistence corrupts JSON files. No recovery mechanism exists. 17 runtime JSON files are vulnerable.

4. **Confidence is synthetic.** High-confidence blocking exists in important paths, but confidence construction may not be calibrated to actual win rates.

5. **Scheduler has no heartbeat.** If the main loop hangs (e.g., blocked on DB I/O), the system appears "running" but is not scanning.

6. **TRAILING_STRUCT — RESOLVED.** Fixed in commit 720b8f0. The wiring gap is closed. Hermes should add enforcement telemetry for ongoing verification.

---

## Next Questions to Each Expert

### To Opus
1. Which learning/ modules are confirmed causal vs decorative? Need a definitive list.
2. Is r_peak actually persisted anywhere during trade lifecycle? If not, can it be reconstructed from ctrader_openapi.db?
3. What is the actual confidence construction formula? Is it calibrated to outcomes?
4. Which gates in the signal routing pipeline have proven ROI? Which don't?
5. TRAILING_STRUCT is fixed (commit 720b8f0). Verify the fix is complete from your perspective.

### To Hermes
1. Is ctrader_openapi.db recoverable? What is causing the extreme latency? See DB_VERIFICATION_PLAN.md.
2. Are atomic writes in place for all runtime JSON state files?
3. Is the scheduler heartbeat monitoring active?
4. Has the DB archival plan been executed? What's the current backup status?
5. Is structured logging deployed? Can we trace a signal lifecycle end-to-end?

### To Reviewer/Integrator
1. Are Opus and Hermes outputs sequenced safely? Any conflicts?
2. Is the action backlog up to date?
3. Are there any P0 items that have been sitting too long?
4. Has the trading mission been preserved in all proposed changes?

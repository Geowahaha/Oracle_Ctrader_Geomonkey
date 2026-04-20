# Multi-Expert Handoff & Coordination System

> **Purpose:** Enable Opus, Hermes, and reviewer/integrator to work continuously
> without losing context, overlapping, or drifting from the live-trading mission.

---

## Project Mission

Dexter Pro is a fully autonomous multi-strategy AI trading system.
It trades XAUUSD (primary), BTCUSD, ETHUSD via cTrader OpenAPI and MT5.

**Core trading mission:**
- Let profits run in trend
- Secure profit efficiently in sideways/chop
- Do not over-block good trades
- Minimize losses, maximize compounded returns

---

## Current Live Status

| Area | Status |
|------|--------|
| Architecture | Mostly sound. Execution wiring and reliability have gaps. |
| Live trading | Active on `deploy-xau-family-canary` branch. |
| DB health | `ctrader_openapi.db` is 5.4 GB. I/O errors observed during review (2026-04-20). |
| Scheduler | 13,679-line god class. Functional but unmaintainable. |
| Config | 1,903 attributes, 1,864 unique env vars. validate() checks only 3. |
| Thread model | 3 locks for 13.7K lines with 35+ scheduled jobs. |
| r_peak persistence | **UNVERIFIED** — must survive restart or winner-protection is unreliable. |
| TRAILING_STRUCT | **WIRING GAP** — intent exists, enforcement not fully realized at caller level. |
| Confidence logic | Partially fixed. Still synthetic/weakly calibrated in some paths. |
| Fake-smart risk | Several modules may be decorative/non-causal. Opus verdict pending. |

---

## Confirmed Truths (Baseline)

These are established facts. Do not re-audit unless contradicted by new code evidence.

### Strong parts (real intelligence on the trading path)
- NeuralBrain: genuine outcome-linked learning component
- HermesLoop: real reinforcement-style modifier loop
- V4 WinnerProtection: directionally correct architecture
- Entry sharpness: substantial feature engineering (8 microstructure features)
- Active position defense: real-time adverse flow detection
- Multi-agent conductor: genuine multi-agent orchestration

### Weak parts (need verification or cleanup)
- Decorative AI/library prior logic — not on live decision path
- LLM/research paths not on live trading decision path
- Auto-calibration paths that may not persist or materially affect live outcomes
- Gate stacks that look sophisticated but lack per-gate live ROI proof
- Confidence construction still synthetic in some paths
- High-confidence blocking still exists in important paths

---

## Ownership Split

### Opus — Execution Truth & Live Trading Behavior

**Owns:**
- Execution wiring: does the system actually place trades correctly?
- Confidence logic: are confidence adjustments real and calibrated?
- Fake-smart detection: which modules add no causal value?
- Opportunity capture vs over-blocking: are strong trades being suppressed?
- Winner protection / active defense / r_peak persistence
- Live policy behavior: does V4 policy layer actually affect live outcomes?
- TRAILING_STRUCT enforcement verification

**Does NOT own:**
- Refactoring decisions (Hermes)
- Config architecture (Hermes)
- Logging infrastructure (Hermes)
- Scheduler decomposition (Hermes)
- DB archival (Hermes)
- Thread model (Hermes)

### Hermes — Architecture, Infrastructure & Observability

**Owns:**
- Refactor map: how to restructure without breaking live trading
- Config hardening: prevent silent misconfiguration
- Logging / monitoring / observability: make everything visible
- Scheduler split: decompose the 13.7K-line god class
- DB archival / retention / backup safety
- Thread model cleanup / shared-state safety
- Production maintainability

**Does NOT own:**
- Evaluating whether a confidence adjustment is "real" (Opus)
- Deciding which gates add value (Opus)
- Fixing execution wiring gaps (Opus)
- Modifying trading logic (Opus)
- Fake-smart module evaluation (Opus)

### Reviewer / Integrator — Coordination & Quality

**Owns:**
- Verifying no-overlap between Opus and Hermes work
- Sequencing: ensuring Opus fixes land before Hermes refactors depend on them
- Quality gate: all changes must pass before merging to live branch
- Conflict resolution when Opus and Hermes recommendations intersect
- Final sign-off on any change that touches live execution paths

---

## No-Overlap Rules

```
RULE 1: Opus identifies the problem. Hermes builds the instrumentation.
        Opus proposes the fix. Hermes builds the infrastructure to deploy it.

RULE 2: Hermes never evaluates whether a module is "real" or "fake-smart."
        That is Opus's domain. Hermes acts on Opus's verdict.

RULE 3: Opus never proposes refactoring or restructuring.
        That is Hermes's domain. Opus flags modules for Hermes to act on.

RULE 4: If either expert's work touches the same file, the reviewer/integrator
        must sequence: Opus fix first, Hermes refactor second.

RULE 5: Both experts preserve the trading mission:
        - let profits run in trend
        - secure profit efficiently in sideways/chop
        - do not over-block good trades
```

---

## Current Priorities

### P0 — Immediate (before any refactoring)

| # | Item | Owner | Status |
|---|------|-------|--------|
| 1 | Verify `ctrader_openapi.db` health (reads AND writes) | Hermes | TODO |
| 2 | Add atomic_json_write to `trading_manager_state.json` | Hermes | TODO |
| 3 | Verify r_peak persistence across restart | Opus | TODO |
| 4 | Add r_peak startup verification + alert | Hermes | TODO |
| 5 | Verify TRAILING_STRUCT enforcement at caller level | Opus | TODO |
| 6 | Add TRAILING_STRUCT enforcement visibility logging | Hermes | TODO |
| 7 | Add token/auth refresh health monitoring | Hermes | TODO |

### P1 — This week

| # | Item | Owner | Status |
|---|------|-------|--------|
| 8 | Identify fake-smart modules (causal audit) | Opus | TODO |
| 9 | Add opportunity suppression tracker | Hermes | TODO |
| 10 | Add gate ROI attribution data collection | Hermes | TODO |
| 11 | Add startup config assertions (Opus-specific) | Hermes | TODO |
| 12 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO |
| 13 | Add structured logging (structlog) | Hermes | TODO |

### P2 — Weeks 2-4

| # | Item | Owner | Status |
|---|------|-------|--------|
| 14 | Extract report generators from scheduler.py | Hermes | TODO |
| 15 | Implement DB archival (backup first, retention later) | Hermes | TODO |
| 16 | Add rejection funnel dashboard | Hermes | TODO |
| 17 | Fix confidence logic gaps (per Opus findings) | Opus | TODO |
| 18 | Extract family builders from scheduler.py | Hermes | TODO |
| 19 | Extract guard logic from scheduler.py | Hermes | TODO |

### P3 — Weeks 5+ (after Opus completes live-trading audit)

| # | Item | Owner | Status |
|---|------|-------|--------|
| 20 | Extract routing logic from scheduler.py | Hermes | TODO |
| 21 | Decompose live_profile_autopilot.py (if causal) | Hermes | TODO |
| 22 | Decompose ctrader_executor.py (separate DB from execution) | Hermes | TODO |
| 23 | Migrate config to Pydantic validation | Hermes | TODO |
| 24 | Remove fake-smart modules (per Opus verdict) | Opus | TODO |

---

## Open Risks

| Risk | Severity | Owner |
|------|----------|-------|
| `ctrader_openapi.db` I/O errors may indicate corruption | P0 | Hermes |
| r_peak may not survive restart — winner-protection unreliable | P0 | Opus |
| TRAILING_STRUCT intent not enforced at execution level | P0 | Opus |
| Scheduler single-thread failure cascades to all families | P1 | Hermes |
| Config typos silently fall back to defaults (923 getattr calls) | P1 | Hermes |
| No per-gate ROI proof — gates may be over-blocking | P1 | Opus |
| Token/auth refresh failure can silently stop all trading | P1 | Hermes |
| Confidence construction still synthetic in some paths | P2 | Opus |
| 5.4 GB DB approaching SQLite practical limits | P2 | Hermes |
| Thread model has insufficient synchronization | P2 | Hermes |

---

## Next Questions to Each Expert

### To Opus:
1. Which modules are confirmed fake-smart vs real? (causal audit)
2. Does r_peak actually persist across restart? What's the current mechanism?
3. Is TRAILING_STRUCT enforced at the MT5/cTrader execution level, or only in intent?
4. Which gates in the signal routing pipeline have proven ROI? Which don't?
5. Is the confidence construction in `_apply_neural_soft_adjustment` calibrated or synthetic?
6. Does the V4 policy layer actually change live behavior, or is it aspirational?

### To Hermes:
1. Is `ctrader_openapi.db` healthy? Can it read AND write?
2. Are all runtime state files written atomically?
3. What's the current scheduler heartbeat status?
4. Which scheduled jobs actually contribute to live trading vs. are decorative?
5. What's the current DB growth rate? When will it hit 10 GB?

### To Reviewer/Integrator:
1. Are Opus and Hermes workstreams properly sequenced?
2. Has any change accidentally overlapped between the two?
3. Are all changes tested before merging to the live branch?
4. Is the trading mission preserved in all proposed changes?

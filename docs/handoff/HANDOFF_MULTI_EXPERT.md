# Multi-Expert Handoff & Coordination

**Created:** 2026-04-20
**Branch:** `deploy-xau-family-canary`
**Remote:** `dexter` → `https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git`
**Owner:** mrgeo | Bangkok (UTC+7)

---

## Project Mission

Dexter Pro is a fully autonomous multi-strategy AI trading system executing real money on cTrader and MT5. Trading XAUUSD (primary), BTCUSD, ETHUSD.

**Core directive:**
- Let profits run in trend
- Secure profit efficiently in sideways/chop
- Do not over-block good trades
- Architecture is mostly sound, but execution/live wiring and reliability have important gaps

---

## Current Live Status

- **Branch:** `deploy-xau-family-canary` — active development, deployed to Oracle VM
- **VM path:** `/opt/dexter_pro`, service: `dexter-monitor` via systemd
- **Local PC:** safety-only mode (`CTRADER_DRY_RUN=1`, `MT5_DRY_RUN=1`), local monitor stopped
- **cTrader auth:** last known issue — invalid/revoked token behavior. Must recheck with fresh tokens before assuming broker-synced.
- **DB health:** `ctrader_openapi.db` is 5.4 GB, returning I/O errors on read during last review session. Needs immediate triage.
- **Test suite:** 60 test files, ~29,500 lines. Last known passing: `31 passed, 1 warning` (pre-deploy local).

---

## Confirmed Truths

### Strong parts (real intelligence, protect these)
- NeuralBrain — genuine outcome-linked learning via SQLite-backed online model
- HermesLoop — real reinforcement-style modifier loop
- V4 WinnerProtection — directionally correct architecture
- Entry Sharpness Score — 8 microstructure features, substantial feature engineering
- Active position defense — real-time adverse flow detection, dynamic stop tightening
- Canary system — low-risk probing before family promotion
- Multi-agent conductor — Risk/Perf/Regime agents that debate and ensemble

### Weak / fake-smart parts (identified, pending full validation)
- Decorative AI/library prior logic — looks sophisticated, not on live decision path
- LLM/research paths — not on live trading decision path
- Auto-calibration paths — may not persist or materially affect live outcomes
- Gate stacks — look sophisticated but not validated by per-gate outcome attribution

### Critical wiring gaps
- TRAILING_STRUCT: structural trailing intent exists, enforcement not fully realized at caller level
- r_peak persistence: must survive restart or winner-protection becomes unreliable
- Entry confidence: partially fixed, still synthetic/weakly calibrated in some paths
- High-confidence blocking: still exists in important paths, may suppress opportunity

---

## Ownership Split

### Opus 4.7 — Execution Truth & Live Behavior

**Owns:**
- Execution truth — does the system actually place trades correctly?
- Confidence logic — are confidence adjustments real or synthetic?
- Fake-smart detection — which modules add no causal value?
- Opportunity capture vs over-blocking — are strong trades being suppressed?
- Live policy behavior — does V4 policy actually affect live trades?
- Winner protection / active defense — is r_peak real? Is trailing enforced?
- Per-gate ROI proof — which gates add value, which just block?

**Does NOT own:**
- Scheduler decomposition
- Config restructuring
- Logging infrastructure
- DB archival strategy
- Thread safety refactors

### Hermes — Architecture, Infrastructure & Observability

**Owns:**
- Refactor map — how to restructure without breaking live trading
- Config hardening — preventing silent misconfiguration
- Logging / monitoring / observability — making problems visible
- Scheduler split — decomposing the 13.7K-line god class
- DB archival / retention / backup safety
- Thread model cleanup / shared-state safety
- Production maintainability

**Does NOT own:**
- Evaluating whether confidence logic is "real"
- Deciding which modules are fake-smart
- Modifying execution wiring
- Changing trading thresholds or risk parameters
- Any live-trading behavior changes (unless explicitly approved)

### Reviewer / Integrator — Coordination & Verification

**Owns:**
- Verifying no-overlap between Opus and Hermes work
- Sequencing changes so live trading is never destabilized
- Running test suites after each change
- Committing and deploying approved changes
- Maintaining the action backlog
- Flagging when two experts' work conflicts

---

## No-Overlap Rules

1. **Opus finds the problem. Hermes builds the dashboard that shows the problem is still happening.**
2. **Opus proposes a fix. Hermes designs the infrastructure to support that fix safely.**
3. **Hermes never evaluates whether a confidence adjustment is "real" — that's Opus.**
4. **Opus never proposes scheduler decomposition or config restructuring — that's Hermes.**
5. **If both experts want to touch the same file, Reviewer decides who goes first.**
6. **No expert modifies live trading logic without explicit owner approval.**

### Weekly Sync Protocol
1. Opus identifies a live-trading issue
2. Hermes adds logging/metrics/alerts to make that issue continuously visible
3. Opus proposes a fix
4. Hermes designs the refactor/infrastructure to support that fix
5. Reviewer sequences the work, runs tests, approves deployment
6. Both verify via the observability infrastructure Hermes built

---

## Current Priorities

### P0 — Immediate (this week)

| # | Task | Owner | Status | Depends On |
|---|------|-------|--------|------------|
| 1 | Triage `ctrader_openapi.db` — health check reads+writes | Hermes | TODO | — |
| 2 | Add atomic JSON write to `trading_manager_state.json` | Hermes | TODO | — |
| 3 | Add r_peak persistence verification at startup | Hermes | TODO | #2 |
| 4 | Add TRAILING_STRUCT enforcement visibility logging | Hermes | TODO | — |
| 5 | Add token/auth refresh health monitoring | Hermes | TODO | — |
| 6 | Verify TRAILING_STRUCT wiring — is enforcement actually missing? | Opus | TODO | — |
| 7 | Verify r_peak — is it persisted? What happens after restart? | Opus | TODO | — |
| 8 | Audit confidence construction — which paths are synthetic? | Opus | TODO | — |

### P1 — Short-term (next 2 weeks)

| # | Task | Owner | Status | Depends On |
|---|------|-------|--------|------------|
| 9 | Add opportunity suppression tracker (per-gate rejection logging) | Hermes | TODO | — |
| 10 | Add gate ROI attribution data collection | Hermes | TODO | — |
| 11 | Add startup config assertions (r_peak, trailing, suppression risk) | Hermes | TODO | #3 |
| 12 | Enable WAL mode on ctrader_openapi.db | Hermes | TODO | #1 |
| 13 | Identify which gates are validated by outcome attribution | Opus | TODO | #10 |
| 14 | Identify which "smart" modules are non-causal | Opus | TODO | — |
| 15 | Validate opportunity suppression — which gates block good trades? | Opus | TODO | #9 |

### P2 — Medium-term (weeks 3-6)

| # | Task | Owner | Status | Depends On |
|---|------|-------|--------|------------|
| 16 | Extract report generators from scheduler.py | Hermes | TODO | — |
| 17 | Set up structured logging (structlog) | Hermes | TODO | — |
| 18 | Add scheduler heartbeat monitoring | Hermes | TODO | — |
| 19 | Implement DB archival (backup first, retention later) | Hermes | TODO | #1, #12 |
| 20 | Extract family builders from scheduler.py | Hermes | TODO | #16 |
| 21 | Remove or deprioritize non-causal modules | Opus+Hermes | TODO | #14 |
| 22 | Fix confidence construction in flagged paths | Opus | TODO | #8 |

### P3 — Long-term (weeks 6+, after Opus completes live audit)

| # | Task | Owner | Status | Depends On |
|---|------|-------|--------|------------|
| 23 | Extract guard logic from scheduler.py | Hermes | TODO | #13 |
| 24 | Extract routing logic from scheduler.py | Hermes | TODO | #23 |
| 25 | Decompose ctrader_executor.py | Hermes | TODO | Opus sign-off |
| 26 | Evaluate live_profile_autopilot.py decomposition | Hermes | TODO | #14 |
| 27 | Migrate config to Pydantic validation | Hermes | TODO | — |
| 28 | Separate read/write DB connections | Hermes | TODO | #19 |

---

## Open Risks

| Risk | Severity | Owner | Notes |
|------|----------|-------|-------|
| `ctrader_openapi.db` I/O errors — may already be failing silently | P0 | Hermes | 5.4 GB, observed I/O error during review |
| r_peak not verified as persisted — winner-protection unreliable after restart | P0 | Opus | Must confirm before trusting emergency logic |
| TRAILING_STRUCT not verified as enforced — structural trailing may be decorative | P0 | Opus | Intent exists, caller-level enforcement uncertain |
| cTrader auth may be revoked — VM broker sync status unknown | P0 | Reviewer | Last known: invalid token errors |
| Confidence construction synthetic in some paths — blocks may be false positives | P1 | Opus | Partially fixed, some paths still weak |
| Opportunity suppression from over-layered gates — no per-gate ROI proof | P1 | Opus | Gate stacks not validated by outcome attribution |
| Scheduler god class — single point of failure for all subsystems | P1 | Hermes | 13,779 lines, 1 class, 3 locks |
| Thread model — atomic writes missing, state corruption risk | P1 | Hermes | JSON state files, no atomic writes |
| Fake-smart modules consuming resources but not affecting live outcomes | P2 | Opus | Decorative AI, LLM paths, auto-calibration |
| DB growth — no archival policy, will hit performance wall | P2 | Hermes | Currently 5.4 GB |

---

## Next Questions to Each Expert

### To Opus
1. Is r_peak actually persisted in `data/runtime/trading_manager_state.json`? If not, what breaks after restart?
2. Is TRAILING_STRUCT actually enforced at the execution caller level, or is it decorative intent?
3. Which specific confidence paths are still synthetic? Give file:line references.
4. Which gates in the signal routing pipeline have proven ROI? Which don't?
5. Which modules in `learning/` are non-causal (decorative AI, LLM paths, auto-calibration that doesn't persist)?

### To Hermes
1. Is `ctrader_openapi.db` actually corrupted, or just slow? Run the health check.
2. Can atomic JSON writes be applied to all `data/runtime/*.json` files without changing behavior?
3. What's the smallest useful first extraction from scheduler.py?
4. Is WAL mode already enabled on any of the SQLite databases?
5. What's the estimated time for each phase of the scheduler split?

### To Reviewer
1. Is the VM currently running and broker-synced? When was last successful auth?
2. Are the current test suites passing on the deployed branch?
3. What's the deployment process for rolling back if a refactor breaks something?
4. Is there a staging environment, or is `deploy-xau-family-canary` the only path to production?

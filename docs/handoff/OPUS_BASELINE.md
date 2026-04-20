# Opus 4.7 — Baseline Findings

> Last updated: 2026-04-20
> Source: Opus 4.7 deep live-trading audit
> Purpose: Compact reference for Opus's confirmed findings. Do not re-state as opinions.

---

## Execution Truth

**What works:**
- cTrader OpenAPI order placement is wired correctly for primary paths
- MT5 execution via RPyC bridge functions (when connection is healthy)
- Risk guards (max positions, max USD at risk) fire before order submission
- Source profile gating prevents unauthorized family/symbol combinations

**What has gaps:**
- TRAILING_STRUCT: structural trailing intent exists in code but enforcement is not fully realized at the caller level. Positions may not receive trailing updates that the design intended.
- Token/auth refresh: failure handling is too weak. A refresh failure can interrupt trading with no automatic recovery.
- Some execution paths have silent failure modes — orders fail but no alert fires.

---

## Confidence Logic

**Status: Partially fixed, still weak.**

- Historical bugs in confidence calculation were fixed in prior sessions
- Confidence construction is still synthetic — not calibrated to actual win rates
- Some confidence adjustments are statistically grounded (winner logic bonuses/penalties)
- Some confidence adjustments are hardcoded magic numbers (+2.0, -2.0, -4.5) without statistical justification
- High-confidence blocking exists in important paths — signals with confidence below threshold are rejected even when they might be profitable
- The neural gate learning loop adjusts confidence policy, but it's unclear if the adjustments materially affect live outcomes

**Bottom line:** Confidence is a number that looks meaningful but may not correlate with actual probability of profit. It gates real money decisions.

---

## Fake-Smart Findings

Modules/features that appear sophisticated but may not add causal value on the live trading path:

| Module/Feature | Assessment |
|---|---|
| Decorative AI/library prior logic | Not on live decision path. Looks smart, does nothing. |
| LLM/research paths | Not on live trading decision path. Informational only. |
| Auto-calibration paths | May not persist or may not materially affect live outcomes. Needs verification. |
| Gate stacks (some) | Not validated by per-gate live ROI attribution. May block more than they help. |
| Some confidence adjustments | Synthetic — not derived from outcome data. |

**Important:** "Fake-smart" does not mean "bad code." It means the code exists, runs, looks complex, but does not causally affect whether trades win or lose. These modules should be flagged for removal or deprioritization, not refactored into cleaner versions.

---

## Critical Live-Risk Issues

### 1. r_peak Persistence (CRITICAL)

- `r_peak` is used by winner-protection and emergency logic
- If r_peak is not persisted to `trading_manager_state.json`, it is lost on restart
- After restart, winner-protection and emergency logic use cold-start defaults
- This makes the system unreliable after every restart until new trade data arrives

**Required:** Verify r_peak is written to trading_manager_state.json. Verify it is read at startup. Verify it can be reconstructed from trade history if missing.

### 2. TRAILING_STRUCT Enforcement Gap (CRITICAL)

- Structural trailing stop logic exists in the codebase
- The intent is to dynamically adjust stops based on market structure
- At the caller level, this trailing is not fully enforced
- Result: positions may not be defended as the design intended

**Required:** Trace the trailing intent from definition to execution. Find where the chain breaks. Close the gap.

### 3. Confidence Blocking on Strong Trades (HIGH)

- High-confidence thresholds block signals that might be profitable
- Confidence is not calibrated to actual win rates
- The system may be suppressing opportunity in trending markets

**Required:** Build per-gate ROI attribution. Prove which gates add value. Reduce or remove gates that don't.

### 4. Silent Failure Modes (HIGH)

- Order failures may not trigger alerts
- Token refresh failures may not trigger recovery
- DB write failures may not be detected
- Scheduler hangs may not be detected

**Required:** Every failure path needs an alert. Every critical operation needs a heartbeat.

---

## Top Priority Fixes

Ordered by live-trading impact:

```
FIX #1: Verify and fix r_peak persistence
  WHY: Winner-protection is unreliable without it
  HOW: Check trading_manager_state.json writes. Add startup verification.
  OWNER: Opus verifies. Hermes builds infrastructure.

FIX #2: Close TRAILING_STRUCT wiring gap
  WHY: Positions not defended as designed
  HOW: Trace trailing intent to execution. Add missing caller-level enforcement.
  OWNER: Opus identifies gap. Hermes adds enforcement telemetry.

FIX #3: Add per-gate ROI attribution
  WHY: Can't validate gates without outcome data
  HOW: Log every gate decision. Correlate with trade outcomes. Report per-gate win rate.
  OWNER: Hermes builds data collection. Opus interprets results.

FIX #4: Harden token/auth refresh
  WHY: Trading interruption risk
  HOW: Add retry logic, expiry alerts, refresh failure recovery.
  OWNER: Hermes builds monitoring. Opus verifies execution path.

FIX #5: Add atomic state writes
  WHY: Crash = corrupted state = unreliable restart
  HOW: Write to .tmp, rename atomically. Verify on read.
  OWNER: Hermes.

FIX #6: Add scheduler heartbeat
  WHY: Hung scheduler = no scans = missed trades
  HOW: Heartbeat counter in main loop. Alert if no heartbeat in 60s.
  OWNER: Hermes.
```

---

## Notes for Hermes

- Do not refactor modules Opus identifies as fake-smart — flag them for removal
- Do not adjust confidence thresholds — that's Opus's domain
- Build observability for every issue Opus identifies
- Prefer making problems visible over fixing them directly
- Every Opus finding should have a corresponding Hermes telemetry/alert

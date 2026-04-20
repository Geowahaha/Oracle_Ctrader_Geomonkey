# Opus 4.7 — Baseline Findings

> **Last updated:** 2026-04-20
> **Status:** Active baseline. Do not re-audit unless contradicted by new evidence.

---

## Execution Truth

**The system has real execution infrastructure** — cTrader OpenAPI subprocess, MT5 RPyC bridge, dual-broker support. But wiring gaps exist between design intent and actual live behavior.

### Confirmed strong:
- cTrader execution subprocess is properly isolated (Twisted lifecycle separate from main process)
- MT5 executor has broker-aware symbol resolution and position limits
- Price sanity guards exist (deviation checks per symbol class)
- Market entry drift guard exists

### Confirmed gaps:
- TRAILING_STRUCT: structural trailing intent exists but enforcement is not fully realized at the caller level. The position manager has trailing logic, but callers don't consistently invoke it.
- Execution journal writes to a 5.4 GB database that is returning I/O errors — live trade records may be failing silently.
- Token/auth refresh failure handling is too weak — a refresh failure can silently stop all trading.

---

## Confidence Logic Status

**Partially fixed.** Historical bugs were addressed, but confidence construction remains synthetic/weakly calibrated in important paths.

### Confirmed:
- NeuralBrain is a genuine outcome-linked learning component — it records trade outcomes and retrains
- `_apply_neural_soft_adjustment` applies neural gate adjustments to signal confidence
- Winner logic applies bonuses (+2.0) and penalties (-2.0/-4.5) based on historical session/band/side/symbol performance

### Concerns:
- Confidence adjustments use hardcoded magic numbers, not statistically derived values
- High-confidence blocking still exists in important paths — signals that might be profitable are rejected
- Confidence construction is synthetic: the composite score doesn't map to a calibrated probability of profit
- The neural gate policy may not be persisting correctly across restarts

---

## Fake-Smart Findings

Several modules appear sophisticated but may not add causal value to live trading decisions.

### Likely decorative / non-causal:
- **Decorative AI/library prior logic** — modules that use LLM or library priors but aren't on the live signal→execution decision path
- **LLM/research paths** — AI research or analysis that feeds reports but doesn't change which trades fire
- **Auto-calibration paths** — calibration loops that don't persist their adjustments or don't materially affect live outcomes
- **Gate stacks without ROI proof** — multiple gating layers that look sophisticated but aren't validated by outcome attribution per gate

### Likely real / causal:
- NeuralBrain (outcome-linked learning)
- HermesLoop (reinforcement-style modifier)
- V4 WinnerProtection (directionally correct)
- Entry sharpness scoring (8 microstructure features — substantial engineering)
- Active position defense (real-time adverse flow detection)
- TradingManagerAgent (xau_execution_directive, xau_cluster_loss_guard)

### Needs verification:
- `position_trailing_brain.py` — small (250 lines) but critical if TRAILING_STRUCT wiring gap originates here
- `strategy_evolution.py` — family promotion/demotion: is it actually wired to live family selection?
- `strategy_lab_team.py` — experimental family management: does it affect which families trade live?
- `adaptive_directional_intelligence.py` — ADI modifier: does it actually change signal direction?

---

## Critical Live-Risk Issues

### 1. r_peak Persistence (CRITICAL)

**Problem:** `r_peak` (peak R-multiple for winner protection) may not survive system restart.

**Impact:** Winner-protection and emergency logic become unreliable after restart. The system loses its memory of recent performance, which can cause:
- Over-trading after restart (no loss-streak awareness)
- Under-trading after restart (no win-streak awareness)
- Emergency guards not triggering when they should

**Fix required:** Verify r_peak is written to `trading_manager_state.json` and read back at startup. If not persisted, reconstruct from trade history.

### 2. TRAILING_STRUCT Enforcement Gap (CRITICAL)

**Problem:** Structural trailing stop intent exists in the design but is not fully enforced at the execution caller level.

**Impact:** Positions that should have trailing stops may not get them, or trailing stops may not move as intended. This means:
- Winners may not run as intended
- Profits may not be secured efficiently in trend
- The system's trailing behavior doesn't match its design

**Fix required:** Verify every position manager trailing call is actually invoked by its callers. Add enforcement checks.

### 3. Confidence Over-Blocking (HIGH)

**Problem:** High-confidence thresholds + multiple gate layers suppress potentially profitable trades.

**Impact:** The system may be blocking strong trades in trend conditions because:
- Confidence is synthetic (not calibrated to actual win probability)
- Gates compound: each gate rejects N% and they stack
- No per-gate ROI proof: we don't know which gates add value vs. just block

**Fix required:** Build per-gate outcome attribution. Identify which gates have positive ROI. Relax or remove gates with negative ROI.

### 4. Silent Failure Modes (HIGH)

**Problem:** Multiple failure modes produce no alert and no log:
- Token/auth refresh failures
- DB write failures (5.4 GB database returning I/O errors)
- r_peak loss after restart
- TRAILING_STRUCT non-enforcement
- Scheduler loop hangs (daemon thread, no watchdog)

**Fix required:** Instrumentation for all failure modes. Heartbeat monitoring. Startup state verification.

---

## Top Priority Fixes

| Priority | Fix | Depends On |
|----------|-----|------------|
| P0 | Verify and fix r_peak persistence across restart | Nothing — do now |
| P0 | Verify and fix TRAILING_STRUCT enforcement at execution level | Nothing — do now |
| P0 | Add token/auth refresh failure alerts | Nothing — do now |
| P1 | Build per-gate outcome attribution (which gates add value?) | Instrumentation (Hermes) |
| P1 | Identify and remove/deprioritize fake-smart modules | Causal audit |
| P1 | Fix confidence calibration in key paths | Gate attribution data |
| P2 | Reduce over-blocking in trend conditions | Gate attribution + confidence fix |
| P2 | Add startup state verification for all critical runtime data | Atomic writes (Hermes) |
| P2 | Verify V4 policy layer actually changes live behavior | Code trace + live log analysis |

---

## What Opus Needs From Hermes

| Need | Why |
|------|-----|
| Structured logging with signal correlation IDs | To trace a signal from scanner → gate → execution → outcome |
| Opportunity suppression tracker | To prove which gates are over-blocking |
| Gate ROI attribution data collection | To measure per-gate value |
| Atomic state writes | To prevent r_peak and other state loss on crash |
| Startup state verification | To alert when r_peak or execution directive is missing |
| TRAILING_STRUCT enforcement visibility | To see the gap between intended and actual trailing |
| Token/auth health monitoring | To catch refresh failures before they stop trading |
| Scheduler heartbeat | To detect when the main loop hangs |

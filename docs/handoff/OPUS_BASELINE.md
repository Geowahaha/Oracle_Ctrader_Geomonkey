# Opus 4.7 Baseline

**Last updated:** 2026-04-20
**Source:** Opus 4.7 multi-expert review session
**Branch:** `deploy-xau-family-canary`

---

## Execution Truth

### What actually works
- Signal generation pipeline fires and routes through gates
- cTrader OpenAPI subprocess execution model is architecturally sound
- MT5 RPyC bridge execution is architecturally sound
- Position manager actively defends open trades (adverse flow detection, dynamic stop tightening)
- Canary system probes experimental families with minimal risk before promotion

### What has gaps
- **TRAILING_STRUCT wiring gap:** Structural trailing intent exists in the design but enforcement was not fully realized at the caller level. The trailing logic may be decorative at some execution paths.
- **cTrader auth reliability:** Token refresh failure handling is too weak. Auth failures cause silent execution stoppage — the system appears "running" but places no trades.
- **Atomic state writes missing:** Runtime JSON state files (`trading_manager_state.json`, etc.) are written with standard open+write — if the process crashes mid-write, the file corrupts. No atomic write pattern (write-to-temp-then-rename).

---

## Confidence Logic Status

### Fixed
- Historical bugs in confidence calculation were identified and patched
- Some confidence paths now use real outcome-linked adjustments

### Still weak
- **Confidence construction is still synthetic / weakly calibrated** in several paths
- High-confidence blocking still exists in important paths — may suppress strong trades
- Confidence adjustments from winner logic use hardcoded magic numbers (+2.0 bonus, -2.0/-4.5 penalty) rather than statistically derived values
- **No per-gate live ROI proof** — gates are layered without evidence that each gate individually improves outcomes

### Critical question
> "Is this confidence adjustment actually affecting live trades, or is it decorative?"
>
> Answer: Some adjustments are real (NeuralBrain outcome linking). Some are synthetic (magic number bonuses). The boundary between them is not clearly mapped.

---

## Fake-Smart Findings

### Confirmed real intelligence (protect these)
- `learning/neural_brain.py` — genuine outcome-linked learning, SQLite-backed online model
- `learning/hermes_loop.py` — real reinforcement-style modifier loop
- `learning/live_profile_autopilot.py` — **mixed**: winner logic and chart state memory are real; some sub-modules may be decorative
- `learning/trading_manager_agent.py` — real XAU orchestration (execution directive, cluster loss guard, micro regime)
- `analysis/entry_sharpness.py` — substantial feature engineering (8 microstructure features)
- `execution/` — active position defense is real

### Suspected decorative / non-causal (pending full validation)
- Decorative AI/library prior logic — looks sophisticated, not on live decision path
- LLM/research paths — not on live trading decision path
- Auto-calibration paths — may not persist or materially affect live outcomes
- Gate stacks that look sophisticated but are not validated by outcome attribution
- Some sub-modules within `live_profile_autopilot.py` (need surgical audit, not blanket refactor)

### Key principle from Opus
> "Do not praise complexity just because it looks advanced. Separate real intelligence from fake-smart complexity. Prefer live causal value over elegance."

---

## Critical Live-Risk Issues

### P0 — Must verify before trusting the system

1. **r_peak persistence**
   - Winner-protection and emergency logic depend on `r_peak` (peak profit level)
   - If `r_peak` is not persisted in `data/runtime/trading_manager_state.json`, it resets to zero on every restart
   - Effect: winner-protection becomes unreliable after any restart. Emergency logic may not trigger.
   - Action: verify persistence. If missing, reconstruct from trade history on startup.

2. **TRAILING_STRUCT enforcement**
   - Structural trailing intent exists in design
   - Enforcement at the execution caller level is uncertain
   - Effect: positions may not be trailed as intended, giving back profits
   - Action: verify caller-level enforcement. Add visibility logging.

3. **Token/auth refresh**
   - cTrader OpenAPI token refresh failure handling is too weak
   - Effect: system appears running but places no trades. Silent failure.
   - Action: add health monitoring, alert on refresh failure, alert on approaching expiry.

4. **Atomic state writes**
   - Runtime JSON state files have no atomic write pattern
   - Effect: crash during write corrupts state file. Recovery is manual.
   - Action: implement write-to-temp-then-rename pattern.

### P1 — Opportunity suppression risk

5. **Over-layered gates without per-gate ROI proof**
   - Signal routing has 8+ gates (session, MTF, regime, confidence, sharpness, direction, source profile, family)
   - Each gate rejects signals, but no evidence that each gate individually improves outcomes
   - Effect: strong trades may be blocked by gates that add no value
   - Action: collect per-gate rejection data + outcome attribution. Identify which gates block winners.

6. **High-confidence blocking**
   - Some paths block trades that don't meet high confidence thresholds
   - Confidence construction is partially synthetic
   - Effect: system may reject trades that would have been profitable
   - Action: audit which paths have high-confidence blocking. Validate with outcome data.

7. **Cutting winners too early**
   - Position manager has profit-retrace guards and TP trimming
   - If these are too aggressive, they cut winners in trending conditions
   - Effect: reduced profit capture in strong trends
   - Action: audit TP trimming behavior in trending vs ranging conditions.

---

## Top Priority Fixes (Opus-owned)

| Priority | Fix | Impact |
|----------|-----|--------|
| P0 | Verify and fix r_peak persistence | Winner-protection reliable after restart |
| P0 | Verify and fix TRAILING_STRUCT enforcement | Structural trailing actually works |
| P0 | Harden token/auth refresh + add alerts | No more silent auth failures |
| P0 | Add atomic writes to all runtime state JSON | No more state corruption on crash |
| P1 | Audit confidence construction paths | Know which are real vs synthetic |
| P1 | Collect per-gate ROI attribution data | Know which gates add value |
| P1 | Validate opportunity suppression | Stop blocking good trades |
| P2 | Identify and remove/deprecate non-causal modules | Reduce complexity, save resources |
| P2 | Fix confidence magic numbers → statistical values | Confidence adjustments are calibrated |

---

## What Opus Does NOT Own

- Scheduler decomposition (Hermes)
- Config restructuring (Hermes)
- Logging infrastructure (Hermes)
- DB archival strategy (Hermes)
- Thread safety refactors (Hermes)
- Any infrastructure that doesn't change trading behavior

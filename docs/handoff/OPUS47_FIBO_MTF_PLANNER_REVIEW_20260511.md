# OPUS 4.7 Review Packet — Fibo MTF Planner Shadow Wiring

Date: 2026-05-11
Owner split: Hermes implemented safety/observability foundation; Opus owns trading-logic/execution-truth approval before any live promotion.

## Incident evidence already established

Recent cTrader statement / DB triage found FIBO_MTF_SHADOW candidates had been promoted as live `fibo_xauusd` orders after opportunity-first unlock.

High-signal findings:
- FIBO_MTF_SHADOW closed deal links: 754
- PnL: -842.91 USD
- `ratio_zone='other'`: -810.44 USD
- `impulse_state='idle'`: all closed shadow rows, -842.91 USD
- Losses concentrated around confidence 20–40
- W1/D1/H4 generated impractical SL/TP distances
- Example bad geometry: W1 short, confidence 24.2, ratio other, impulse idle, correction_end false, entry 4668.22, SL 5656.86, TP 2690.95

Root cause class:
- Structural Fibo telemetry was being treated as broker-executable trade plan.
- `nearest_level_price` + high-TF `swing_start` produced absurd broker SL/TP.
- Opportunity-first unlock was interpreted as direct promotion, not route intelligence.

## Commits in current local branch

- `fb8b9a4 fix(fibo): block MTF shadow dispatch leak`
  - scheduler invariant: FIBO_MTF_SHADOW / `fibo_mtf_live_enabled=False` cannot dispatch live.

- `fc119e8 feat(fibo): add MTF trade planner routes`
  - pure planner routes: `observe_only`, `probe`, `base_live`, `runner_add`.
  - separates thesis stop from broker execution stop.
  - caps broker SL by ATR.

## New delta for this review

Changed files:
- `analysis/fibo_mtf_trade_planner.py`
- `scheduler.py`
- `tests/test_fibo_mtf_trade_planner.py`

### What changed

1. Added adapter `planner_input_from_signal(signal)`.
   - Converts existing FIBO_MTF_SHADOW `TradeSignal` into `FiboMtfPlannerInput`.
   - Treats old entry/SL/TP as telemetry, not live permission.
   - Reads optional local execution anchors:
     - `execution_swing_low`, `local_swing_low`, `recent_swing_low`
     - `execution_swing_high`, `local_swing_high`, `recent_swing_high`
   - Reads trigger metadata if present:
     - `reclaim_confirmed`, `reclaim_trigger`, `flow_reclaim_confirmed`
     - `sweep_confirmed`, `liquidity_sweep_confirmed`, `sweep_trigger`
     - `impulse_birth_confirmed`, `impulse_restart_confirmed`

2. Added `annotate_signal_with_fibo_mtf_plan(signal)`.
   - Writes route metadata into `raw_scores`:
     - `fibo_mtf_trade_planner`
     - `fibo_mtf_route`
     - `fibo_mtf_route_intent`
     - `fibo_mtf_route_reasons`
   - Forces:
     - `fibo_mtf_live_enabled=False`
     - `fibo_mtf_planner_shadow_only=True`
   - This is still shadow-only; no live behavior promotion.

3. Changed scheduler `_run_fibo_mtf_shadow_scan()`.
   - Removed direct live promotion path when `XAU_OPPORTUNITY_FIRST_LIVE_UNLOCK_ENABLED=1`.
   - Always annotates planner route.
   - Always stores to shadow journal with block reason `fibo_mtf_planner:<route>`.
   - Report now includes route counts.
   - `executed` remains 0.

## Verification run

Commands:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_fibo_mtf_trade_planner.py tests/test_fibo_mtf_shadow.py
PYTHONPATH=. .venv/bin/python -m py_compile analysis/fibo_mtf_trade_planner.py scheduler.py scanners/fibo_mtf_shadow.py
```

Result:
- 10 tests passed
- py_compile passed

## Reviewer question

Opus 4.7: review only; do not implement.

Please assess whether this is the correct safe next step after the Fibo MTF loss incident.

Required output:
1. APPROVE / BLOCK / NEEDS CHANGES for deploying this shadow-only planner wiring.
2. Exact trading-risk findings.
3. Whether `observe_only/probe/base_live/runner_add` route semantics are directionally correct for profit improvement without over-blocking.
4. Whether any extra metadata is required before a future micro-live promotion.
5. Minimal safe criteria to promote from shadow route metadata to micro-live probe/base route.
6. Any line-level issues in `analysis/fibo_mtf_trade_planner.py` or `scheduler.py`.

Important user priority:
- Do not over-block good opportunities.
- Stop fake-smart complexity.
- Let profits run in trend.
- Secure profit in chop.
- Preserve capital; target asymmetric R:R minimum 1:3 where appropriate.
- Stop bleeding/wrong-way-add belongs in PM/sizing/evidence upgrade, not blanket pre-entry blocker.

## Current stance from Hermes

Deploying this patch should not make live entries more profitable immediately because it intentionally keeps FIBO_MTF shadow-only. It makes the system smarter in the safe way: every candidate now gets route intelligence in shadow evidence, and the old leaking live promotion path is removed. Live profit improvement requires Opus-approved micro-live adapter after shadow route quality is verified.

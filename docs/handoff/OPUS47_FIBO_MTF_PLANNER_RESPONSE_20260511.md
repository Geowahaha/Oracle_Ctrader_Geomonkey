# OPUS 4.7 Response — Fibo MTF Planner Shadow Wiring

Date: 2026-05-11
Reviewer: Opus 4.7
Scope: review-only, no implementation

## Verdict

APPROVE for shadow-only deploy on `deploy-xau-family-canary`.

Opus rationale:
- Three-layer lock is sound:
  1. planner forces `fibo_mtf_live_enabled=False` and `fibo_mtf_planner_shadow_only=True`;
  2. scheduler removed opportunity-first direct live promotion and stores all Fibo MTF rows in shadow journal;
  3. scheduler invariant `_is_fibo_mtf_shadow_signal()` blocks any shadow-tagged Fibo signal if another path tries to dispatch it.
- The original incident class is structurally prevented: high-TF `nearest_level_price` + `swing_start` can no longer become broker SL/TP from this path.
- No live profit gain should be expected from this patch alone; that is intentional and correct.

## Trading-risk findings

- FIBO_MTF_SHADOW → `fibo_xauusd` live bleed lane is closed.
- HTF-only `ratio_zone=other` + `impulse_state=idle` + no trigger routes to `observe_only`.
- Broker SL geometry now requires local anchor and ATR bounds.
- Remaining live-promotion gaps, only relevant later:
  - live spread,
  - broker minimum stop distance,
  - fresh cTrader tick,
  - news guard,
  - session guard,
  - wrong-way/existing XAU position conflict,
  - per-route rolling stats.

## Route semantics

Approved directionally:
- `observe_only`: pure HTF/unconfirmed context.
- `probe`: small permissive lane when local trigger exists, even outside ideal ratio zone.
- `base_live`: golden/deep ratio + active impulse + correction_end + trigger + confidence.
- `runner_add`: winner-basket mature impulse add-on, not fresh full-size entry.

Opus caveat handled after review:
- Added code comment documenting runner_add branch priority.

## Required before future micro-live

Opus requires:
1. live broker context: fresh tick/current price, spread, min stop distance, commission/swap estimate;
2. conflict state: existing XAU direction/risk, pending count, per-family caps;
3. time/news/session gates;
4. real `winner_basket_aligned` and real regime source, not defaults;
5. rolling per-route win-rate, expectancy, MAE, cooldowns;
6. distinct non-shadow source token that does not include `fibo_mtf_shadow`;
7. single env kill switch.

## Minimal safe promotion criteria

Promote `probe` first only if all are true:
- ≥30 shadow probe decisions;
- ≥14 calendar days;
- ≥2 distinct sessions;
- planner-geometry win-rate ≥50%;
- expectancy ≥ +0.4R;
- no simulated MAE > 3.5R;
- ≥95% probe rows had real local execution anchors;
- first 10 trading days hard caps:
  - risk ≤ 0.30 USD,
  - max 1 probe / 4h,
  - max 2 / UTC day,
  - daily loss kill at -1.0 USD,
  - single live position only,
  - no base_live or runner_add activation.

## Line-level review items and fixes applied

Opus flagged:
- missing/zero `raw_stop_loss` could silently pass cap guard;
- `impulse_birth_confirmed` condition duplicated `has_trigger`;
- runner_add branch priority needed explicit intent comment;
- scheduler should expose `planner_errors`;
- CI/static assertion should prevent `_run_fibo_mtf_shadow_scan()` from reintroducing direct live promotion.

Hermes applied these fixes after review:
- missing raw stop + no local execution anchor now routes `observe_only` with `raw_stop_loss_missing` + `execution_anchor_missing`;
- removed duplicate `or candidate.impulse_birth_confirmed`;
- documented runner_add as winner-aligned add-on branch;
- added `planner_errors` report counter/log field;
- added `tests/test_fibo_mtf_scheduler_invariant.py` asserting no direct live promotion in the Fibo MTF shadow scheduler section.

## Final status

Opus approved shadow-only deploy after the above fixes. Micro-live remains blocked until route evidence gates are met.

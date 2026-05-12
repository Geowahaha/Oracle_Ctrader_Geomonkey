# Fibo MTF Evidence Design — Opus 4.7 + GPT 5.5

Date UTC: 2026-05-12
Scope: Prevent wasted shadow-collection days for Fibo MTF evidence after commit `bdc2178`.
Roles:
- Opus 4.7: execution-truth / risk reviewer, read-only.
- GPT 5.5 / Hermes: architecture, observability, deployment safety.

## Current Verified State

- VM is live on `deploy-xau-family-canary` at `bdc2178`.
- `dexter-monitor` is active.
- Focused VM tests passed: 20/20.
- Change is shadow-only; no Fibo MTF live dispatch.
- `xau_shadow_journal` has `shadow_mae_rr` and `shadow_mfe_rr`.
- Current report snapshot: decisions=577, resolved=198, calendar_days=2, expectancy_R=0.0911, max_mae_R=1.572, real_anchor_rate=0.0.
- Newest rows show `execution_anchor_source=unavailable`, `execution_anchor_is_live_plan=0`.

## Shared Conclusion

Leaving the VM running is operationally safe, but collecting many more days without fixing evidence quality can waste time.

The experiment must answer a precise question:

> Does the Fibo MTF planner produce better route quality than the current/baseline path, without increasing adverse excursion or relying on unavailable/biased anchors?

If the rows cannot tell us anchor availability, anchor drift, MAE/MFE path risk, and baseline delta, then calendar days alone are not useful evidence.

## Hypotheses

Primary hypothesis:
- MTF fibo route selection has better expectancy or better path quality than baseline/single-TF behavior.

Secondary hypothesis:
- Execution anchor snapshots are available and stable enough to be used as evidence.

Failure hypothesis:
- MTF looks good only because anchors are unavailable, include signal-bar lookahead, or MAE/MFE is truncated at first TP/SL.

## Metric Taxonomy

### Gate Metrics

Use these for future promotion decisions:

1. `expectancy_delta_R = expectancy_R_mtf - expectancy_R_baseline`
   - Target: >= +0.03 to +0.05 after enough sample.
2. `resolved_count`
   - Minimum before any serious review: 150.
   - Preferred before promotion: 500+ resolved rows across multiple sessions/days.
3. `anchor_snapshot_coverage`
   - Percent of rows with non-null pre-signal swing low/high and non-`unavailable` source.
   - 24h target: > 0.5.
   - Promotion target: >= 0.6 to 0.95 depending on how strict Opus gate is.
4. `max_mae_R` / tail MAE distribution
   - Must not be materially worse than baseline; rough limit <= baseline * 1.15.
5. `anchor_drift_R`
   - Median planner-vs-real/broker anchor drift should be small, target < 0.3R when real/broker anchor exists.

### Diagnostics

Useful for debugging, not enough for promotion by themselves:

- decisions count.
- calendar_days.
- sessions count.
- route distribution.
- timeframe distribution.
- same-bar TP/SL ambiguous rate.
- anchor source distribution.
- unresolved row count.

## Key Design Corrections

### P0 — Must Fix Before Counting More Days as Useful Evidence

1. Separate anchor coverage labels

Current `real_anchor_rate=0.0` is confusing because `execution_anchor_is_live_plan=False` is intentional. Add/report separate fields:

- `anchor_snapshot_coverage`: has pre-signal local swing low/high.
- `anchor_source_distribution`: unavailable/local_swing/context/etc.
- `live_plan_rate`: should remain 0 for shadow-only.

2. Fix pre-signal anchor window

Use the window before the signal bar, not including the signal bar:

```python
pre_signal = df.iloc[-(lookback + 1):-1]
```

This avoids lookahead bias where the trigger candle influences the anchor.

3. Make anchor unavailability actionable

If newest rows are mostly `execution_anchor_source=unavailable`, the report should fail a data-quality check within 24h. Do not wait 14 days for rows that cannot prove anchor behavior.

4. Report must prefer persisted MAE/MFE columns

The report should read `shadow_mae_rr` / `shadow_mfe_rr` first, then fallback to raw JSON only when null. Add `mfe_R` summary.

5. Add baseline comparison arm

Every Fibo MTF row should have a comparable baseline route/anchor/outcome so expectancy is a delta, not a floating absolute number.

Minimum fields:
- `baseline_entry` / `baseline_stop_loss` / `baseline_take_profit` if available.
- `baseline_shadow_pnl_rr` or equivalent calculated report-side if schema changes should be avoided.
- `mtf_vs_baseline_delta_R` in report.

### P1 — Important After P0

1. Same-bar TP/SL ambiguity flag

When TP and SL occur in the same coarse bar, mark `same_bar_ambiguous=True`. Prefer conservative handling for gate metrics or exclude ambiguous rows from promotion calculations.

2. MAE/MFE horizon clarity

Separate:
- `mae_pre_resolve_rr` / `mfe_pre_resolve_rr`: before TP/SL resolution.
- Optional future `mae_window_rr` / `mfe_window_rr`: fixed horizon path risk.

Do not mix these in the same metric name.

3. Daily gate report

A daily report should explicitly say:
- evidence usable / not usable.
- if not usable, exact blocker.
- whether the day counts toward the 14-day evidence window.

## Stop / Continue Rules

### After 24h

Continue only if:
- `anchor_snapshot_coverage > 0.5`, and
- MAE/MFE persisted values are being populated for newly resolved rows, and
- no live path references Fibo MTF planner routes.

Stop/fix if:
- coverage is still 0 or mostly unavailable.
- report cannot distinguish live-plan rate from anchor coverage.

### After 72h

Continue only if:
- resolved sample >= 60, and
- expectancy_R is not materially bad, roughly > -0.05, and
- max_mae_R is not exploding, rough stop > 2.5R unless explained by baseline too.

Stop/fix if:
- anchor coverage < 0.3.
- same-bar ambiguous rows dominate outcomes.
- MAE/MFE columns remain mostly null after resolution.

### After 14d

Consider promotion review only if:
- enough resolved rows exist, ideally 150 minimum and preferably 500+.
- expectancy_delta_R beats baseline.
- MAE/tail risk is acceptable.
- anchor coverage and drift pass thresholds.
- Opus reviews a separate promotion plan.

If not, close the experiment or redesign. Do not promote because time passed.

## Minimal Shadow-Only Implementation Plan

1. Patch scanner anchor window to pre-signal only.
2. Add tests proving signal bar high/low is excluded from anchor payload.
3. Patch report labels:
   - replace/augment `real_anchor_rate` with `anchor_snapshot_coverage` and `live_plan_rate`.
   - prefer persisted `shadow_mae_rr`/`shadow_mfe_rr`.
4. Add report data-quality verdict:
   - `usable_for_gate: true/false`.
   - `blockers: [...]`.
5. Add baseline comparison in report or row metadata, keeping it shadow-only.
6. Run focused local tests + VM tests.
7. Deploy patch to VM only after tests and compile pass.
8. Restart/verify `dexter-monitor`.
9. Start counting evidence days only after P0 green.

## No-Live Boundary

Do not enable Fibo MTF live dispatch.
Do not make MTF anchor influence live SL/TP.
Do not auto-promote with code thresholds.
Any promotion requires a separate plan, diff, verification, and Opus review.

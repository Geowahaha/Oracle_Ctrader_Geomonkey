# Opus 4.7 Review — Fibo MTF Shadow Telemetry

Timestamp UTC: 2026-05-12T18:27:41Z
Role: Claude Opus 4.7 reviewer only
Scope: Review deployed shadow-only Fibo MTF improvement at commit `bdc2178` for missing improvements before leaving VM collecting evidence.

## Prompt Summary

Known verified facts:
- VM branch `deploy-xau-family-canary`, `HEAD=origin=bdc2178`.
- `dexter-monitor` active.
- Focused VM tests: 20 passed.
- Imports OK: `analysis.shadow_outcome_metrics`, `scanners.fibo_mtf_shadow`.
- `xau_shadow_journal` has `shadow_mae_rr` and `shadow_mfe_rr`.
- Current report: decisions=577, resolved=198, calendar_days=2, expectancy_R=0.0911, max_mae_R=1.572, real_anchor_rate=0.0.
- Newest rows: `execution_anchor_source=unavailable`, `execution_anchor_is_live_plan=0`, unresolved MAE/MFE until resolver.
- Changes are shadow-only: scanner anchor payload, MAE/MFE helper, scheduler/autopilot schema and resolution, planner report.

Question: Any missing improvement before leaving VM collecting evidence? Do not implement, no live promotion.

## Opus Verdict

APPROVE — safe to leave VM running.

Diff is strictly additive and shadow-only: `_execution_anchor_payload` always sets `execution_anchor_is_live_plan=False`; resolver only writes to `xau_shadow_journal`; ALTER guards are idempotent; report path is read-only. Nothing in `bdc2178` opens an execution lane. Collecting more days of resolved samples is the right next move.

The items below are quality-of-evidence improvements, not blockers.

## Missing Improvements / Findings

1. Anchor includes the signal bar — `scanners/fibo_mtf_shadow.py:_execution_anchor_payload`

`df.tail(lookback)` pulls min/max through the just-triggered bar. The anchor is supposed to model invalidation before entry, so it should use the prior window only, for example `df.iloc[-(lookback + 1):-1]`. Otherwise the swing already absorbs the breakout wick that caused the signal and future broker-geometry comparison is biased.

2. MAE/MFE truncated at first TP/SL — `analysis/shadow_outcome_metrics.py:compute_shadow_path_metrics`

The loop breaks on TP/SL hit. A TP that survived a deep drawdown can look clean; an SL that briefly moved favorable is hidden. For route rejection based on ugly path risk, extend MAE/MFE over a fixed lookahead window and persist pre-resolve metrics separately from full-window metrics.

3. Report should prefer persisted MAE/MFE columns — `ops/fibo_mtf_planner_shadow_report.py:_mae_r`

New `shadow_mae_rr` / `shadow_mfe_rr` columns are selected, but `_mae_r` may reconstruct MAE from `raw_scores_json`. Make `_mae_r` prefer `row["shadow_mae_rr"]` and fall back to raw scores only when null, and add MFE summary.

## Minor Notes

- `compute_shadow_path_metrics` docstring says if TP and SL occur in same coarse bar, TP wins and calls this conservative. Opus notes this is optimistic because true intra-bar sequence is unknown. Add a `tp_sl_same_bar=True` flag or treat same-bar ties as ambiguous later.
- `real_anchor_rate=0.0` is by design because live-plan flag is forced false. The report should state this to avoid future readers interpreting it as regression.

## Minimal Safe Shadow-Only Fix

Lowest blast radius: change `_execution_anchor_payload` to use the pre-signal window:

```python
df.iloc[-(lookback + 1):-1]
```

This is additive, one-line, and does not touch live path.

Defer MAE-window expansion and report rewrite to a second shadow PR because they require backfill/migration thinking.

## Required Verification Before Any Live Promotion

- At least 3 calendar days and at least 500 resolved shadow rows before re-evaluating expectancy_R; current N=198 over 2 days is undersized.
- Check that `execution_anchor_source='unavailable'` stays low; if newest rows are mostly unavailable, debug dataframe availability in `_candidate_from_context` before trusting anchor stats.
- Spot-check rows where `shadow_outcome='tp_hit' AND shadow_mae_rr > 0.7` to find TP cases that nearly stopped out.
- Grep journal/execution logs for `fibo_mtf_planner` strings on the live order path; expect zero hits.
- Re-run `tests/test_fibo_mtf_shadow.py` and `tests/test_shadow_outcome_metrics.py` on VM after any fixes.

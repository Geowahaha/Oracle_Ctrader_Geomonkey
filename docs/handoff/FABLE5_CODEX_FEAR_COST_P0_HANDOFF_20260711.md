# Fable 5 handoff — fear-cost look-ahead P0 closed in code (2026-07-11)

## Outcome

Commit this handoff accompanies removes the invalid `fear_cost` direction
fallback. A skipped decision may now be evaluated only when its stored feature
snapshot determines a candidate side at the decision timestamp. The former
`day_range_drift` fallback selected direction from the *subsequent* 30-minute
evaluation window, so it was look-ahead and could not support a strategy or
size promotion.

## Change set

- `dexter3/skip_evaluator.py`
  - removed `_day_range_drift_side` and its future-window fallback;
  - a skip with no recorded candidate becomes `no_determinable_side` /
    unevaluable rather than receiving a hindsight direction;
  - legacy persisted outcomes with `side_source="day_range_drift"` remain in
    SQLite for audit but are excluded from `fear_cost_summary`;
  - KPI now reports `invalid_lookahead`, and supports an explicit `now` for
    deterministic reporting/tests.
- `dexter3/shadow_runner.py`
  - emits `invalid_lookahead=<n>` beside the fear-cost KPI so production logs
    make the discard visible.
- `tests/test_dexter3_skipeval.py`
  - pins no-future-direction behavior, legacy-result exclusion, and fixes the
    date-window test deterministically.

## Verification

```
python -m pytest -q tests/test_dexter3_skipeval.py  # 26 passed
python -m pytest -q <all tests/test_dexter3_*.py>   # 973 passed
git diff --check                                   # clean
```

## Deployment / measurement rule

This is a telemetry-integrity repair, not an entry-rule change. Deploy it with
the normal VM code path when the canonical VM owner authorizes a restart. Do
not backfill/re-score existing `skip_outcomes`; the report deliberately
quarantines their `day_range_drift` rows instead. From the first post-deploy
cycle, `fear_cost` is valid only for `features_candidate` rows and should be
treated as a coverage KPI until its forward sample is substantial.

## Next P0 item

Broker-side SL/TP closes still bypass `close_lane_position`, so their outcomes
do not reach the newly repaired learner. Add a lane reconcile that detects a
previously-owned position disappearing, fetches its closed deal, and writes a
deduplicated `lane_position_closed` outcome with the originating
`entry_executed` setup/session metadata. Start with temp-DB and fake-MCP tests;
do not change VM execution behavior until the reconciliation evidence is
complete.

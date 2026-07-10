# Fable 5 handoff — lane-isolated learner (2026-07-11)

## Why this is required before empirical sizing

The VM Fable and Grok services share `dexter3_journal.db` and both trade
XAUUSD. Before this change, `empirical_stats.compute_from_journal(symbol)`
filtered only by symbol, so any future use of the learner for a Fable sizing
decision could include Grok outcomes (and vice versa). That would be false
evidence, not diversification.

## Change

- Every new `entry_executed` event persists the executor's exact `label`.
- Manual/OM closes and broker-side vanish-reconciled closes copy that label
  from their originating entry event.
- `compute_from_journal(..., label=...)` filters outcomes by lane label.
  Rows without a label—including historical rows created before this change—
  are deliberately excluded from a lane-specific learner.
- `shadow_runner` refreshes stats with its active label, so Fable consumes
  Fable-only stats and Grok consumes Grok-only stats.
- Unlabelled basket events are excluded from lane-specific stats because they
  cannot prove which service created them.

## Verification

```
tests/test_dexter3_empirical.py + executor.py + hunt.py : 427 passed
all tests/test_dexter3_*.py                            : 980 passed
git diff --check                                       : clean
```

## What this does not do

It does not enable empirical sizing or alter any live risk. It makes the
input trustworthy first. After deployment, wait for **10 fully labelled,
post-deployment closed outcomes in one (setup, session, label) bucket**;
then replay a bounded downsize-only policy against the same window before any
live sizing flag is enabled. Do not mix the earlier label-less or
double-owner-period results into that gate.

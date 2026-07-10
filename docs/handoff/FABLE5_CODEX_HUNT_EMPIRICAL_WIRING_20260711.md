# Fable 5 handoff — HUNT empirical p-win wiring (2026-07-11)

`decide_hunt()` now accepts the same flattened `journal_stats` shape already
used by `hunter_brain.decide()`. For a mature, lane-isolated `(setup, session)`
bucket it records the blended `p_win_est` and an `empirical_p_win` audit block
on the decision. Buckets below `MIN_SAMPLES=10` remain byte-for-byte on the
base prior.

This commit deliberately changes **no entry, side, stop, target, or risk**.
It completes the measurement path: lane-specific close → stats refresh → live
HUNT p-win annotation. Run `ops/dexter3_lane_tally.py --since
2026-07-10T16:55:00Z` and wait for labelled forward buckets before designing
a replay-backed, downsize-only empirical policy.

Verification: focused HUNT/empirical/wiring tests 416 passed; full Dexter3
suite 983 passed.

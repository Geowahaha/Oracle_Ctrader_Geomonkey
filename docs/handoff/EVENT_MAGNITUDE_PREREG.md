# PRE-REGISTRATION — event-anchored magnitude on XAU H1 (Daily Laboratory test #4)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~08:30Z,
Fable 5, standing delegation). Frozen; not re-tuned after seeing results.

## Why this test exists

After tests #1–#3, direction-from-history is closed at every reachable horizon.
The one quantity repeatedly measured as forecastable is **magnitude** — and the
one time magnitude should be MOST forecastable is around scheduled macro
events. The 08-07 expansion-straddle design died UNVALIDATABLE (N=13 on M5);
H1 back to 2024-03 makes it powered. This is not a direction predictor.

## EVENT SET (self-derivable, no external data — stated to avoid look-up bias)

**NFP days = the first Friday of each calendar month**, 2024-04 → 2026-08
(~29 events inside the H1 window). Release 12:30Z (winter 13:30Z — the 12:30Z
H1 bar covers both cases at H1 granularity; deviation noted). CPI/FOMC are NOT
included — their dates need an external calendar and would be fetched
mid-analysis; scope is NFP only, declared now.

## DATA

`XAUUSD` H1, 13,999 bars 2024-03-27 → 2026-08-07 (already fetched, same file
as test #1). ATR14 = Wilder RMA on H1. Spread 0.30. Points = dollars.
Segments 50/30/20 by bar index as before; an event belongs to the segment its
day falls in (≈ 14 / 9 / 6 events — SMALL; if the primary measurement's derive
N < 10 events the whole test is declared INCONCLUSIVE, stated in advance).

## MEASUREMENTS

**M1 — Is event-hour magnitude actually abnormal? (the foundation)**
For each NFP day: `R_event = (high−low of the 12:00Z–15:00Z window) /
ATR14(at 11:00Z)`. Control: the SAME window on all non-NFP Fridays.
Report means ± SE and the ratio. **If the ratio is not ≥ 1.5× with ≥ 2 SE
separation pooled (derive+validate), everything downstream is moot and the
test ends there** — declared in advance.

**M2 — Does the pre-event coil predict the expansion?** Corr between coil
(11:00Z bar range / ATR) and `R_event`, pooled; ± SE via Fisher. Reported,
no gate (informational).

**M3 — Trade expression (only if M1 passes): the 08-07 bracket, now powered.**
At 12:00Z place buy-stop at prior-2-bar high + spread and sell-stop at
prior-2-bar low − spread; first touch fills, other cancels; TTL 3 bars.
SL = the opposite trigger (structural: the coil's far edge). TP = 2× the
coil width. Flat by 21:00Z. Controls: the SAME structure on non-NFP Fridays
(cost control), and INVERT is meaningless (symmetric) — the comparison IS the
control Fridays.

## DECISION RULE (declared before the run)

* M1 fails → **CLOSED: event magnitude not abnormal enough to build on.**
* M1 passes but M3 net ≤ control-Friday net → **CLOSED: magnitude real but
  not capturable by this structure.**
* M1 passes AND M3 net > 0 in pooled derive+validate AND beats control
  Fridays → **SHADOW CANDIDATE**: a journal-only forward shadow (no live
  trades) for the owner to sign off — event N is too small for more.

No re-tuning of the window hours, the coil definition, the TTL, the TP
multiple, or the event set. Replay = FILTER; broker deals = proof.

---

# RESULT (measured 2026-08-08 ~08:40Z, spec frozen at `f319766`)

**VERDICT BY THE FROZEN GATE: CLOSED — M1 fails by its own letter.**
Ratio 1.47× (gate: ≥ 1.5×) with 2.74 SE separation (gate: ≥ 2, passed).
1.47 < 1.5. The same discipline that killed eurusd's gate-2 miss by 0.0003
applies here: no rounding up, no "close enough".

| M1 (12–15Z range, ATR units) | derive | validate | holdout |
|---|---|---|---|
| NFP Fridays | N=14, 5.31 ±0.63 | N=9, 3.54 ±0.74 | N=5, 4.46 ±1.27 |
| control Fridays | N=46, 3.24 ±0.22 | N=28, 2.98 ±0.38 | N=19, 2.97 ±0.23 |
| **pooled d+v** | **NFP 4.62 vs ctrl 3.14 = 1.47× @ 2.74 SE** | | |

M2 (coil→expansion): corr **+0.103** (z-SE 0.092) — the pre-event coil does
NOT predict the expansion; the 08-07 design's core assumption is unsupported.

M3 (bracket, reported but unclaimable per the gate): NFP +54.2/+129.9/+69.8
(N=14/9/5) vs control-Fridays +8.6/+95.2/−97.2. Positive in all three NFP
segments — and gated off. At N=28 events total, a per-event SD of ~30 points
makes these means swing by ±$12/event; this is exactly the sample size the
prereg pre-committed to distrust.

## Honest reading

NFP hours ARE more volatile (2.74 SE — real), by ~47%, not the pre-declared
50%. The coil does not predict the expansion (M2 ≈ 0.1), so the magnitude is
event-driven, not chart-forecastable — you cannot size the bracket from the
chart. Per the frozen rule nothing is built and nothing re-tuned. **A future
re-registration on NEW events only (they accrue ~1/month; ~12 fresh events by
mid-2027) would be legitimate — this document has consumed the current set.**

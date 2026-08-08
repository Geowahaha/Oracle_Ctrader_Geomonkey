# PRE-REGISTRATION v3 — sharper entry / smaller SL for the fib-zone family (USTEC M5)

**Written and committed BEFORE the PnL runs** (2026-08-08 ~05:40Z). Owner order:
*"งั้นต้องพัฒนา c4 หาจุดเข้าให้คม SL น้อย"*.

## Two honest constraints stated up front

1. **C4 cannot be developed on its own data.** It has N = 12/9/7 (28 signals in
   50 trading days). Any threshold sharpened on 28 trades is fitted to noise, and
   sharpening can only make N smaller. So the *stop* work below is measured on
   the **BASE** zone population (N = 303/196/116) and merely *reported* for C3/C4.
2. **The "sharper entry" half was measured first and came back NULL** — see the
   pre-run measurement below. Nothing observable at entry time stably predicts a
   small adverse excursion, so v3 does not propose an entry filter. Inventing one
   anyway would be the 11th such attempt.

## PRE-RUN MEASUREMENT #1 — what a "sharp" entry would have to look like

Adverse excursion (MAE) of the zone population, in points:

| population | winners' MAE (median / p75 / p90) | losers' MAE median | actual SL median |
|---|---|---|---|
| BASE derive (N=303) | 12.4 / 30.6 / 59.5 | 61.1 | 44.1 |
| BASE validate (N=196) | 10.8 / 21.5 / 33.2 | 55.0 | 42.5 |
| BASE holdout (N=116) | 12.5 / 28.5 / 50.1 | 57.8 | 42.6 |

The separation is real and stable: **a trade that is going to work rarely goes
more than ~20–30 points against you; one that fails goes 55–60.** The structural
stop (~43 pts) therefore pays for the losers at full price.

## PRE-RUN MEASUREMENT #2 — can that be predicted at entry time? NO

Median MAE (in ATR units) and reach-rate by entry-time feature, derive vs validate:

| feature | derive (MAE / reach) | validate (MAE / reach) | stable? |
|---|---|---|---|
| retracement depth (low/mid/high) | 1.62/1.44/1.05 · 43/36/42% | 1.36/1.22/1.34 · 49/48/31% | **no** |
| confirmation bar size | 1.26/1.52/1.41 · 41/35/45% | 1.35/1.24/1.45 · 39/50/40% | **no** |
| body fraction | 1.41/1.37/1.40 · 33/39/49% | 1.41/1.22/1.30 · 41/49/42% | **no** |
| wick fraction | 1.46/1.37/1.37 · 43/42/36% | 1.27/1.39/1.35 · 42/44/45% | **no** |
| session (Asia/London/NY/late) | 1.27/1.54/1.36/2.23 · 46/36/42/12% | 1.46/1.05/1.31/1.41 · 33/53/46/41% | **no** |
| **impulse leg size** | **1.14/1.36/2.00** · 38/44/39% | **0.97/1.39/1.93** · 60/41/31% | MAE yes, reach **no** |

Only leg size moves MAE consistently — and that is **largely mechanical** (a
bigger leg makes every distance bigger in ATR terms). The quantity that would
actually pay, the reach rate, is 38/39% in derive versus 60/31% in validate:
not stable. **Conclusion recorded in advance: there is no measured sharp-entry
filter, so v3 tests only the stop.**

## THE SPEC (frozen)

One change against the v1/v2 geometry, nothing else:

* **SL = k × ATR14 from the entry**, replacing the structural leg-origin stop.
* **k = 1.04** — the **derive-only** p75 of winners' MAE in ATR units
  (0.48 / 1.04 / 1.14 / 1.42 at p50/p75/p80/p90, N=122). Chosen from a
  **non-PnL** quantity measured on the derive segment alone, before any tight-stop
  PnL was computed — the same discipline that picked xaudaily's 13:00Z hour from
  volatility rather than profit. A second value **k = 1.42** (p90) is declared now
  as the only alternative, so the choice cannot be shopped afterwards.
* TP unchanged: 1.618 extension, plain first touch. Populations: BASE, C3-A, C4-A.
* Exits also re-run with the **$5 dollar-take** on the same tight stop, because
  the 05:05Z addendum showed the take fails on a ~45-point stop and its
  break-even sits at ~90% WR — a ~1×ATR stop is the only configuration in which
  that exit could arithmetically work.

That is **3 populations × 2 stops (k) × 2 exits = 12 comparisons.** Expected
false passes at the bar below ≈ 0.5–1.2; stated in advance, repeated in the report.

## SUCCESS CRITERION (unchanged from v2, declared before the run)

Build only if a cell has: net > 0 in all three segments; PF ≥ 1.0 in all three;
N ≥ 30 in derive and validate; **INVERT arm net-negative in ≥ 2 of 3 segments**;
and it beats the same population's structural-stop version on net-per-trade in
derive AND validate. `N < 30` ⇒ INCONCLUSIVE, not a fail (this pre-commits C4 to
INCONCLUSIVE — it is reported for completeness, never as a build).

**On failure:** record and stop. Do not re-tune k, the TP, the zone or the
confirmations. The fib family is then closed for replay, and the only remaining
honest route is forward N from a journal-only shadow.

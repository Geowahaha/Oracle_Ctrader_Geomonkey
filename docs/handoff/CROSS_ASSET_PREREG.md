# PRE-REGISTRATION — cross-asset lead-lag into XAU (Daily Laboratory test #3)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~09:50Z,
Fable 5, standing delegation). Frozen; not re-tuned after seeing results.

## Why this test exists

Every information test so far measured an instrument against ITS OWN price
history (M1→D1, 50 days→28 years: nothing). The one untested information class
is OTHER instruments' completed moves. Gold has mechanical contemporaneous
links (dollar, real yields, silver); the testable question is whether any of it
LEADS by one completed bar. Symbol probe (2026-08-08): DXY feed is dead on this
broker (stale since 2026-03); usable drivers with current data:
**EURUSD, USDJPY, XAGUSD, US500, XTIUSD**.

**Alignment note (stated in advance):** all series come from the SAME broker
with the SAME 21:00Z daily close and the same H1 grid, so this is a clean
lead-lag test — no asynchronous-close artifact (the classic false positive in
cross-market studies).

## DATA

From the live daemon: D1 (13,999-cap ⇒ years, per symbol) and H1 (2.4y) for
XAUUSD + the five drivers. Days/hours are joined on identical timestamps;
a bar missing on either side drops that observation.

## THE SPEC (frozen)

Predictor: driver `X`'s **previous completed bar** return `x(t) = X_close(t) −
X_close(t−1)`. Outcome: XAU's **next bar** move `r(t+1) = C(t+1) − C(t)` in
ATR14(XAU) units. Direction map (economic sign, fixed in advance):

* EURUSD **up** → dollar weak → XAU **up** (follow)
* USDJPY **up** → dollar strong → XAU **down** (inverse)
* XAGUSD up → XAU up · US500 up → XAU up (risk/liquidity) · XTIUSD up → XAU up
  (inflation proxy)

**P1 (primary):** mean of `sign_mapped(x(t)) × r(t+1)` in ATR units ± SE, per
driver, per timeframe (D1, H1), per segment (50/30/20 chronological, holdout
sealed). **10 comparisons total** (5 drivers × 2 timeframes); stated now.

**Controls:** ORACLE (engine sanity); RANDOM 3 seeds; **XAU-SELF** (XAU's own
lag-1 sign as predictor — the autocorr baseline any real driver must beat).

## DECISION RULE (declared before the run)

A driver×timeframe cell is a **LEAD CANDIDATE** only if:
1. |P1| ≥ **2 SE** in derive, AND the SAME SIGN in validate AND holdout;
2. |P1| exceeds |XAU-SELF| in derive and validate;
3. the sign agrees with the economic map above **or** is stably opposite in
   all three segments (a stable inverse is information too — but it must be
   stable, not segment-hopping).

Expected false passes across 10 two-sided comparisons ≈ 0.5; repeated in the
report. Anything else = FAIL (or INCONCLUSIVE if joined N < 300 in derive).
**On failure: record, stop — no extra lags, no extra drivers, no interaction
terms.** A lead candidate does NOT deploy; it graduates to its own forward
design under owner sign-off.

Replay = FILTER; broker deals = proof.

---

# RESULT (measured 2026-08-08 ~10:05Z, spec frozen at `0c43c82`)

**VERDICT BY THE DECLARED RULE: NO LEAD CANDIDATE.** ORACLE ≈ +0.50 ATR
everywhere (sane); RANDOM straddles zero. us500/xtiusd D1 derive have no joined
data (feeds start 2012/2017) → INCONCLUSIVE there by the N<300 clause.

| driver × tf | derive | validate | holdout | verdict |
|---|---|---|---|---|
| **eurusd D1** | **+0.0754 (+6.74 SE)** N=3293 | +0.0100 (+0.68) | +0.0055 (+0.31) | gate 1 ✓; **gate 2 FAIL by a hair** (validate 0.0100 < XAU-SELF 0.0103) |
| **usdjpy D1** | **+0.0483 (+4.32 SE)** | +0.0047 (+0.32) | −0.0200 (−1.11) | **FAIL gate 1** (holdout sign flip) |
| xagusd D1 | −0.28 SE | +0.11 | −1.02 | FAIL |
| us500 / xtiusd D1 | no derive data | — | — | INCONCLUSIVE |
| all five × H1 | none ≥ 2 SE in derive | — | — | FAIL |

## The finding that matters — for the whole program, not just this test

**The dollar really did lead gold by one day — before ~2012.** EURUSD's
previous-day move predicted XAU's next day at **+6.74 SE** (N=3,293, 1998→2012)
and USDJPY at +4.32 SE. In the validate segment (~2012→2020) the effect is
one-tenth the size; in 2020→2026 it is zero.

Two consequences:

1. **Meta-validation of the entire measurement program.** The repeated nulls of
   the last two days are NOT because the instruments are blunt — the same
   pipeline detects a genuine historical edge instantly and at enormous
   significance. The nulls mean the edges are genuinely absent from the
   modern data.
2. **A dated efficiency story, consistent with everything else measured:**
   information classes that once paid (cross-asset lag, and by extension the
   simple price patterns of the pre-HFT era) have been arbitraged to zero.
   History-based direction prediction — own-price OR cross-asset — is dead in
   the modern regime at every horizon this laboratory can reach.

Per the frozen rule: no extra lags, drivers, or interaction terms. The still
untested information classes on the map: **event-anchored magnitude (N4)** and
**maker-posture cost inversion (N3)** — neither is a history-based direction
predictor, which is now exactly the point.

# PRE-REGISTRATION — MSCALP2 TWO-WAY: harvest the swing, not the direction (USTEC M5)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~06:20Z).
Frozen; not re-tuned after seeing results.

## Why this test exists — and why it is the right shape

Owner order: *"Mscalp2 twoway entry ได้ทั้งสองด้าน เก็บที่ $5 ทั้งสองด้าน แล้วพยายาม
หาทางบริหารให้การแกว่งทำกำไรมากที่สุด"*.

This is the first design in the campaign that matches what has actually been
**measured** on this instrument, and it comes from the owner, not from a
back-fitted cell:

* 2026-08-07: USTEC M5 **direction** is a fair game (autocorr ≈ 0, brackets ≈
  control, fades worse than random). **Magnitude** is the only forecastable
  quantity.
* 2026-08-08 fib v3: a tight stop under a far target made money in BOTH
  directions and a random side captured the same payoff — i.e. the profitable
  object in these tests was always a long-gamma structure, never a direction.

A two-way structure stops paying for a directional call it cannot make. **But
being the right shape is not evidence** — the honest question is whether it
survives its own cost, since a two-way pays the spread TWICE.

## THE COST FLOOR (stated before any result)

Spread 1.5 points, charged per side. Any simultaneous two-way starts **−3.0
points** and a $5 take on one leg nets **+2.0** before the other leg is
resolved. **A two-way that banks $5 on the winner and lets the loser run to a
1.3×ATR stop (≈ −30 pts) loses on every excursion that does not fully reverse.**
The whole hypothesis is that managing the loser recovers more than that costs.
This arithmetic is the thing being tested, not decoration.

## DATA / ENGINE

Unchanged from every run this week: **13,999 real USTEC M5 bars**
(2026-05-28T19:05Z → 2026-08-07T20:55Z), decisions on the closed bar `i`, exits
scanned from `i+1` only, SL assumed first on a both-touch bar, points = dollars
(1-unit volume floor), spread **1.5 charged per side**. Splits by entry-bar
index: derive 0–6999 · validate 7000–11199 · holdout 11200–13998.

**Signal population = the LIVE producer `dexter3.mscalp.decide_mscalp`** at its
deployed defaults (ATR14, body 0.6, range 1.0×ATR, drift 72 bars ≥ 3.0×ATR,
SL 1.3×ATR) — parity by construction, the same call `dexter3-mscalp2` makes.
The two-way structure replaces only what happens AFTER the signal.

## THE SPEC (frozen) — four structures, one signal stream

* **S1 — simultaneous two-way, $5 both sides (the owner's literal proposal).**
  At the signal bar's close open BOTH a buy and a sell. Each leg independently
  closes at +$5. Loser leg: mscalp's own 1.3×ATR stop. Basket time stop 15 min
  (3 bars) flattens whatever is left.
* **S2 — symmetric $5 management ("บริหารการแกว่ง", simplest honest form).**
  S1, except the loser leg is also cut at **−$5** instead of riding to the ATR
  stop. Symmetric $5 in, $5 out, both sides.
* **S3 — breakout straddle (only one side ever fills).** Buy-stop at the signal
  bar's high + spread, sell-stop at its low − spread, TTL 3 bars, first touch
  fills and the opposite order is cancelled. SL 1.3×ATR from the fill, $5 take.
  Pays the spread ONCE — the cheap version of the same idea.
* **S4 — S3 without the cap.** Identical fills, but exits are mscalp's own
  (bank at the first M5 close ≥ +0.5R, 15-min time stop, 1.3×ATR SL). Tests
  directly whether the $5 cap is beheading the magnitude the structure exists
  to collect — the mscalp2-on-BRK lesson (`60b822b`), re-asked here.

## CONTROLS (mandatory)

* **RANDOM-TIME** — every structure re-run on the same NUMBER of entries placed
  at random bars (3 seeds). This is the decisive control here: an INVERT arm is
  meaningless for a symmetric structure, so the question becomes *does the
  mscalp signal bar pick a better moment to be long gamma than a coin-toss
  moment does?* If a structure cannot beat random timing, it is a pure
  volatility bet and must then also clear its cost floor on its own.
* **COST FLOOR** — report net-per-structure against the −3.0 (S1/S2) and −1.5
  (S3/S4) point starting deficit explicitly.

## SUCCESS CRITERION (declared before the run)

**BUILD only if a structure has:**
1. net > 0 in derive, validate AND holdout;
2. PF ≥ 1.0 in all three;
3. N ≥ 30 in derive and validate;
4. it **beats the RANDOM-TIME control** on net-per-entry in derive AND validate
   (on the median of the 3 seeds);
5. and — because this structure is symmetric and therefore immune to the
   inverted-arm test that killed the fib cells — **its edge over random timing
   must be ≥ 1 SE in both derive and validate**, not merely positive.

`N < 30` ⇒ INCONCLUSIVE, not a fail.

**On failure:** record and stop. No re-tuning of the $5, the −$5, the TTL, the
stop or the time stop.

**On success:** build off-by-default as `dexter3:mscalp2w:canary` — a twin of
`dexter3-mscalp2`, same producer, same label-family fork pattern (`SUFFIX` read
at import so the running lanes keep their names and their N) — and judge it on
**N ≥ 30 broker deals in dollars.** Note in advance: a simultaneous two-way may
be subject to broker hedging rules on this account; if S1/S2 win, that must be
verified live before any deploy.

Replay remains a FILTER, never a proof ([[feedback_live_wins_only]]).

# The new possibility space — consultation, 2026-08-08 (Fable 5)

Owner: *"ผมอยากให้มีความเป็นไปได้ใหม่จริงๆ ผมรู้ว่ามันยาก ... เราต้องนอกกรอบ
สร้างความเป็นไปได้ใหม่เท่านั้น"*

This is a consultation document, not a build. One fact in it is freshly
VERIFIED (the history probe); everything else is a ranked map of genuinely new
territory — none of it is a re-tune of the 10 dead entry families.

## 0. The box we were actually trapped in (VERIFIED today)

Every test this summer — 10 entry families, 3 fib pre-registrations, the
two-way, the information campaign — ran inside **13,999 M5 bars = ~50 trading
days**, because that is the broker cap *per timeframe*. Probed today on the live
daemon (`/tmp/tf_probe.py`):

| tf | XAUUSD | USTEC | US30 |
|---|---|---|---|
| m15 | 2026-01 → now (~7 mo) | ~7 mo | ~7 mo |
| h1 | **2024-03 → now (2.4 yr)** | 2.4 yr | 2.4 yr |
| h4 | **2017-07 → now (9 yr)** | 9 yr | 9 yr |
| d1 | **1998-04 → now (28 yr, N=7,351)** | 2012 → now (N=3,617) | 2012 → now (N=3,592) |

The 08-07 power verdict — "70 days of M5 cannot resolve a scalping edge either
way" — was true and remains true. But it silently defined the whole search space
as M5 scalping. **At the daily horizon the same broker hands us 3,600–7,351
bars: enough N to resolve a 0.1–0.2 ATR edge, which M5 never could.** The
out-of-the-box move is not a cleverer M5 entry; it is leaving M5.

Why this compounds with everything already learned:
* **Cost:** one D1 trade pays the spread once for a target measured in
  hundreds of points — spread becomes ~0.1–0.5% of target instead of 7–30%.
* **Stake:** at daily stop distances the 1-unit volume floor is *proportionally
  saner*, and floor-risk budgeting (PATH A) can be computed honestly.
* **Validation:** derive/validate/holdout with THOUSANDS per segment, decades
  apart — regime-dependence (the killer of the mscalp family) becomes visible
  instead of fatal.

## 1. The five genuinely new directions (ranked by evidence-readiness)

### N1 — The Daily Laboratory (unlocked today; highest leverage)
Re-ask every surviving question at D1/H4 where N exists:
* **xaudaily's own geometry** (day-open bias, 2×ATR SL, 4×ATR TP, unmanaged)
  is live on a replay base of N=30/19. It can now be validated on **28 years /
  ~7,000 days** of XAU D1 — including 2008, 2011, 2013, 2020, 2022 regimes.
  Whatever the answer, it is the first *powered* answer this repo ever gets.
* **The 08-07 expansion straddle** (bracket the pre-event coil, direction-
  agnostic) died UNVALIDATABLE at N=13. On H1 since 2024 (~600 trading days,
  every 13:30Z macro window) it becomes a powered test of the one quantity we
  can forecast: magnitude.
* **Multi-day trend/continuation families** — literally never tested here (all
  prior work was intraday). 9 years of H4 supports honest 3-segment splits.
* **Seasonality with power**: day-of-week, month, session-of-day effects on
  XAU D1 across 28 years — classic, measurable, and this account's cost
  structure at D1 could actually harvest a small one.

### N2 — Meta-allocation: trade the LANES, not the market (new information source)
The mscalp saga's central finding was that the family flips sign by REGIME
(wk27 −675, wk28 −425 → wk31 +356, wk32 +124). Nobody has measured whether
lane-level performance **autocorrelates** — whether "the hot lane stays hot"
for days-to-weeks. If it does, capital allocation across the existing zero-edge
lanes is itself an edge (ride the in-season lane at full risk-frac, bench the
out-of-season one), harvested from data we already journal on every trade.
Pre-registerable from our own decision journals + deals; also testable on
decades of D1 with proxy strategies. **This is an edge layer that requires no
new market prediction at all.**

### N3 — Stop paying the toll: maker-posture entries (structural, hard, honest)
On a fair game minus cost, the only guaranteed-sign lever is the cost term.
Every live lane crosses the spread (market/stop entries). A passive-limit
posture *earns* ~half the spread instead of paying it — worth ~3 points per
round trip on USTEC, which is larger than most measured "edges". The known
danger is adverse selection (limits fill exactly when flow turns against you) —
that is measurable: replay fill-simulation first, then a journal-only shadow
counting fills-vs-outcomes. High difficulty, but it attacks the only term in
the equation with a guaranteed sign.

### N4 — Event-anchored magnitude (the one forecastable thing, at the one
forecastable time)
Scheduled macro events are when magnitude is *predictably* abnormal. With H1
back to 2024 there are hundreds of NFP/CPI/FOMC windows to measure: coil size
before, expansion after, whether a direction-agnostic bracket clears its cost
**at H1 geometry** (the M5 version was unvalidatable, not refuted). Needs an
event calendar source; the structure is already designed (08-07).

### N5 — Cross-asset information at H1 (never tested here)
DXY / yields → XAU lead-lag; SPX↔NDX spread behaviour. 2.4 years of H1 across
instruments from the same daemon. The information campaign only ever tested
XAU/USTEC *against themselves*. A lead-lag test is cheap, pre-registerable,
and either yields a real conditioning variable or closes the question.

## 2. What guards this from becoming the 11th dead family

The same law that killed the others, applied from day one:
* pre-register every spec before measuring (this file's children);
* 3 chronological segments with a sealed holdout — now DECADES apart;
* ORACLE positive control in every harness; INVERT/RANDOM controls where
  direction/timing is claimed;
* points = dollars where the volume floor binds; spread charged;
* replay = FILTER; live broker deals = the only proof; live trials sized by
  floor-risk arithmetic (PATH A) so a null result costs ~nothing.

## 3. Recommended order (consultation, awaiting owner)

1. **N1 first probe:** xaudaily geometry on 28y of XAU D1 — it validates (or
   honestly bounds) a lane that is ALREADY live, so the result acts either way.
2. **N2 in parallel** (pure journal-mining, no market risk).
3. N4 behind N1 (needs calendar data), N5 as a cheap side-test, N3 last (hardest
   to simulate honestly).

Nothing deployed. No live lane touched. The M5 lanes keep collecting their N
untouched — this document opens a second laboratory, it closes nothing.

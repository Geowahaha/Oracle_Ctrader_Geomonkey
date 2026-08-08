# PRE-REGISTRATION — D1-native families on 28 years of XAU (Daily Laboratory test #2)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~09:15Z,
Fable 5, standing owner delegation). Frozen; not re-tuned after seeing results.

## Why this test exists

Every strategy this repo has ever tested was intraday. Multi-day holding has
literally never been measured here — and it is the one horizon where (a) the
verified 28-year D1 history gives real statistical power (N=7,351 days), and
(b) cost is negligible (spread 0.30 vs daily ATR ~10-30 points ≈ 1-3%).

**The trap this prereg exists to catch (test #1's lesson, encoded):** XAU has a
large secular upward drift over 28 years. Any long-leaning rule will print PnL
by sitting on that drift. Therefore the mandatory benchmark is **ALWAYS-LONG**,
and a family only counts as having timing skill if it beats it.

## DATA

`XAUUSD` D1, **7,351 bars, 1998-04-21 → 2026-08-06** (fetched from the live
daemon, `/tmp/xau_d1.json`). Close-to-close returns. Costs: **0.30 points
charged per position CHANGE** (flip or entry from flat; holding costs nothing).
Points = dollars at the 1-oz floor. ATR = Wilder RMA 14 on D1.

**Segments (chronological, holdout sealed):** derive = days 0–3675 (1998→
~2012) · validate = 3676–5880 (~2012→~2020) · holdout = 5881–7350 (~2020→2026).
Three genuinely different macro regimes by construction.

## THE FAMILIES (frozen — exactly these, NO parameter sweeps)

Parameters are the literature's classics, chosen before any measurement:

* **F1 — TSMOM-20**: position `b(t) = sign(C(t) − C(t−20))`, held for day t+1.
* **F2 — TSMOM-250**: same with a 250-day (12-month) lookback — the
  Moskowitz/Ooi/Pedersen classic.
* **F3 — Donchian 20/10** (turtle): go long when C(t) exceeds the prior 20-day
  high, flat when C(t) falls below the prior 10-day low; mirrored short leg.
  Position from the state machine, held for day t+1.
* **F4 — MA-200 filter**: `b(t) = sign(C(t) − MA200(t))`.

## MEASUREMENTS

* **P1 (primary, information):** mean of `b(t) × r(t+1)` in ATR-units,
  ± SE, per segment, per family. `r(t+1)` = next-day close-to-close move.
* **Benchmarks / controls (all mandatory):**
  * **ALWAYS-LONG**: mean of `r(t+1)` in ATR units — the drift floor.
  * **ORACLE**: `b = sign(r(t+1))` — engine sanity, must be hugely positive.
  * **RANDOM**: 3 seeds, must straddle zero.
* **P2 (trade expression):** cumulative points of the daily position with the
  0.30 flip cost, per segment: net, per-day ± SE, PF on daily PnLs, exposure
  (% days long / short / flat), number of flips.

## DECISION RULE (declared before the run)

A family is a **BUILD CANDIDATE** only if ALL hold:
1. P1 ≥ **+2 SE** in derive, AND P1 > 0 in validate AND holdout;
2. **P1 > ALWAYS-LONG's mean in derive AND validate** (timing skill beyond
   drift — the test-#1 trap gate);
3. P2 net > 0 after costs in all three segments.

4 families are being tested; expected false passes on gate 1 alone ≈ 0.1.
Anything less than all three gates = FAIL or (if a family's active-day N < 100
in a segment) INCONCLUSIVE. **On failure: record, stop, no lookback sweeps, no
new variants.** A build candidate does NOT deploy from this document — it gets
a fresh forward design (sizing via floor-risk arithmetic, own label/lane,
N≥30 broker deals as the only proof) under owner sign-off.

Replay = FILTER; broker deals = proof ([[feedback_live_wins_only]]).

---

# RESULT (measured 2026-08-08 ~09:30Z, spec frozen at `fcef957`)

**VERDICT: FAIL — all four families. The ALWAYS-LONG drift gate (gate 2)
eliminated every timing rule, exactly the trap this prereg was written to
catch.** ORACLE +43..+63 SE (engine sane); RANDOM straddles zero.

## P1 (signed next-day return, ATR units) vs the drift benchmark

| family | derive | validate | holdout | gate 1 | gate 2 (beat LONG in d+v) |
|---|---|---|---|---|---|
| **ALWAYS-LONG** | **+0.0374 (+3.57 SE)** | +0.0068 (+0.47) | **+0.0491 (+2.74 SE)** | — | benchmark |
| F1 TSMOM-20 | +0.0213 (+2.02) | +0.0064 | +0.0158 | pass | **FAIL** (0.0213 < 0.0374) |
| F2 TSMOM-250 | +0.0257 (+2.35) | +0.0101 | +0.0304 | pass | **FAIL** |
| F3 Donchian 20/10 | +0.0096 (+0.76) | +0.0104 | +0.0186 | FAIL | — |
| F4 MA-200 | +0.0224 (+2.07) | +0.0137 | +0.0273 | pass | **FAIL** |

P2 confirms the same ordering in dollars: ALWAYS-LONG nets **+4,041 points over
28 years with ONE position change** (+1318 / +192 / +2531, PF positive in all
three segments) while every timing overlay captures LESS (F2 +2,738, F4 +2,720,
F1 +1,027, F3 +364) despite 186–701 flips each.

## What this actually established

1. **XAU D1 carries a real, large, positive DRIFT — and no measurable timing
   signal on top of it.** F1/F2/F4's "+2 SE derive" passes are drift-loading
   (they are 55–68% long); their timing component strictly subtracts from full
   drift capture in every segment.
2. Combined with test #1 (13Z bias = regime glow) and the entire M5 campaign:
   at every horizon measured — M1, M5, H1, H4, D1, 50 days to 28 years —
   **the direction of gold and indices has refused to be predicted by price
   history in this repo's hands.** What exists: magnitude structure (08-07),
   cost arithmetic (08-08), and drift (this test).
3. Drift is an asset property, not a trading edge — harvesting it is a
   portfolio decision that belongs to the owner, not to a trading lane, and
   this document deliberately makes no recommendation about it.

Per the frozen rule: no lookback sweeps, no new variants of these families.

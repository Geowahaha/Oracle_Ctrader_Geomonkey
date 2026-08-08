# PRE-REGISTRATION — Fibonacci retracement/extension zone producer (USTEC M5)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~03:15Z,
H3/KillRBD discipline). The spec below is frozen and will NOT be re-tuned after
seeing results.

## Why this test exists

Owner order (2026-08-08): *"หา Fibonacci level โซนที่รอ แล้วใช้ Mscalp2 เก็บใน
โซนนั้นๆ ... pullback/throwback มาเทส 61.8% แล้วไปต่อ หรือ impulse ยืดไปถึง
1.618 / 2.618 หรือจะมา correcting อีกรอบ"*.

Standing evidence that shapes the design (not a reason to skip the test — a
reason to test it properly):

* 8 families of "fix the entry of mscalp" have failed the two-segment bar
  (headroom, BRK blue-sky, wick-tip, dip 0.25/0.5/0.75R, VP thin-air, VP
  value-area, FVG retest, KillRBD DBR).
* 2026-08-07 information test: USTEC M5 **price-path direction is a fair game**
  (autocorr ≈ 0, bracket ≈ control, fade worse than random). So a Fibonacci
  level is a *hypothesis about where the fair game stops being fair* — it must
  beat a random-entry control, not merely be profitable-looking.
* Binding law: this is a volume-floored lane → **judge in POINTS** (fixed
  volume ⇒ account dollars = points), never in own-risk R-sums.

## DATA

`USTEC` M5, **13,999 bars = 2026-05-28T19:05Z → 2026-08-07T20:55Z** (the broker
cap), fetched from the live daemon on the VM (`/tmp/ustec_m5.json`). Bars are
labelled by OPEN time. Every decision is taken on the CLOSED bar `i`; exits are
scanned from bar `i+1` onward (never the entry bar — the 2026-08-07 "fade
+7.41/trade" illusion came from resolving on the entry bar). Spread **1.5 points
charged on entry**. One position at a time; signals arriving while in a position
are skipped. Max hold **288 bars (24h)**, then exit at that close.

**Splits by ENTRY bar index (no overlap, chronological):**
derive = bars 0–6999 (50%) · validate = 7000–11199 (30%) · **holdout =
11200–13998 (20%)**. The holdout is not looked at until derive+validate are
reported.

## THE SPEC (frozen)

### Leg detection (no lookahead)
Pivots on M5 with wing `k=3`; a pivot at index `j` is only *confirmed* at bar
`j+3`. At decision bar `i`, let `P1` = the most recent confirmed pivot with
index ≤ i, and `P0` = the most recent confirmed pivot of the OPPOSITE kind
before `P1`. The leg is `P0 → P1`; direction is **up** when `P1` is a pivot
high. Leg size `L = |price(P1) − price(P0)|`, required `L ≥ 1.5 × ATR14(i)`
(ATR14 = simple mean of true range over the last 14 bars, the repo's `_atr`).

Retracement at bar `i` (up leg): `r = (price(P1) − close_i) / L`
(`r=0` at the leg extreme, `r=1` at the leg origin). Mirror for a down leg.
The setup is **void** if, since `P1`, any bar traded beyond `P1` (leg extended —
a new leg will form) or beyond `P0` (`r > 1`, leg broken).

### Cell A1 — PRIMARY: deep-zone continuation ("มาเทส 61.8% แล้วไปต่อ")
* Condition: `0.5 ≤ r ≤ 0.786` at bar `i`.
* Trigger: bar `i` closes **in the leg direction** (`close > open` for an up
  leg) and its close is inside the zone.
* Entry: MARKET at `close_i`, side = leg direction.
* SL: leg origin ∓ `0.15 × ATR14` (below `P0` for a buy). `risk = |entry − SL|`.
* TP: **1.618 extension**, `P0 + 1.618·L` for an up leg (mirror for down).
* Exit: first touch, plain. If a bar touches both SL and TP, **SL is assumed
  first** (conservative).

### Declared secondary cells (exploratory; each = A1 with ONE change)
* **A2** shallow zone `0.382 ≤ r ≤ 0.618`.
* **A3** TP = the `1.0` level (the prior extreme `P1`).
* **A4** TP = `2.618` extension.
* **B1** extension-exhaustion fade ("จะมา correcting อีกรอบ"): when a bar
  closes beyond the `1.618` extension, enter **counter-leg** at that close;
  SL beyond the `2.618` extension; TP at the `1.0` level (`P1`).

### Exit twins (measured ONLY on an entry cell that has already passed)
* **E1** mscalp exits: bank at the first M5 CLOSE ≥ +0.5R, 15-minute time stop,
  structural SL unchanged.
* **E2** mscalp2 dollar-take: close at the first bar whose favourable excursion
  reaches **$5** (= 5 points at the 1-unit floor). Stated approximation: M5
  bar extremes, not ticks — it can only over-state the take.

### Controls (mandatory)
* **INVERT** — every cell re-run with the side flipped, same geometry.
* **RANDOM** — same number of entries at random bars, same side mix, same
  geometry (expected ≈ −spread × N; anything much better means the geometry,
  not the level, is doing the work).
* **ORACLE** (positive control for the engine) — entries whose side is chosen
  from the realised next-24h move. Must come out strongly positive; if it does
  not, the harness is broken and no result is reportable.

## SUCCESS CRITERION (declared before the run)

**BUILD the lane only if cell A1:**
1. `net > 0` in **all three** segments (derive, validate, holdout), AND
2. `PF ≥ 1.0` in all three, AND
3. `N ≥ 30` in derive and in validate (holdout: `net > 0` suffices), AND
4. its **INVERT** arm is net-negative in ≥ 2 of 3 segments, AND
5. it beats the **RANDOM** control's net-per-trade in derive and validate.

If A1 fails, the secondaries are **exploratory only**. A secondary may be
promoted to a live lane only if it clears the identical 5-point bar, and the
report must state it was 1 of 5 comparisons (expected false passes ≈ 0.2–0.5).

**On failure:** record the numbers, do NOT re-tune zone bounds, `k`, the ATR
multiples, the TP, or the exit, and do not run further variants of this idea.

**On success:** build as an off-by-default canary (`dexter3:fib:canary`) with
exactly the specified geometry, judged on **N ≥ 30 broker deals in dollars**.

Replay remains a FILTER, never a proof ([[feedback_live_wins_only]]).

---

# RESULT (measured 2026-08-08 ~03:40Z, spec frozen at commit `9df6c0a`)

**VERDICT: FAIL — no Fibonacci lane will be built.** Per the declared rule the
idea is closed: no re-tuning of the zone bounds, `k`, the ATR multiples, the TP
or the exit, and no further variants. The exit twins (E1/E2) were never run —
the spec makes them conditional on an entry cell passing first, and none did.

Harness: scratchpad `fib_sweep.py` (+ `fib_b1.py` amendment), 13,999 real USTEC
M5 bars, spread 1.5, exits from bar i+1, SL-first on both-touch, points = dollars.

| Cell | derive (N/net/PF) | validate | holdout | verdict |
|---|---|---|---|---|
| **ORACLE** (engine positive control) | 105 / **+4984** / 2.42 | 66 / +1926 / 1.91 | 48 / +260 / 1.15 | engine sane ✅ |
| **A1 real** (0.5–0.786 → TP 1.618) | 133 / **−351** / 0.95 | 78 / +787 / 1.29 | 46 / +607 / 1.39 | **FAIL crit-1** |
| A1 invert | 117 / +1192 / 1.23 | 72 / −206 / 0.92 | 44 / −606 / 0.67 | mirrors A1 |
| A1 random (coin-flip side) | 131 / −1456 / 0.78 | 84 / −826 / 0.75 | 43 / +295 / 1.18 | — |
| A2 (0.382–0.618) | 130 / −492 / 0.94 | 77 / −873 / 0.78 | 55 / −179 / 0.93 | FAIL, all three negative |
| A3 (TP = 1.0 level) | 147 / −2153 / 0.65 | 89 / −93 / 0.96 | 56 / +446 / 1.32 | FAIL |
| **A4 (TP 2.618)** | 100 / **+689** / 1.13 | 64 / **+725** / 1.28 | 33 / **+1026** / 1.76 | passes 1–3, **FAILS crit-4** |
| A4 **invert** | 89 / **+1885** / 1.43 | 58 / **+359** / 1.15 | 36 / **+329** / 1.21 | both directions win |
| A4 random (seeds 7/11/23) | +2108 / −1947 / −2094 | +76 / −155 / +1124 | +1350 / −185 / +1275 | ±2000 swing per seed |
| B1 fade 1.618 (amended) | 36 / −143 / 0.86 | 13 / +19 / 1.04 | 8 / −426 / 0.21 | INCONCLUSIVE (N<30) |
| B2 continue through 1.618 (amended) | 36 / +35 / 1.04 | 13 / −58 / 0.88 | 8 / +402 / 4.34 | INCONCLUSIVE (N<30) |

### The three findings worth keeping

1. **A4 is the trap this pre-registration was written to catch.** Net-positive in
   all three segments with N≥30 — it would have shipped under the old
   two-segments bar. But its INVERT arm is positive in all three too, and
   coin-flip sides swing ±2000 points across seeds. A TP at 2.618×leg with a
   24h hold is a **wide bracket**, and a wide bracket on USTEC M5 pays out on
   magnitude regardless of side — the 2026-08-07 "bracket ≈ control" result,
   reproduced. Nothing about the Fibonacci level was doing the work.
2. **A1 has the same regime signature as every other mscalp entry filter:**
   derive (late May–early July) negative, validate + holdout (mid-July onward)
   positive. This is now the 9th entry family showing it. The fib level does not
   escape the regime dependence; it inherits it.
3. **B1/B2 are unmeasurable, not disproven.** Closes beyond a 1.618 extension
   happen ~57 times in 50 trading days — 13 and 8 trades in the later segments.
   Any verdict at that N is noise (the 08-07 history-cap problem again).

**Amendment disclosed:** B1 as originally frozen produced **zero** signals — the
leg-validity rule voids a leg the moment price extends past `P1`, so a
"close beyond the 1.618 extension" cannot coexist with a live leg. That is a
defect in my spec, not a property of the market. The amendment (P1-extension
voiding removed, everything else identical) was declared before the run and
adds 2 exploratory comparisons; both landed under the N bar anyway.

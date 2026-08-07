# PRE-REGISTRATION — KillRBD DBR/RBD + imbalance producer (USTEC)

**Written and committed BEFORE the measurement runs** (H3 discipline, 2026-08-07
~10:00Z). The spec below is frozen: it is translated from the owner's own
Pine script `🏆 KillRBD Enhanced - OPTIMIZED V3.0` (© Geowahaha), not invented
here, and it will NOT be re-tuned after seeing results.

## Why this test exists

Seven separate attempts to fix `decide_mscalp`'s ENTRY (headroom, BRK blue-sky,
wick-tip, dip-limits 0.25/0.5/0.75R, VP thin-air, VP value-area, FVG retest)
have all failed the two-segment bar — the conclusion on the board is that
mscalp's issue is the signal's regime dependence, not its entry placement.
KillRBD's trigger is a **different producer** (pattern + imbalance, i.e. a
structural setup rather than momentum extension) and has never been measured.
This is the only remaining untested idea from the owner's toolkit.

## THE SPEC (frozen)

Symbol USTEC, M5 bars, one position at a time, spread 1.5 charged on entry,
bars labeled by OPEN time, every decision taken on the CLOSED bar `i` with no
lookahead (exits scanned from bar `i+1` onward).

### Pattern state machine (verbatim from the script)
```
green = close > open ; red = close < open
rally = green and green[1]        # two consecutive greens
drop  = red  and red[1]           # two consecutive reds
base  = not rally and not drop
```
A 3-slot rolling array holds the last three DISTINCT states; a token is pushed
only when it differs from the current last token. The pattern string is the
concatenation of those three slots.
```
detect_dbr = pattern == "DBR" and pattern[prev bar] != "DBR"   -> BUY
detect_rbd = pattern == "RBD" and pattern[prev bar] != "RBD"   -> SELL
```

### Imbalance requirement (verbatim)
```
bull_fvg = low > high[2] and close[1] > high[2]
bull_og  = low > high[1]
bull_vi  = low > high[1] and close > open and (close-open) > (high-low)*0.3
bull_has_imbalance = bull_fvg or bull_og or bull_vi
bear_fvg = high < low[2] and close[1] < low[2]
bear_og  = high < low[1]
bear_vi  = high < low[1] and close < open and (open-close) > (high-low)*0.3
```
A signal requires `detect_dbr and bull_has_imbalance` (mirror for sells).

### Trade score (verbatim, threshold = the script's default 60)
```
confluence (max 40) = 15*fvg + 12*og + 8*vi + 5*pattern
momentum   (max 40) = RSI part + volume part
    RSI(14) standard path:  buy  rsi<35 -> 20 ; rsi<50 -> 12 ; rsi<65 -> 7
                            sell rsi>65 -> 20 ; rsi>50 -> 12 ; rsi>35 -> 7
    volume(SMA20):          vol > 1.5x -> 20 ; vol > 1.0x -> 10
timing     (max 20) = NY AM 13:30-16:00Z or NY PM 18:30-21:00Z -> 20
                      London 07:00-10:00Z -> 15 ; Asian 01:00-05:00Z -> 8
                      otherwise -> 10
signal qualifies when total >= 60
```
**Two documented deviations, both stated in advance:**
1. The script's `use_dxy_correlation` path is Gold-specific (it scores USTEC off
   TVC:DXY and labels the row "Gold RSI"). On USTEC the standard-RSI branch is
   used instead — the DXY branch is measured noise for this instrument.
2. In the script the session flags are ANDed with their DISPLAY toggles, so with
   default inputs only NY AM contributes 20 and everything else falls to 10.
   Scoring must not depend on display settings, so all four windows are honoured
   as written above.

### Geometry and exits (verbatim from `create_unified_signal`)
```
entry = close[i] (market at the signal bar's close)
buy : sl = min(lowest(low,20),  entry - 1.5*ATR14) ; risk = entry - sl
      tp = entry + 2.0*risk        (the script's min_rr_ratio default)
sell: sl = max(highest(high,20), entry + 1.5*ATR14) ; risk = sl - entry
      tp = entry - 2.0*risk
```
ATR14 = Wilder RMA (the script's `ta.atr(14)`). Exit = broker SL / TP, first
touch wins (PLAIN — the script draws these levels and defines no other exit).
No bank, no time stop: adding one would no longer be this script's idea.

## MEASUREMENT

9000 real USTEC M5 bars (2026-06-23 -> 2026-08-07), the same harness every
other cell used today, 60/40 derive/validate split.

- **Cell A (primary):** pattern + imbalance + score >= 60.
- **Cell B (contingency, declared NOW, evaluated ONLY if Cell A yields fewer
  than 30 trades in either segment):** pattern + imbalance, score gate removed.
  Nothing else changes.

## SUCCESS CRITERION (declared before the run)

BUILD the lane only if, in the evaluated cell:
1. net > 0 in BOTH derive and validate, AND
2. PF >= 1.0 in BOTH, AND
3. N >= 30 in BOTH segments.

Anything else = FAIL or INCONCLUSIVE. On failure: record the result, do not
re-tune the score threshold, the imbalance set, the RR, or the exit, and do not
run further variants of this idea. On success: build as a canary lane
(`dexter3:killrbd:canary`), exits exactly as specified, and judge on N>=30
broker deals in dollars like every other lane.

Replay remains a FILTER, never a proof ([[feedback_live_wins_only]]).

---

# RESULT (measured 2026-08-07 ~10:10Z, spec frozen at commit `373d1db`)

**VERDICT: FAIL — no lane will be built. Per the declared rule the idea is
closed: no re-tuning of the score threshold, imbalance set, RR or exit.**

| Cell | Segment | N | net | WR | PF |
|---|---|---|---|---|---|
| A (score >= 60) | derive | 6 | **-522.5** | 17% | 0.32 |
| A (score >= 60) | validate | 4 | **-98.4** | 25% | 0.69 |
| B (no score gate) | derive | 57 | **-614.3** | 28% | 0.88 |
| B (no score gate) | validate | 39 | **-1250.9** | 23% | 0.64 |

Cell A produced only **10 qualifying signals in 45 days** (N=6/4), so by the
pre-declared contingency it is INCONCLUSIVE on sample size — and the
contingency Cell B, evaluated exactly as declared, is **negative in BOTH
segments**. Criterion 1 (net>0 both) fails outright.

## Why it fails — mechanism, measured

1. **The score gate almost never fires on USTEC M5: 10 of 330 pattern+imbalance
   signals reached 60.** Typical arithmetic at a DBR: pattern 5 + OG/VI 8-12 +
   RSI 7-12 (a DBR forms after a *base*, so RSI sits mid-range and rarely pays
   the 20) + timing 10-20 = roughly 30-54. FVG (the 15-point component) is rare
   on continuous M5 data. **This is why the owner's dashboard shows "No Signal"
   most of the time — it is the formula, not a quiet market.**
2. **Win rate 23-28% against a fixed 2R target, where breakeven is ~33% before
   spread.** The geometry is the cause: the stop is
   `min(lowest(low,20), entry - 1.5*ATR)`, i.e. at least 1.5xATR and usually the
   whole 20-bar swing — a wide stop paired with a 2R target needs a large,
   sustained move that a 2-bar rally after a 2-bar base does not reliably
   deliver.
3. The raw pattern is common (330 signals / 45 days ~ 7 per day); it is not
   selectivity that is missing, it is edge.

## Standing conclusion

This was the 8th distinct family tested against the USTEC scalp problem
(headroom, BRK blue-sky, wick-tip, dip-limits, VP thin-air, VP value-area,
FVG retest, and now KillRBD DBR/RBD). **None has cleared the two-segment bar.**
The board's converging read stands: on this instrument the difficulty is regime
dependence, not entry placement or setup naming. The four live USTEC cells plus
h3fade keep running and are judged on broker deals at N>=30.

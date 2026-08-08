# PRE-REGISTRATION — XAUDAILY powered validation on years of H1/H4 (Daily Laboratory test #1)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~08:30Z,
Fable 5, full owner delegation: "ลุยเลย ผมยกให้คุณออกแบบและพิสูจน์จนกว่าจะได้
ของจริง"). Frozen; not re-tuned after seeing results.

## What this tests and why it matters

`dexter3-xaudaily` is LIVE on replay evidence of N=30/19 with **no cell above
1.38 SE and no untouched holdout** — the lane explicitly claims no backtested
edge and waits ~6 weeks for N≥30 broker deals. Today's verified history probe
(28y D1 / 9y H4 / 2.4y H1 behind the per-timeframe cap) makes a POWERED test of
its signal possible for the first time. The result acts either way:

* signal confirms → the lane's forward trial continues with real grounds, and
  sizing-up after N≥30 has a foundation;
* signal measurably wrong → recommend killing the lane EARLY, saving 6 weeks;
* inconclusive → status quo, trial continues unchanged.

**The live lane itself is not touched by this measurement in any case** — any
kill/size decision is the owner's, made on this evidence plus live deals.

## THE SIGNAL UNDER TEST (verbatim from `dexter3/xaudaily.py`)

At 13:00Z, bias `b = sign(P(13:00Z) − P(00:00Z day open))`; trade WITH `b`,
SL 2×ATR14(M5) Wilder, TP 4×ATR (ratio 1:2), unmanaged, first touch.

## DATA

From the live daemon (fetched today): `XAUUSD` **H1 13,999 bars
(2024-03-27 → 2026-08-07, ~600 trading days)** and **H4 13,999 bars
(2017-07-17 → 2026-08-07, ~2,250 trading days)**. Spread **0.30** charged per
trade (the live measurement's figure). Both-touch bar ⇒ SL first. Points =
dollars at the 1-oz floor.

**Documented deviations from the live lane (stated in advance):**
1. Entry = the 13:00Z bar's OPEN (live enters at the 13:00 M5 bar's close,
   13:05Z) — a 5-minute difference.
2. ATR is computed at the test's own timeframe (Wilder RMA 14 on H1 or H4).
   The live SL 2×ATR(M5) ≈ **0.5×ATR14(H1)** by typical scale; the ratio 1:2
   is preserved exactly. Two declared scale cells, no others:
   **G1: SL 0.5×ATR14(tf), TP 1.0×ATR14(tf)** (closest to live absolute scale)
   **G2: SL 1.0×ATR14(tf), TP 2.0×ATR14(tf)** (robustness, wider).
3. On H4 the day-open anchor is the 01:00Z bar's open (H4 grid has no 00:00Z
   bar) — a 1-hour anchor deviation, H4 arm only.
4. Max hold 5 trading days, then exit at close. Weekend gaps are in the data
   and are kept (the live lane also holds through what it holds through).

## SEGMENTS (chronological, holdout sealed until derive+validate are reported)

* H1: derive = first 50% of days · validate = next 30% · holdout = last 20%.
* H4: same 50/30/20 by day index. (H4's holdout overlaps H1's calendar — they
  are different-granularity views, both reported, neither averaged together.)

## MEASUREMENTS

**P1 — INFORMATION (primary; PnL-free):** per day, the bias-signed
continuation `R = b × (P(21:00Z) − P(13:00Z))` in ATR14(tf) units. Report mean
± SE and hit-rate ± SE per segment. Secondary horizon (declared): to next day's
13:00Z. Controls: **ORACLE** (b from the realised move — must be strongly
positive or the harness is broken) and **RANDOM** b (3 seeds — must straddle 0).
An INVERT arm is the exact negation of a signed mean by construction and is
therefore not evidence; it is listed only as an arithmetic check.

**P2 — TRADE EXPRESSION:** PnL of G1 and G2 with the bias, spread charged,
per segment: N, net, per-trade ± SE, PF, WR. Same ORACLE/RANDOM controls.

## DECISION RULE (declared before the run)

Primary = **P1 on H4 derive** (the largest non-overlapping sample, ~1,100 days):

* **VALIDATED**: P1 mean ≥ **+2 SE** in H4-derive AND > 0 in H4-validate AND
  H4-holdout AND > 0 in ≥2 of 3 H1 segments. → Lane keeps its forward trial;
  size-up remains gated on N≥30 live deals (unchanged).
* **REFUTED**: P1 mean ≤ **−2 SE** in H4-derive AND < 0 in H4-validate.
  → Recommend the owner kill the lane early.
* Anything else: **INCONCLUSIVE** → status quo; the forward trial continues and
  is judged on broker deals as originally registered.

P2 is reported but CANNOT upgrade an inconclusive P1 to validated — geometry
was shown this week (fib v1 A4 / v3) to manufacture PnL out of magnitude;
information first.

**On any result:** no re-tuning of the hour, the anchor, the ATR multiples or
the horizon. Follow-up ideas (other hours, other anchors, D1-native families)
require their own pre-registrations.

Replay remains a FILTER; live broker deals remain the only proof
([[feedback_live_wins_only]]).

---

# RESULT (measured 2026-08-08 ~08:50Z, spec frozen at `f05be81`)

**VERDICT BY THE DECLARED RULE: INCONCLUSIVE — status quo. The lane's forward
trial continues unchanged and is judged on broker deals as registered.**
(Primary P1 H4-derive = **−0.60 SE**: neither ≥+2 SE nor ≤−2 SE.)

Harness: scratchpad `xaudaily_powered.py`. ORACLE controls passed everywhere
(H1 +10.2/+10.6/+6.6 SE, H4 +28.1/+23.3/+19.1 SE); RANDOM arms straddle zero.

## P1 — information (bias-signed continuation, ATR units)

| arm | derive | validate | holdout |
|---|---|---|---|
| **H4 (9y, primary)** 13Z→21Z | N=601 **−0.028 (−0.60 SE)** | N=395 −0.013 (−0.23) | N=232 **+0.143 (+2.15 SE)** |
| H4 13Z→next-13Z | N=753 −0.134 (−1.66) | +0.026 (+0.28) | +0.144 (+1.11) |
| H1 (2.4y) 13Z→21Z | N=83 **+0.794 (+3.11 SE)** | N=69 −0.144 (−0.52) | N=13 +0.399 (+0.59) |
| H1 13Z→next-13Z | N=304 +0.455 (+2.43 SE) | +0.284 (+1.08) | −0.214 (−0.65) |

## P2 — trade expression (reported; cannot upgrade P1 per the frozen rule)

G1 (SL 0.5/TP 1.0 ATR) is net-positive in all six segments (H1 +194/+144/+245;
H4 +274/+366/+980, PF 1.14–1.35) — **but the random-side control is also
positive on H4 in all three segments** (+81/+199/+207): an asymmetric 1:2
bracket on an instrument in a secular uptrend makes money without any signal.
The fib-week lesson, reproduced at daily scale on the first try.

## The finding that matters more than the verdict

The 13Z bias signal is **not a stable property of XAU — it is a property of
2024-2025.** Over 9 years (H4) it is flat (−0.6 SE). It turns strongly positive
exactly in the H1-derive window (2024-03→2025-05, +3.11 SE) and in H4's holdout
(2024-09→, +2.15 SE) — the gold-bull regime — and decays toward zero in 2026
(H1 validate −0.52, H1 holdout +0.59, next-13Z holdout −0.65). The live lane's
own N=30/19 M5 evidence (May-Aug 2026) sits at the fading tail of that regime.

Honest read: **the lane is not refuted (the rule requires ≤−2 SE and it never
gets there), but its prior is weakened — its replay support was likely regime
glow, not structure.** The forward trial (N≥30 broker deals) proceeds exactly
as registered; this measurement changes no deployment and no config. What it
DOES change: any future size-up must confront the 9-year flat line, and
regime-stamping every verdict (the standing rule) is now mandatory here too.

No re-tuning of hour/anchor/multiples/horizon. Next Daily-Laboratory tests
(D1-native families on 28y, N2 meta-allocation) get their own pre-registrations.

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

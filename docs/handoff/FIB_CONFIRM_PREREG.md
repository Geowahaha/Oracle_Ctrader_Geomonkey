# PRE-REGISTRATION v2 — Fibonacci zone + CONFIRMATION before entry (USTEC M5)

**Written and committed BEFORE the measurement runs** (2026-08-08 ~04:10Z).
Frozen; not re-tuned after seeing results.

## Why a second test (this is not a re-tune of v1)

v1 (`FIB_ZONE_PREREG.md`) measured **entering at the level itself** and failed.
Owner's correction, 2026-08-08: *"มันไม่กลับตัวเป๊ะที่ level นั้นๆ หรอก แต่จะต้องรอ
VP LVN หรือสัญญาณอ่อนแรง หรือ PA มาช่วย ต้องหาความจริงก่อนเข้า ไม่รีบมีด"* — the
fib zone is a **waiting area**, not a trigger. That is a different hypothesis
with a different mechanism, so it gets its own pre-registration rather than a
tweak of the frozen v1 spec.

**The claim under test is comparative, not absolute:** *waiting for a
confirmation inside the zone beats entering at the zone.* The v1 A1 cell is the
baseline every confirmed cell must beat — a confirmed cell that is merely
profitable proves nothing if the unconfirmed one is equally profitable.

## DATA / ENGINE

Identical to v1 and unchanged: 13,999 real USTEC M5 bars (2026-05-28T19:05Z →
2026-08-07T20:55Z), spread 1.5 charged on entry, decisions on closed bar `i`,
exits scanned from `i+1` only, SL assumed first on a both-touch bar, one
position at a time, max hold 288 bars, points = dollars (volume floor).
Splits by entry-bar index: derive 0–6999 · validate 7000–11199 · holdout
11200–13998.

Leg/zone detection is v1's, unchanged: confirmed pivots `k=3` (confirmed at
`j+3`), leg `P0→P1` with `L ≥ 1.5×ATR14`, retracement `r`, leg void if price
trades beyond `P1` or `P0`.

## THE SPEC (frozen)

### The waiting area
A leg becomes **armed** the first bar its `r` enters `[0.5, 0.786]`. While armed
the lane takes NO trade. It watches for at most **24 bars**; the arm is
cancelled if the leg voids or `r` leaves `[0.382, 1.0]`.

### Confirmation triggers (entry = the CONFIRMATION bar's close, side = leg direction)
* **C1 — VP LVN.** Built with the LIVE module `dexter3.volume_profile.
  build_profile` over the trailing 288 bars (parity by construction, the same
  call the `dexter3-vp` lane makes — the one lane with a durable live edge).
  Trigger: the bar's range entered an `lvn` zone and it **closed back out** of
  that zone on the leg-direction side.
* **C2 — exhaustion / อ่อนแรง.** The counter-move is losing steam: the last
  three bars' true ranges are strictly decreasing **and** the bar's volume is
  `< 0.8 ×` the mean bar volume of the impulse leg `P0→P1`.
* **C3 — PA rejection.** The bar's wick INTO the zone is `≥ 50%` of its range
  and it closes in the leg direction **beyond the previous bar's close**.
* **C4 — confluence.** Any TWO of C1/C2/C3 on the same bar.

### Geometry (identical to v1 A1 so the comparison isolates the confirmation)
* **SL-A (structural):** leg origin `P0` ∓ `0.15×ATR14`.
* **SL-B (the "ไม่รีบมีด" dividend):** beyond the CONFIRMATION bar's own extreme
  ∓ `0.15×ATR14` — the reason for waiting is that confirmation should let the
  stop be smaller. Declared now, applied to all four C cells.
* **TP:** `1.618` extension (`P0 + 1.618·L` for an up leg), plain first-touch.

That is **8 comparisons** (4 confirmations × 2 stops). Expected false passes at
the bar below ≈ 0.3–0.8; stated in advance and repeated in the report.

### Controls (mandatory, all reported)
* **BASELINE** — v1 cell A1 (zone touch, no confirmation), the number to beat.
* **INVERT** — every cell re-run with the side flipped, same geometry.
* **ORACLE** — engine positive control (side from the realised next-24h move);
  if it is not strongly positive the harness is broken and nothing is reportable.

### Information-first readout (08-07 method, reported for every cell)
Independent of PnL: `P(price reaches the 1.0 extension before the SL)` with its
standard error, for the baseline and each confirmation. A confirmation that
cannot lift this rate is not adding information, whatever its PnL says.

## SUCCESS CRITERION (declared before the run)

**BUILD the lane only if a cell satisfies ALL of:**
1. `net > 0` in derive, validate AND holdout;
2. `PF ≥ 1.0` in all three;
3. `N ≥ 30` in derive and in validate;
4. its **INVERT** arm is net-negative in ≥ 2 of 3 segments *(the v1 A4 trap —
   a cell whose mirror also wins is measuring geometry, not direction)*;
5. it beats **BASELINE A1** on net-per-trade in derive AND validate;
6. its continuation-rate lift over baseline is `≥ 1 SE` in derive AND validate.

`N < 30` ⇒ **INCONCLUSIVE**, explicitly NOT a fail — the honest verdict when a
confluence filter starves (v1's B1/B2 lesson).

**On failure:** record the numbers, do not re-tune the zone bounds, the 24-bar
window, the C-thresholds, the stops or the TP, and do not run further variants
of the confirmation idea.

**On success:** build off-by-default as `dexter3:fibc:canary` with exactly this
geometry, entry stream reusing the live `volume_profile` module, judged on
**N ≥ 30 broker deals in dollars**.

Replay remains a FILTER, never a proof ([[feedback_live_wins_only]]).

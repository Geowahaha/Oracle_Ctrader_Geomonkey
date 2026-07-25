"""Volatility-normalized minimum stop distance — the audit's #1 measured edge.

Pure logic, no I/O, no env reads (the caller owns those) so it is fully
testable offline.

WHY (2026-07-25 whole-project audit, the only pre-trade discriminator that
survived refutation):

    SL < 1.2x true range :  N=30, -0.398 R/trade
    SL >= 1.2x true range:  N=25, +0.116 R/trade      swing ~ +0.51 R/trade

  Controls passed 4/4 — same direction within fable alone, within dtr alone,
  and in BOTH time halves. The mechanism is visible rather than fitted: the
  tight cohort reaches its take-profit only 6-8% of the time versus 27-29%
  for the wide cohort *at comparable planned RR* (median 2.11 vs 2.00). Under
  a random walk a NEARER target should be hit MORE often; it is hit far less,
  so the tight stops are being taken out by noise before the thesis can
  resolve. Downstream signature: 38 of 105 trades died inside 15 minutes at
  -1.110R with a median peak MFE of only 0.23R — they never worked at all.

  Measured live geometry (journal, entries since 2026-07-16) shows the MEDIAN
  trade of every lane sits just under the threshold — fable 1.13, dpull 1.17,
  dpull-cs 1.14, dtr 1.06 — so a floor at 1.2 lifts the losing bottom half and
  leaves the healthy top half untouched.

  This is also the single ROOT behind four separate "wick-out" diagnoses the
  project fixed one lane at a time (dpull intrabar stop, fable sweep wick-SL,
  dpull-cs close-stop, the 07-25 -9.56 h1_context loss).

DESIGN:
  * RR IS PRESERVED. Widening the stop alone would create a geometry that
    exists nowhere in the evidence; the winning cohort was wide-stop *at
    comparable RR*, so the take-profit distance is scaled by the same factor.
    This also respects the audit's other hard finding — every take-profit
    SHORTENING tested lost money, and fixed far TPs produce 66.5% of all gross
    profit.
  * BOUNDED. At the XAU 1-ounce volume floor, dollar risk == stop distance, and
    ``executor`` REFUSES a trade whose floored risk exceeds
    ``DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD``. ``max_sl_abs`` lets the caller cap
    the floor so a volatile bar widens the stop as far as is affordable instead
    of silently converting the trade into a refusal (which would confound
    "better stops" with "fewer trades" in the forward measurement).
  * WIDEN-ONLY. A stop already at or beyond the floor is never touched, and the
    function never moves a stop closer to entry.
"""
from __future__ import annotations

from typing import Any, Sequence


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def median_true_range(bars: Sequence[dict], lookback: int = 14) -> float:
    """Median true range over the last ``lookback`` COMPLETED bars.

    TR = max(high-low, |high-prev_close|, |low-prev_close|) — the standard
    definition, so gaps count. Median (not mean) for robustness against a
    single spike bar, matching the ``tr_q50`` measure the hunt lane's own
    geometry already uses. Returns 0.0 when there is not enough usable data,
    which the caller must treat as "no floor" (fail-open).
    """
    if not bars or lookback < 1:
        return 0.0
    window = list(bars)[-(int(lookback) + 1):]
    if len(window) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(window)):
        cur, prev = window[i], window[i - 1]
        hi, lo = _f(cur.get("high")), _f(cur.get("low"))
        pc = _f(prev.get("close"))
        if hi <= 0.0 or lo <= 0.0 or pc <= 0.0:
            continue
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return 0.0
    trs.sort()
    mid = len(trs) // 2
    return trs[mid] if len(trs) % 2 else (trs[mid - 1] + trs[mid]) / 2.0


def apply_floor(
    *,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    tr: float,
    mult: float,
    max_sl_abs: float = 0.0,
) -> dict[str, Any] | None:
    """Return widened ``{"sl", "tp", "meta"}`` or ``None`` when unchanged.

    ``None`` means "leave the decision exactly as the producer built it" and is
    returned for every unusable input — this must never fabricate geometry.
    """
    side_l = str(side or "").strip().lower()
    if side_l not in ("buy", "sell"):
        return None
    entry_f, sl_f, tp_f = _f(entry), _f(sl), _f(tp)
    tr_f, mult_f = _f(tr), _f(mult)
    if entry_f <= 0.0 or sl_f <= 0.0 or tp_f <= 0.0 or tr_f <= 0.0 or mult_f <= 0.0:
        return None

    sl_dist = abs(entry_f - sl_f)
    tp_dist = abs(tp_f - entry_f)
    if sl_dist <= 0.0 or tp_dist <= 0.0:
        return None

    # Producer geometry must already be sane; never "repair" an inverted setup.
    if side_l == "buy" and not (sl_f < entry_f < tp_f):
        return None
    if side_l == "sell" and not (tp_f < entry_f < sl_f):
        return None

    floor = mult_f * tr_f
    max_abs = _f(max_sl_abs)
    capped = False
    if max_abs > 0.0 and floor > max_abs:
        floor = max_abs
        capped = True
    if floor <= sl_dist:
        return None  # already wide enough (or the cap leaves no room) — widen-only

    scale = floor / sl_dist
    new_sl_dist = floor
    new_tp_dist = tp_dist * scale  # preserve the planned reward:risk ratio

    if side_l == "buy":
        new_sl = entry_f - new_sl_dist
        new_tp = entry_f + new_tp_dist
    else:
        new_sl = entry_f + new_sl_dist
        new_tp = entry_f - new_tp_dist

    if new_sl <= 0.0 or new_tp <= 0.0:
        return None

    return {
        "sl": round(new_sl, 5),
        "tp": round(new_tp, 5),
        "meta": {
            "applied": True,
            "tr": round(tr_f, 5),
            "mult": mult_f,
            "sl_dist_before": round(sl_dist, 5),
            "sl_dist_after": round(new_sl_dist, 5),
            "tp_dist_before": round(tp_dist, 5),
            "tp_dist_after": round(new_tp_dist, 5),
            "scale": round(scale, 4),
            "sl_over_tr_before": round(sl_dist / tr_f, 4),
            "sl_over_tr_after": round(new_sl_dist / tr_f, 4),
            "capped_by_max_abs": capped,
        },
    }

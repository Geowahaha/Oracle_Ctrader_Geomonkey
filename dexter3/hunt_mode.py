"""Dexter3 HUNT MODE — participation-first M5 decision engine.

Owner pivot (2026-07-05): the system must produce a market ACTION on EVERY
M5 close. Intelligence lives in direction/size/geometry and in the basket
repair engine (``dexter3.basket_live``), NOT in skipping. This module is the
opposite design point from ``dexter3.hunter_brain`` (which participates only
when a named candidate setup fires and RR clears): here, a weighted
DIRECTION COMMITTEE always produces a side once there are enough bars and a
sane quote, and geometry (SL/TP) is always derived from structure so every
entry still carries an exact invalidation and a cost-aware target.

``decide_hunt()`` returns a ``dexter3.hunter_brain.Decision`` — the same
dataclass/contract already journaled by ``shadow_runner`` — so hunt mode is a
drop-in alternative decision source, not a new contract.

Pure module: no I/O, no MCP, no randomness. Same additive-only posture as
every other ``dexter3/`` module (see docs/DEXTER3_M5_HUNTER_BLUEPRINT.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dexter3 import empirical_stats, market_lens
from dexter3.hunter_brain import Decision, _bar_close_ts


def _env_f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)

Bar = dict[str, Any]

# ---------------------------------------------------------------------------
# committee member weights (module constants — auditable/tunable in one place)
# ---------------------------------------------------------------------------

WEIGHT_CLOSE_LOCATION_PRESSURE = 1.0
WEIGHT_SWING_STRUCTURE = 1.2
WEIGHT_DAY_RANGE_TILT = 1.0
WEIGHT_DISPLACEMENT = 0.8
WEIGHT_COMPRESSION_RELEASE = 0.8
# FIX 3 (2026-07-07): rebalanced 1.0->1.3. Diagnosis: the committee shorted
# INTO strength (28 of 34 shorts clustered at range tops per the 2026-05-20
# adversarial-awareness audit; the 2026-07-06/07 real-fill review showed the
# same pattern — sell side -$116 vs buy +$5) because day_range_tilt's
# mean-reversion vote (w=1.0) out-muscled the trend-following members. Bumping
# m15_drift's weight strengthens the trend-following voice in the raw sum
# (see _run_committee/_committee_side_and_conviction), on top of the new
# trend_agreement conviction-penalty step below (_apply_trend_agreement_guard)
# which additionally reshapes side/size when the two are in direct conflict.
WEIGHT_M15_DRIFT = 1.3
WEIGHT_SWEEP_RECLAIM_OVERRIDE = 1.5
# FIX 3: rebalanced 0.6->0.9 — H1 context is the higher-timeframe trend voice,
# under-weighted enough that day_range_tilt (mean-reversion) could dominate it.
# 2026-07-24: isolated backtests show h1_context is fable's BEST-edge voter
# (with-H1-trend expR +0.14..+0.22, robust 2 windows) yet it still carries the
# LOWEST committee weight while the -edge fade-sweep carried the HIGHEST (1.5
# override) — weight was INVERSELY correlated with edge. Env-tunable so the
# best signal can be leaned into; default 0.9 preserves prior behaviour.
WEIGHT_H1_CONTEXT = _env_f("DEXTER3_HUNT_W_H1", 0.9)

TOTAL_COMMITTEE_WEIGHT = (
    WEIGHT_CLOSE_LOCATION_PRESSURE
    + WEIGHT_SWING_STRUCTURE
    + WEIGHT_DAY_RANGE_TILT
    + WEIGHT_DISPLACEMENT
    + WEIGHT_COMPRESSION_RELEASE
    + WEIGHT_M15_DRIFT
    + WEIGHT_SWEEP_RECLAIM_OVERRIDE
    + WEIGHT_H1_CONTEXT
)

# ---------------------------------------------------------------------------
# FIX 3 (2026-07-07) — trend-agreement conviction guard constants
# ---------------------------------------------------------------------------
# trend_agreement = the SIGNED average of the m15_drift and h1_context raw
# vote components (each already in [-1, 1] — see _vote_m15_drift/_vote_h1_context).
# A "strong ALIGNED trend" means both members agree in sign AND
# abs(trend_agreement) clears this threshold — the same 0.5 the blueprint
# uses elsewhere as a "strong signal" bar (see day_range_position shelf
# thresholds 0.78/0.22, a comparable "clearly one-sided" cutoff).
TREND_AGREEMENT_THRESHOLD = 0.5

# When the committee's chosen side OPPOSES a strong aligned trend, conviction
# is halved (this reshapes SIZE — small vs scout — never participation; the
# blueprint's "participation-first" contract is preserved, see decide_hunt).
COUNTER_TREND_CONVICTION_PENALTY_MULT = 0.5

# The counter-trend side is only allowed to stand (not flipped toward the
# trend) when its own weighted evidence is at least this much stronger than
# the trend's pull, OR a confirmed sweep_reclaim fired in the counter-trend
# direction (a sweep-reclaim is itself structural evidence of a genuine
# reversal, not mean-reversion noise — see _vote_sweep_reclaim's docstring:
# market_lens only reports value=True once the reclaim already confirmed).
COUNTER_TREND_OVERRIDE_STRENGTH_MULT = 1.2

# ---------------------------------------------------------------------------
# geometry constants
# ---------------------------------------------------------------------------

MIN_REWARD_RISK = 1.2
MIN_SL_SPREAD_MULT = 6.0
MAX_SL_SPREAD_MULT = None  # placeholder kept explicit; real cap below (TR_q90 based)
MIN_TP_SPREAD_MULT = 8.0
SL_TR_Q50_QUANTILE = 0.50
SL_TR_Q90_QUANTILE = 0.90

# Applied on top of the exact RR/cost-guard floors in _compute_tp so that
# independent 5dp rounding of entry/sl/tp in the final Decision can never
# realize a floor breach (see _compute_tp's inline comment for the
# reproduction that surfaced this).
_RR_ROUNDING_SAFETY_MARGIN = 0.001

CONVICTION_SMALL_FLOOR = 0.35

# p_win_est heuristic — documented constant until empirical stats mature
# (mirrors hunter_brain.BASE_P_WIN_BY_SETUP's "conservative starting point,
# not measured values" posture).
P_WIN_BASE = 0.44
P_WIN_CONVICTION_SLOPE = 0.12

# hard vetoes — the ONLY allowed skip paths (see _veto_skip / tests grep
# assertion that no other 'skip' construction exists in this module).
MIN_BARS_M5 = 60
DEFAULT_MAX_SPREAD_BPS = 15.0  # BTC default per spec; callers pass config for XAU etc.


@dataclass(frozen=True)
class HuntConfig:
    max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS


# ---------------------------------------------------------------------------
# small local helpers (pure, no cross-module state)
# ---------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ols_slope(values: list[float]) -> float:
    """Ordinary-least-squares slope of ``values`` against index 0..n-1.

    Plain statistics (not an indicator) — used for the m15_drift committee
    member. Returns 0.0 for degenerate inputs (n<2 or zero variance in x).
    """
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return num / den


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


# ---------------------------------------------------------------------------
# direction committee members — each returns (vote in [-1, +1], detail dict)
# ---------------------------------------------------------------------------


def _vote_close_location_pressure(lens: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    clp = lens.get("close_location_pressure") or {}
    clp_value = _f(clp.get("value"), 0.5)
    vote = _clip((clp_value - 0.5) * 2.0, -1.0, 1.0)
    return vote, {"clp_value": clp_value, "evidence": clp.get("evidence")}


def _vote_swing_structure(lens: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    swing = lens.get("swing_structure") or {}
    classification = swing.get("value")
    if classification == "uptrend":
        vote = 1.0
    elif classification == "downtrend":
        vote = -1.0
    else:
        vote = 0.0
    # EXTENSION gate (2026-07-24, own-terms fable fix): swing_structure is a
    # MOMENTUM signal -- deep-pullback trend-follows lose consistently across 3
    # windows (the OPPOSITE of dtr's pullback edge; hence NOT a clone). When
    # DEXTER3_HUNT_SWING_MIN_EXT>0, skip a trend-follow whose price has pulled
    # back below that extension (structure weakening) so noise pullbacks no
    # longer vote. Off by default -> legacy behaviour preserved.
    min_ext = _env_f("DEXTER3_HUNT_SWING_MIN_EXT", 0.0)
    if vote != 0.0 and min_ext > 0.0:
        ext = float(swing.get("extension") or 0.5)
        if ext < min_ext:
            return 0.0, {"classification": classification, "extension": ext,
                         "skipped": "deep_pullback_weak_momentum"}
    return vote, {"classification": classification, "extension": swing.get("extension")}


def _vote_day_range_tilt(lens: dict[str, Any], m15_bars: list[Bar]) -> tuple[float, dict[str, Any]]:
    drp = lens.get("day_range_position") or {}
    position = _f(drp.get("value"), 0.5)
    if position >= 0.78:
        vote = -0.8  # mean-revert short from the upper shelf
        mode = "mean_revert_short"
    elif position <= 0.22:
        vote = 0.8  # mean-revert long from the lower shelf
        mode = "mean_revert_long"
    else:
        drift = _m15_drift_raw(m15_bars)
        vote = _sign(drift) * 0.4
        mode = "momentum_mid_range"
    return vote, {"position": position, "mode": mode}


def _vote_displacement(lens: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    disp = lens.get("displacement") or {}
    if not disp.get("value"):
        return 0.0, {"fired": False}
    rank = _f(disp.get("tr_quantile_rank"), 0.0)
    direction = disp.get("direction")
    sign = 1.0 if direction == "buy" else (-1.0 if direction == "sell" else 0.0)
    vote = _clip(sign * rank, -1.0, 1.0)
    return vote, {"fired": True, "direction": direction, "tr_quantile_rank": rank}


def _vote_compression_release(lens: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    comp = lens.get("compression_release") or {}
    if not comp.get("value"):
        return 0.0, {"fired": False}
    direction = comp.get("direction")
    sign = 1.0 if direction == "buy" else (-1.0 if direction == "sell" else 0.0)
    vote = sign * 0.7
    return vote, {"fired": True, "direction": direction}


def _m15_drift_raw(m15_bars: list[Bar]) -> float:
    """OLS slope over the last 12 M15 closes, normalized by median M15 TR, clipped to [-1,1]."""
    if not m15_bars or len(m15_bars) < 3:
        return 0.0
    sample = m15_bars[-12:] if len(m15_bars) > 12 else list(m15_bars)
    closes = [_f(b.get("close")) for b in sample]
    slope = _ols_slope(closes)
    trs = market_lens.true_ranges(sample)
    med_tr = _median(trs)
    if med_tr <= 0:
        return 0.0
    return _clip(slope / med_tr, -1.0, 1.0)


def _vote_m15_drift(m15_bars: list[Bar]) -> tuple[float, dict[str, Any]]:
    drift = _m15_drift_raw(m15_bars)
    return drift, {"drift_normalized": drift, "bars_used": min(len(m15_bars or []), 12)}


def _vote_sweep_reclaim(lens: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    sweep = lens.get("liquidity_sweep") or {}
    if not sweep.get("value"):
        return 0.0, {"fired": False}
    # market_lens.liquidity_sweep() only reports value=True when the bar
    # already closed back inside the swept level (its own detection IS the
    # reclaim confirmation).
    side = sweep.get("side")
    sign = 1.0 if side == "buy" else (-1.0 if side == "sell" else 0.0)
    # FADE→FOLLOW flip (2026-07-24, owner "fade→follow ยาที่ลึกกว่า"): the legacy
    # vote FADES the grab (sell a high-poke) — fable's -$92 @ 22% WR bleeder.
    # Isolated 3-window backtest: FADE is a coin-flip the tight geometry loses;
    # FOLLOWING a STRONG grab (wick>=1xATR + volume>=avg) is a robust +edge. So
    # when enabled: require grab strength, then FLIP the sign to follow. Weak
    # grabs vote 0 (don't let noise pokes dominate the committee). Off by
    # default -> legacy fade preserved.
    if os.environ.get("DEXTER3_HUNT_SWEEP_FOLLOW", "0").strip() == "1":
        wick_atr = float(sweep.get("wick_atr") or 0.0)
        vol_ratio = float(sweep.get("vol_ratio") or 0.0)
        min_wick = _env_f("DEXTER3_HUNT_SWEEP_MIN_WICK_ATR", 1.0)
        min_vol = _env_f("DEXTER3_HUNT_SWEEP_MIN_VOL", 1.0)
        if wick_atr < min_wick or (min_vol > 0 and vol_ratio < min_vol):
            return 0.0, {"fired": True, "skipped": "weak_grab",
                         "wick_atr": wick_atr, "vol_ratio": vol_ratio}
        sign = -sign  # FOLLOW the grab (a real grab fuels the next leg)
        return sign * 1.0, {"fired": True, "follow": True, "side_follow": ("buy" if sign > 0 else "sell"),
                            "wick_atr": wick_atr, "vol_ratio": vol_ratio,
                            "evidence": sweep.get("evidence")}
    vote = sign * 1.0
    return vote, {"fired": True, "side": side, "evidence": sweep.get("evidence")}


def _h1_context_raw(h1_bars: list[Bar]) -> tuple[float, float]:
    """Return (normalized_vote_component, net_change) over the last 6 H1 bars."""
    if not h1_bars or len(h1_bars) < 2:
        return 0.0, 0.0
    sample = h1_bars[-6:] if len(h1_bars) > 6 else list(h1_bars)
    net_change = _f(sample[-1].get("close")) - _f(sample[0].get("open"))
    trs = market_lens.true_ranges(sample)
    med_tr = _median(trs)
    if med_tr <= 0:
        return 0.0, net_change
    magnitude = min(1.0, abs(net_change) / med_tr)
    return _sign(net_change) * magnitude, net_change


def _vote_h1_context(h1_bars: list[Bar]) -> tuple[float, dict[str, Any]]:
    vote, net_change = _h1_context_raw(h1_bars)
    return vote, {"net_change": net_change, "bars_used": min(len(h1_bars or []), 6)}


# ---------------------------------------------------------------------------
# committee aggregation
# ---------------------------------------------------------------------------

_COMMITTEE_MEMBER_NAMES = (
    "close_location_pressure",
    "swing_structure",
    "day_range_tilt",
    "displacement",
    "compression_release",
    "m15_drift",
    "sweep_reclaim",
    "h1_context",
)


def _run_committee(
    lens: dict[str, Any], m15_bars: list[Bar], h1_bars: list[Bar]
) -> dict[str, dict[str, Any]]:
    """Run every committee member; return {name: {vote, weight, weighted, detail}}."""
    clp_vote, clp_detail = _vote_close_location_pressure(lens)
    swing_vote, swing_detail = _vote_swing_structure(lens)
    tilt_vote, tilt_detail = _vote_day_range_tilt(lens, m15_bars)
    disp_vote, disp_detail = _vote_displacement(lens)
    comp_vote, comp_detail = _vote_compression_release(lens)
    drift_vote, drift_detail = _vote_m15_drift(m15_bars)
    sweep_vote, sweep_detail = _vote_sweep_reclaim(lens)
    h1_vote, h1_detail = _vote_h1_context(h1_bars)

    raw = {
        "close_location_pressure": (clp_vote, WEIGHT_CLOSE_LOCATION_PRESSURE, clp_detail),
        "swing_structure": (swing_vote, WEIGHT_SWING_STRUCTURE, swing_detail),
        "day_range_tilt": (tilt_vote, WEIGHT_DAY_RANGE_TILT, tilt_detail),
        "displacement": (disp_vote, WEIGHT_DISPLACEMENT, disp_detail),
        "compression_release": (comp_vote, WEIGHT_COMPRESSION_RELEASE, comp_detail),
        "m15_drift": (drift_vote, WEIGHT_M15_DRIFT, drift_detail),
        "sweep_reclaim": (sweep_vote, WEIGHT_SWEEP_RECLAIM_OVERRIDE, sweep_detail),
        "h1_context": (h1_vote, WEIGHT_H1_CONTEXT, h1_detail),
    }
    committee: dict[str, dict[str, Any]] = {}
    for name, (vote, weight, detail) in raw.items():
        committee[name] = {
            "vote": round(vote, 6),
            "weight": weight,
            "weighted": round(vote * weight, 6),
            "detail": detail,
        }
    return committee


def _committee_side_and_conviction(
    committee: dict[str, dict[str, Any]], lens: dict[str, Any]
) -> tuple[str, float, str]:
    """Resolve (side, conviction[0..1], dominant_member_name) from committee votes.

    side is NEVER None when this is called (bars are sufficient by the time
    we get here — hard vetoes already ran). Ties are broken by CLP sign,
    final fallback is sell if day_range_position>0.5 else buy.
    """
    weighted_sum = sum(m["weighted"] for m in committee.values())
    conviction = _clip(abs(weighted_sum) / TOTAL_COMMITTEE_WEIGHT, 0.0, 1.0) if TOTAL_COMMITTEE_WEIGHT else 0.0

    if weighted_sum > 0:
        side = "buy"
    elif weighted_sum < 0:
        side = "sell"
    else:
        clp_value = _f((lens.get("close_location_pressure") or {}).get("value"), 0.5)
        if clp_value > 0.5:
            side = "buy"
        elif clp_value < 0.5:
            side = "sell"
        else:
            drp_position = _f((lens.get("day_range_position") or {}).get("value"), 0.5)
            side = "sell" if drp_position > 0.5 else "buy"

    dominant = max(committee.items(), key=lambda kv: abs(kv[1]["weighted"]))[0]
    return side, conviction, dominant


# ---------------------------------------------------------------------------
# FIX 3 (2026-07-07) — trend-agreement conviction guard
# ---------------------------------------------------------------------------


def _trend_agreement(committee: dict[str, dict[str, Any]]) -> tuple[float, str | None]:
    """Return (trend_agreement, trend_side) from the m15_drift + h1_context
    committee members' raw (unweighted) votes.

    trend_agreement = the signed average of the two members' ``vote`` fields
    (each already in [-1, 1]) — a plain, auditable stat, not an indicator.
    trend_side is 'buy'/'sell' when BOTH members agree in sign (a real
    "aligned trend"); None when they disagree (no aligned trend to guard
    against — day_range_tilt's mean-reversion vote is then free to dominate,
    per spec: "day_range_tilt ... only let it dominate when NOT against an
    aligned trend").
    """
    m15_vote = _f((committee.get("m15_drift") or {}).get("vote"), 0.0)
    h1_vote = _f((committee.get("h1_context") or {}).get("vote"), 0.0)
    agreement = (m15_vote + h1_vote) / 2.0
    if _sign(m15_vote) != 0 and _sign(m15_vote) == _sign(h1_vote):
        trend_side = "buy" if m15_vote > 0 else "sell"
    else:
        trend_side = None
    return agreement, trend_side


def _apply_trend_agreement_guard(
    side: str,
    conviction: float,
    committee: dict[str, dict[str, Any]],
) -> tuple[str, float, dict[str, Any]]:
    """FIX 3: reshape (side, conviction) when the committee's chosen side
    OPPOSES a strong aligned trend. Never turns into a skip (participation-
    first stays, per blueprint HUNT MODE contract v2) — this only reshapes
    side/size, mirroring the FIX 3 spec verbatim.

    A "strong aligned trend" = m15_drift AND h1_context agree in sign AND
    ``abs(trend_agreement) >= TREND_AGREEMENT_THRESHOLD``. When the
    committee's ``side`` is the OPPOSITE of that trend_side:
      1. Conviction is halved (``COUNTER_TREND_CONVICTION_PENALTY_MULT``) —
         this alone pushes many borderline counter-trend entries from
         size_class='small' down to 'scout' (see decide_hunt's
         CONVICTION_SMALL_FLOOR gate), i.e. it "requires the opposing votes
         to be stronger" to still qualify for full size.
      2. If the counter-trend evidence is NOT strong enough to justify
         standing against the trend — defined as: no confirmed
         ``sweep_reclaim`` fired in the counter-trend (committee's chosen)
         direction, AND the committee's own weighted_sum magnitude is not
         at least ``COUNTER_TREND_OVERRIDE_STRENGTH_MULT`` times the raw
         trend pull — the side is FLIPPED toward the trend instead. This is
         the "else flip toward trend if the counter-trend evidence isn't a
         confirmed sweep_reclaim" clause: a confirmed sweep_reclaim is
         allowed to stand (it IS structural evidence of a genuine reversal,
         not mean-reversion noise), everything weaker gets flipped.

    Returns (final_side, final_conviction, detail) — detail is always
    present (even when the guard does not fire) so the decision's features
    snapshot can show why/why-not for every entry, not just the flipped ones.
    """
    agreement, trend_side = _trend_agreement(committee)
    detail: dict[str, Any] = {
        "trend_agreement": round(agreement, 6),
        "trend_side": trend_side,
        "threshold": TREND_AGREEMENT_THRESHOLD,
        "fired": False,
    }

    if trend_side is None or abs(agreement) < TREND_AGREEMENT_THRESHOLD:
        detail["reason"] = "no_strong_aligned_trend"
        return side, conviction, detail

    if side == trend_side:
        detail["reason"] = "side_already_aligned_with_trend"
        return side, conviction, detail

    # side OPPOSES a strong aligned trend — the counter-trend case.
    detail["fired"] = True
    penalized_conviction = round(conviction * COUNTER_TREND_CONVICTION_PENALTY_MULT, 6)

    sweep = committee.get("sweep_reclaim") or {}
    sweep_detail = sweep.get("detail") or {}
    confirmed_sweep_reclaim_counter_trend = bool(sweep_detail.get("fired")) and sweep_detail.get("side") == side

    weighted_sum = sum(m["weighted"] for m in committee.values())
    trend_pull = abs(agreement) * (WEIGHT_M15_DRIFT + WEIGHT_H1_CONTEXT) / 2.0
    strong_enough_to_stand = abs(weighted_sum) >= COUNTER_TREND_OVERRIDE_STRENGTH_MULT * trend_pull

    detail.update(
        {
            "penalized_conviction": penalized_conviction,
            "confirmed_sweep_reclaim_counter_trend": confirmed_sweep_reclaim_counter_trend,
            "weighted_sum": round(weighted_sum, 6),
            "trend_pull": round(trend_pull, 6),
            "strong_enough_to_stand": strong_enough_to_stand,
        }
    )

    if confirmed_sweep_reclaim_counter_trend or strong_enough_to_stand:
        detail["reason"] = "counter_trend_evidence_strong_enough_to_stand (penalized conviction only)"
        return side, penalized_conviction, detail

    detail["reason"] = "counter_trend_evidence_too_weak — flipped toward aligned trend"
    return trend_side, penalized_conviction, detail


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _tr_quantiles(m5_bars: list[Bar]) -> tuple[float, float]:
    trs = market_lens.true_ranges(m5_bars)
    q50 = market_lens._quantile(trs, SL_TR_Q50_QUANTILE)
    q90 = market_lens._quantile(trs, SL_TR_Q90_QUANTILE)
    return q50, q90


def _nearest_opposing_swing(
    lens: dict[str, Any], side: str, entry: float
) -> float | None:
    """Nearest opposing M5 swing point beyond entry (structural invalidation)."""
    swing = lens.get("swing_structure") or {}
    if side == "buy":
        candidate = swing.get("last_swing_low") or {}
        price = candidate.get("price")
        if price is not None and float(price) < entry:
            return float(price)
    else:
        candidate = swing.get("last_swing_high") or {}
        price = candidate.get("price")
        if price is not None and float(price) > entry:
            return float(price)
    return None


def _compute_sl(
    side: str, entry: float, lens: dict[str, Any], m5_bars: list[Bar], spread_abs: float
) -> tuple[float, dict[str, Any]]:
    tr_q50, tr_q90 = _tr_quantiles(m5_bars)
    min_dist = max(MIN_SL_SPREAD_MULT * spread_abs, tr_q50)
    max_dist = max(min_dist, 2.0 * tr_q90)

    structural = _nearest_opposing_swing(lens, side, entry)
    if structural is not None:
        raw_dist = abs(entry - structural)
        source = "swing_structure"
    else:
        # No opposing swing point recovered from the lens (e.g. flat/unknown
        # structure) — fall back to the TR-based minimum distance so SL
        # remains a structural-scale (not percentage-of-price) invalidation.
        raw_dist = min_dist
        source = "tr_fallback_no_swing"

    dist = _clip(raw_dist, min_dist, max_dist)
    sl = entry - dist if side == "buy" else entry + dist
    detail = {
        "source": source,
        "raw_distance": raw_dist,
        "clamped_distance": dist,
        "min_distance": min_dist,
        "max_distance": max_dist,
        "tr_q50": tr_q50,
        "tr_q90": tr_q90,
    }
    return sl, detail


def _nearest_favorable_shelf(
    lens: dict[str, Any], side: str, entry: float
) -> float | None:
    """Next favorable day-range shelf (hi/lo) or opposing-direction swing point."""
    drp = lens.get("day_range_position") or {}
    swing = lens.get("swing_structure") or {}
    candidates: list[float] = []
    if side == "buy":
        day_hi = drp.get("day_hi")
        if day_hi is not None and float(day_hi) > entry:
            candidates.append(float(day_hi))
        swing_high = (swing.get("last_swing_high") or {}).get("price")
        if swing_high is not None and float(swing_high) > entry:
            candidates.append(float(swing_high))
        return min(candidates) if candidates else None
    day_lo = drp.get("day_lo")
    if day_lo is not None and float(day_lo) < entry:
        candidates.append(float(day_lo))
    swing_low = (swing.get("last_swing_low") or {}).get("price")
    if swing_low is not None and float(swing_low) < entry:
        candidates.append(float(swing_low))
    return max(candidates) if candidates else None


def _compute_tp(
    side: str, entry: float, sl: float, lens: dict[str, Any], spread_abs: float
) -> tuple[float, dict[str, Any]]:
    sl_dist = abs(entry - sl)
    min_tp_dist_cost_guard = MIN_TP_SPREAD_MULT * spread_abs
    min_tp_dist_rr = MIN_REWARD_RISK * sl_dist
    # Small safety margin above the exact floor: entry/sl/tp are each rounded
    # independently to 5dp in the final Decision, and sl itself is already a
    # rounded/clamped value by the time this runs. Sitting exactly ON the RR
    # floor pre-rounding can realize fractionally BELOW 1.2 post-rounding
    # (observed live via the property test: RR=1.19998... on seed=2) — the
    # margin absorbs that rounding error so the floor holds for the actual
    # returned values, not just the pre-rounding intermediate math.
    min_tp_dist = max(min_tp_dist_cost_guard, min_tp_dist_rr) * (1.0 + _RR_ROUNDING_SAFETY_MARGIN)

    shelf = _nearest_favorable_shelf(lens, side, entry)
    source = "natural_target_shelf_or_swing"
    if shelf is not None:
        natural_dist = abs(shelf - entry)
    else:
        natural_dist = 0.0
        source = "no_natural_target"

    if natural_dist >= min_tp_dist:
        dist = natural_dist
    else:
        # Natural target too near (or absent) -> extend to satisfy BOTH the
        # RR floor and the cost guard (per spec: "extend to 1.2*SL distance"
        # read together with the 8*spread cost guard already folded into
        # min_tp_dist above, so the extension always clears both floors,
        # plus the rounding safety margin above).
        dist = min_tp_dist
        source = f"{source}_extended" if shelf is not None else "extended_min_tp_dist"

    tp = entry + dist if side == "buy" else entry - dist
    detail = {
        "source": source,
        "natural_distance": natural_dist,
        "final_distance": dist,
        "min_tp_dist_cost_guard": min_tp_dist_cost_guard,
        "min_tp_dist_rr": min_tp_dist_rr,
    }
    return tp, detail


# ---------------------------------------------------------------------------
# reasons (bilingual Thai/English, per project convention)
# ---------------------------------------------------------------------------


def _top_committee_members(committee: dict[str, dict[str, Any]], n: int = 3) -> list[tuple[str, dict[str, Any]]]:
    ranked = sorted(committee.items(), key=lambda kv: abs(kv[1]["weighted"]), reverse=True)
    return ranked[:n]


def _build_reasons(
    side: str,
    conviction: float,
    dominant: str,
    committee: dict[str, dict[str, Any]],
    sl_detail: dict[str, Any],
    tp_detail: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    reasons.append(
        f"HUNT MODE: ทุก M5 close ต้องมี action / participation-first, side={side}, "
        f"dominant={dominant}"
    )
    top = _top_committee_members(committee)
    top_txt = "; ".join(f"{name}=vote:{m['vote']:.2f}*w:{m['weight']:.2f}" for name, m in top)
    reasons.append(f"คณะกรรมการทิศทาง (ท็อป 3) / direction committee (top 3): {top_txt}")
    reasons.append(
        f"geometry: SL={sl_detail['source']} dist={sl_detail['clamped_distance']:.5f}; "
        f"TP={tp_detail['source']} dist={tp_detail['final_distance']:.5f}"
    )
    reasons.append(
        f"conviction={conviction:.3f} (|weighted_sum|/total_weight) / ความเชื่อมั่นของคณะกรรมการ"
    )
    return reasons


# ---------------------------------------------------------------------------
# hard vetoes — the ONLY allowed skip paths
# ---------------------------------------------------------------------------


def _veto_skip(*, ts_close: str, symbol: str, reason: str, features: dict[str, Any]) -> Decision:
    """Construct a skip Decision for a hard veto. The ONLY place 'skip' is built."""
    return Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="skip",
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=0.0,
        p_win_est=0.0,
        setup="none",
        reasons=[f"HARD VETO / ยับยั้งบังคับ: {reason}"],
        features=features,
    )


def _check_hard_vetoes(
    symbol: str,
    m5_bars: list[Bar],
    spread_abs: float,
    config: HuntConfig,
    ts_close: str,
    features: dict[str, Any],
) -> Decision | None:
    """Return a veto Decision, or None if the caller may proceed to enter.

    Exactly 3 hard vetoes, per spec: (1) bars_m5 < 60, (2) spread_abs <= 0 OR
    an invalid quote (combined into one veto category — either condition
    means the geometry inputs cannot be trusted), (3) spread_bps over the
    configured cap. Nothing else may ever produce a skip.
    """
    if len(m5_bars) < MIN_BARS_M5:
        return _veto_skip(
            ts_close=ts_close,
            symbol=symbol,
            reason=f"bars_m5={len(m5_bars)} < required {MIN_BARS_M5}",
            features=features,
        )
    last_close = _f(m5_bars[-1].get("close"))
    if spread_abs is None or spread_abs <= 0 or last_close <= 0:
        return _veto_skip(
            ts_close=ts_close,
            symbol=symbol,
            reason=(
                f"spread_abs <= 0 or invalid quote: spread_abs={spread_abs!r}, last_close={last_close!r}"
            ),
            features=features,
        )
    spread_bps = (spread_abs / last_close) * 10000.0
    if spread_bps > config.max_spread_bps:
        return _veto_skip(
            ts_close=ts_close,
            symbol=symbol,
            reason=f"spread_bps={spread_bps:.3f} > max_spread_bps={config.max_spread_bps}",
            features=features,
        )
    return None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def decide_hunt(
    symbol: str,
    m5_bars: list[Bar],
    m15_bars: list[Bar] | None,
    h1_bars: list[Bar] | None,
    lens: dict[str, Any] | None,
    spread_abs: float,
    config: HuntConfig | None = None,
    journal_stats: dict[str, Any] | None = None,
) -> Decision:
    """HUNT MODE decision: participation-first, always ACTION on every M5 close.

    Returns a ``hunter_brain.Decision``. Only the three hard vetoes (bars_m5
    < 60, invalid spread/quote, spread_bps over the configured cap) may
    produce ``action='skip'``; every other path returns ``action='enter'``
    with a fully-specified side/geometry/size_class.
    """
    cfg = config or HuntConfig()
    m15 = list(m15_bars or [])
    h1 = list(h1_bars or [])
    ts_close = _bar_close_ts(str(m5_bars[-1]["ts"])) if m5_bars else ""

    lens_computed = lens if lens is not None else (_safe_run_lens(m5_bars, ts_close) if m5_bars else {})
    features_snapshot = {"symbol": symbol, "bar_count_m5": len(m5_bars)}
    features_snapshot.update(lens_computed)

    veto = _check_hard_vetoes(symbol, m5_bars, spread_abs, cfg, ts_close, features_snapshot)
    if veto is not None:
        return veto

    committee = _run_committee(lens_computed, m15, h1)
    raw_side, raw_conviction, dominant = _committee_side_and_conviction(committee, lens_computed)

    # FIX 3 (2026-07-07): trend-agreement guard — reshapes side/conviction
    # only, never participation (see _apply_trend_agreement_guard docstring).
    side, conviction, trend_guard_detail = _apply_trend_agreement_guard(raw_side, raw_conviction, committee)

    entry = _f(m5_bars[-1].get("close"))
    sl, sl_detail = _compute_sl(side, entry, lens_computed, m5_bars, spread_abs)
    tp, tp_detail = _compute_tp(side, entry, sl, lens_computed, spread_abs)

    size_class = "small" if conviction >= CONVICTION_SMALL_FLOOR else "scout"
    base_p_win_est = round(_clip(P_WIN_BASE + P_WIN_CONVICTION_SLOPE * conviction, 0.0, 1.0), 4)
    setup = f"hunt_{dominant}"
    session_label = str((lens_computed.get("session_context") or {}).get("value") or "unknown")
    p_win_est = empirical_stats.blended_p_win(base_p_win_est, setup, session_label, journal_stats)

    reasons = _build_reasons(side, conviction, dominant, committee, sl_detail, tp_detail)
    if trend_guard_detail.get("fired"):
        reasons.append(
            "TREND GUARD / ป้องกันสวนเทรนด์: "
            f"raw_side={raw_side} vs trend_side={trend_guard_detail.get('trend_side')} "
            f"(agreement={trend_guard_detail.get('trend_agreement')}) -> final_side={side}, "
            f"conviction {raw_conviction:.3f}->{conviction:.3f} ({trend_guard_detail.get('reason')})"
        )

    features_snapshot["hunt_committee"] = committee
    features_snapshot["hunt_geometry"] = {"sl": sl_detail, "tp": tp_detail}
    features_snapshot["hunt_conviction"] = conviction
    features_snapshot["hunt_dominant_member"] = dominant
    features_snapshot["hunt_raw_side"] = raw_side
    features_snapshot["hunt_raw_conviction"] = raw_conviction
    features_snapshot["hunt_trend_guard"] = trend_guard_detail
    features_snapshot["empirical_p_win"] = {
        "base": base_p_win_est,
        "blended": p_win_est,
        "applied": p_win_est != base_p_win_est,
        "setup": setup,
        "session": session_label,
    }

    # session bucket for the learner — the SAME session_context label
    # hunter_brain keys (setup, session) on; carried by the executor into
    # entry_executed so every close (manual/OM/broker-side) lands in
    # empirical_stats under a blendable key. HUNT is the LIVE entry producer
    # (DEXTER3_HUNT=1 in the VM units), so without this every live outcome
    # journaled session="" and the learner skipped it (found 2026-07-11 in
    # the first vanish-reconcile backfill: session=None on every row).
    return Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="enter",
        side=side,
        entry_type="market",
        entry=round(entry, 5),
        sl=round(sl, 5),
        tp=round(tp, 5),
        size_class=size_class,
        leader_score=round(conviction, 4),
        p_win_est=p_win_est,
        setup=setup,
        reasons=reasons,
        features=features_snapshot,
        session=session_label,
    )


def _safe_run_lens(m5_bars: list[Bar], ts_close: str) -> dict[str, Any]:
    """Compute the full lens feature set from M5 bars (mirrors hunter_brain._run_lens).

    Only called when the caller did not already supply a precomputed lens —
    shadow_runner/callers are expected to pass ``lens`` explicitly (one lens
    computation per M5 close, shared with hunter_brain), but this keeps
    ``decide_hunt`` usable standalone (e.g. in tests) without forcing every
    caller to pre-run market_lens by hand.
    """
    return {
        "swing_structure": market_lens.swing_structure(m5_bars),
        "liquidity_sweep": market_lens.liquidity_sweep(m5_bars),
        "displacement": market_lens.displacement(m5_bars),
        "compression_release": market_lens.compression_release(m5_bars),
        "close_location_pressure": market_lens.close_location_pressure(m5_bars),
        "day_range_position": market_lens.day_range_position(m5_bars),
        "session_context": market_lens.session_context(ts_close),
        "volatility_state": market_lens.volatility_state(m5_bars),
    }

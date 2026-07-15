"""Dexter3 PRICE ACTION EYE — Layer 1 Bar Anatomy Reader (Phase A, 2026-07-15).

Design: ``docs/DEXTER3_PRICE_ACTION_EYE_DESIGN.md`` ("Layer 1 — Bar Anatomy
Reader" + "Pattern Catalog"). Owner seed idea: make both lanes price-action
intelligent — candle anatomy, down to trend bars — so the system SEES what
the owner sees on a chart (wick rejection at a minor level, a bullish close
before a sell signal) instead of trading through it.

GOVERNING PHILOSOPHY (owner-endorsed, explicit in the design doc's
"Promotion policy"): detectors are born POWERLESS. Every function in this
module is a PURE, JOURNALED FEATURE — none of them block, resize, or veto a
trade. Power is earned later, per-detector, only after its own
journaled-vs-outcome replay evidence shows separation on real M1/M5 data
(the design doc's promotion gate). Phase A ships OFF/SHADOW modes only —
see ``dexter3/shadow_runner.py::_apply_pa_eye_shadow``.

No I/O, no MCP, no randomness, no mutation of the input ``bars``. Every
function takes the same OHLC bar-dict shape the lanes already fetch
(``open``/``high``/``low``/``close``/``ts``, ``volume`` optional and unused
in Phase A) and returns a small evidence dict, mirroring the conventions
already established by ``dexter3/market_lens.py`` and
``dexter3/edge_buckets.py`` — several primitives (``body_ratio``,
``close_location``, wick ratios, ``true_ranges``, ``swing_structure``,
``liquidity_sweep``) are reused directly from ``market_lens`` rather than
re-derived, since those are already the repo's proven pure price-action
primitives (unlike ``edge_buckets.py``'s deliberate duplication of a
``scripts/`` sweep script, there is no "never touch scripts/" constraint
here to justify reinventing them).

Public API: ``evaluate(bars, side) -> {"features": {...}, "verdict": ...,
"verdict_reasons": [...]}`` — see its docstring below for the full contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dexter3 import market_lens

Bar = dict[str, Any]

MIN_BARS_FOR_EVALUATION = 5


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# config — every threshold env-tunable via
# dexter3/shadow_runner.py::_pa_eye_config_from_env, defaults documented here
# with a one-line rationale each (owner directive 2026-07-10: "no careless
# hardcode" — every magic number gets a stated reason).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceActionEyeConfig:
    """Bar-anatomy detector thresholds (Phase A, Layer 1).

    wick_frac_min:
        DEXTER3_PA_EYE_WICK_FRAC_MIN (default 0.5). A wick must be AT LEAST
        half the bar's range to count as a rejection wick / hammer /
        shooting-star wick — below half, the body dominates the bar's
        character. Matches the design doc's stated Layer-1 default exactly.
    trend_body_frac_min:
        DEXTER3_PA_EYE_TREND_BODY_FRAC_MIN (default 0.6). Brooks' rule-of-
        thumb trend-bar body threshold: >=60% body-to-range is a directional
        bar, not noise. Matches the design doc's stated default.
    doji_body_frac_max:
        DEXTER3_PA_EYE_DOJI_BODY_FRAC_MAX (default 0.3). <=30% body marks
        indecision (doji) — half the trend-bar floor, leaving 0.3-0.6 as an
        ordinary/ambiguous bar where neither classification fires. Matches
        the design doc's stated default. Also reused as the "closed near
        the bar's edge" threshold for hammer/shooting-star (close_pos
        >= 1 - doji_body_frac_max / <= doji_body_frac_max) to avoid adding a
        near-duplicate knob for a closely related concept.
    exhaustion_atr_mult:
        DEXTER3_PA_EYE_EXHAUSTION_ATR_MULT (default 2.0). A bar's range at
        >=2x the rolling ATR is climactic ("range >> ATR" per the design
        doc) — mirrors market_lens.displacement's "hot" quantile intuition,
        expressed in ATR units for the anatomy layer.
    atr_window:
        DEXTER3_PA_EYE_ATR_WINDOW (default 14). Classic Wilder ATR lookback.
    swing_window:
        DEXTER3_PA_EYE_SWING_WINDOW (default 20). Matches
        market_lens.swing_structure's own default lookback — same "recent
        micro-structure" horizon already proven in this repo.
    swing_pivot_span:
        DEXTER3_PA_EYE_SWING_PIVOT_SPAN (default 2). Matches
        market_lens.swing_structure's own pivot_span (2 bars each side
        confirm a swing point).
    near_level_atr_dist:
        DEXTER3_PA_EYE_NEAR_LEVEL_ATR_DIST (default 0.5). "Near" a swing
        level means within half an ATR of it — close enough that the
        level's rejection/reclaim dynamics plausibly still apply.
    round_number_grid:
        DEXTER3_PA_EYE_ROUND_NUMBER_GRID (default 5.0). XAUUSD's round-
        number magnet spacing (whole $5 handles) — the design doc's stated
        XAU default verbatim.
    round_number_atr_frac:
        DEXTER3_PA_EYE_ROUND_NUMBER_ATR_FRAC (default 0.25). "Near" a round
        number means within a quarter-ATR of the nearest grid line.
    always_in_window:
        DEXTER3_PA_EYE_ALWAYS_IN_WINDOW (default 10). Brooks' "always-in"
        direction is read from a rolling ~10-bar window of trend-bar
        dominance — long enough to filter single-bar noise, short enough to
        flip promptly at a real change of character.
    microchannel_min_bars:
        DEXTER3_PA_EYE_MICROCHANNEL_MIN_BARS (default 4). Brooks calls 3+
        consecutive same-direction bars with no opposite-side pullback a
        microchannel; 4 is used as the "counts as a channel" floor so a bare
        3-bar run (the textbook minimum) does not over-fire the flag.
    rejection_cluster_min / rejection_cluster_lookback:
        DEXTER3_PA_EYE_REJECTION_CLUSTER_MIN (default 2) /
        DEXTER3_PA_EYE_REJECTION_CLUSTER_LOOKBACK (default 3). The v0
        verdict's rejection-cluster rule is literally ">=2 of the last 3
        bars" per the 2026-07-15 fable regression fixture — these ARE that
        fixture's own numbers, not independently chosen.
    trading_range_overlap_min:
        DEXTER3_PA_EYE_TRADING_RANGE_OVERLAP_MIN (default 0.7). Bars whose
        ranges overlap their immediate predecessor by >=70% on average are
        "chopping in place" -> trading_range flag.
    """

    wick_frac_min: float = 0.5
    trend_body_frac_min: float = 0.6
    doji_body_frac_max: float = 0.3
    exhaustion_atr_mult: float = 2.0
    atr_window: int = 14
    swing_window: int = 20
    swing_pivot_span: int = 2
    near_level_atr_dist: float = 0.5
    round_number_grid: float = 5.0
    round_number_atr_frac: float = 0.25
    always_in_window: int = 10
    microchannel_min_bars: int = 4
    rejection_cluster_min: int = 2
    rejection_cluster_lookback: int = 3
    trading_range_overlap_min: float = 0.7


# ---------------------------------------------------------------------------
# ATR (single reading reused across every detector in one evaluate() call —
# recomputing a distinct trailing ATR per detector/per-bar would add
# complexity without changing any threshold decision at M5 timeframes).
# ---------------------------------------------------------------------------


def _atr_excluding_last(bars: list[Bar], window: int) -> float:
    """Rolling ATR (simple mean of true ranges) computed from ``bars[:-1]``
    — i.e. EXCLUDING the most recently closed bar — so a bar's own range is
    always compared against a baseline it cannot inflate itself. Returns 0.0
    when there is not enough history (callers must treat 0.0 as "ATR
    unavailable", not "zero volatility")."""
    if len(bars) < 2:
        return 0.0
    trs = market_lens.true_ranges(bars[:-1])
    if not trs:
        return 0.0
    sample = trs[-window:] if len(trs) > window else trs
    return sum(sample) / len(sample) if sample else 0.0


# ---------------------------------------------------------------------------
# single-bar anatomy
# ---------------------------------------------------------------------------


def _classify_bar(bar: Bar, atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Per-bar anatomy: body/wick fractions, close position, ATR ratio, and
    the trend-bar / doji / pin-bar / exhaustion classifications."""
    body = market_lens.body_ratio(bar)
    upper_wick = market_lens.upper_wick_ratio(bar)
    lower_wick = market_lens.lower_wick_ratio(bar)
    close_pos = market_lens.close_location(bar)
    rng = market_lens.bar_range(bar)
    range_atr_ratio = (rng / atr_ref) if atr_ref > 0 else 0.0

    direction = "buy" if market_lens.is_green(bar) else ("sell" if market_lens.is_red(bar) else None)

    is_trend_bar = False
    trend_direction: str | None = None
    if body >= cfg.trend_body_frac_min and direction is not None:
        leading_third = (close_pos >= (2.0 / 3.0)) if direction == "buy" else (close_pos <= (1.0 / 3.0))
        if leading_third:
            is_trend_bar = True
            trend_direction = direction

    is_doji = body <= cfg.doji_body_frac_max
    strong_close_hi = close_pos >= (1.0 - cfg.doji_body_frac_max)
    strong_close_lo = close_pos <= cfg.doji_body_frac_max
    is_hammer = lower_wick >= cfg.wick_frac_min and strong_close_hi
    is_shooting_star = upper_wick >= cfg.wick_frac_min and strong_close_lo
    is_exhaustion = range_atr_ratio >= cfg.exhaustion_atr_mult

    return {
        "body_frac": round(body, 4),
        "upper_wick_frac": round(upper_wick, 4),
        "lower_wick_frac": round(lower_wick, 4),
        "close_pos": round(close_pos, 4),
        "range_atr_ratio": round(range_atr_ratio, 4),
        "direction": direction,
        "is_trend_bar": is_trend_bar,
        "trend_direction": trend_direction,
        "is_doji": bool(is_doji),
        "is_hammer": bool(is_hammer),
        "is_shooting_star": bool(is_shooting_star),
        "is_exhaustion": bool(is_exhaustion),
    }


def _rejection_cluster(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig, wick_side: str) -> dict[str, Any]:
    """Count of the last ``rejection_cluster_lookback`` bars showing a
    rejection wick on ``wick_side`` ('upper' opposes BUY at resistance,
    'lower' opposes SELL at support) with a weak/opposite close. This is the
    EXACT shape of the 2026-07-15 fable regression fixture."""
    n = min(cfg.rejection_cluster_lookback, len(bars))
    sample = bars[-n:] if n > 0 else []
    count = 0
    for b in sample:
        anatomy = _classify_bar(b, atr_ref, cfg)
        if wick_side == "upper" and anatomy["upper_wick_frac"] >= cfg.wick_frac_min and anatomy["close_pos"] <= 0.5:
            count += 1
        elif wick_side == "lower" and anatomy["lower_wick_frac"] >= cfg.wick_frac_min and anatomy["close_pos"] >= 0.5:
            count += 1
    return {
        "count": count,
        "lookback": n,
        "value": count >= cfg.rejection_cluster_min,
        "evidence": f"{count}/{n} bars show {wick_side}-wick rejection (min={cfg.rejection_cluster_min})",
    }


# ---------------------------------------------------------------------------
# multi-bar
# ---------------------------------------------------------------------------


def _inside_outside_bar(bars: list[Bar]) -> dict[str, Any]:
    if len(bars) < 2:
        return {"inside": False, "outside": False, "evidence": "insufficient_bars"}
    prev, cur = bars[-2], bars[-1]
    p_hi, p_lo = _f(prev.get("high")), _f(prev.get("low"))
    c_hi, c_lo = _f(cur.get("high")), _f(cur.get("low"))
    inside = c_hi <= p_hi and c_lo >= p_lo
    outside = (c_hi >= p_hi and c_lo <= p_lo) and not inside
    return {
        "inside": bool(inside),
        "outside": bool(outside),
        "evidence": f"cur=[{c_lo:.4f},{c_hi:.4f}] vs prev=[{p_lo:.4f},{p_hi:.4f}]",
    }


def _engulfing(bars: list[Bar]) -> dict[str, Any]:
    if len(bars) < 2:
        return {"value": False, "direction": None, "evidence": "insufficient_bars"}
    prev, cur = bars[-2], bars[-1]
    p_o, p_c = _f(prev.get("open")), _f(prev.get("close"))
    c_o, c_c = _f(cur.get("open")), _f(cur.get("close"))
    bullish = market_lens.is_red(prev) and market_lens.is_green(cur) and c_o <= p_c and c_c >= p_o
    bearish = market_lens.is_green(prev) and market_lens.is_red(cur) and c_o >= p_c and c_c <= p_o
    direction = "buy" if bullish else ("sell" if bearish else None)
    return {
        "value": direction is not None,
        "direction": direction,
        "evidence": f"prev=({p_o:.4f}->{p_c:.4f}) cur=({c_o:.4f}->{c_c:.4f})",
    }


def _two_bar_reversal(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """A prior trend bar followed by a strong opposite-direction close beyond
    its midpoint — a classical two-bar reversal."""
    if len(bars) < 2:
        return {"value": False, "direction": None, "evidence": "insufficient_bars"}
    prev, cur = bars[-2], bars[-1]
    prev_anatomy = _classify_bar(prev, atr_ref, cfg)
    if not prev_anatomy["is_trend_bar"]:
        return {"value": False, "direction": None, "evidence": "prior_bar_not_trend_bar"}
    cur_anatomy = _classify_bar(cur, atr_ref, cfg)
    prev_dir = prev_anatomy["trend_direction"]
    opposite = "sell" if prev_dir == "buy" else "buy"
    prev_mid = (_f(prev.get("high")) + _f(prev.get("low"))) / 2.0
    cur_close = _f(cur.get("close"))
    beyond_mid = (cur_close < prev_mid) if opposite == "sell" else (cur_close > prev_mid)
    reversed_strong = (
        cur_anatomy["direction"] == opposite and cur_anatomy["body_frac"] >= cfg.trend_body_frac_min and beyond_mid
    )
    return {
        "value": bool(reversed_strong),
        "direction": opposite if reversed_strong else None,
        "evidence": (
            f"prior_trend={prev_dir} cur_dir={cur_anatomy['direction']} "
            f"cur_body={cur_anatomy['body_frac']:.2f} prev_mid={prev_mid:.4f} cur_close={cur_close:.4f}"
        ),
    }


def _microchannel(bars: list[Bar], cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Length of the consecutive-bar run ending at the last bar with no
    opposite-side pullback: bull = each low > the prior bar's low, bear =
    each high < the prior bar's high."""
    if len(bars) < 2:
        return {"length": 0, "direction": None, "value": False, "evidence": "insufficient_bars"}
    length_bull = 1
    for i in range(len(bars) - 1, 0, -1):
        if _f(bars[i].get("low")) > _f(bars[i - 1].get("low")):
            length_bull += 1
        else:
            break
    length_bear = 1
    for i in range(len(bars) - 1, 0, -1):
        if _f(bars[i].get("high")) < _f(bars[i - 1].get("high")):
            length_bear += 1
        else:
            break
    if length_bull >= length_bear and length_bull >= cfg.microchannel_min_bars:
        return {"length": length_bull, "direction": "buy", "value": True, "evidence": f"{length_bull} consecutive higher-lows"}
    if length_bear > length_bull and length_bear >= cfg.microchannel_min_bars:
        return {"length": length_bear, "direction": "sell", "value": True, "evidence": f"{length_bear} consecutive lower-highs"}
    return {
        "length": max(length_bull, length_bear),
        "direction": None,
        "value": False,
        "evidence": f"bull_len={length_bull} bear_len={length_bear} < min={cfg.microchannel_min_bars}",
    }


def _pullback_second_entry_count(bars: list[Bar], cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Approximate Brooks 'H2'/'L2' pullback-count: number of confirmed
    MICRO (pivot_span=1) swing pullback points within the current clean-trend
    window. Deliberately approximate — the formal Brooks count also weighs
    pullback depth/duration, which this journaled-only feature does not
    model. Report-only: no verdict weight in v0."""
    window = min(len(bars), cfg.swing_window)
    sample = bars[-window:] if window > 0 else []
    micro = market_lens.swing_structure(sample, lookback=window, pivot_span=1)
    swing_val = micro.get("value")
    if swing_val == "uptrend":
        lows = micro.get("swing_lows") or []
        return {"direction": "buy", "count": len(lows), "evidence": "confirmed micro pullback lows in the uptrend window"}
    if swing_val == "downtrend":
        highs = micro.get("swing_highs") or []
        return {"direction": "sell", "count": len(highs), "evidence": "confirmed micro pullback highs in the downtrend window"}
    return {"direction": None, "count": 0, "evidence": f"no clean trend (swing={swing_val})"}


def _three_push_wedge(swing: dict[str, Any]) -> dict[str, Any]:
    """Report-only heuristic: three consecutive swing points in the same
    direction with CONTRACTING increments (each push smaller than the last)
    — the classic Brooks wedge shape. No verdict weight in v0."""
    highs = swing.get("swing_highs") or []
    lows = swing.get("swing_lows") or []
    if len(highs) >= 3:
        p1, p2, p3 = highs[-3]["price"], highs[-2]["price"], highs[-1]["price"]
        inc1, inc2 = p2 - p1, p3 - p2
        if inc1 > 0 and 0 < inc2 < inc1:
            return {"value": True, "direction": "buy", "evidence": f"pushes {p1:.4f}->{p2:.4f}->{p3:.4f} contracting"}
    if len(lows) >= 3:
        p1, p2, p3 = lows[-3]["price"], lows[-2]["price"], lows[-1]["price"]
        inc1, inc2 = p1 - p2, p2 - p3
        if inc1 > 0 and 0 < inc2 < inc1:
            return {"value": True, "direction": "sell", "evidence": f"pushes {p1:.4f}->{p2:.4f}->{p3:.4f} contracting"}
    return {"value": False, "direction": None, "evidence": "no_three_push_contraction"}


def _breakout_follow_through(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Breakout bar identified at index -2 (a trend-bar break of the swing
    level formed by bars[:-2]); bar[-1] is then classified as follow_through
    (continues beyond the breakout bar) or failed_break (closes back inside
    the pre-breakout level — the one-bar false break)."""
    if len(bars) < 4:
        return {"is_breakout": False, "direction": None, "follow_through": False, "failed_break": False, "evidence": "insufficient_bars"}
    base = bars[:-2]
    swing = market_lens.swing_structure(base, lookback=cfg.swing_window, pivot_span=cfg.swing_pivot_span)
    last_high = (swing.get("last_swing_high") or {}).get("price")
    last_low = (swing.get("last_swing_low") or {}).get("price")
    breakout_bar = bars[-2]
    confirm_bar = bars[-1]
    breakout_anatomy = _classify_bar(breakout_bar, atr_ref, cfg)
    breakout_close = _f(breakout_bar.get("close"))

    direction: str | None = None
    is_breakout = False
    if (
        last_high is not None
        and breakout_anatomy["is_trend_bar"]
        and breakout_anatomy["trend_direction"] == "buy"
        and breakout_close > float(last_high)
    ):
        direction, is_breakout = "buy", True
    elif (
        last_low is not None
        and breakout_anatomy["is_trend_bar"]
        and breakout_anatomy["trend_direction"] == "sell"
        and breakout_close < float(last_low)
    ):
        direction, is_breakout = "sell", True

    follow_through = False
    failed_break = False
    if is_breakout:
        confirm_close = _f(confirm_bar.get("close"))
        if direction == "buy":
            follow_through = confirm_close >= breakout_close
            failed_break = confirm_close < float(last_high)
        else:
            follow_through = confirm_close <= breakout_close
            failed_break = confirm_close > float(last_low)

    return {
        "is_breakout": is_breakout,
        "direction": direction,
        "follow_through": bool(follow_through),
        "failed_break": bool(failed_break),
        "evidence": f"is_breakout={is_breakout} dir={direction} follow_through={follow_through} failed_break={failed_break}",
    }


def _trend_follow_through(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Two-bar continuation used by the v0 'support' verdict: bar[-2] is a
    trend bar in direction D and bar[-1] is ALSO a trend bar in the same
    direction — distinct from ``_breakout_follow_through`` above, which
    requires a prior swing-level break; this is pure anatomy continuation."""
    if len(bars) < 2:
        return {"value": False, "direction": None, "evidence": "insufficient_bars"}
    prev_anatomy = _classify_bar(bars[-2], atr_ref, cfg)
    cur_anatomy = _classify_bar(bars[-1], atr_ref, cfg)
    if (
        prev_anatomy["is_trend_bar"]
        and cur_anatomy["is_trend_bar"]
        and prev_anatomy["trend_direction"] == cur_anatomy["trend_direction"]
    ):
        return {"value": True, "direction": cur_anatomy["trend_direction"], "evidence": "two consecutive same-direction trend bars"}
    return {"value": False, "direction": None, "evidence": "no two-bar same-direction trend continuation"}


# ---------------------------------------------------------------------------
# structure / location
# ---------------------------------------------------------------------------


def _swing_and_location(swing: dict[str, Any], bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    close = _f(bars[-1].get("close")) if bars else 0.0
    last_high = swing.get("last_swing_high") or {}
    last_low = swing.get("last_swing_low") or {}

    dist_high_atr = None
    at_minor_resistance = False
    if last_high.get("price") is not None and atr_ref > 0:
        dist_high_atr = abs(close - float(last_high["price"])) / atr_ref
        at_minor_resistance = dist_high_atr <= cfg.near_level_atr_dist

    dist_low_atr = None
    at_minor_support = False
    if last_low.get("price") is not None and atr_ref > 0:
        dist_low_atr = abs(close - float(last_low["price"])) / atr_ref
        at_minor_support = dist_low_atr <= cfg.near_level_atr_dist

    return {
        "swing": swing.get("value"),
        "last_swing_high": last_high or None,
        "last_swing_low": last_low or None,
        "distance_to_resistance_atr": round(dist_high_atr, 4) if dist_high_atr is not None else None,
        "distance_to_support_atr": round(dist_low_atr, 4) if dist_low_atr is not None else None,
        "at_minor_resistance": bool(at_minor_resistance),
        "at_minor_support": bool(at_minor_support),
    }


def _bar_position_in_leg(swing: dict[str, Any], bars: list[Bar], cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Bars since the most recently confirmed swing point (high or low,
    whichever is more recent) — a proxy for "how far into the current leg
    are we" (design doc: "bar position in current leg (bars since last
    swing flip)")."""
    last_high = swing.get("last_swing_high") or {}
    last_low = swing.get("last_swing_low") or {}
    sample_len = min(len(bars), cfg.swing_window)
    candidates = [x["index"] for x in (last_high, last_low) if x.get("index") is not None]
    if not candidates:
        return {"value": None, "evidence": "no_confirmed_swing_point"}
    most_recent_index = max(candidates)
    bars_since = (sample_len - 1) - most_recent_index
    return {"value": bars_since, "evidence": f"{bars_since} bars since the most recent confirmed swing point"}


def _round_number_proximity(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    if not bars:
        return {"value": False, "distance": None, "nearest": None, "evidence": "no_bars"}
    close = _f(bars[-1].get("close"))
    grid = cfg.round_number_grid
    if grid <= 0:
        return {"value": False, "distance": None, "nearest": None, "evidence": "grid_disabled"}
    nearest = round(close / grid) * grid
    distance = abs(close - nearest)
    threshold = (atr_ref * cfg.round_number_atr_frac) if atr_ref > 0 else (grid * 0.1)
    near = distance <= threshold
    return {
        "value": bool(near),
        "distance": round(distance, 4),
        "nearest": round(nearest, 4),
        "threshold": round(threshold, 4),
        "evidence": f"close={close:.4f} nearest_grid={nearest:.4f} dist={distance:.4f} thresh={threshold:.4f}",
    }


def _trading_range(bars: list[Bar], cfg: PriceActionEyeConfig, window: int = 8) -> dict[str, Any]:
    """Mean overlap ratio of each bar's range against its immediate
    predecessor's range, over the last ``window`` bars — a "chopping in
    place" proxy for the design doc's "trading-range vs trend day
    detection" (measured at M5-window scale, not literal trading-day
    scale — no day-boundary data is threaded into this pure module)."""
    sample = bars[-window:] if len(bars) >= window else bars
    if len(sample) < 3:
        return {"value": False, "overlap_ratio": None, "evidence": "insufficient_bars"}
    overlaps: list[float] = []
    for prev, cur in zip(sample, sample[1:]):
        p_hi, p_lo = _f(prev.get("high")), _f(prev.get("low"))
        c_hi, c_lo = _f(cur.get("high")), _f(cur.get("low"))
        p_rng = max(1e-9, p_hi - p_lo)
        overlap = max(0.0, min(p_hi, c_hi) - max(p_lo, c_lo))
        overlaps.append(overlap / p_rng)
    mean_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0
    return {
        "value": mean_overlap >= cfg.trading_range_overlap_min,
        "overlap_ratio": round(mean_overlap, 4),
        "window": len(sample),
        "evidence": f"mean_overlap={mean_overlap:.3f} over {len(sample)} bars",
    }


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def _always_in_direction(bars: list[Bar], atr_ref: float, cfg: PriceActionEyeConfig) -> dict[str, Any]:
    """Brooks-style 'always-in' direction: which side's trend bars dominate
    the last ``always_in_window`` bars, cross-checked against
    market_lens.swing_structure's own classification. Returns
    'long'|'short'|'neutral'."""
    window = min(cfg.always_in_window, len(bars))
    sample = bars[-window:] if window > 0 else []
    if len(sample) < 3:
        return {"value": "neutral", "bull_trend_bars": 0, "bear_trend_bars": 0, "evidence": "insufficient_bars"}
    bull = 0
    bear = 0
    for b in sample:
        anatomy = _classify_bar(b, atr_ref, cfg)
        if anatomy["is_trend_bar"] and anatomy["trend_direction"] == "buy":
            bull += 1
        elif anatomy["is_trend_bar"] and anatomy["trend_direction"] == "sell":
            bear += 1
    swing_val = market_lens.swing_structure(bars, lookback=cfg.swing_window, pivot_span=cfg.swing_pivot_span).get("value")

    if bull > bear and swing_val in ("uptrend", "transition"):
        direction = "long"
    elif bear > bull and swing_val in ("downtrend", "transition"):
        direction = "short"
    elif bull > bear:
        direction = "long"
    elif bear > bull:
        direction = "short"
    else:
        direction = "neutral"

    return {
        "value": direction,
        "bull_trend_bars": bull,
        "bear_trend_bars": bear,
        "swing": swing_val,
        "evidence": f"bull={bull} bear={bear} swing={swing_val} -> {direction}",
    }


# ---------------------------------------------------------------------------
# verdict (v0 — conservative, high-confidence anatomy ONLY, else neutral)
# ---------------------------------------------------------------------------


def _resolve_verdict(side: str, features: dict[str, Any], cfg: PriceActionEyeConfig) -> tuple[str, list[str]]:
    swing_location = features["swing_location"]
    at_res = swing_location["at_minor_resistance"]
    at_sup = swing_location["at_minor_support"]
    sweep = features["sweep_and_reclaim"]
    always_in_val = features["always_in"]["value"]
    last_bar = features["last_bar"]

    if side == "buy":
        cluster_oppose = bool(features["rejection_cluster_upper"]["value"]) and at_res
        # A sell-side sweep-and-reclaim (wick pokes above a swing high,
        # closes back below it) AT that same resistance is the mirror-image
        # single-bar oppose signal to the SELL note below — kept symmetric
        # even though only the SELL case was an explicit regression fixture.
        sweep_oppose = bool(sweep.get("value")) and sweep.get("side") == "sell" and at_res
        always_in_oppose = always_in_val == "short" and last_bar["is_trend_bar"] and last_bar["trend_direction"] == "sell"

        reasons: list[str] = []
        if cluster_oppose:
            reasons.append(f"rejection_cluster_at_resistance: {features['rejection_cluster_upper']['evidence']}")
        if sweep_oppose:
            reasons.append(f"sweep_and_reclaim_against_buy_at_resistance: {sweep.get('evidence')}")
        if always_in_oppose:
            reasons.append("always_in_short_with_bear_trend_bar")
        if reasons:
            return "oppose", reasons

        support = (
            always_in_val == "long"
            and last_bar["is_trend_bar"]
            and last_bar["trend_direction"] == "buy"
            and features["trend_follow_through"]["value"]
            and features["trend_follow_through"]["direction"] == "buy"
        )
        if support:
            return "support", ["always_in_long_with_bull_trend_bar_follow_through"]
        return "neutral", ["no_high_confidence_anatomy_match"]

    # side == "sell"
    cluster_oppose = bool(features["rejection_cluster_lower"]["value"]) and at_sup
    # NOTE (2026-07-15 grok regression fixture #2): a SELL into a bar that
    # CLOSED bullish with a lower-wick sweep-and-reclaim at support opposes
    # the SELL even as a SINGLE bar — the 2-of-3 cluster rule above must not
    # be the only path to "oppose", or this exact incident would not veto.
    sweep_oppose = bool(sweep.get("value")) and sweep.get("side") == "buy" and at_sup
    always_in_oppose = always_in_val == "long" and last_bar["is_trend_bar"] and last_bar["trend_direction"] == "buy"

    reasons = []
    if cluster_oppose:
        reasons.append(f"rejection_cluster_at_support: {features['rejection_cluster_lower']['evidence']}")
    if sweep_oppose:
        reasons.append(f"sweep_and_reclaim_against_sell_at_support: {sweep.get('evidence')}")
    if always_in_oppose:
        reasons.append("always_in_long_with_bull_trend_bar")
    if reasons:
        return "oppose", reasons

    support = (
        always_in_val == "short"
        and last_bar["is_trend_bar"]
        and last_bar["trend_direction"] == "sell"
        and features["trend_follow_through"]["value"]
        and features["trend_follow_through"]["direction"] == "sell"
    )
    if support:
        return "support", ["always_in_short_with_bear_trend_bar_follow_through"]
    return "neutral", ["no_high_confidence_anatomy_match"]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def evaluate(bars: list[Bar], side: str, cfg: PriceActionEyeConfig | None = None) -> dict[str, Any]:
    """Evaluate the Layer-1 bar-anatomy catalog against ``bars`` for the
    committee's proposed ``side``.

    Args:
        bars: completed-bar list, OLDEST -> NEWEST (the caller's M5 prefix,
            e.g. ``m5_bars[:i+1]`` in ``shadow_runner.run_symbol_cycle`` —
            the last element is the most recently CLOSED bar to be judged;
            callers are responsible for trimming any still-forming bar).
            ``volume`` is accepted per-bar but unused in Phase A.
        side: "buy" or "sell" — the committee's proposed trade direction.
        cfg: optional ``PriceActionEyeConfig`` override (defaults to the
            documented Phase-A thresholds).

    Returns:
        {
          "features": {... every detector's full evidence dict, always
                       present regardless of verdict — POWERLESS by design:
                       this dict is pure journaled telemetry},
          "verdict": "support" | "neutral" | "oppose",
          "verdict_reasons": [human-readable strings explaining the verdict],
        }

    v0 verdict policy (conservative, high-confidence anatomy ONLY):
        oppose BUY  -- (>=2 of the last 3 bars show an upper-wick rejection
                        AND price sits within ``near_level_atr_dist`` ATRs of
                        a confirmed minor-resistance swing high) OR (a
                        sell-side sweep-and-reclaim at that resistance) OR
                        (always_in == "short" AND the last bar is a bear
                        trend bar).
        oppose SELL -- the exact mirror, PLUS the explicit single-bar case:
                        a bar that sweeps below a minor-support swing low
                        and CLOSES bullish with a long lower wick opposes a
                        SELL even without a 2-bar cluster (2026-07-15 grok
                        regression fixture #2).
        support     -- always_in agrees with ``side`` AND the last closed
                        bar is a same-direction trend bar with a two-bar
                        trend follow-through.
        else        -- neutral (the default — anatomy short of the above is
                        NOT considered evidence either way in v0).

    This function raises NOTHING it can avoid raising for malformed bar
    dicts (missing OHLC keys default to 0.0 via ``_f``); the only raised
    exceptions are for structurally wrong inputs (``side`` not "buy"/"sell",
    or fewer than ``MIN_BARS_FOR_EVALUATION`` bars) — both are returned as a
    neutral verdict with an explanatory reason rather than raised, since
    this module's only caller (``shadow_runner``'s shadow wiring) must never
    have a decision blocked by an Eye error (fail-open is the CALLER's
    contract, but returning cleanly here is the friendlier default).
    """
    cfg = cfg or PriceActionEyeConfig()
    side_norm = (side or "").strip().lower()
    if side_norm not in ("buy", "sell"):
        return {
            "features": {"error": f"invalid_side:{side!r}"},
            "verdict": "neutral",
            "verdict_reasons": ["invalid_side"],
        }
    if len(bars) < MIN_BARS_FOR_EVALUATION:
        return {
            "features": {"bar_count": len(bars), "error": "insufficient_bars"},
            "verdict": "neutral",
            "verdict_reasons": ["insufficient_bars"],
        }

    atr_ref = _atr_excluding_last(bars, cfg.atr_window)
    swing = market_lens.swing_structure(bars, lookback=cfg.swing_window, pivot_span=cfg.swing_pivot_span)

    features: dict[str, Any] = {
        "bar_count": len(bars),
        "atr_ref": round(atr_ref, 5),
        "last_bar": _classify_bar(bars[-1], atr_ref, cfg),
        "rejection_cluster_upper": _rejection_cluster(bars, atr_ref, cfg, "upper"),
        "rejection_cluster_lower": _rejection_cluster(bars, atr_ref, cfg, "lower"),
        "swing_location": _swing_and_location(swing, bars, atr_ref, cfg),
        "sweep_and_reclaim": market_lens.liquidity_sweep(bars, lookback=cfg.swing_window),
        "round_number_proximity": _round_number_proximity(bars, atr_ref, cfg),
        "always_in": _always_in_direction(bars, atr_ref, cfg),
        "bar_position_in_leg": _bar_position_in_leg(swing, bars, cfg),
        "inside_outside_bar": _inside_outside_bar(bars),
        "engulfing": _engulfing(bars),
        "two_bar_reversal": _two_bar_reversal(bars, atr_ref, cfg),
        "microchannel": _microchannel(bars, cfg),
        "three_push_wedge": _three_push_wedge(swing),
        "breakout_follow_through": _breakout_follow_through(bars, atr_ref, cfg),
        "trend_follow_through": _trend_follow_through(bars, atr_ref, cfg),
        "pullback_second_entry": _pullback_second_entry_count(bars, cfg),
        "trading_range": _trading_range(bars, cfg),
    }

    verdict, reasons = _resolve_verdict(side_norm, features, cfg)
    return {"features": features, "verdict": verdict, "verdict_reasons": reasons}

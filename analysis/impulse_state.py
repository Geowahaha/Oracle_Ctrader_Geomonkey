"""Shared XAU impulse/correction/reversal state reader.

This module is intentionally pure and side-effect free.  It does not place,
block, cancel, or resize trades.  Live behavior must consume this output only
behind feature flags after shadow validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Optional


class ImpulseStateName(Enum):
    IDLE = "idle"
    IMPULSE_START = "impulse_start"
    IMPULSE_RUN = "impulse_run"
    IMPULSE_EXHAUST = "impulse_exhaust"
    CORRECTION = "correction"
    RESUME = "resume"
    REVERSAL = "reversal"


@dataclass(frozen=True)
class ImpulseState:
    name: ImpulseStateName
    direction: str = ""
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()
    retracement_depth: float = 0.0
    impulse_score: int = 0

    def aligned_with(self, direction: str) -> bool:
        return _norm_direction(direction) == self.direction and bool(self.direction)

    def blocks_direction(self, direction: str) -> bool:
        candidate = _norm_direction(direction)
        if not candidate or not self.direction:
            return False
        if self.name not in {
            ImpulseStateName.IMPULSE_START,
            ImpulseStateName.IMPULSE_RUN,
            ImpulseStateName.RESUME,
        }:
            return False
        return candidate != self.direction


def compute_impulse_state(
    candles: Iterable[Mapping[str, object]],
    *,
    current_direction: str = "",
    entry_sharpness: float = 0.0,
    higher_tf_bias: str = "neutral",
    prior_impulse_direction: str = "",
    prior_impulse_start: Optional[float] = None,
    prior_impulse_end: Optional[float] = None,
    structure_break_direction: str = "",
    retest_rejection_direction: str = "",
) -> ImpulseState:
    """Return a conservative wave state for recent candles.

    The detector is deliberately simple for the first no-behavior-change commit:
    it favors transparent rules that can be shadow-logged and audited against
    live outcomes before any gate consumes them.
    """
    bars = [dict(c) for c in candles]
    if len(bars) < 8:
        return ImpulseState(ImpulseStateName.IDLE, reasons=("window_too_small",))

    prior_dir = _norm_direction(prior_impulse_direction)
    current_dir = _norm_direction(current_direction)
    tf_bias = _norm_direction(higher_tf_bias)
    structure_dir = _norm_direction(structure_break_direction)
    retest_dir = _norm_direction(retest_rejection_direction)

    reversal = _detect_reversal(prior_dir, structure_dir, retest_dir, tf_bias)
    if reversal:
        return ImpulseState(
            ImpulseStateName.REVERSAL,
            reversal,
            confidence=0.82,
            reasons=("reversal_confirmed", "structure_break", "retest_rejection"),
        )

    correction = _detect_correction_end(
        bars,
        prior_dir,
        prior_impulse_start,
        prior_impulse_end,
        tf_bias,
    )
    if correction is not None:
        direction, depth, reasons = correction
        return ImpulseState(
            ImpulseStateName.RESUME,
            direction,
            confidence=0.72,
            reasons=reasons,
            retracement_depth=depth,
            impulse_score=len(reasons),
        )

    impulse = _detect_impulse(bars, entry_sharpness=entry_sharpness, higher_tf_bias=tf_bias)
    if impulse is None and len(bars) >= 4:
        # A small counter candle after a strong directional expansion is still
        # an impulse run, not a reversal.  This is the key anti-counter-trade
        # read for pullbacks that have not confirmed reversal.
        impulse = _detect_impulse(bars[:-1], entry_sharpness=entry_sharpness, higher_tf_bias=tf_bias)
        if impulse is not None and _pullback_preserves_impulse(bars, impulse[0]):
            direction, score, reasons = impulse
            impulse = (direction, score, (*reasons, "pullback_without_reversal"))
        else:
            impulse = None
    if impulse is not None:
        direction, score, reasons = impulse
        state_name = ImpulseStateName.IMPULSE_START if score >= 3 and "pullback_without_reversal" not in reasons else ImpulseStateName.IMPULSE_RUN
        return ImpulseState(
            state_name,
            direction,
            confidence=min(0.95, 0.45 + score * 0.12),
            reasons=reasons,
            impulse_score=score,
        )

    if prior_dir and current_dir and current_dir != prior_dir:
        return ImpulseState(
            ImpulseStateName.CORRECTION,
            prior_dir,
            confidence=0.45,
            reasons=("counter_move_without_reversal_confirmation",),
        )

    return ImpulseState(ImpulseStateName.IDLE, reasons=("no_impulse_state_detected",))


def _detect_reversal(
    prior_dir: str,
    structure_dir: str,
    retest_dir: str,
    tf_bias: str,
) -> str:
    if not prior_dir or not structure_dir or not retest_dir:
        return ""
    if structure_dir != retest_dir or structure_dir == prior_dir:
        return ""
    if tf_bias and tf_bias not in {"neutral", structure_dir}:
        return ""
    return structure_dir


def _detect_correction_end(
    bars: list[Mapping[str, object]],
    prior_dir: str,
    start: Optional[float],
    end: Optional[float],
    tf_bias: str,
) -> Optional[tuple[str, float, list[str]]]:
    if prior_dir not in {"long", "short"} or start is None or end is None:
        return None
    impulse_range = abs(float(end) - float(start))
    if impulse_range <= 0:
        return None
    if tf_bias and tf_bias not in {"neutral", prior_dir}:
        return None

    last = bars[-1]
    prev = bars[-2]
    low = _num(last, "low")
    high = _num(last, "high")
    close = _num(last, "close")
    prev_close = _num(prev, "close")
    delta = _num(last, "delta_proxy")
    tick_up = _num(last, "tick_up_ratio", 0.5)

    if prior_dir == "long":
        retrace_price = min(low, close)
        depth = (float(end) - retrace_price) / impulse_range
        reaction = close > prev_close and delta > 0.8 and tick_up >= 0.58
    else:
        retrace_price = max(high, close)
        depth = (retrace_price - float(end)) / impulse_range
        reaction = close < prev_close and delta < -0.8 and tick_up <= 0.42

    near_fib_cluster = 0.50 <= depth <= 0.68
    if near_fib_cluster and reaction:
        return prior_dir, round(depth, 3), ("correction_end", "fib_cluster_reaction", "flow_flip")
    return None


def _detect_impulse(
    bars: list[Mapping[str, object]],
    *,
    entry_sharpness: float,
    higher_tf_bias: str,
) -> Optional[tuple[str, int, list[str]]]:
    lookback = bars[:-2] if len(bars) >= 5 else bars[:-1]
    if not lookback:
        return None
    last = bars[-1]
    prev = bars[-2]
    range_high = max(_num(b, "high") for b in lookback)
    range_low = min(_num(b, "low") for b in lookback)
    avg_volume = sum(max(_num(b, "volume"), 1.0) for b in lookback) / len(lookback)

    last_close = _num(last, "close")
    prev_close = _num(prev, "close")
    prev_open = _num(prev, "open")
    prev_high = _num(prev, "high")
    prev_low = _num(prev, "low")
    prev_close_value = _num(prev, "close")

    up_break = last_close > range_high
    down_break = last_close < range_low
    momentum_dir = "long" if last_close > prev_close else "short" if last_close < prev_close else ""
    direction = "long" if up_break else "short" if down_break else momentum_dir
    if direction not in {"long", "short"}:
        return None

    reasons: list[str] = []
    if up_break or down_break:
        reasons.append("range_break")

    delta = _num(last, "delta_proxy")
    tick_up = _num(last, "tick_up_ratio", 0.5)
    volume_expansion = _num(last, "volume") >= avg_volume * 1.5
    if direction == "long" and (delta >= 1.2 or tick_up >= 0.65) and volume_expansion:
        reasons.append("flow_expansion")
    if direction == "short" and (delta <= -1.2 or tick_up <= 0.35) and volume_expansion:
        reasons.append("flow_expansion")

    if _is_sweep_continuation(direction, range_high, range_low, prev, last):
        reasons.append("sweep_continuation")

    if higher_tf_bias in {"neutral", direction}:
        reasons.append("multi_tf_not_opposed")

    if float(entry_sharpness or 0.0) >= 60.0:
        reasons.append("entry_sharpness")

    # A strong two-bar directional expansion can be an existing impulse run even
    # if the first breakout candle occurred before this small window.
    if not (up_break or down_break):
        body_now = abs(last_close - _num(last, "open"))
        body_prev = abs(prev_close_value - prev_open)
        direction_body = (
            direction == "long" and last_close > _num(last, "open") and prev_close_value > prev_open
        ) or (
            direction == "short" and last_close < _num(last, "open") and prev_close_value < prev_open
        )
        if direction_body and (body_now + body_prev) >= max((range_high - range_low) * 0.6, 1e-9):
            reasons.append("directional_expansion")

    score = len(set(reasons))
    has_material_trigger = bool({"range_break", "flow_expansion", "sweep_continuation"}.intersection(reasons))
    if score >= 3 and has_material_trigger:
        return direction, score, tuple(reasons)
    return None


def _pullback_preserves_impulse(bars: list[Mapping[str, object]], direction: str) -> bool:
    if len(bars) < 4 or direction not in {"long", "short"}:
        return False
    impulse_bars = bars[:-1]
    pullback = bars[-1]
    impulse_start = _num(impulse_bars[0], "open")
    impulse_end = _num(impulse_bars[-1], "close")
    body = abs(impulse_end - impulse_start)
    if body <= 0:
        return False
    if direction == "long":
        retrace = impulse_end - min(_num(pullback, "low"), _num(pullback, "close"))
    else:
        retrace = max(_num(pullback, "high"), _num(pullback, "close")) - impulse_end
    return retrace <= body * 0.5


def _is_sweep_continuation(
    direction: str,
    range_high: float,
    range_low: float,
    sweep_bar: Mapping[str, object],
    follow_bar: Mapping[str, object],
) -> bool:
    sweep_open = _num(sweep_bar, "open")
    sweep_high = _num(sweep_bar, "high")
    sweep_low = _num(sweep_bar, "low")
    sweep_close = _num(sweep_bar, "close")
    follow_close = _num(follow_bar, "close")
    midpoint = (sweep_high + sweep_low) / 2.0

    if direction == "long":
        swept_low = sweep_low < range_low
        closed_through = sweep_close > range_high
        held_breakout = follow_close >= midpoint
        return swept_low and closed_through and held_breakout
    if direction == "short":
        swept_high = sweep_high > range_high
        closed_through = sweep_close < range_low
        held_breakout = follow_close <= midpoint
        return swept_high and closed_through and held_breakout
    return False


def _norm_direction(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "bull", "bullish", "up", "long"}:
        return "long"
    if text in {"sell", "bear", "bearish", "down", "short"}:
        return "short"
    if text == "neutral":
        return "neutral"
    return ""


def _num(bar: Mapping[str, object], key: str, default: float = 0.0) -> float:
    try:
        value = bar.get(key, None)
        return float(default) if value is None else float(value)
    except (TypeError, ValueError):
        return float(default)

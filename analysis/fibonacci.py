"""
analysis/fibonacci.py - Fibonacci & Elliott Wave Analysis Engine

Detects swing points, computes Fibonacci retracement/extension levels,
identifies Golden Pocket zones, and evaluates Elliott Wave impulse context.

Design philosophy (from research):
  - Fibonacci alone has ~37% edge — NOT used as primary signal
  - Golden Pocket (0.618-0.65) + SMC confluence → success rate jumps to ~68%
  - Elliott Wave impulse context confirms the retracement direction
  - Order flow (DOM, delta) is required final gate before entry
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Fibonacci ratios ──────────────────────────────────────────────────────────
FIBO_RETRACEMENT_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.65, 0.786, 1.0]
FIBO_EXTENSION_LEVELS   = [1.0, 1.272, 1.414, 1.618, 2.0, 2.618]

# Golden Pocket: institutional accumulation zone (ICT methodology)
GOLDEN_POCKET_LOW  = 0.618
GOLDEN_POCKET_HIGH = 0.65


@dataclass
class SwingPoint:
    index: int
    price: float
    bar_time: any
    swing_type: str  # 'high' | 'low'


@dataclass
class FibonacciLevels:
    direction: str           # 'bullish' (price retracing down) | 'bearish' (price retracing up)
    swing_start: float       # origin of the impulse move
    swing_end: float         # end of the impulse move (where retracement begins)
    swing_range: float       # abs(swing_end - swing_start)
    levels: dict             # ratio → price  e.g. {0.618: 2345.10, ...}
    extensions: dict         # ratio → price  e.g. {1.618: 2380.00, ...}
    golden_pocket_low: float
    golden_pocket_high: float
    impulse_strength: float  # 0-1 score of the impulse quality
    swing_start_idx: int = 0
    swing_end_idx: int = 0


@dataclass
class FiboSignalContext:
    """Summary of Fibonacci analysis for signal scoring."""
    fib_levels: Optional[FibonacciLevels] = None
    nearest_level_ratio: float = 0.0
    nearest_level_price: float = 0.0
    nearest_level_dist_pct: float = 0.0   # % distance from current price to level
    in_golden_pocket: bool = False
    in_382_zone: bool = False
    in_786_zone: bool = False
    retracement_depth: float = 0.0        # how far retraced (0-1)
    retracement_healthy: bool = False     # True if < 0.786 (structure still intact)
    elliott_wave_count: int = 0           # estimated wave count in current move
    impulse_confirmed: bool = False       # True if preceding move qualifies as impulse
    volume_diminishing: bool = False      # True if volume contracting on retracement
    fibo_confluence_score: float = 0.0   # 0-100 composite Fibonacci confidence
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # Impulse-birth fields (additive; older consumers ignore these freely).
    entry_mode: str = "none"                           # "none"|"late_retrace"|"early_origin"
    impulse_age_bars: int = 0                          # bars since late-retrace swing_end
    impulse_birth_detected: bool = False
    impulse_birth_direction: str = ""                  # "bullish"|"bearish"
    impulse_birth_anchor_price: float = 0.0            # base low (bullish) / base high (bearish)
    impulse_birth_base_start_idx: int = 0
    impulse_birth_breakout_idx: int = 0
    impulse_birth_confidence: float = 0.0              # 0-1
    impulse_birth_fib_levels: Optional[FibonacciLevels] = None


class FibonacciAnalyzer:
    """
    Fibonacci retracement and Elliott Wave impulse analyzer.
    Designed to integrate with Dexter's SMC + order-flow pipeline.
    """

    def __init__(
        self,
        swing_lookback: int = 5,
        min_impulse_atr_mult: float = 1.2,
        *,
        impulse_birth_enabled: bool = True,
        impulse_birth_base_bars: int = 8,
        impulse_birth_max_base_atr: float = 1.2,
        impulse_birth_min_break_atr: float = 0.5,
        impulse_birth_min_body_pct: float = 0.55,
        impulse_birth_max_breakout_age: int = 3,
        impulse_birth_max_chase_atr: float = 1.5,
        impulse_birth_whipsaw_lookback: int = 10,
        impulse_birth_min_confidence: float = 0.55,
        impulse_birth_score_bonus: float = 10.0,
        impulse_birth_stale_age_bars: int = 25,
    ):
        self.swing_lookback = swing_lookback
        self.min_impulse_atr_mult = min_impulse_atr_mult
        # Impulse-birth thresholds (see analysis/fibonacci.py _detect_impulse_birth).
        self.impulse_birth_enabled = bool(impulse_birth_enabled)
        self.impulse_birth_base_bars = int(impulse_birth_base_bars)
        self.impulse_birth_max_base_atr = float(impulse_birth_max_base_atr)
        self.impulse_birth_min_break_atr = float(impulse_birth_min_break_atr)
        self.impulse_birth_min_body_pct = float(impulse_birth_min_body_pct)
        self.impulse_birth_max_breakout_age = int(impulse_birth_max_breakout_age)
        self.impulse_birth_max_chase_atr = float(impulse_birth_max_chase_atr)
        self.impulse_birth_whipsaw_lookback = int(impulse_birth_whipsaw_lookback)
        self.impulse_birth_min_confidence = float(impulse_birth_min_confidence)
        self.impulse_birth_score_bonus = float(impulse_birth_score_bonus)
        self.impulse_birth_stale_age_bars = int(impulse_birth_stale_age_bars)

    # ── Impulse Birth Detection ───────────────────────────────────────────────

    def _detect_impulse_birth(
        self,
        df: pd.DataFrame,
        current_price: float,
        atr: float,
    ) -> dict:
        """Detect the *origin* of a nascent impulse before full expansion.

        The late-retrace path anchors fib on a *completed* swing; this one
        anchors on the base that the impulse is *just now* leaving. We
        only fire when several conditions agree, because fake breakouts
        on XAU are common:

        1. Base: prior N bars have compressed range <= atr * max_base_atr.
        2. Breakout: a recent bar (last `max_breakout_age` bars) closes
           beyond the base's high/low by >= atr * min_break_atr.
        3. Body dominance: the breakout bar's body is >= min_body_pct of
           its full range (no big rejection wick).
        4. Not-chased: current price is within max_chase_atr * atr of the
           breakout close — keeps entries near the origin, not post-run.
        5. Whipsaw guard: the prior `whipsaw_lookback` bars must NOT
           contain a confirmed opposite-direction breakout that failed
           (a recent failed break in the other direction is a trap flag).

        Returns a dict; empty when no valid birth was found. Never raises.
        """
        result: dict = {
            "detected": False,
            "direction": "",
            "anchor_price": 0.0,
            "base_start_idx": 0,
            "breakout_idx": 0,
            "confidence": 0.0,
            "reasons": [],
        }
        try:
            if not self.impulse_birth_enabled:
                return result
            if df is None or df.empty or atr <= 0:
                return result

            n_total = len(df)
            n_base = self.impulse_birth_base_bars
            n_lookback = self.impulse_birth_whipsaw_lookback
            max_age = max(1, self.impulse_birth_max_breakout_age)
            # Minimum: one breakout candidate at offset=1 must have room
            # for base + whipsaw history (no bars before whip window).
            if n_total < n_base + n_lookback + 2:
                return result

            highs = df["high"].values
            lows = df["low"].values
            opens = df["open"].values
            closes = df["close"].values

            # Walk breakout candidates from newest to oldest, within max_age.
            for offset in range(1, max_age + 1):
                break_idx = n_total - offset
                base_end = break_idx  # base ends the bar before breakout
                base_start = base_end - n_base
                if base_start <= n_lookback:
                    continue

                base_high = float(np.max(highs[base_start:base_end]))
                base_low = float(np.min(lows[base_start:base_end]))
                base_range = base_high - base_low
                if base_range <= 0:
                    continue

                # Condition 1: tight base
                if base_range > atr * self.impulse_birth_max_base_atr:
                    continue

                b_open = float(opens[break_idx])
                b_close = float(closes[break_idx])
                b_high = float(highs[break_idx])
                b_low = float(lows[break_idx])
                b_range = b_high - b_low
                if b_range <= 0:
                    continue
                b_body = abs(b_close - b_open)
                body_pct = b_body / b_range

                bullish_break = b_close > base_high + atr * self.impulse_birth_min_break_atr
                bearish_break = b_close < base_low - atr * self.impulse_birth_min_break_atr
                if not (bullish_break or bearish_break):
                    continue

                # Condition 3: body dominance
                if body_pct < self.impulse_birth_min_body_pct:
                    continue

                direction = "bullish" if bullish_break else "bearish"

                # Condition 4: not chased past max_chase_atr from breakout close
                chase = abs(current_price - b_close)
                if chase > atr * self.impulse_birth_max_chase_atr:
                    continue

                # Condition 5: whipsaw guard — recent failed opposite break.
                # Scan the `n_lookback` bars immediately preceding the base
                # for a confirmed opposite-direction breakout (close beyond
                # running extreme by >= min_break_atr * atr). If present,
                # treat this new same-direction break as possible trap.
                whip_slice_start = max(0, base_start - n_lookback)
                whip_closes = closes[whip_slice_start:base_start]
                whip_highs = highs[whip_slice_start:base_start]
                whip_lows = lows[whip_slice_start:base_start]
                whipsawed = False
                break_thr = atr * self.impulse_birth_min_break_atr
                for k in range(1, len(whip_closes)):
                    prior_high = float(np.max(whip_highs[:k]))
                    prior_low = float(np.min(whip_lows[:k]))
                    if direction == "bullish":
                        # Prior bearish break: close fell below prior low by thr.
                        if float(whip_closes[k]) < prior_low - break_thr:
                            whipsawed = True
                            break
                    else:
                        # Prior bullish break: close rose above prior high by thr.
                        if float(whip_closes[k]) > prior_high + break_thr:
                            whipsawed = True
                            break
                if whipsawed:
                    continue

                # Confidence score — proportion-of-threshold rewards over-delivery
                break_size = (b_close - base_high) if direction == "bullish" else (base_low - b_close)
                break_ratio = break_size / max(atr * self.impulse_birth_min_break_atr, 1e-9)
                tightness = 1.0 - (base_range / max(atr * self.impulse_birth_max_base_atr, 1e-9))
                proximity = 1.0 - (chase / max(atr * self.impulse_birth_max_chase_atr, 1e-9))
                body_score = min(max((body_pct - self.impulse_birth_min_body_pct) /
                                     max(1.0 - self.impulse_birth_min_body_pct, 1e-9), 0.0), 1.0)
                confidence = (
                    0.30 * min(max(break_ratio, 0.0) / 1.5, 1.0)
                    + 0.25 * max(tightness, 0.0)
                    + 0.25 * max(proximity, 0.0)
                    + 0.20 * body_score
                )
                confidence = round(min(max(confidence, 0.0), 1.0), 3)

                if confidence < self.impulse_birth_min_confidence:
                    continue

                anchor = base_low if direction == "bullish" else base_high
                reasons = [
                    f"base_bars={n_base}",
                    f"base_range_atr={base_range / max(atr, 1e-9):.2f}",
                    f"break_atr={break_size / max(atr, 1e-9):.2f}",
                    f"body_pct={body_pct:.2f}",
                    f"chase_atr={chase / max(atr, 1e-9):.2f}",
                    f"breakout_age={offset}",
                ]
                result.update({
                    "detected": True,
                    "direction": direction,
                    "anchor_price": float(anchor),
                    "base_start_idx": int(base_start),
                    "breakout_idx": int(break_idx),
                    "confidence": confidence,
                    "reasons": reasons,
                })
                return result

        except Exception as exc:
            logger.debug("[FiboAnalyzer] impulse-birth error: %s", exc)
        return result

    # ── Swing Detection ───────────────────────────────────────────────────────

    def detect_swings(self, df: pd.DataFrame, left_bars: int = 5, right_bars: int = 3) -> list[SwingPoint]:
        """Detect significant swing highs and lows using fractal logic."""
        swings = []
        highs = df["high"].values
        lows  = df["low"].values
        idx   = df.index

        for i in range(left_bars, len(df) - right_bars):
            # Swing high: highest in left_bars+right_bars window
            left_high  = max(highs[i - left_bars:i])
            right_high = max(highs[i + 1:i + right_bars + 1])
            if highs[i] > left_high and highs[i] > right_high:
                swings.append(SwingPoint(
                    index=i, price=float(highs[i]),
                    bar_time=idx[i], swing_type="high"
                ))

            # Swing low: lowest in window
            left_low  = min(lows[i - left_bars:i])
            right_low = min(lows[i + 1:i + right_bars + 1])
            if lows[i] < left_low and lows[i] < right_low:
                swings.append(SwingPoint(
                    index=i, price=float(lows[i]),
                    bar_time=idx[i], swing_type="low"
                ))

        return sorted(swings, key=lambda s: s.index)

    # ── Impulse Quality ───────────────────────────────────────────────────────

    def _impulse_strength(self, df: pd.DataFrame, start_idx: int, end_idx: int,
                          atr: float) -> float:
        """
        Score the quality of an impulse move (0-1).
        High score = strong directional move with volume + momentum.
        """
        if end_idx <= start_idx or atr <= 0:
            return 0.0

        segment = df.iloc[start_idx:end_idx + 1]
        price_range = abs(float(df["close"].iloc[end_idx]) - float(df["close"].iloc[start_idx]))

        # Range vs ATR (institutional-grade move = > 2x ATR)
        range_score = min(price_range / (atr * 3.0), 1.0)

        # Directional consistency: most bars should close in impulse direction
        is_up = float(df["close"].iloc[end_idx]) > float(df["close"].iloc[start_idx])
        closes = segment["close"].values
        opens  = segment["open"].values
        aligned = sum(1 for c, o in zip(closes, opens) if (c > o) == is_up)
        direction_score = aligned / max(len(segment), 1)

        # Volume (if available): should be above average on impulse bars
        vol_score = 0.5
        if "volume" in df.columns:
            avg_vol = float(df["volume"].mean()) or 1.0
            seg_vol = float(segment["volume"].mean()) or 0.0
            vol_score = min(seg_vol / avg_vol, 2.0) / 2.0

        strength = (range_score * 0.5 + direction_score * 0.35 + vol_score * 0.15)
        return round(min(strength, 1.0), 3)

    # ── Fibonacci Levels ──────────────────────────────────────────────────────

    def compute_fibonacci_levels(self, swing_start: float, swing_end: float,
                                 swing_start_idx: int, swing_end_idx: int,
                                 impulse_strength: float) -> FibonacciLevels:
        """
        Compute retracement and extension levels.
        For a bullish impulse (low → high): retracement = price pulling back down.
        For a bearish impulse (high → low): retracement = price pulling back up.
        """
        is_bullish = swing_end > swing_start
        direction  = "bullish" if is_bullish else "bearish"
        rng        = abs(swing_end - swing_start)

        levels = {}
        for ratio in FIBO_RETRACEMENT_LEVELS:
            if is_bullish:
                levels[ratio] = swing_end - ratio * rng
            else:
                levels[ratio] = swing_end + ratio * rng

        extensions = {}
        for ratio in FIBO_EXTENSION_LEVELS:
            if is_bullish:
                extensions[ratio] = swing_start + ratio * rng
            else:
                extensions[ratio] = swing_start - ratio * rng

        return FibonacciLevels(
            direction=direction,
            swing_start=swing_start,
            swing_end=swing_end,
            swing_range=rng,
            levels=levels,
            extensions=extensions,
            golden_pocket_low=levels[GOLDEN_POCKET_LOW],
            golden_pocket_high=levels[GOLDEN_POCKET_HIGH],
            impulse_strength=impulse_strength,
            swing_start_idx=swing_start_idx,
            swing_end_idx=swing_end_idx,
        )

    # ── Nearest Level Detection ───────────────────────────────────────────────

    def nearest_retracement_level(self, current_price: float,
                                  fib: FibonacciLevels) -> tuple[float, float, float]:
        """
        Returns (ratio, level_price, distance_pct) for the nearest retracement level.
        """
        best_ratio = 0.0
        best_price = 0.0
        best_dist  = float("inf")

        for ratio, price in fib.levels.items():
            dist = abs(current_price - price)
            if dist < best_dist:
                best_dist  = dist
                best_ratio = ratio
                best_price = price

        dist_pct = (best_dist / max(current_price, 1.0)) * 100.0
        return best_ratio, best_price, round(dist_pct, 4)

    # ── Elliott Wave Context (validated) ─────────────────────────────────────

    def estimate_wave_count(self, swings: list[SwingPoint], direction: str) -> int:
        """
        Elliott Wave count with structural validation rules.

        Rules enforced (institution-grade):
          1. Wave 3 cannot be the shortest impulse wave
          2. Wave 2 cannot retrace beyond the start of Wave 1
          3. Wave 4 cannot overlap Wave 1 price territory (in trending markets)

        Returns the validated wave number (1-5 for impulse, 0 if invalid/uncertain).
        """
        if len(swings) < 3:
            return 0

        relevant = list(swings[-12:])
        if len(relevant) < 3:
            return 0

        # Build alternating wave segments
        waves: list[dict] = []
        for i in range(1, len(relevant)):
            if relevant[i].swing_type != relevant[i - 1].swing_type:
                waves.append({
                    "start_price": relevant[i - 1].price,
                    "end_price": relevant[i].price,
                    "start_idx": relevant[i - 1].index,
                    "end_idx": relevant[i].index,
                    "magnitude": abs(relevant[i].price - relevant[i - 1].price),
                    "is_impulse": (relevant[i].price > relevant[i - 1].price) == (direction == "bullish"),
                })

        if len(waves) < 2:
            return 0

        # Map alternating moves to Elliott waves
        impulse_waves = [w for w in waves if w["is_impulse"]]
        corrective_waves = [w for w in waves if not w["is_impulse"]]

        wave_count = min(len(waves), 5)

        # ── Validation: Wave 3 cannot be shortest impulse ─────────────
        if len(impulse_waves) >= 3:
            magnitudes = [w["magnitude"] for w in impulse_waves[:3]]
            if magnitudes[1] == min(magnitudes):
                wave_count = min(wave_count, 2)

        # ── Validation: Wave 2 must not retrace past Wave 1 start ─────
        if len(waves) >= 2 and not waves[1]["is_impulse"]:
            w1_start = waves[0]["start_price"]
            w2_end = waves[1]["end_price"]
            if direction == "bullish" and w2_end < w1_start:
                wave_count = 0
            elif direction == "bearish" and w2_end > w1_start:
                wave_count = 0

        # ── Validation: Wave 4 must not overlap Wave 1 territory ──────
        if len(waves) >= 4 and not waves[3]["is_impulse"]:
            w1_end = waves[0]["end_price"]
            w4_end = waves[3]["end_price"]
            if direction == "bullish" and w4_end < w1_end:
                wave_count = min(wave_count, 3)
            elif direction == "bearish" and w4_end > w1_end:
                wave_count = min(wave_count, 3)

        return wave_count

    # ── Volume Trend on Retracement ───────────────────────────────────────────

    def _volume_diminishing(self, df: pd.DataFrame, retracement_start_idx: int) -> bool:
        """
        Healthy retracement = volume contracts as price retraces.
        Compare last N bars' volume to prior N bars.
        """
        if "volume" not in df.columns:
            return True  # assume healthy if no volume data

        lookback = min(5, retracement_start_idx)
        if retracement_start_idx < lookback:
            return True

        impulse_vol    = float(df["volume"].iloc[retracement_start_idx - lookback:retracement_start_idx].mean())
        retracement_vol = float(df["volume"].iloc[retracement_start_idx:].mean())

        if impulse_vol <= 0:
            return True

        return retracement_vol < impulse_vol * 0.85  # volume < 85% of impulse = contracting

    # ── Main Analysis Entry Point ─────────────────────────────────────────────

    def analyze(self, df_structure: pd.DataFrame, df_entry: pd.DataFrame,
                current_price: float, atr: float,
                smc_context=None) -> FiboSignalContext:
        """
        Full Fibonacci analysis.

        Args:
            df_structure: H4 bars for swing/impulse detection
            df_entry:     H1 bars for entry-level Fibonacci precision
            current_price: live price
            atr:           ATR from entry timeframe
            smc_context:   SMCContext from smc.py (optional, for confluence)

        Returns:
            FiboSignalContext with score and metadata
        """
        ctx = FiboSignalContext()

        def _annotate_birth_only(score: float = 0.0) -> FiboSignalContext:
            """Populate ctx with birth info when no completed-impulse fib was
            computed. Additive — does not alter the ctx on None paths that
            could not access price/atr."""
            try:
                if df_structure is None or df_structure.empty:
                    return ctx
                birth_local = self._detect_impulse_birth(df_structure, current_price, atr)
                if not birth_local.get("detected"):
                    return ctx
                ctx.impulse_birth_detected = True
                ctx.impulse_birth_direction = birth_local["direction"]
                ctx.impulse_birth_anchor_price = birth_local["anchor_price"]
                ctx.impulse_birth_base_start_idx = birth_local["base_start_idx"]
                ctx.impulse_birth_breakout_idx = birth_local["breakout_idx"]
                ctx.impulse_birth_confidence = birth_local["confidence"]

                b_idx = birth_local["breakout_idx"]
                b_start = birth_local["base_start_idx"]
                swing_end_price = (
                    float(df_structure["high"].iloc[b_idx])
                    if birth_local["direction"] == "bullish"
                    else float(df_structure["low"].iloc[b_idx])
                )
                ctx.impulse_birth_fib_levels = self.compute_fibonacci_levels(
                    swing_start=birth_local["anchor_price"],
                    swing_end=swing_end_price,
                    swing_start_idx=b_start,
                    swing_end_idx=b_idx,
                    impulse_strength=birth_local["confidence"],
                )
                ctx.entry_mode = "early_origin"
                reasons_local = list(ctx.reasons or [])
                reasons_local.append(
                    f"impulse_birth_{birth_local['direction']}_conf_{birth_local['confidence']:.2f}"
                )
                for r in birth_local.get("reasons", []):
                    reasons_local.append(f"birth:{r}")
                reasons_local.append("entry_mode_early_origin_no_completed_impulse")
                ctx.reasons = reasons_local
                # Score only when there is no completed-impulse base score.
                if ctx.fibo_confluence_score <= 0.0:
                    ctx.fibo_confluence_score = round(
                        min(score + self.impulse_birth_score_bonus *
                            birth_local["confidence"], 100.0), 2)
            except Exception as exc:
                logger.debug("[FiboAnalyzer] birth-only annotate error: %s", exc)
            return ctx

        try:
            if df_structure is None or df_structure.empty or len(df_structure) < 20:
                ctx.warnings.append("insufficient_structure_data")
                return ctx

            # ── 1. Detect swings on structure TF (H4) ─────────────────────
            swings = self.detect_swings(df_structure, left_bars=5, right_bars=3)
            if len(swings) < 4:
                ctx.warnings.append("insufficient_swings")
                return _annotate_birth_only()

            # ── 2. Identify the most recent completed impulse ─────────────
            # Look at the last 4 swings: find a high-low or low-high pair
            # where the move qualifies as an impulse
            last_swings = swings[-6:]
            fib_levels: Optional[FibonacciLevels] = None
            impulse_start_idx = 0
            impulse_end_idx   = 0

            for i in range(len(last_swings) - 1, 0, -1):
                s_end   = last_swings[i]
                s_start = last_swings[i - 1]

                if s_start.swing_type == s_end.swing_type:
                    continue  # need alternating high/low

                rng = abs(s_end.price - s_start.price)
                if atr > 0 and rng < atr * self.min_impulse_atr_mult:
                    continue  # too small to be impulse

                strength = self._impulse_strength(
                    df_structure, s_start.index, s_end.index, atr
                )
                if strength < 0.35:
                    continue  # weak move, skip

                fib_levels = self.compute_fibonacci_levels(
                    swing_start=s_start.price,
                    swing_end=s_end.price,
                    swing_start_idx=s_start.index,
                    swing_end_idx=s_end.index,
                    impulse_strength=strength,
                )
                impulse_start_idx = s_start.index
                impulse_end_idx   = s_end.index
                ctx.impulse_confirmed = strength >= 0.5
                break

            if fib_levels is None:
                ctx.warnings.append("no_valid_impulse_found")
                return _annotate_birth_only()

            ctx.fib_levels = fib_levels

            # ── 3. Nearest level + retracement depth ─────────────────────
            ratio, level_price, dist_pct = self.nearest_retracement_level(current_price, fib_levels)
            ctx.nearest_level_ratio  = ratio
            ctx.nearest_level_price  = level_price
            ctx.nearest_level_dist_pct = dist_pct

            rng = fib_levels.swing_range
            if rng > 0:
                if fib_levels.direction == "bullish":
                    ctx.retracement_depth = (fib_levels.swing_end - current_price) / rng
                else:
                    ctx.retracement_depth = (current_price - fib_levels.swing_end) / rng
                ctx.retracement_depth = max(0.0, min(ctx.retracement_depth, 1.2))

            ctx.retracement_healthy = ctx.retracement_depth <= 0.786

            # ── 4. Zone classification ────────────────────────────────────
            gp_lo = fib_levels.golden_pocket_low
            gp_hi = fib_levels.golden_pocket_high
            zone_tol = atr * 0.3  # tolerance around each level

            ctx.in_golden_pocket = (
                min(gp_lo, gp_hi) - zone_tol <= current_price <= max(gp_lo, gp_hi) + zone_tol
            )
            lvl_382 = fib_levels.levels.get(0.382, 0.0)
            lvl_786 = fib_levels.levels.get(0.786, 0.0)
            ctx.in_382_zone = abs(current_price - lvl_382) <= zone_tol
            ctx.in_786_zone = abs(current_price - lvl_786) <= zone_tol

            # ── 5. Elliott Wave estimate ──────────────────────────────────
            ctx.elliott_wave_count = self.estimate_wave_count(swings, fib_levels.direction)

            # ── 6. Volume check ───────────────────────────────────────────
            ctx.volume_diminishing = self._volume_diminishing(df_structure, impulse_end_idx)

            # ── 7. Confluence scoring ─────────────────────────────────────
            score   = 0.0
            reasons = []
            warnings = []

            if ctx.in_golden_pocket:
                score += 30.0
                reasons.append("golden_pocket_0618_065")
            elif ctx.in_382_zone:
                score += 18.0
                reasons.append("fib_382_zone")
            elif ctx.in_786_zone:
                score += 8.0
                warnings.append("deep_retracement_786_risky")
            elif 0.45 < ctx.nearest_level_ratio <= 0.55:
                score += 12.0
                reasons.append("fib_50pct_zone")
            elif dist_pct < 0.15:
                score += 5.0
                reasons.append(f"near_fib_{ctx.nearest_level_ratio:.3f}")

            if ctx.impulse_confirmed:
                score += 15.0
                reasons.append(f"impulse_confirmed_strength_{fib_levels.impulse_strength:.2f}")
            else:
                warnings.append("impulse_weak")

            if ctx.retracement_healthy:
                score += 8.0
                reasons.append(f"retracement_healthy_{ctx.retracement_depth:.2f}")
            else:
                score -= 15.0
                warnings.append("retracement_deep_structure_at_risk")

            if ctx.volume_diminishing:
                score += 8.0
                reasons.append("retracement_volume_contracting")

            if ctx.elliott_wave_count in (2, 4):
                score += 10.0
                reasons.append(f"ew_wave_{ctx.elliott_wave_count}_retracement")
            elif ctx.elliott_wave_count == 3:
                score += 5.0
                reasons.append("ew_wave_3_continuation")

            # SMC confluence
            if smc_context is not None:
                ob = smc_context.nearest_ob
                fvg = smc_context.nearest_fvg
                if ob and not ob.broken:
                    ob_mid = (ob.high + ob.low) / 2
                    if abs(ob_mid - current_price) <= atr * 0.5:
                        score += 12.0
                        reasons.append("ob_at_fib_level")
                if fvg and not fvg.filled:
                    fvg_mid = (fvg.upper + fvg.lower) / 2
                    if abs(fvg_mid - current_price) <= atr * 0.6:
                        score += 8.0
                        reasons.append("fvg_at_fib_level")
                if smc_context.recent_bos:
                    score += 5.0
                    reasons.append("bos_confirmed")

            # ── 8. Impulse-birth detection (additive) ─────────────────
            # Late-retrace path is primary. If a fresher origin is
            # detected AND either the completed impulse has gone stale
            # or no retracement zone is currently in play, promote
            # entry_mode to "early_origin" and publish a birth-anchored
            # fib for downstream use. Otherwise stay as late_retrace.
            try:
                df_latest_idx = len(df_structure) - 1
                ctx.impulse_age_bars = max(0, df_latest_idx - int(impulse_end_idx))
                ctx.entry_mode = "late_retrace"

                birth = self._detect_impulse_birth(df_structure, current_price, atr)
                if birth.get("detected"):
                    ctx.impulse_birth_detected = True
                    ctx.impulse_birth_direction = birth["direction"]
                    ctx.impulse_birth_anchor_price = birth["anchor_price"]
                    ctx.impulse_birth_base_start_idx = birth["base_start_idx"]
                    ctx.impulse_birth_breakout_idx = birth["breakout_idx"]
                    ctx.impulse_birth_confidence = birth["confidence"]

                    # Build a birth-anchored fib for downstream use.
                    b_idx = birth["breakout_idx"]
                    b_start = birth["base_start_idx"]
                    swing_end_price = (
                        float(df_structure["high"].iloc[b_idx])
                        if birth["direction"] == "bullish"
                        else float(df_structure["low"].iloc[b_idx])
                    )
                    ctx.impulse_birth_fib_levels = self.compute_fibonacci_levels(
                        swing_start=birth["anchor_price"],
                        swing_end=swing_end_price,
                        swing_start_idx=b_start,
                        swing_end_idx=b_idx,
                        impulse_strength=birth["confidence"],
                    )

                    reasons.append(
                        f"impulse_birth_{birth['direction']}_conf_{birth['confidence']:.2f}"
                    )
                    for r in birth.get("reasons", []):
                        reasons.append(f"birth:{r}")

                    # Promote when late-retrace anchor is stale OR current
                    # price is not in any useful retracement zone.
                    stale = ctx.impulse_age_bars >= self.impulse_birth_stale_age_bars
                    in_zone = (
                        ctx.in_golden_pocket or ctx.in_382_zone or ctx.in_786_zone
                    )
                    if stale or not in_zone:
                        ctx.entry_mode = "early_origin"
                        score += self.impulse_birth_score_bonus * birth["confidence"]
                        reasons.append("entry_mode_early_origin")
                    else:
                        reasons.append("entry_mode_late_retrace_birth_available")
                else:
                    ctx.entry_mode = "late_retrace"

                ctx.fibo_confluence_score = round(min(score, 100.0), 2)
            except Exception as exc:
                logger.debug("[FiboAnalyzer] impulse-birth annotation error: %s", exc)

            ctx.fibo_confluence_score = round(min(score, 100.0), 2)
            ctx.reasons  = reasons
            ctx.warnings = warnings

        except Exception as e:
            logger.warning("[FiboAnalyzer] analyze error: %s", e)
            ctx.warnings.append(f"analyzer_error:{e}")

        return ctx

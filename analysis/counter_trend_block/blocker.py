"""Counter-Trend Blocker implementation.

Algorithm (last `lookback` M1 candles, oldest → newest):
  1. ``bull_count`` = candles where close > open
  2. ``bear_count`` = candles where close < open
  3. ``avg_body`` = mean(|close - open|) across the window
  4. If proposed direction is `short` AND bull_count >= lookback AND
     avg_body >= min_body_atr_ratio × ATR → BLOCK with reason
     ``counter_trend_block:bull_streak``.
  5. Mirror for `long` against bear streak.
  6. Otherwise PASS.

Why streak + body magnitude? A streak alone catches small chop; we also
require meaningful bodies so we only block during *actual* impulses.

Outputs:
  - ``blocked = False`` means "proceed". The blocker is permissive by default.
  - ``blocked = True`` carries an explanation; the caller decides whether
    to drop the signal, downgrade to shadow, or just log.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class M1Candle:
    open: float
    close: float
    high: float
    low: float


@dataclass(frozen=True)
class BlockDecision:
    blocked: bool
    reason: str
    bull_count: int
    bear_count: int
    avg_body: float
    atr: float


@dataclass
class CounterTrendBlockerConfig:
    enabled: bool = False
    lookback: int = 3
    min_body_atr_ratio: float = 0.30   # avg body must be at least 0.30 × ATR
    require_full_streak: bool = True   # all `lookback` must agree
    # When require_full_streak=False, this is the min streak fraction (0..1).
    min_streak_fraction: float = 0.85


class CounterTrendBlocker:
    """Stateless evaluator — pass candles + ATR + direction, get a decision."""

    def __init__(self, *, config: Optional[CounterTrendBlockerConfig] = None) -> None:
        self.config = config or CounterTrendBlockerConfig()

    def evaluate(
        self,
        *,
        direction: str,
        m1_candles: Iterable[M1Candle],
        atr: float,
    ) -> BlockDecision:
        cfg = self.config
        if not cfg.enabled:
            return BlockDecision(False, "disabled", 0, 0, 0.0, float(atr))

        candles = list(m1_candles)[-cfg.lookback:]
        if len(candles) < cfg.lookback:
            return BlockDecision(False, "insufficient_candles", 0, 0, 0.0, float(atr))
        if atr is None or float(atr) <= 0:
            return BlockDecision(False, "atr_invalid", 0, 0, 0.0, float(atr or 0.0))

        bull = sum(1 for c in candles if c.close > c.open)
        bear = sum(1 for c in candles if c.close < c.open)
        bodies = [abs(c.close - c.open) for c in candles]
        avg_body = sum(bodies) / len(bodies) if bodies else 0.0
        direction = _norm_dir(direction)

        if cfg.require_full_streak:
            need_match = cfg.lookback
        else:
            need_match = max(1, int(round(cfg.lookback * cfg.min_streak_fraction)))

        body_ok = avg_body >= cfg.min_body_atr_ratio * float(atr)

        if direction == "short" and bull >= need_match and body_ok:
            reason = (
                f"counter_trend_block:bull_streak={bull}/{cfg.lookback}"
                f",avg_body={avg_body:.3f}>={cfg.min_body_atr_ratio:.2f}*atr({atr:.3f})"
            )
            return BlockDecision(True, reason, bull, bear, round(avg_body, 4), float(atr))
        if direction == "long" and bear >= need_match and body_ok:
            reason = (
                f"counter_trend_block:bear_streak={bear}/{cfg.lookback}"
                f",avg_body={avg_body:.3f}>={cfg.min_body_atr_ratio:.2f}*atr({atr:.3f})"
            )
            return BlockDecision(True, reason, bull, bear, round(avg_body, 4), float(atr))

        return BlockDecision(False, "ok", bull, bear, round(avg_body, 4), float(atr))


__all__ = [
    "CounterTrendBlocker",
    "CounterTrendBlockerConfig",
    "BlockDecision",
    "M1Candle",
]

"""Entry Quality Router implementation.

Decision tree:

    KILL conditions (any → drop):
      - confidence < kill_floor
      - NOT m1_trend_aligned AND counter_trend AND NOT has_anchor
        (= chase against impulse with no anchor evidence)

    MARKET upgrade (all must pass):
      - confidence >= market_threshold
      - m1_trend_aligned AND m5_trend_aligned
      - delta_confirms
      - flow_confirmed
      - has_structure_break (price already broke in direction)

    Otherwise PASS — downstream BreakConfirmEntry / CounterTrendBlocker
    decide what to do with the limit signal.

Why "trend-aligned + delta + flow + break" for MARKET?
We only upgrade to live market when ALL evidence is unanimous. A
counter-trend MARKET entry would be even worse than a counter-trend
LIMIT (no chance to cancel before fill). The bar is high by design.

Why "counter_trend AND no_anchor" for KILL?
The proven losers had no anchor: just a limit price hanging in the air
against a fresh impulse. Anchored counter-trend (rejection wick at
support/resistance) can still convert to a wait-break STOP downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


ACTION_KILL = "kill"
ACTION_MARKET = "market"
ACTION_PASS = "pass"


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class EntryQualityInputs:
    direction: str
    confidence: float
    current_price: float          # ask for long, bid for short
    original_entry: float
    original_stop_loss: float
    original_take_profit: float
    atr: float
    # Trend / momentum alignment
    m1_trend_aligned: bool
    m5_trend_aligned: bool
    delta_confirms: bool
    flow_confirmed: bool
    has_structure_break: bool     # M1 broke prior swing in direction
    # Counter-trend / anchor
    counter_trend: bool
    has_anchor: bool              # rejection candle / VP / Fibo anchor


@dataclass(frozen=True)
class EntryQualityDecision:
    action: str                   # ACTION_KILL / ACTION_MARKET / ACTION_PASS
    new_entry_type: str           # "market" when action=MARKET, "" otherwise
    new_entry: float              # current_price when MARKET; 0 otherwise
    new_stop_loss: float          # for MARKET we keep original SL
    new_take_profit: float        # original TP retained
    reason: str
    market_score: int = 0         # how many of the 5 market conditions passed


@dataclass
class EntryQualityRouterConfig:
    enabled: bool = False
    kill_confidence_floor: float = 60.0        # drop signals below this
    market_confidence_threshold: float = 75.0  # bar for MARKET upgrade
    require_all_market_conditions: bool = True # if False, allow 4/5 quorum
    market_quorum: int = 5                     # used when require_all=False (1..5)
    # When counter_trend AND no anchor, KILL even above the floor.
    kill_counter_trend_when_no_anchor: bool = True


class EntryQualityRouter:
    """Stateless router that decides per-signal action."""

    def __init__(self, *, config: Optional[EntryQualityRouterConfig] = None) -> None:
        self.config = config or EntryQualityRouterConfig()

    def evaluate(self, inputs: EntryQualityInputs) -> EntryQualityDecision:
        cfg = self.config
        if not cfg.enabled:
            return self._pass("disabled", inputs)

        direction = _norm_dir(inputs.direction)
        if direction not in {"long", "short"}:
            return self._pass("bad_direction", inputs)

        # ---- KILL conditions ---------------------------------------
        if inputs.confidence < cfg.kill_confidence_floor:
            return self._kill(
                f"confidence_below_floor:{inputs.confidence:.1f}<{cfg.kill_confidence_floor:.1f}"
            )
        if cfg.kill_counter_trend_when_no_anchor and inputs.counter_trend and not inputs.has_anchor:
            return self._kill("counter_trend_without_anchor")

        # ---- MARKET upgrade conditions -----------------------------
        passes = [
            inputs.confidence >= cfg.market_confidence_threshold,
            bool(inputs.m1_trend_aligned),
            bool(inputs.m5_trend_aligned),
            bool(inputs.delta_confirms),
            bool(inputs.flow_confirmed),
            bool(inputs.has_structure_break),
        ]
        # Count: confidence + 4 conditions + structure_break = 6 booleans
        score = sum(1 for p in passes if p)
        need = len(passes) if cfg.require_all_market_conditions else min(len(passes), max(1, int(cfg.market_quorum)))

        if score >= need:
            # MARKET upgrade — fire at current price right now.
            current = float(inputs.current_price) if inputs.current_price > 0 else float(inputs.original_entry)
            reason = (
                f"market_upgrade:score={score}/{len(passes)},"
                f"conf={inputs.confidence:.1f}>={cfg.market_confidence_threshold:.1f}"
            )
            return EntryQualityDecision(
                action=ACTION_MARKET,
                new_entry_type="market",
                new_entry=round(current, 6),
                new_stop_loss=round(float(inputs.original_stop_loss), 6),
                new_take_profit=round(float(inputs.original_take_profit), 6),
                reason=reason,
                market_score=score,
            )

        return self._pass(f"pass_through:score={score}/{len(passes)}", inputs, market_score=score)

    # ----- helpers ------------------------------------------------
    def _kill(self, reason: str) -> EntryQualityDecision:
        return EntryQualityDecision(
            action=ACTION_KILL,
            new_entry_type="",
            new_entry=0.0,
            new_stop_loss=0.0,
            new_take_profit=0.0,
            reason=reason,
        )

    def _pass(self, reason: str, inputs: EntryQualityInputs, market_score: int = 0) -> EntryQualityDecision:
        return EntryQualityDecision(
            action=ACTION_PASS,
            new_entry_type="",
            new_entry=0.0,
            new_stop_loss=0.0,
            new_take_profit=0.0,
            reason=reason,
            market_score=market_score,
        )


__all__ = [
    "EntryQualityRouter",
    "EntryQualityRouterConfig",
    "EntryQualityInputs",
    "EntryQualityDecision",
    "ACTION_KILL",
    "ACTION_MARKET",
    "ACTION_PASS",
]

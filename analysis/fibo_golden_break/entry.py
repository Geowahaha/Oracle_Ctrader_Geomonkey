"""Fibo Golden-Break Entry implementation.

Geometry:
For a LONG retracement after a bullish impulse:
   impulse_low ────────────────────────── 0% (start of impulse, bottom)
   ...
   golden_zone_low  = impulse_low + 0.50 * (impulse_high - impulse_low)
   golden_zone_mid  = impulse_low + 0.618 * range  (the 0.618 level)
   golden_zone_high = impulse_low + 0.88 * range
   impulse_high ────────────────────────── 100% (end of impulse, top)

Price retracing INTO the golden zone is in the "pullback" region. A LONG
break-bar at the LOW end of the zone (just before price bounces up) is
the cleanest live-entry signal. The legacy code places LIMIT inside the
zone hoping price stops — but price often slices through the zone and
hunts the stop before reversing.

Decision logic:

    if price OUTSIDE golden zone:
        return SKIP (signal too early or retracement too deep)

    if break-bar in trade direction confirmed:
        # last M1 close beyond recent micro swing in trade direction
        return LIVE_MARKET at current price
              (with original SL/TP)

    if inside zone, no break yet:
        # place a STOP order at recent micro swing + small buffer
        for LONG: STOP @ recent_micro_high + buffer*ATR (buy_stop)
        for SHORT: STOP @ recent_micro_low - buffer*ATR (sell_stop)
        return WAIT_BREAK_STOP

Safety:
- Guarded by `enabled` flag; default OFF preserves legacy behaviour.
- Returns ACTION_SKIP when geometry is invalid (impulse_high <= impulse_low).
- Risk check: aborts to PASS when computed risk is degenerate or >
  max_risk_atr_mult × ATR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


ACTION_LIVE_MARKET = "live_market"
ACTION_WAIT_BREAK_STOP = "wait_break_stop"
ACTION_SKIP = "skip"


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class FiboGoldenInputs:
    direction: str                 # "long" or "short"
    impulse_high: float            # the high of the originating impulse leg
    impulse_low: float             # the low of the originating impulse leg
    current_price: float
    atr: float
    # Last micro-swing in the recent M1 window (used as break trigger).
    recent_micro_high: float
    recent_micro_low: float
    # Last M1 close used to detect the break-bar.
    last_m1_close: float
    # Original signal payload to preserve when emitting decision.
    original_entry: float
    original_stop_loss: float
    original_take_profit: float


@dataclass(frozen=True)
class FiboGoldenDecision:
    action: str
    entry_type: str
    new_entry: float
    new_stop_loss: float
    new_take_profit: float
    ratio_at_price: float          # where in the zone the current price sits (0..1)
    inside_golden_zone: bool
    break_confirmed: bool
    reason: str


@dataclass
class FiboGoldenBreakConfig:
    enabled: bool = False
    zone_low: float = 0.50         # lower fib level
    zone_high: float = 0.88        # upper fib level
    stop_trigger_buffer_atr: float = 0.10
    sl_buffer_atr: float = 1.0
    tp_rr_target: float = 1.5
    min_risk_distance: float = 0.5
    max_risk_atr_mult: float = 4.5
    # When the zone is missing/invalid the gate must let the signal pass
    # rather than silently dropping it. Set to True to *skip* instead.
    skip_when_zone_invalid: bool = False


def _ratio_at(price: float, impulse_low: float, impulse_high: float, *, direction: str) -> float:
    """Compute the retracement ratio of `price` relative to the impulse leg.

    For a LONG retracement (after bullish impulse, expecting pullback down then up):
        ratio = (impulse_high - price) / (impulse_high - impulse_low)
        0.0 means price is at impulse_high (no retracement)
        1.0 means price is at impulse_low (full retracement)

    For SHORT (after bearish impulse, pullback up then down):
        ratio = (price - impulse_low) / (impulse_high - impulse_low)
    """
    rng = impulse_high - impulse_low
    if rng <= 0:
        return -1.0
    if _norm_dir(direction) == "long":
        return (impulse_high - price) / rng
    return (price - impulse_low) / rng


def _passthrough(reason: str, *, inputs: FiboGoldenInputs, ratio: float, inside: bool, broken: bool) -> FiboGoldenDecision:
    return FiboGoldenDecision(
        action=ACTION_SKIP if reason.startswith("skip:") else ACTION_SKIP,
        entry_type="",
        new_entry=0.0,
        new_stop_loss=0.0,
        new_take_profit=0.0,
        ratio_at_price=round(ratio, 4),
        inside_golden_zone=inside,
        break_confirmed=broken,
        reason=reason,
    )


class FiboGoldenBreakEntry:
    """Stateless evaluator: decide live_market / wait_break_stop / skip."""

    def __init__(self, *, config: Optional[FiboGoldenBreakConfig] = None) -> None:
        self.config = config or FiboGoldenBreakConfig()

    def evaluate(self, inputs: FiboGoldenInputs) -> FiboGoldenDecision:
        cfg = self.config
        if not cfg.enabled:
            return _passthrough("disabled", inputs=inputs, ratio=0.0, inside=False, broken=False)

        direction = _norm_dir(inputs.direction)
        if direction not in {"long", "short"}:
            return _passthrough("bad_direction", inputs=inputs, ratio=0.0, inside=False, broken=False)

        if inputs.atr is None or float(inputs.atr) <= 0:
            return _passthrough("atr_invalid", inputs=inputs, ratio=0.0, inside=False, broken=False)

        if inputs.impulse_high <= inputs.impulse_low:
            if cfg.skip_when_zone_invalid:
                return _passthrough("zone_invalid_skip", inputs=inputs, ratio=0.0, inside=False, broken=False)
            return _passthrough("zone_invalid_pass", inputs=inputs, ratio=0.0, inside=False, broken=False)

        ratio = _ratio_at(
            inputs.current_price, inputs.impulse_low, inputs.impulse_high, direction=direction,
        )
        inside_zone = cfg.zone_low <= ratio <= cfg.zone_high

        if not inside_zone:
            return FiboGoldenDecision(
                action=ACTION_SKIP, entry_type="",
                new_entry=0.0, new_stop_loss=0.0, new_take_profit=0.0,
                ratio_at_price=round(ratio, 4),
                inside_golden_zone=False, break_confirmed=False,
                reason=f"outside_golden_zone:ratio={ratio:.3f}∉[{cfg.zone_low:.2f},{cfg.zone_high:.2f}]",
            )

        # Break-bar check.
        if direction == "long":
            broken = float(inputs.last_m1_close) > float(inputs.recent_micro_high)
        else:
            broken = float(inputs.last_m1_close) < float(inputs.recent_micro_low)

        atr = float(inputs.atr)
        if broken:
            # LIVE MARKET: fire at current price; preserve original SL/TP.
            return FiboGoldenDecision(
                action=ACTION_LIVE_MARKET,
                entry_type="market",
                new_entry=round(float(inputs.current_price), 6),
                new_stop_loss=round(float(inputs.original_stop_loss), 6),
                new_take_profit=round(float(inputs.original_take_profit), 6),
                ratio_at_price=round(ratio, 4),
                inside_golden_zone=True,
                break_confirmed=True,
                reason=(
                    f"break_confirmed_live:dir={direction},ratio={ratio:.3f},"
                    f"micro_high={inputs.recent_micro_high},last_close={inputs.last_m1_close}"
                ),
            )

        # WAIT_BREAK_STOP: place STOP at micro swing + buffer in trade direction.
        buffer = atr * float(cfg.stop_trigger_buffer_atr)
        sl_buffer = atr * float(cfg.sl_buffer_atr)
        if direction == "long":
            new_entry = float(inputs.recent_micro_high) + buffer
            new_sl = float(inputs.recent_micro_low) - sl_buffer
            risk = new_entry - new_sl
        else:
            new_entry = float(inputs.recent_micro_low) - buffer
            new_sl = float(inputs.recent_micro_high) + sl_buffer
            risk = new_sl - new_entry

        if risk < cfg.min_risk_distance:
            return _passthrough(
                f"risk_too_small:{risk:.4f}<{cfg.min_risk_distance:.2f}",
                inputs=inputs, ratio=ratio, inside=True, broken=False,
            )
        max_risk = atr * float(cfg.max_risk_atr_mult)
        if risk > max_risk:
            return _passthrough(
                f"risk_too_large:{risk:.4f}>{max_risk:.4f}",
                inputs=inputs, ratio=ratio, inside=True, broken=False,
            )

        rr = float(cfg.tp_rr_target)
        new_tp = new_entry + risk * rr if direction == "long" else new_entry - risk * rr
        return FiboGoldenDecision(
            action=ACTION_WAIT_BREAK_STOP,
            entry_type="stop",
            new_entry=round(new_entry, 6),
            new_stop_loss=round(new_sl, 6),
            new_take_profit=round(new_tp, 6),
            ratio_at_price=round(ratio, 4),
            inside_golden_zone=True,
            break_confirmed=False,
            reason=(
                f"wait_break_stop:dir={direction},ratio={ratio:.3f},"
                f"entry={new_entry:.4f},sl={new_sl:.4f},tp={new_tp:.4f},rr={rr:.2f}"
            ),
        )


__all__ = [
    "FiboGoldenBreakEntry",
    "FiboGoldenBreakConfig",
    "FiboGoldenInputs",
    "FiboGoldenDecision",
    "ACTION_LIVE_MARKET",
    "ACTION_WAIT_BREAK_STOP",
    "ACTION_SKIP",
]

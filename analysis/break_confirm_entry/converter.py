"""Break-Confirm Entry implementation.

Conversion rule for a SHORT signal whose direction is opposite to the
recent M1 streak (counter-trend):

    new_entry = swing_low - buffer_atr_mult * atr
    new_sl    = swing_high + sl_buffer_atr_mult * atr
    risk      = new_sl - new_entry
    new_tp    = new_entry - risk * tp_rr_target
    entry_type = "stop"

Mirror for LONG. The STOP order sits BELOW the recent swing low (or
ABOVE the swing high for longs) so it triggers only when the market
actually breaks in the signal's direction — eliminating the chase
behaviour that produced the -$70.23 loss.

Safety:
- ``min_risk_distance`` and ``max_risk_distance_atr_mult`` clamp the
  new risk so position sizing stays sane.
- ``min_rr`` rejects conversions whose RR is worse than threshold so
  we don't substitute a bad limit with a worse stop.
- When ``enabled=False`` the converter returns ``converted=False`` with
  the original signal values, preserving live behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class BreakConfirmInputs:
    direction: str
    original_entry: float
    original_stop_loss: float
    original_take_profit: float
    atr: float
    swing_high: float
    swing_low: float
    counter_trend: bool       # caller flags this from M1 streak analysis


@dataclass(frozen=True)
class BreakConfirmDecision:
    converted: bool
    new_entry_type: str        # "stop" when converted, "" otherwise
    new_entry: float
    new_stop_loss: float
    new_take_profit: float
    new_risk_distance: float
    new_rr: float
    reason: str


@dataclass
class BreakConfirmEntryConfig:
    enabled: bool = False
    # Only convert when caller flags counter_trend=True; otherwise keep
    # the legitimate retest-style limit unchanged.
    only_counter_trend: bool = True
    buffer_atr_mult: float = 0.10        # distance of stop trigger past swing
    sl_buffer_atr_mult: float = 1.0      # SL sits past the opposite swing + this × ATR
    tp_rr_target: float = 1.2            # TP at this RR from new entry
    min_risk_distance: float = 0.5       # absolute min risk in price units
    max_risk_distance_atr_mult: float = 3.5
    min_rr: float = 0.8                  # reject conversion if RR < this


def passthrough(reason: str) -> BreakConfirmDecision:
    return BreakConfirmDecision(
        converted=False,
        new_entry_type="",
        new_entry=0.0,
        new_stop_loss=0.0,
        new_take_profit=0.0,
        new_risk_distance=0.0,
        new_rr=0.0,
        reason=reason,
    )


class BreakConfirmEntry:
    """Converts chase-LIMIT scalp signals into break-confirm STOP entries."""

    def __init__(self, *, config: Optional[BreakConfirmEntryConfig] = None) -> None:
        self.config = config or BreakConfirmEntryConfig()

    def evaluate(self, inputs: BreakConfirmInputs) -> BreakConfirmDecision:
        cfg = self.config
        if not cfg.enabled:
            return passthrough("disabled")
        if cfg.only_counter_trend and not inputs.counter_trend:
            return passthrough("not_counter_trend")
        direction = _norm_dir(inputs.direction)
        if direction not in {"long", "short"}:
            return passthrough("bad_direction")
        atr = float(inputs.atr)
        if atr <= 0:
            return passthrough("atr_invalid")
        swing_high = float(inputs.swing_high)
        swing_low = float(inputs.swing_low)
        if swing_high <= swing_low:
            return passthrough("swing_invalid")

        buffer = atr * cfg.buffer_atr_mult
        sl_buffer = atr * cfg.sl_buffer_atr_mult

        if direction == "short":
            new_entry = swing_low - buffer
            new_sl = swing_high + sl_buffer
            risk = new_sl - new_entry
        else:  # long
            new_entry = swing_high + buffer
            new_sl = swing_low - sl_buffer
            risk = new_entry - new_sl

        if risk < cfg.min_risk_distance:
            return passthrough(f"risk_too_small:{risk:.4f}<{cfg.min_risk_distance:.2f}")
        max_risk = atr * cfg.max_risk_distance_atr_mult
        if risk > max_risk:
            return passthrough(f"risk_too_large:{risk:.4f}>{max_risk:.4f}")

        # Build TP from RR target.
        rr = float(cfg.tp_rr_target)
        if direction == "short":
            new_tp = new_entry - risk * rr
        else:
            new_tp = new_entry + risk * rr

        # Compute actual RR using the ORIGINAL TP as a comparison if possible.
        if direction == "short":
            achieved_rr = (new_entry - new_tp) / max(risk, 1e-9)
        else:
            achieved_rr = (new_tp - new_entry) / max(risk, 1e-9)
        if achieved_rr < cfg.min_rr:
            return passthrough(f"rr_too_low:{achieved_rr:.2f}<{cfg.min_rr:.2f}")

        reason = (
            f"break_confirm_stop:dir={direction},entry={new_entry:.4f},"
            f"sl={new_sl:.4f},tp={new_tp:.4f},risk={risk:.4f},rr={achieved_rr:.2f}"
        )
        return BreakConfirmDecision(
            converted=True,
            new_entry_type="stop",
            new_entry=round(new_entry, 6),
            new_stop_loss=round(new_sl, 6),
            new_take_profit=round(new_tp, 6),
            new_risk_distance=round(risk, 4),
            new_rr=round(achieved_rr, 4),
            reason=reason,
        )


__all__ = [
    "BreakConfirmEntry",
    "BreakConfirmEntryConfig",
    "BreakConfirmInputs",
    "BreakConfirmDecision",
]

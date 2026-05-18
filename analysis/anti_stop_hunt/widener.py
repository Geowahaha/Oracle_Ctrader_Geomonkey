"""Pure function: widen a proposed SL beyond the swing extreme + ATR buffer.

Inputs and outputs are simple floats; no state, no I/O.

For a SHORT position:
    safe_sl = max(proposed_sl, swing_extreme + atr * buffer_mult)
For a LONG position:
    safe_sl = min(proposed_sl, swing_extreme - atr * buffer_mult)

The hard cap `max_widening_atr_mult` prevents the widener from blowing
the SL too far out (which would make R-distance huge and shrink position
size). If the widening would exceed that cap, we use the cap instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class AntiHuntConfig:
    enabled: bool = False
    buffer_atr_mult: float = 1.0
    max_widening_atr_mult: float = 2.5  # don't widen further than this from proposed
    min_atr: float = 1e-6


@dataclass(frozen=True)
class AntiHuntDecision:
    """Output bundle (used by tests; the convenience wrapper returns a float)."""

    safe_sl: float
    widened_by: float
    reason: str


def _decide(
    *,
    entry: float,
    direction: str,
    proposed_sl: float,
    swing_extreme: float,
    atr: float,
    config: AntiHuntConfig,
) -> AntiHuntDecision:
    if not config.enabled:
        return AntiHuntDecision(safe_sl=float(proposed_sl), widened_by=0.0, reason="disabled")
    if atr is None or atr < config.min_atr:
        return AntiHuntDecision(safe_sl=float(proposed_sl), widened_by=0.0, reason="atr_too_small")
    direction = _norm_dir(direction)
    if direction not in {"long", "short"}:
        return AntiHuntDecision(safe_sl=float(proposed_sl), widened_by=0.0, reason="bad_direction")

    buffer = float(atr) * float(config.buffer_atr_mult)
    max_widening = float(atr) * float(config.max_widening_atr_mult)

    if direction == "short":
        # SL is above entry; we want max(proposed, swing+buffer).
        candidate = float(swing_extreme) + buffer
        cap = float(proposed_sl) + max_widening
        safe = min(max(float(proposed_sl), candidate), cap)
        widened = safe - float(proposed_sl)
    else:  # long
        # SL is below entry; we want min(proposed, swing-buffer).
        candidate = float(swing_extreme) - buffer
        cap = float(proposed_sl) - max_widening
        safe = max(min(float(proposed_sl), candidate), cap)
        widened = float(proposed_sl) - safe

    reason = (
        f"anti_hunt:dir={direction},buffer={buffer:.4f},widened_by={widened:.4f}"
        if widened > 1e-9
        else "no_widening_needed"
    )
    return AntiHuntDecision(safe_sl=round(safe, 6), widened_by=round(max(0.0, widened), 6), reason=reason)


def widen_sl_for_anti_hunt(
    *,
    entry: float,
    direction: str,
    proposed_sl: float,
    swing_extreme: float,
    atr: float,
    config: Optional[AntiHuntConfig] = None,
) -> float:
    """Return the safe SL price; falls back to `proposed_sl` when disabled."""
    cfg = config or AntiHuntConfig()
    decision = _decide(
        entry=entry, direction=direction, proposed_sl=proposed_sl,
        swing_extreme=swing_extreme, atr=atr, config=cfg,
    )
    return decision.safe_sl


__all__ = ["AntiHuntConfig", "AntiHuntDecision", "widen_sl_for_anti_hunt", "_decide"]

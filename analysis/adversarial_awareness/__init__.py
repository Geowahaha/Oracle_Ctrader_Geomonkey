"""Adversarial Awareness — detect the patterns the market is playing on us.

Operator directive 2026-05-20:
> "คิดกลับกัน สวนทางกัน กระบวนการเทรดและตลาดกำลังเล่นอะไรกับเรา
>  จิตวิทยาการเทรด เอามาใส่ด้วย"

A 48-hour audit (-$451.81 / 34 trades) revealed the meta-pattern:
- 41% of trades closed as PANIC_CLOSE_NOISE (small loss on a noise wiggle
  with SL still far away)
- 18% STOP_HUNT_FULL_SL (the SL was hit exactly where market makers want)
- 6% MFE_GIVEBACK (peak >1R then gave it all back)
- 6% WIN_CUT_EARLY (closed too soon on a real move)
- 24% genuine wins
- 28 of 34 trades were shorts clustered at the top of recent ranges =
  we were the liquidity, not the smart money.

This module exposes two cognitive layers:

1. **PsychologyTagger** — classifies any closed trade into one of the six
   recognised patterns so downstream systems (and operators) can reason
   about them by name rather than just by PnL.

2. **AdversarialAwareness** — the meta-cognitive layer that tracks recent
   close events and emits ``CoolDownDirective`` when it detects:
     - revenge clustering (N losses same-direction in M minutes)
     - hunt clustering (M STOP_HUNT_FULL_SL events within window)
     - panic clustering (M PANIC_CLOSE_NOISE in a row)
     - global drawdown burst (cumulative loss over N trades > threshold)

The directive carries a ``cooldown_until_utc`` and a ``scope`` (per
direction / per source / global) so the scheduler can short-circuit
new signals.

Public surface:

    from analysis.adversarial_awareness import (
        PsychologyTagger, PsychologyTag,
        AdversarialAwareness, AdversarialAwarenessConfig,
        CloseEvent, CoolDownDirective,
    )
"""
from __future__ import annotations

from analysis.adversarial_awareness.awareness import (
    AdversarialAwareness,
    AdversarialAwarenessConfig,
    CloseEvent,
    CoolDownDirective,
    PsychologyTag,
    PsychologyTagger,
)


__all__ = [
    "AdversarialAwareness",
    "AdversarialAwarenessConfig",
    "CloseEvent",
    "CoolDownDirective",
    "PsychologyTag",
    "PsychologyTagger",
]

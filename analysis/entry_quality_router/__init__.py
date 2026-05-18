"""Entry Quality Router — decide between MARKET / STOP / KILL for scalp entries.

Operator directive 2026-05-18: "ดูทั้งสองด้าน kill and improve BUY/SELL
limit แบบไร้คุณภาพ improve ด้วย stop order or live executed entry แบบสด
ไม่เมื่อมีสัญญาณดีกว่ส" — view both sides; KILL low-quality limit orders;
IMPROVE with stop or live market entry when signal is good.

This module is the *first* quality gate after a scalp signal is composed.
For each scalp BUY or SELL LIMIT signal it returns one of:

  - ``KILL``      — signal is below the quality floor; drop entirely.
  - ``MARKET``    — signal is strong enough to fire LIVE now; upgrade
                    entry_type to ``market`` so the broker fills at the
                    current ask/bid instead of waiting on a limit.
  - ``PASS``      — quality is mid-band; let downstream guards
                    (BreakConfirmEntry, CounterTrendBlocker) decide.

The router never *creates* a STOP order itself — that's
``analysis.break_confirm_entry`` (which handles the counter-trend case).
The router only KILLS or UPGRADES; PASS preserves the existing signal so
the proven path continues unchanged.

Public surface:

    from analysis.entry_quality_router import (
        EntryQualityRouter, EntryQualityRouterConfig, EntryQualityInputs,
        EntryQualityDecision, ACTION_KILL, ACTION_MARKET, ACTION_PASS,
    )
    decision = router.evaluate(inputs)
"""
from __future__ import annotations

from analysis.entry_quality_router.router import (
    ACTION_KILL,
    ACTION_MARKET,
    ACTION_PASS,
    EntryQualityDecision,
    EntryQualityInputs,
    EntryQualityRouter,
    EntryQualityRouterConfig,
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

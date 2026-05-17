"""Predictive Pre-Signal Engine (Tier 3 #10).

The signal generator takes ~3 seconds to confirm. By that time the move is
sometimes 0.4× ATR done. This engine continuously fuses Volume Profile, DOM
Liquidity, and Sharpness scores into a conviction value every second. When
conviction crosses `arm_threshold`, it emits a `PreArmDirective` that the
executor turns into a small pending order with a short TTL. If the real signal
fires inside the TTL, the pre-arm becomes a confirmed entry; if not, the
pre-arm cancels itself.

Public surface:

    from analysis.pre_signal_engine import (
        ConvictionInputs, PreSignalEngine, PreArmDirective,
    )

    engine = PreSignalEngine(config=...)
    directive = engine.evaluate(symbol="XAUUSD", inputs=inputs, now=now)
    if directive is not None:
        executor.place_pre_arm(directive)
"""
from __future__ import annotations

from analysis.pre_signal_engine.engine import (
    ConvictionInputs,
    PreArmDirective,
    PreSignalEngine,
    PreSignalEngineConfig,
)


__all__ = [
    "PreSignalEngine",
    "PreSignalEngineConfig",
    "ConvictionInputs",
    "PreArmDirective",
]

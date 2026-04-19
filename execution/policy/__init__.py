"""
execution.policy — V4 execution policy layer (phase 1: Winner + Defense only).

Detection → Policy → Enforcement architecture.
This package is the POLICY layer. It owns no state; it is pure stateless
decision logic that callers query. The ENFORCEMENT layer (ctrader_executor)
reads these decisions and applies them.

Feature-gated via CTRADER_PM_POLICY_LAYER_ENABLED (default: False).
When disabled, no call site changes behavior — legacy paths run unchanged.
"""

from execution.policy.active_defense import (
    ActiveDefenseInput,
    ActiveDefensePolicy,
    ActiveDefenseThresholds,
)
from execution.policy.resolver import (
    PolicyResolver,
    RegimeSnapshot,
    default_resolver,
)
from execution.policy.winner_protection import (
    WinnerProtectionConfig,
    WinnerProtectionDecision,
    WinnerProtectionInput,
    WinnerProtectionPolicy,
    WinnerState,
)

__all__ = [
    "ActiveDefenseInput",
    "ActiveDefensePolicy",
    "ActiveDefenseThresholds",
    "PolicyResolver",
    "RegimeSnapshot",
    "WinnerProtectionConfig",
    "WinnerProtectionDecision",
    "WinnerProtectionInput",
    "WinnerProtectionPolicy",
    "WinnerState",
    "default_resolver",
]

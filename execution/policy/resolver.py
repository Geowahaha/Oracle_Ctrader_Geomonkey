"""
PolicyResolver — minimal query API for the V4 execution policy layer.

Phase 1 exposes only `defense()` and `winner()` — exactly what the
ctrader_executor active-defense call site needs. Future phases will add
`entry()`, `sizing()`, `swarm()` without changing this interface.

Design
------
- Stateless. The resolver holds policy objects and a snapshot provider
  callable; it mutates nothing.
- The RegimeSnapshot class here is a minimal placeholder. A real snapshot
  lands in phase 0/2 when we wire `openclaw/agents/regime_agent.py` into
  the resolver. Until then, the default provider returns
  `confidence=0.0`, which forces every downstream policy to use its
  static fallback — zero behavior change on deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from execution.policy.active_defense import ActiveDefensePolicy
from execution.policy.winner_protection import (
    WinnerProtectionConfig,
    WinnerProtectionPolicy,
)


@dataclass(frozen=True)
class RegimeSnapshot:
    """Minimal placeholder until openclaw.regime_agent is bound in."""

    label: str = "ranging"
    confidence: float = 0.0
    stability_s: int = 0
    features: Mapping[str, Any] = field(default_factory=dict)
    source: str = "placeholder"


SnapshotProvider = Callable[[], RegimeSnapshot]


def _placeholder_snapshot() -> RegimeSnapshot:
    return RegimeSnapshot()


class PolicyResolver:
    def __init__(
        self,
        winner_policy: Optional[WinnerProtectionPolicy] = None,
        active_defense_policy: Optional[ActiveDefensePolicy] = None,
        snapshot_provider: Optional[SnapshotProvider] = None,
    ) -> None:
        self._winner = winner_policy or WinnerProtectionPolicy()
        self._defense = active_defense_policy or ActiveDefensePolicy()
        self._snapshot_provider = snapshot_provider or _placeholder_snapshot

    def winner(self) -> WinnerProtectionPolicy:
        return self._winner

    def defense(self) -> ActiveDefensePolicy:
        return self._defense

    def snapshot(self) -> RegimeSnapshot:
        try:
            snap = self._snapshot_provider()
            if snap is None:
                return _placeholder_snapshot()
            return snap
        except Exception:
            return _placeholder_snapshot()


_default_resolver: Optional[PolicyResolver] = None


def default_resolver() -> PolicyResolver:
    """Process-wide resolver. Cheap to construct; lazy for test isolation."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = PolicyResolver()
    return _default_resolver


def reset_default_resolver() -> None:
    """Test-only. Forces the next `default_resolver()` to rebuild."""
    global _default_resolver
    _default_resolver = None


def configure_default_resolver(
    *,
    winner_policy: Optional[WinnerProtectionPolicy] = None,
    active_defense_policy: Optional[ActiveDefensePolicy] = None,
    snapshot_provider: Optional[SnapshotProvider] = None,
    winner_config: Optional[WinnerProtectionConfig] = None,
) -> PolicyResolver:
    """Install a resolver configured with specific policies."""
    global _default_resolver
    wp = winner_policy or WinnerProtectionPolicy(cfg=winner_config)
    adp = active_defense_policy or ActiveDefensePolicy()
    _default_resolver = PolicyResolver(
        winner_policy=wp,
        active_defense_policy=adp,
        snapshot_provider=snapshot_provider,
    )
    return _default_resolver

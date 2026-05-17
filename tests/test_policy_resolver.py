"""Unit tests for PolicyResolver singleton + DI helpers."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from execution.policy.active_defense import ActiveDefensePolicy
from execution.policy.resolver import (
    PolicyResolver,
    RegimeSnapshot,
    configure_default_resolver,
    default_resolver,
    reset_default_resolver,
)
from execution.policy.winner_protection import (
    WinnerProtectionConfig,
    WinnerProtectionPolicy,
)


class DefaultResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_default_resolver()

    def tearDown(self) -> None:
        reset_default_resolver()

    def test_default_resolver_is_singleton(self) -> None:
        a = default_resolver()
        b = default_resolver()
        self.assertIs(a, b)

    def test_default_resolver_provides_default_policies(self) -> None:
        r = default_resolver()
        self.assertIsInstance(r.winner(), WinnerProtectionPolicy)
        self.assertIsInstance(r.defense(), ActiveDefensePolicy)

    def test_reset_yields_new_instance(self) -> None:
        a = default_resolver()
        reset_default_resolver()
        b = default_resolver()
        self.assertIsNot(a, b)

    def test_default_snapshot_has_zero_confidence(self) -> None:
        # Placeholder snapshot must force static fallback in downstream policies.
        snap = default_resolver().snapshot()
        self.assertEqual(snap.confidence, 0.0)
        self.assertEqual(snap.source, "placeholder")


class ConfigureResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_default_resolver()

    def tearDown(self) -> None:
        reset_default_resolver()

    def test_configure_with_winner_config(self) -> None:
        cfg = WinnerProtectionConfig(arm_r=1.5, lock_r=2.5, trail_r=4.0, lock_floor_r=1.0)
        r = configure_default_resolver(winner_config=cfg)
        self.assertIs(r, default_resolver())
        self.assertEqual(r.winner().config.arm_r, 1.5)
        self.assertEqual(r.winner().config.lock_r, 2.5)

    def test_configure_with_explicit_winner_policy(self) -> None:
        wp = WinnerProtectionPolicy(WinnerProtectionConfig(arm_r=2.5, lock_r=4.0, trail_r=6.0))
        r = configure_default_resolver(winner_policy=wp)
        self.assertIs(r.winner(), wp)

    def test_configure_with_explicit_defense_policy(self) -> None:
        adp = ActiveDefensePolicy(confidence_threshold=0.40)
        r = configure_default_resolver(active_defense_policy=adp)
        self.assertIs(r.defense(), adp)

    def test_configure_with_snapshot_provider(self) -> None:
        def provider() -> RegimeSnapshot:
            return RegimeSnapshot(label="trending_bull", confidence=0.85, source="test")

        r = configure_default_resolver(snapshot_provider=provider)
        snap = r.snapshot()
        self.assertEqual(snap.label, "trending_bull")
        self.assertEqual(snap.confidence, 0.85)
        self.assertEqual(snap.source, "test")

    def test_explicit_winner_policy_takes_precedence_over_config(self) -> None:
        wp = WinnerProtectionPolicy(WinnerProtectionConfig(arm_r=2.5, lock_r=3.5, trail_r=5.5))
        cfg = WinnerProtectionConfig(arm_r=4.0, lock_r=6.0, trail_r=8.0, lock_floor_r=2.0)
        r = configure_default_resolver(winner_policy=wp, winner_config=cfg)
        # Explicit policy wins; config is ignored.
        self.assertEqual(r.winner().config.arm_r, 2.5)


class SnapshotErrorHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_default_resolver()

    def tearDown(self) -> None:
        reset_default_resolver()

    def test_provider_exception_returns_placeholder(self) -> None:
        def bad_provider() -> RegimeSnapshot:
            raise RuntimeError("snapshot blew up")

        r = configure_default_resolver(snapshot_provider=bad_provider)
        snap = r.snapshot()
        # Falls back to placeholder; never propagates exception to executor.
        self.assertEqual(snap.confidence, 0.0)
        self.assertEqual(snap.source, "placeholder")

    def test_provider_returning_none_returns_placeholder(self) -> None:
        def none_provider():
            return None

        r = configure_default_resolver(snapshot_provider=none_provider)
        snap = r.snapshot()
        self.assertEqual(snap.confidence, 0.0)


class DirectInstantiationTests(unittest.TestCase):
    def test_construct_with_no_args_uses_defaults(self) -> None:
        r = PolicyResolver()
        self.assertIsInstance(r.winner(), WinnerProtectionPolicy)
        self.assertIsInstance(r.defense(), ActiveDefensePolicy)
        self.assertEqual(r.snapshot().confidence, 0.0)


if __name__ == "__main__":
    unittest.main()

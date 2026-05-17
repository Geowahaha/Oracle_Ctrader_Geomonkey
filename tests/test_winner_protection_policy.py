"""Unit tests for WinnerProtectionPolicy state machine."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from execution.policy.winner_protection import (
    WinnerProtectionConfig,
    WinnerProtectionInput,
    WinnerProtectionPolicy,
    WinnerState,
)


def _decide(policy: WinnerProtectionPolicy, *, r_now: float, r_peak: float | None = None):
    return policy.decide(
        WinnerProtectionInput(r_now=r_now, r_peak=r_peak if r_peak is not None else r_now)
    )


class WinnerProtectionConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        cfg = WinnerProtectionConfig()
        self.assertLess(cfg.arm_r, cfg.lock_r)
        self.assertLess(cfg.lock_r, cfg.trail_r)
        self.assertLess(cfg.lock_floor_r, cfg.lock_r)
        self.assertGreater(cfg.giveback_emergency_ratio, 0.0)
        self.assertLess(cfg.giveback_emergency_ratio, 1.0)

    def test_invalid_ordering_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(arm_r=3.0, lock_r=2.0, trail_r=5.0)
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(arm_r=2.0, lock_r=3.0, trail_r=2.5)
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(lock_floor_r=3.5)
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(giveback_emergency_ratio=0.0)
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(giveback_emergency_ratio=1.0)
        with self.assertRaises(ValueError):
            WinnerProtectionConfig(locked_active_defense_score_bonus=-1)


class StateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = WinnerProtectionPolicy()
        self.cfg = self.policy.config

    def test_running_at_zero_r(self) -> None:
        d = _decide(self.policy, r_now=0.0)
        self.assertEqual(d.state, WinnerState.RUNNING)
        self.assertTrue(d.allow_active_defense_close)
        self.assertTrue(d.allow_active_defense_tighten)
        self.assertIsNone(d.structural_sl_floor_r)
        self.assertFalse(d.force_close)

    def test_running_below_arm(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.arm_r - 0.01)
        self.assertEqual(d.state, WinnerState.RUNNING)

    def test_armed_at_arm_threshold(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.arm_r)
        self.assertEqual(d.state, WinnerState.ARMED)
        self.assertTrue(d.allow_active_defense_close)
        self.assertTrue(d.allow_active_defense_tighten)
        self.assertIsNone(d.structural_sl_floor_r)

    def test_armed_below_lock(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.lock_r - 0.01)
        self.assertEqual(d.state, WinnerState.ARMED)

    def test_locked_at_lock_threshold(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.lock_r)
        self.assertEqual(d.state, WinnerState.LOCKED)
        self.assertTrue(d.allow_active_defense_close)
        self.assertEqual(d.structural_sl_floor_r, self.cfg.lock_floor_r)
        self.assertFalse(d.force_close)

    def test_locked_persists_when_giving_back_above_floor(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.lock_r, r_peak=self.cfg.trail_r - 0.5)
        self.assertEqual(d.state, WinnerState.LOCKED)

    def test_trailing_at_trail_threshold(self) -> None:
        d = _decide(self.policy, r_now=self.cfg.trail_r)
        self.assertEqual(d.state, WinnerState.TRAILING_STRUCT)
        self.assertFalse(d.allow_active_defense_close)
        self.assertFalse(d.allow_active_defense_tighten)
        self.assertIsNotNone(d.structural_sl_floor_r)
        self.assertGreaterEqual(d.structural_sl_floor_r, self.cfg.lock_floor_r)
        self.assertFalse(d.force_close)

    def test_trailing_persists_when_pulling_back(self) -> None:
        # Peak above trail_r, current safely above giveback emergency
        # threshold (peak * giveback_ratio) so we test TRAILING, not
        # EMERGENCY. r_now also still above lock_r so the persistence
        # branch fires.
        peak = self.cfg.trail_r + 1.0
        r_now = peak * 0.6
        d = _decide(self.policy, r_now=r_now, r_peak=peak)
        self.assertEqual(d.state, WinnerState.TRAILING_STRUCT)
        self.assertGreaterEqual(d.structural_sl_floor_r, peak * 0.5)

    def test_emergency_when_giveback_excessive(self) -> None:
        peak = self.cfg.lock_r + 2.0
        # Below ratio of peak → EMERGENCY force close.
        r_now = peak * (self.cfg.giveback_emergency_ratio - 0.05)
        d = _decide(self.policy, r_now=r_now, r_peak=peak)
        self.assertEqual(d.state, WinnerState.EMERGENCY)
        self.assertTrue(d.force_close)

    def test_emergency_does_not_trigger_below_lock_peak(self) -> None:
        # Peak never reached lock_r → no emergency, just RUNNING/ARMED.
        peak = self.cfg.arm_r + 0.5
        r_now = -1.0
        d = _decide(self.policy, r_now=r_now, r_peak=peak)
        self.assertNotEqual(d.state, WinnerState.EMERGENCY)


class GivebackBehaviorTests(unittest.TestCase):
    def test_locked_state_score_bonus(self) -> None:
        policy = WinnerProtectionPolicy(
            WinnerProtectionConfig(locked_active_defense_score_bonus=4)
        )
        self.assertEqual(policy.locked_close_score_bonus(), 4)

    def test_r_peak_corrected_when_below_r_now(self) -> None:
        # Caller may pass a stale r_peak smaller than r_now; policy should
        # treat r_peak = max(r_peak, r_now) so emergency can't false-trigger.
        policy = WinnerProtectionPolicy()
        d = policy.decide(WinnerProtectionInput(r_now=4.0, r_peak=1.0))
        self.assertNotEqual(d.state, WinnerState.EMERGENCY)

    def test_invalid_inputs_handled_gracefully(self) -> None:
        policy = WinnerProtectionPolicy()
        d = policy.decide(WinnerProtectionInput(r_now=float("nan"), r_peak=float("nan")))
        # nan comparisons → all False → falls through to RUNNING
        self.assertEqual(d.state, WinnerState.RUNNING)


class CustomThresholdTests(unittest.TestCase):
    def test_aggressive_thresholds(self) -> None:
        # Lower thresholds → faster lock/trail.
        cfg = WinnerProtectionConfig(arm_r=1.0, lock_r=2.0, trail_r=3.0, lock_floor_r=0.5)
        policy = WinnerProtectionPolicy(cfg)
        self.assertEqual(_decide(policy, r_now=2.0).state, WinnerState.LOCKED)
        self.assertEqual(_decide(policy, r_now=3.0).state, WinnerState.TRAILING_STRUCT)

    def test_conservative_thresholds(self) -> None:
        # Higher thresholds → trades stay RUNNING longer; matches the
        # "do not BE too early" preference from project memory.
        cfg = WinnerProtectionConfig(arm_r=3.0, lock_r=5.0, trail_r=8.0, lock_floor_r=2.5)
        policy = WinnerProtectionPolicy(cfg)
        self.assertEqual(_decide(policy, r_now=2.5).state, WinnerState.RUNNING)
        self.assertEqual(_decide(policy, r_now=4.5).state, WinnerState.ARMED)
        self.assertEqual(_decide(policy, r_now=6.0).state, WinnerState.LOCKED)


if __name__ == "__main__":
    unittest.main()

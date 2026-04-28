"""Surgery 2026-04-29: tests for XAU directive pause ceiling + high-confidence bypass.

These guard the change that prevents a single bad XAU trade from freezing the
lane for hours. Without them an `xau_execution_directive` set with `pause_until_utc`
hours in the future would block every subsequent signal until manual intervention.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scheduler as scheduler_module


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestActiveDirectiveCeiling(unittest.TestCase):
    def test_directive_within_ceiling_returns_state(self):
        now = datetime.now(timezone.utc)
        runtime = {
            "xau_execution_directive": {
                "status": "active",
                "applied_at": _iso(now - timedelta(minutes=2)),
                "pause_until_utc": _iso(now + timedelta(minutes=8)),
                "trigger_ts": (now - timedelta(minutes=2)).timestamp(),
                "blocked_direction": "short",
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            state = scheduler_module.DexterScheduler._active_xau_execution_directive(runtime)
        self.assertTrue(state)
        self.assertEqual(state.get("blocked_direction"), "short")

    def test_directive_past_ceiling_returns_empty(self):
        # applied_at is older than the ceiling, even though pause_until_utc is far in the future.
        now = datetime.now(timezone.utc)
        runtime = {
            "xau_execution_directive": {
                "status": "active",
                "applied_at": _iso(now - timedelta(minutes=240)),
                "pause_until_utc": _iso(now + timedelta(hours=4)),
                "trigger_ts": (now - timedelta(minutes=240)).timestamp(),
                "blocked_direction": "short",
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            state = scheduler_module.DexterScheduler._active_xau_execution_directive(runtime)
        self.assertEqual(state, {})

    def test_directive_pause_expired_returns_empty(self):
        now = datetime.now(timezone.utc)
        runtime = {
            "xau_execution_directive": {
                "status": "active",
                "applied_at": _iso(now - timedelta(minutes=3)),
                "pause_until_utc": _iso(now - timedelta(minutes=1)),
                "trigger_ts": (now - timedelta(minutes=3)).timestamp(),
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            state = scheduler_module.DexterScheduler._active_xau_execution_directive(runtime)
        self.assertEqual(state, {})

    def test_inactive_directive_returns_empty(self):
        runtime = {"xau_execution_directive": {"status": "inactive"}}
        state = scheduler_module.DexterScheduler._active_xau_execution_directive(runtime)
        self.assertEqual(state, {})

    def test_regime_transition_past_ceiling_returns_empty(self):
        now = datetime.now(timezone.utc)
        runtime = {
            "xau_regime_transition": {
                "status": "active",
                "applied_at": _iso(now - timedelta(minutes=120)),
                "hold_until_utc": _iso(now + timedelta(hours=2)),
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            state = scheduler_module.DexterScheduler._active_xau_regime_transition(runtime)
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()

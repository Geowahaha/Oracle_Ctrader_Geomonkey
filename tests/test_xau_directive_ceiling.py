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


class TestRuntimeStateCeilingApplied(unittest.TestCase):
    """The load-time ceiling backstop catches every xau_* state machine, not just
    the directive/transition pair. Without this, shock_demote / order_care /
    family_routing remained pinned for hours after a single bad bar."""

    def test_stale_active_xau_states_flipped_to_inactive(self):
        now = datetime.now(timezone.utc)
        payload = {
            "xau_shock_profile": {
                "status": "active",
                "mode": "shock_protect",
                "applied_at": _iso(now - timedelta(minutes=240)),
            },
            "xau_family_routing": {
                "status": "active",
                "mode": "shock_demote",
                "applied_at": _iso(now - timedelta(minutes=60)),
            },
            "xau_order_care": {
                "status": "active",
                "mode": "continuation_fail_fast",
                "applied_at": _iso(now - timedelta(minutes=2)),  # fresh — must keep
            },
            "non_xau_thing": {"status": "active", "applied_at": _iso(now - timedelta(hours=24))},
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            cleaned = scheduler_module.DexterScheduler._apply_runtime_state_ceiling(payload)
        self.assertEqual(cleaned["xau_shock_profile"]["status"], "inactive")
        self.assertTrue(cleaned["xau_shock_profile"].get("ceiling_expired"))
        self.assertEqual(cleaned["xau_family_routing"]["status"], "inactive")
        self.assertEqual(cleaned["xau_order_care"]["status"], "active")  # within ceiling
        # non-xau states untouched
        self.assertEqual(cleaned["non_xau_thing"]["status"], "active")

    def test_ceiling_skipped_when_no_applied_at(self):
        payload = {
            "xau_some_state": {
                "status": "active",
                "mode": "test",
                # no applied_at — leave alone (cannot judge age)
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 10):
            cleaned = scheduler_module.DexterScheduler._apply_runtime_state_ceiling(payload)
        self.assertEqual(cleaned["xau_some_state"]["status"], "active")

    def test_ceiling_disabled_when_zero(self):
        now = datetime.now(timezone.utc)
        payload = {
            "xau_shock_profile": {
                "status": "active",
                "applied_at": _iso(now - timedelta(hours=24)),
            }
        }
        with patch.object(scheduler_module.config, "XAU_DIRECTIVE_PAUSE_CEILING_MIN", 0):
            cleaned = scheduler_module.DexterScheduler._apply_runtime_state_ceiling(payload)
        self.assertEqual(cleaned["xau_shock_profile"]["status"], "active")


class TestCryptoFamilyWeekdayMap(unittest.TestCase):
    """ETH/BTC mapping flips between weekday and weekend variants based on UTC date.

    Note: the production helper imports `datetime` lazily inside the function so
    we cannot patch it via `patch("execution.ctrader_executor.datetime")`. We
    therefore assert that on a real weekday execution, the weekday families are
    returned — which is the regression we actually want to guard against
    (eth_weekend_winner being the default on a Tuesday).
    """

    def test_real_today_routes_via_weekday_branch_on_weekdays(self):
        from execution.ctrader_executor import CTraderExecutor
        today_weekday = datetime.now(timezone.utc).weekday()
        family_btc = CTraderExecutor._source_family("scalp_btcusd:canary")
        family_eth = CTraderExecutor._source_family("scalp_ethusd:canary")
        if today_weekday < 5:
            self.assertEqual(family_btc, "btc_weekday_lob_momentum")
            self.assertEqual(family_eth, "eth_weekday_overlap_probe")
        else:
            self.assertEqual(family_btc, "btc_weekend_winner")
            self.assertEqual(family_eth, "eth_weekend_winner")


class TestPatientStrategyProtection(unittest.TestCase):
    """Surgery 3 (2026-04-29) — fibo/scheduled limits must not be cancelled or
    closed by scalp-side heuristics. Without these guards, a 4604.62 sell-limit
    gets killed at 74min before price rallies to hit it, and an open fibo
    short gets force-closed at -0.04R abandoning the planned 1.5R+ target."""

    def test_is_patient_strategy_source_recognises_fibo(self):
        from execution.ctrader_executor import CTraderExecutor
        self.assertTrue(CTraderExecutor._is_patient_strategy_source("fibo_xauusd"))
        self.assertTrue(CTraderExecutor._is_patient_strategy_source("fibo_xauusd:winner"))
        self.assertTrue(CTraderExecutor._is_patient_strategy_source("fibo_xauusd:scout"))
        self.assertTrue(CTraderExecutor._is_patient_strategy_source("xauusd_scheduled"))
        self.assertTrue(CTraderExecutor._is_patient_strategy_source("xauusd_scheduled:canary"))
        # Scalp sources are NOT patient
        self.assertFalse(CTraderExecutor._is_patient_strategy_source("scalp_xauusd:fss:canary"))
        self.assertFalse(CTraderExecutor._is_patient_strategy_source("scalp_xauusd:winner"))
        self.assertFalse(CTraderExecutor._is_patient_strategy_source(""))

    def test_pending_order_ttl_fibo_uses_240min(self):
        from execution.ctrader_executor import ctrader_executor as exec_inst
        ttl = exec_inst._pending_order_ttl_min("fibo_xauusd", "XAUUSD")
        self.assertGreaterEqual(ttl, 240)  # at least 4 hours
        # Scalp source still uses the short TTL
        ttl_scalp = exec_inst._pending_order_ttl_min("scalp_xauusd:fss:canary", "XAUUSD")
        self.assertLessEqual(ttl_scalp, 60)


if __name__ == "__main__":
    unittest.main()

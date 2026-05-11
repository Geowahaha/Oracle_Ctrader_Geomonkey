import unittest

from analysis.fibo_mtf_trade_planner import FiboMtfPlannerInput, FiboMtfTradePlanner


class FiboMtfTradePlannerTests(unittest.TestCase):
    def test_other_idle_without_trigger_routes_observe_not_broker_order(self):
        planner = FiboMtfTradePlanner()
        decision = planner.plan(
            FiboMtfPlannerInput(
                symbol="XAUUSD",
                direction="short",
                timeframe="W1",
                current_price=4668.22,
                nearest_level_price=4668.22,
                raw_stop_loss=5656.86,
                atr=12.0,
                ratio_zone="other",
                impulse_state="idle",
                correction_end_confirmed=False,
                confidence=24.2,
            )
        )

        self.assertEqual(decision.route, "observe_only")
        self.assertIsNone(decision.trade_plan)
        self.assertIn("idle_no_trigger", decision.reasons)
        self.assertEqual(decision.intent, "learn_context")

    def test_other_zone_with_reclaim_trigger_becomes_small_probe_with_execution_sl_tp(self):
        planner = FiboMtfTradePlanner()
        decision = planner.plan(
            FiboMtfPlannerInput(
                symbol="XAUUSD",
                direction="long",
                timeframe="M15",
                current_price=2340.0,
                nearest_level_price=2339.5,
                raw_stop_loss=2280.0,
                atr=2.0,
                ratio_zone="other",
                impulse_state="idle",
                correction_end_confirmed=False,
                confidence=42.0,
                reclaim_confirmed=True,
                sweep_confirmed=True,
                execution_swing_low=2336.8,
            )
        )

        self.assertEqual(decision.route, "probe")
        self.assertIsNotNone(decision.trade_plan)
        plan = decision.trade_plan
        self.assertEqual(plan.size_multiplier, 0.30)
        self.assertFalse(plan.runner_enabled)
        self.assertGreaterEqual(plan.entry_price - plan.stop_loss, 1.2 * 2.0)
        self.assertLessEqual(plan.entry_price - plan.stop_loss, 4.0 * 2.0)
        self.assertEqual([tp.fraction for tp in plan.tp_plan], [0.70, 0.30])

    def test_confirmed_golden_ratio_impulse_restart_routes_base_live_with_staged_runner(self):
        planner = FiboMtfTradePlanner()
        decision = planner.plan(
            FiboMtfPlannerInput(
                symbol="XAUUSD",
                direction="long",
                timeframe="H1",
                current_price=2350.0,
                nearest_level_price=2349.5,
                raw_stop_loss=2344.0,
                atr=2.0,
                ratio_zone="0.65_0.70",
                impulse_state="restart",
                correction_end_confirmed=True,
                confidence=68.0,
                reclaim_confirmed=True,
                execution_swing_low=2346.4,
                regime="trend",
            )
        )

        self.assertEqual(decision.route, "base_live")
        plan = decision.trade_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.size_multiplier, 1.0)
        self.assertTrue(plan.runner_enabled)
        self.assertEqual([tp.fraction for tp in plan.tp_plan], [0.50, 0.30, 0.20])
        self.assertGreater(plan.thesis_stop_loss, 0.0)
        self.assertNotEqual(plan.stop_loss, plan.thesis_stop_loss)

    def test_absurd_high_timeframe_sl_distance_observes_instead_of_using_broker_sl(self):
        planner = FiboMtfTradePlanner()
        decision = planner.plan(
            FiboMtfPlannerInput(
                symbol="XAUUSD",
                direction="short",
                timeframe="D1",
                current_price=4660.10,
                nearest_level_price=4660.10,
                raw_stop_loss=4773.84,
                atr=5.0,
                ratio_zone="0.618",
                impulse_state="restart",
                correction_end_confirmed=True,
                confidence=70.0,
            )
        )

        self.assertEqual(decision.route, "observe_only")
        self.assertIsNone(decision.trade_plan)
        self.assertIn("sl_distance_over_cap", decision.reasons)


if __name__ == "__main__":
    unittest.main()

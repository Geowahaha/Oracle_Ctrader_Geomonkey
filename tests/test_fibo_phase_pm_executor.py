import unittest

from execution.ctrader_executor import CTraderExecutor


class _DummyExecutor:
    _apply_fibo_phase_extension = CTraderExecutor._apply_fibo_phase_extension
    _target_valid_for_position = staticmethod(CTraderExecutor._target_valid_for_position)
    _stop_valid_for_position = staticmethod(CTraderExecutor._stop_valid_for_position)
    _apply_fibo_winner_pm_profile = staticmethod(CTraderExecutor._apply_fibo_winner_pm_profile)
    _fibo_partial_close_volume = staticmethod(CTraderExecutor._fibo_partial_close_volume)


class TestFiboPhasePmExecutor(unittest.TestCase):
    def test_impulse_restart_extends_tp_more_than_base(self):
        dummy = _DummyExecutor()
        extension = {
            "active": True,
            "reason": "xau_profit_extension",
            "new_take_profit": 103.0,
            "new_stop_loss": 100.8,
        }
        out = dummy._apply_fibo_phase_extension(
            extension=extension,
            phase_pm={"phase": "impulse_restart", "live_extension_step_mult": 1.3, "live_extension_lock_r_mult": 1.2},
            direction="long",
            entry=100.0,
            stop_loss=99.0,
            current_tp=102.0,
        )
        self.assertTrue(out["new_take_profit"] > 103.0)
        self.assertTrue(out["new_stop_loss"] > 100.8)
        self.assertEqual(out["reason"], "xau_profit_extension")
        self.assertEqual(out["details"]["phase"], "impulse_restart")
    def test_correction_profile_requires_less_extension_than_impulse_restart(self):
        base_extension = {
            "active": True,
            "reason": "xau_profit_extension",
            "new_take_profit": 103.0,
            "new_stop_loss": 100.8,
        }
        dummy = _DummyExecutor()
        impulse = dummy._apply_fibo_phase_extension(
            extension=base_extension,
            phase_pm={"phase": "impulse_restart", "live_extension_step_mult": 1.3, "live_extension_lock_r_mult": 1.2},
            direction="long",
            entry=100.0,
            stop_loss=99.0,
            current_tp=102.0,
        )
        correction = dummy._apply_fibo_phase_extension(
            extension=base_extension,
            phase_pm={"phase": "correction", "live_extension_step_mult": 0.85, "live_extension_lock_r_mult": 1.05},
            direction="long",
            entry=100.0,
            stop_loss=99.0,
            current_tp=102.0,
        )
        self.assertTrue(impulse["new_take_profit"] > correction["new_take_profit"])

    def test_phase_pm_profile_marks_winner_profile(self):
        pm = CTraderExecutor._fibo_phase_pm_profile({"phase": "impulse_restart", "winner_eligible": True})
        self.assertTrue(pm["winner_profile"])
        self.assertGreater(pm["live_extension_step_mult"], 1.0)

    def test_winner_lane_profile_is_looser_than_base(self):
        dummy = _DummyExecutor()
        base = CTraderExecutor._fibo_phase_pm_profile({"phase": "impulse_restart", "winner_eligible": False})
        winner = dummy._apply_fibo_winner_pm_profile("fibo_xauusd:winner", base)
        self.assertTrue(winner["winner_profile"])
        self.assertGreater(winner["live_extension_step_mult"], base["live_extension_step_mult"])
        self.assertGreater(winner["time_lock_be_min_mult"], base["time_lock_be_min_mult"])

    def test_partial_close_volume_keeps_remainder_tradeable(self):
        dummy = _DummyExecutor()
        self.assertEqual(dummy._fibo_partial_close_volume(4000, 0.25, min_volume=1000, step=1000), 1000)
        self.assertEqual(dummy._fibo_partial_close_volume(1000, 0.50, min_volume=1000, step=1000), 0)


if __name__ == "__main__":
    unittest.main()

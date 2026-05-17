import unittest
from unittest.mock import patch

import scheduler as scheduler_module
from analysis.signals import TradeSignal


def _make_signal(confidence: float) -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction="long",
        confidence=confidence,
        entry=3300.0,
        stop_loss=3295.0,
        take_profit_1=3305.0,
        take_profit_2=3310.0,
        take_profit_3=3315.0,
        risk_reward=2.0,
        timeframe="5m",
        session="new_york",
        trend="bullish",
        rsi=55.0,
        atr=3.0,
        pattern="TEST",
        reasons=[],
        warnings=[],
        raw_scores={},
    )


class SchedulerConfidenceCapTests(unittest.TestCase):
    def test_normalize_confidence_value_caps_upper_tail(self):
        self.assertEqual(scheduler_module.DexterScheduler._normalize_confidence_value(110.4), 99.9)
        self.assertEqual(scheduler_module.DexterScheduler._normalize_confidence_value(-2), 0.0)

    def test_send_signal_with_trace_clamps_confidence_before_notify(self):
        dexter = scheduler_module.DexterScheduler()
        sig = _make_signal(108.6)
        with patch.object(scheduler_module.notifier, "send_signal", return_value=True) as send_signal:
            sent = dexter._send_signal_with_trace(sig, source="xauusd_scheduled")

        self.assertTrue(sent)
        self.assertEqual(sig.confidence, 99.9)
        self.assertEqual(send_signal.call_count, 1)
        self.assertTrue(bool(sig.raw_scores.get("confidence_clamped")))
        self.assertEqual(str(sig.raw_scores.get("confidence_clamp_stage")), "send_signal")

    def test_neural_soft_adjustment_still_clamps_when_already_adjusted(self):
        dexter = scheduler_module.DexterScheduler()
        sig = _make_signal(107.2)
        sig.raw_scores = {"neural_confidence_adjusted": True}

        out = dexter._apply_neural_soft_adjustment(sig, source="xauusd_scheduled")

        self.assertEqual(str(out.get("reason")), "already_adjusted")
        self.assertEqual(sig.confidence, 99.9)
        self.assertTrue(bool(sig.raw_scores.get("confidence_clamped")))
        self.assertEqual(str(sig.raw_scores.get("confidence_clamp_stage")), "neural_pre")


if __name__ == "__main__":
    unittest.main()

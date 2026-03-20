import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from learning.neural_gate_learning_loop import NeuralGateLearningLoop


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(ts: datetime, *, p: float, decision: str, canary: bool, status: str, conf: float = 75.0) -> dict:
    return {
        "created_at": _iso(ts),
        "neural_prob": p,
        "confidence": conf,
        "force_mode": True,
        "decision": decision,
        "canary_applied": canary,
        "mt5_status": status,
    }


class NeuralGateLearningLoopAutoStepTests(unittest.TestCase):
    def setUp(self):
        self.loop = NeuralGateLearningLoop()
        self.now = datetime.now(timezone.utc)

    def _rows_low_fill(self) -> list[dict]:
        rows: list[dict] = []
        # 18 eligible rows blocked by neural gate.
        for i in range(18):
            rows.append(
                _row(
                    self.now - timedelta(minutes=5 + i),
                    p=0.545,
                    decision="neural_block",
                    canary=False,
                    status="skipped",
                )
            )
        # 2 eligible rows passed via canary and filled.
        for i in range(2):
            rows.append(
                _row(
                    self.now - timedelta(minutes=2 + i),
                    p=0.55,
                    decision="allow",
                    canary=True,
                    status="filled",
                )
            )
        return rows

    def test_auto_step_down_on_low_fill_rate(self):
        rows = self._rows_low_fill()
        with patch.object(self.loop, "_previous_scope_config", return_value={}), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_ENABLED", True), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_LOOKBACK_HOURS", 6), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_ELIGIBLE", 8), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_TARGET_FILL_RATE", 0.20), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_BLOCK_RATE", 0.40), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_DOWN_STEP", 0.01), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_ALLOW_LOW", 0.53), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_VOLUME_CAP_STEP", 0.02), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_VOLUME_CAP", 0.16), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_COOLDOWN_MIN", 30), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_REQUIRE_ACTIVE_SCOPE", True), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_STOP_ON_TRIPWIRE", True):
            allow_after, cap_after, info = self.loop._auto_step_canary_scope(
                source="scalp_xauusd",
                symbol="XAUUSD",
                rows=rows,
                base_min=0.58,
                allow_low=0.54,
                low_floor=0.53,
                min_conf=70.0,
                require_force=True,
                volume_cap=0.20,
                scope_active=True,
                tripwire_triggered=False,
            )

        self.assertAlmostEqual(allow_after, 0.53, places=4)
        self.assertAlmostEqual(cap_after, 0.18, places=4)
        self.assertTrue(bool(info.get("applied", False)))
        self.assertEqual(str(info.get("action", "")), "step_down")
        self.assertEqual(str(info.get("reason", "")), "low_fill_rate")
        self.assertEqual((info.get("execution_window") or {}).get("eligible"), 20)
        self.assertAlmostEqual(float((info.get("execution_window") or {}).get("fill_rate") or 0.0), 0.10, places=4)

    def test_auto_step_hold_during_cooldown(self):
        rows = self._rows_low_fill()
        prev = {
            "allow_low": 0.53,
            "volume_multiplier_cap": 0.18,
            "auto_step": {"last_step_at": _iso(self.now - timedelta(minutes=10)), "steps_applied": 1},
        }
        with patch.object(self.loop, "_previous_scope_config", return_value=prev), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_ENABLED", True), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_LOOKBACK_HOURS", 6), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_ELIGIBLE", 8), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_TARGET_FILL_RATE", 0.20), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_BLOCK_RATE", 0.40), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_DOWN_STEP", 0.01), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_ALLOW_LOW", 0.53), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_VOLUME_CAP_STEP", 0.02), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_MIN_VOLUME_CAP", 0.16), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_COOLDOWN_MIN", 30), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_REQUIRE_ACTIVE_SCOPE", True), \
             patch("learning.neural_gate_learning_loop.config.NEURAL_GATE_CANARY_AUTO_STEP_STOP_ON_TRIPWIRE", True):
            allow_after, cap_after, info = self.loop._auto_step_canary_scope(
                source="scalp_xauusd",
                symbol="XAUUSD",
                rows=rows,
                base_min=0.58,
                allow_low=0.54,
                low_floor=0.53,
                min_conf=70.0,
                require_force=True,
                volume_cap=0.20,
                scope_active=True,
                tripwire_triggered=False,
            )

        self.assertAlmostEqual(allow_after, 0.53, places=4)
        self.assertAlmostEqual(cap_after, 0.18, places=4)
        self.assertFalse(bool(info.get("applied", False)))
        self.assertEqual(str(info.get("reason", "")), "cooldown")
        self.assertFalse(bool(info.get("cooldown_ok", True)))


if __name__ == "__main__":
    unittest.main()

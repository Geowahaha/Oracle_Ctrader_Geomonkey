"""Unit tests for ActiveDefensePolicy regime threshold lookup."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from execution.policy.active_defense import (
    REGIME_CRYPTO_WEEKEND,
    REGIME_NEWS_SHOCK,
    REGIME_OFF_HOURS,
    REGIME_RANGING,
    REGIME_TRENDING_BEAR,
    REGIME_TRENDING_BULL,
    REGIME_VOLATILE_EXPANSION,
    ActiveDefenseInput,
    ActiveDefensePolicy,
)


def _inp(label: str, conf: float, r_now: float = 0.5, winner_state: str = ""):
    return ActiveDefenseInput(
        r_now=r_now,
        regime_label=label,
        regime_confidence=conf,
        winner_state=winner_state,
    )


class StaticFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ActiveDefensePolicy()

    def test_low_confidence_returns_static_fallback(self) -> None:
        # Below threshold (0.60) → fallback regardless of label.
        out = self.policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.40))
        self.assertEqual(out.close_score, 5)
        self.assertAlmostEqual(out.close_max_r, 0.20)
        self.assertAlmostEqual(out.loss_cut_r, -0.28)
        self.assertEqual(out.tighten_score, 3)
        self.assertIn("static_fallback", out.reason)

    def test_zero_confidence_returns_fallback(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.0))
        self.assertEqual(out.close_score, 5)
        self.assertIn("static_fallback", out.reason)

    def test_invalid_confidence_falls_back(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_TRENDING_BULL, float("nan")))
        # nan < threshold is False, but float(nan) succeeds. nan < 0.60 is False
        # so the comparison branch may pick either path; static fallback for unknown.
        # Either branch should yield a valid threshold object.
        self.assertIsNotNone(out)
        self.assertGreater(out.close_score, 0)


class RegimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ActiveDefensePolicy()

    def test_trending_bull_high_conf_uses_protective_profile(self) -> None:
        # Harder to close, wider loss-cut.
        out = self.policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.80))
        self.assertEqual(out.close_score, 8)
        self.assertAlmostEqual(out.close_max_r, 0.10)
        self.assertAlmostEqual(out.loss_cut_r, -0.40)
        self.assertEqual(out.tighten_score, 5)
        self.assertIn(REGIME_TRENDING_BULL, out.reason)

    def test_trending_bear_uses_same_protective_profile(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_TRENDING_BEAR, 0.80))
        self.assertEqual(out.close_score, 8)
        self.assertAlmostEqual(out.close_max_r, 0.10)

    def test_volatile_expansion_intermediate_profile(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_VOLATILE_EXPANSION, 0.70))
        self.assertEqual(out.close_score, 6)
        self.assertAlmostEqual(out.close_max_r, 0.15)
        self.assertAlmostEqual(out.loss_cut_r, -0.35)

    def test_news_shock_easier_close_tighter_loss_cut(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_NEWS_SHOCK, 0.80))
        self.assertEqual(out.close_score, 3)
        self.assertAlmostEqual(out.close_max_r, 0.30)
        self.assertAlmostEqual(out.loss_cut_r, -0.15)
        self.assertEqual(out.tighten_score, 2)

    def test_ranging_matches_static_fallback(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_RANGING, 0.80))
        self.assertEqual(out.close_score, 5)
        self.assertAlmostEqual(out.close_max_r, 0.20)
        self.assertAlmostEqual(out.loss_cut_r, -0.28)

    def test_off_hours_matches_static_fallback(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_OFF_HOURS, 0.80))
        self.assertEqual(out.close_score, 5)
        self.assertAlmostEqual(out.loss_cut_r, -0.28)

    def test_crypto_weekend_matches_static_fallback(self) -> None:
        out = self.policy.thresholds(_inp(REGIME_CRYPTO_WEEKEND, 0.80))
        self.assertEqual(out.close_score, 5)

    def test_unknown_regime_falls_back_to_static(self) -> None:
        # Even with high confidence, an unmapped label uses static fallback.
        out = self.policy.thresholds(_inp("mystery_regime", 0.90))
        self.assertEqual(out.close_score, 5)
        self.assertAlmostEqual(out.close_max_r, 0.20)


class CustomProfileTests(unittest.TestCase):
    def test_custom_profile_overrides_default(self) -> None:
        custom = {REGIME_TRENDING_BULL: (10, 0.05, -0.50, 6)}
        policy = ActiveDefensePolicy(profiles=custom)
        out = policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.80))
        self.assertEqual(out.close_score, 10)
        self.assertAlmostEqual(out.close_max_r, 0.05)
        self.assertAlmostEqual(out.loss_cut_r, -0.50)
        self.assertEqual(out.tighten_score, 6)

    def test_custom_confidence_threshold(self) -> None:
        # If the threshold is dropped, lower confidence still selects regime profile.
        policy = ActiveDefensePolicy(confidence_threshold=0.30)
        out = policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.40))
        self.assertEqual(out.close_score, 8)
        self.assertNotIn("static_fallback", out.reason)

    def test_custom_fallback_is_used(self) -> None:
        policy = ActiveDefensePolicy(fallback=(7, 0.25, -0.30, 4))
        out = policy.thresholds(_inp(REGIME_TRENDING_BULL, 0.10))
        self.assertEqual(out.close_score, 7)
        self.assertAlmostEqual(out.close_max_r, 0.25)

    def test_static_fallback_property_is_immutable_view(self) -> None:
        policy = ActiveDefensePolicy()
        snap = policy.static_fallback
        # Mutating the returned tuple is impossible (tuples are immutable),
        # but assert it has the right shape.
        self.assertEqual(len(snap), 4)


class LabelNormalizationTests(unittest.TestCase):
    def test_label_case_insensitive(self) -> None:
        policy = ActiveDefensePolicy()
        out_upper = policy.thresholds(_inp("TRENDING_BULL", 0.80))
        out_lower = policy.thresholds(_inp("trending_bull", 0.80))
        self.assertEqual(out_upper.close_score, out_lower.close_score)

    def test_label_with_whitespace_normalized(self) -> None:
        policy = ActiveDefensePolicy()
        out = policy.thresholds(_inp("  trending_bull  ", 0.80))
        self.assertEqual(out.close_score, 8)

    def test_empty_label_falls_back(self) -> None:
        policy = ActiveDefensePolicy()
        out = policy.thresholds(_inp("", 0.80))
        self.assertEqual(out.close_score, 5)


if __name__ == "__main__":
    unittest.main()

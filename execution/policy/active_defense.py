"""
ActiveDefensePolicy — regime-aware thresholds for the existing XAU active
defense evaluator.

This policy does NOT replace the active defense logic in
`ctrader_executor._xau_active_defense_plan`. It returns the thresholds
(close_score, close_max_r, loss_cut_r, tighten_score) that the evaluator
should use. The caller picks them up from the policy envelope and either
passes them to the evaluator or post-filters the evaluator's decision
against them.

Behavior
--------
- High-confidence TRENDING regimes  → harder to close, wider loss-cut.
  (protect winners from adverse micro-moves)
- VOLATILE_EXPANSION               → slightly harder to close.
- RANGING / OFF_HOURS / default     → current system thresholds (unchanged).
- NEWS_SHOCK                        → easier to close, tighter loss-cut
  (exit fast on macro events).
- Low regime_confidence             → fall back to static defaults.

Profiles are tuple-encoded to avoid accidental multi-line continuation
issues; one entry per line, single-line tuples only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


REGIME_TRENDING_BULL = "trending_bull"
REGIME_TRENDING_BEAR = "trending_bear"
REGIME_VOLATILE_EXPANSION = "volatile_expansion"
REGIME_RANGING = "ranging"
REGIME_NEWS_SHOCK = "news_shock"
REGIME_OFF_HOURS = "off_hours"
REGIME_CRYPTO_WEEKEND = "crypto_weekend"


@dataclass(frozen=True)
class ActiveDefenseInput:
    r_now: float
    regime_label: str
    regime_confidence: float
    winner_state: str = ""


@dataclass(frozen=True)
class ActiveDefenseThresholds:
    close_score: int
    close_max_r: float
    loss_cut_r: float
    tighten_score: int
    reason: str


# (close_score, close_max_r, loss_cut_r, tighten_score)
_STATIC_FALLBACK = (5, 0.20, -0.28, 3)

_REGIME_PROFILES: Mapping[str, tuple] = {
    REGIME_TRENDING_BULL: (8, 0.10, -0.40, 5),
    REGIME_TRENDING_BEAR: (8, 0.10, -0.40, 5),
    REGIME_VOLATILE_EXPANSION: (6, 0.15, -0.35, 4),
    REGIME_RANGING: _STATIC_FALLBACK,
    REGIME_OFF_HOURS: _STATIC_FALLBACK,
    REGIME_CRYPTO_WEEKEND: _STATIC_FALLBACK,
    REGIME_NEWS_SHOCK: (3, 0.30, -0.15, 2),
}

_CONFIDENCE_THRESHOLD = 0.60


class ActiveDefensePolicy:
    """Stateless. Threshold lookup only."""

    def __init__(
        self,
        profiles: Mapping[str, tuple] | None = None,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
        fallback: tuple = _STATIC_FALLBACK,
    ) -> None:
        self._profiles = dict(profiles) if profiles else dict(_REGIME_PROFILES)
        self._confidence_threshold = float(confidence_threshold)
        self._fallback = tuple(fallback)

    @property
    def static_fallback(self) -> tuple:
        return tuple(self._fallback)

    def thresholds(self, inp: ActiveDefenseInput) -> ActiveDefenseThresholds:
        try:
            conf = float(inp.regime_confidence)
        except (TypeError, ValueError):
            conf = 0.0

        if conf < self._confidence_threshold:
            score, cmr, lcr, tighten = self._fallback
            return ActiveDefenseThresholds(
                close_score=int(score),
                close_max_r=float(cmr),
                loss_cut_r=float(lcr),
                tighten_score=int(tighten),
                reason=f"static_fallback:conf={conf:.2f}<{self._confidence_threshold:.2f}",
            )

        label = str(inp.regime_label or "").strip().lower()
        profile = self._profiles.get(label, self._fallback)
        score, cmr, lcr, tighten = profile
        return ActiveDefenseThresholds(
            close_score=int(score),
            close_max_r=float(cmr),
            loss_cut_r=float(lcr),
            tighten_score=int(tighten),
            reason=f"regime:{label or 'unknown'}",
        )

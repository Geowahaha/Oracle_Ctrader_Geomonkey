"""Confluence Booster — turns family vote counts into a risk multiplier.

Rules (defaults, all configurable):
- 3+ families agree, none disagree → multiplier = 2.0 (system consensus, fire big)
- 3+ families agree, 1+ disagree   → multiplier = 1.30 (consensus with friction)
- 2 families agree, none disagree  → multiplier = 1.15 (modest tailwind)
- 1 family alone                   → multiplier = 1.0  (neutral)
- families split evenly            → multiplier = 0.7  (chop — probe only)
- the opposite side wins on count  → multiplier = 0.5  (fight the tape minimally)

The booster ONLY scales risk; it never blocks. Callers compose this with
their own gates and with the EquityGovernor multiplier. The final product is
clamped to a configurable hard ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from analysis.confluence.ledger import ConfluenceLedger, VoteCount


@dataclass(frozen=True)
class ConfluenceMultiplier:
    """Output of the booster — multiplier plus the reasoning."""

    multiplier: float
    state: str
    agree_count: int
    disagree_count: int
    families_agree: tuple[str, ...]
    families_disagree: tuple[str, ...]
    reasons: tuple[str, ...]


class ConfluenceBooster:
    """Turns a VoteCount into a ConfluenceMultiplier."""

    def __init__(
        self,
        *,
        ledger: ConfluenceLedger,
        multiplier_high: float = 2.0,
        multiplier_mid: float = 1.30,
        multiplier_low: float = 1.15,
        multiplier_split: float = 0.70,
        multiplier_fight: float = 0.50,
        min_agree_high: int = 3,
        min_agree_low: int = 2,
        max_multiplier: float = 2.0,
    ) -> None:
        self.ledger = ledger
        self.multiplier_high = float(multiplier_high)
        self.multiplier_mid = float(multiplier_mid)
        self.multiplier_low = float(multiplier_low)
        self.multiplier_split = float(multiplier_split)
        self.multiplier_fight = float(multiplier_fight)
        self.min_agree_high = int(min_agree_high)
        self.min_agree_low = int(min_agree_low)
        self.max_multiplier = float(max_multiplier)

    def multiplier_for(self, *, symbol: str, side: str) -> ConfluenceMultiplier:
        side_n = str(side or "").strip().lower()
        if side_n in {"buy", "long"}:
            side_n = "long"
        elif side_n in {"sell", "short"}:
            side_n = "short"
        votes = self.ledger.count_for(symbol=symbol)
        agree = votes.long if side_n == "long" else votes.short
        disagree = votes.short if side_n == "long" else votes.long
        families_agree = votes.families_long if side_n == "long" else votes.families_short
        families_disagree = votes.families_short if side_n == "long" else votes.families_long
        multiplier, state, reasons = self._decide(agree=agree, disagree=disagree)
        multiplier = min(multiplier, self.max_multiplier)
        return ConfluenceMultiplier(
            multiplier=round(multiplier, 4),
            state=state,
            agree_count=int(agree),
            disagree_count=int(disagree),
            families_agree=tuple(families_agree),
            families_disagree=tuple(families_disagree),
            reasons=tuple(reasons),
        )

    def _decide(self, *, agree: int, disagree: int) -> tuple[float, str, list[str]]:
        # Opposite side dominates → fight-the-tape minimum.
        if disagree >= max(self.min_agree_low, 2) and disagree > agree:
            return self.multiplier_fight, "opposite_consensus", [
                f"disagree>{agree} ({disagree}>{agree}) — probe-only",
            ]
        # Split: equal agree/disagree at >=2 each → chop.
        if agree >= self.min_agree_low and disagree >= self.min_agree_low and agree == disagree:
            return self.multiplier_split, "split", [
                f"split:{agree}={disagree} — defensive sizing",
            ]
        # Strong consensus with no friction.
        if agree >= self.min_agree_high and disagree == 0:
            return self.multiplier_high, "strong_consensus", [
                f"agree>=high:{agree}>={self.min_agree_high}",
                "no_disagreement",
            ]
        # Consensus with friction.
        if agree >= self.min_agree_high and disagree >= 1:
            return self.multiplier_mid, "consensus_with_friction", [
                f"agree>=high:{agree}", f"friction:{disagree}",
            ]
        # Modest tailwind.
        if agree >= self.min_agree_low and disagree == 0:
            return self.multiplier_low, "tailwind", [
                f"agree>=low:{agree}",
            ]
        # Solo or no signal — neutral.
        return 1.0, "neutral", [f"agree={agree},disagree={disagree}"]


__all__ = ["ConfluenceBooster", "ConfluenceMultiplier"]

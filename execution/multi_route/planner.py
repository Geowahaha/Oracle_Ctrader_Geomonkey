"""Multi-Route planner — splits one high-conviction signal into up to three legs.

Three legs (default shares sum to 1.0):
- probe   (0.20 of basket risk) — market entry at signal time
- retest  (0.40 of basket risk) — limit entry at the projected retest zone
- breakout(0.40 of basket risk) — stop entry above/below structure

Eligibility gates (configurable):
- signal.confidence >= min_confidence
- signal.route in {"breakdown_continuation", "retest_entry"}
- free margin >= margin_safety_multiplier * baseline_leg_margin

A planned basket exposes `eligible: bool` + `legs: tuple[MultiRouteLeg]`. If
ineligible, the caller falls back to the existing single-leg path unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


ROUTE_BREAKDOWN = "breakdown_continuation"
ROUTE_RETEST = "retest_entry"


@dataclass(frozen=True)
class MultiRouteSignal:
    """Input — the candidate signal under consideration."""

    signal_run_id: str
    symbol: str
    direction: str             # "long" or "short"
    confidence: float          # 0..100
    route: str                 # from analysis.route_classifier
    baseline_risk_usd: float   # what the single-leg path would have risked
    market_entry_price: float
    retest_entry_price: float
    breakout_entry_price: float
    stop_loss_price: float
    take_profit_price: float
    free_margin_usd: float
    baseline_leg_margin_usd: float


@dataclass(frozen=True)
class MultiRouteLeg:
    """One concrete order in the basket."""

    leg_id: str                # e.g. "probe", "retest", "breakout"
    entry_type: str            # "market" | "limit" | "stop"
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_usd: float
    reason: str


@dataclass(frozen=True)
class MultiRouteBasket:
    """Output — a basket of legs, plus eligibility verdict."""

    signal_run_id: str
    symbol: str
    direction: str
    eligible: bool
    legs: Tuple[MultiRouteLeg, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    total_risk_usd: float = 0.0


@dataclass
class MultiRoutePlannerConfig:
    enabled: bool = False
    min_confidence: float = 80.0
    allowed_routes: Tuple[str, ...] = (ROUTE_BREAKDOWN, ROUTE_RETEST)
    probe_share: float = 0.20
    retest_share: float = 0.40
    breakout_share: float = 0.40
    margin_safety_multiplier: float = 3.0
    min_total_risk_usd: float = 0.50

    def shares(self) -> dict[str, float]:
        return {"probe": self.probe_share, "retest": self.retest_share, "breakout": self.breakout_share}


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


class MultiRoutePlanner:
    """Turns a single high-confidence signal into a 1- to 3-leg basket plan."""

    def __init__(self, *, config: Optional[MultiRoutePlannerConfig] = None) -> None:
        self.config = config or MultiRoutePlannerConfig()

    def plan(self, *, signal: MultiRouteSignal) -> MultiRouteBasket:
        cfg = self.config
        reasons: list[str] = []
        direction = _norm_dir(signal.direction)

        if not cfg.enabled:
            return MultiRouteBasket(signal.signal_run_id, signal.symbol, direction, False,
                                     reasons=("multi_route_disabled",))

        if signal.confidence < cfg.min_confidence:
            return MultiRouteBasket(signal.signal_run_id, signal.symbol, direction, False,
                                     reasons=(f"confidence_below_min:{signal.confidence:.1f}<{cfg.min_confidence:.1f}",))

        if signal.route not in set(cfg.allowed_routes):
            return MultiRouteBasket(signal.signal_run_id, signal.symbol, direction, False,
                                     reasons=(f"route_not_allowed:{signal.route}",))

        baseline = float(signal.baseline_risk_usd)
        if baseline < cfg.min_total_risk_usd:
            return MultiRouteBasket(signal.signal_run_id, signal.symbol, direction, False,
                                     reasons=(f"baseline_risk_too_small:{baseline:.2f}<{cfg.min_total_risk_usd:.2f}",))

        # Margin gate: caller must have at least margin_safety_multiplier x single-leg margin.
        if signal.free_margin_usd < cfg.margin_safety_multiplier * signal.baseline_leg_margin_usd:
            return MultiRouteBasket(signal.signal_run_id, signal.symbol, direction, False,
                                     reasons=("free_margin_insufficient",))

        # Build legs.
        shares = cfg.shares()
        leg_specs = [
            ("probe", "market", signal.market_entry_price, shares["probe"], "probe_market"),
            ("retest", "limit", signal.retest_entry_price, shares["retest"], "retest_limit"),
            ("breakout", "stop", signal.breakout_entry_price, shares["breakout"], "breakout_stop"),
        ]
        legs: list[MultiRouteLeg] = []
        for name, entry_type, price, share, reason in leg_specs:
            risk = round(baseline * float(share), 4)
            if risk <= 0:
                continue
            legs.append(
                MultiRouteLeg(
                    leg_id=f"{signal.signal_run_id}:{name}",
                    entry_type=entry_type,
                    entry_price=float(price),
                    stop_loss=float(signal.stop_loss_price),
                    take_profit=float(signal.take_profit_price),
                    risk_usd=risk,
                    reason=reason,
                )
            )
        total_risk = round(sum(leg.risk_usd for leg in legs), 4)
        return MultiRouteBasket(
            signal_run_id=signal.signal_run_id,
            symbol=signal.symbol,
            direction=direction,
            eligible=True,
            legs=tuple(legs),
            reasons=(f"confidence:{signal.confidence:.1f}", f"route:{signal.route}", f"total_risk:{total_risk:.2f}"),
            total_risk_usd=total_risk,
        )


__all__ = [
    "MultiRoutePlanner",
    "MultiRoutePlannerConfig",
    "MultiRouteSignal",
    "MultiRouteLeg",
    "MultiRouteBasket",
    "ROUTE_BREAKDOWN",
    "ROUTE_RETEST",
]

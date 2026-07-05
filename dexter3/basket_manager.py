"""Dexter3 basket manager — pure deterministic position-set state machine.

States: FLAT -> OPEN -> REPAIR -> RESOLVING -> (back to FLAT).

This module holds NO broker/MCP calls and NO randomness — every method is a
pure function of (current state, inputs) -> (new state, actions). The
``shadow_runner`` (Phase 1) feeds it simulated fills; a later phase wires
the returned actions to real orders. Actions are descriptive tokens the
caller executes: ``add_repair_leg``, ``hedge_lock``, ``close_all_in_profit``,
``close_all_cap_stop``, ``none``.

HARD CAPS (docs/DEXTER3_M5_HUNTER_BLUEPRINT.md, "Basket repair — HARD CAPS")
are enforced in exactly one choke-point method, ``_enforce_caps``, which
every path that could grow the basket or extend its lifetime must pass
through before actually adding a leg or letting time continue. No other
method may bypass it. This is what makes the caps unbreachable under any
call sequence, including adversarial ones (see
tests/test_dexter3_basket.py's property-style loop test).

Repair requires STRUCTURE evidence (``level_lost`` AND ``m5_close_beyond``),
never price-distance alone — this is what keeps basket repair from
degenerating into martingale (see blueprint "Honest engineering
translation").
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

FLAT = "FLAT"
OPEN = "OPEN"
REPAIR = "REPAIR"
RESOLVING = "RESOLVING"

ACTION_NONE = "none"
ACTION_ADD_REPAIR_LEG = "add_repair_leg"
ACTION_HEDGE_LOCK = "hedge_lock"
ACTION_CLOSE_ALL_IN_PROFIT = "close_all_in_profit"
ACTION_CLOSE_ALL_CAP_STOP = "close_all_cap_stop"


@dataclass(frozen=True)
class BasketConfig:
    max_legs: int = 3
    max_basket_risk_mult: float = 3.0
    time_stop_min: int = 180
    daily_loss_baskets: int = 2
    resolve_target_r: float = 0.2


@dataclass
class Leg:
    side: str
    entry: float
    sl: float
    risk_usd: float
    opened_at_min: float  # minutes since basket/session epoch, caller-supplied clock
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BasketState:
    state: str = FLAT
    legs: list[Leg] = field(default_factory=list)
    opened_at_min: float | None = None
    base_risk_usd: float = 0.0
    daily_resolved_loss_baskets: int = 0
    basket_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "legs": [leg.to_dict() for leg in self.legs],
            "opened_at_min": self.opened_at_min,
            "base_risk_usd": self.base_risk_usd,
            "daily_resolved_loss_baskets": self.daily_resolved_loss_baskets,
            "basket_id": self.basket_id,
        }


@dataclass
class Decision:
    action: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "detail": self.detail}


class BasketManager:
    """Deterministic FLAT/OPEN/REPAIR/RESOLVING state machine with hard caps."""

    def __init__(self, config: BasketConfig | None = None) -> None:
        self.config = config or BasketConfig()
        self.state = BasketState()
        self._basket_seq = 0

    # -- introspection --------------------------------------------------

    def current_basket_risk_usd(self) -> float:
        return sum(leg.risk_usd for leg in self.state.legs)

    def max_basket_risk_usd(self) -> float:
        return self.state.base_risk_usd * self.config.max_basket_risk_mult

    def basket_age_min(self, now_min: float) -> float:
        if self.state.opened_at_min is None:
            return 0.0
        return max(0.0, now_min - self.state.opened_at_min)

    def daily_loss_cap_reached(self) -> bool:
        return self.state.daily_resolved_loss_baskets >= self.config.daily_loss_baskets

    # -- the single choke point every cap-relevant path must pass through --

    def _enforce_caps(
        self,
        *,
        proposed_action: str,
        now_min: float,
        would_add_leg: Leg | None = None,
    ) -> Decision | None:
        """Return a cap-driven override Decision, or None if the caller may proceed.

        This is the ONLY place hard caps are checked. Every method that
        could grow the basket (add a leg) or that evaluates whether the
        basket may keep living (time/daily-loss) MUST call this first and
        obey a non-None result unconditionally — including refusing
        ``would_add_leg`` even if structure evidence looked valid.
        """
        # Daily loss cap: once reached, no NEW basket may open, and any open
        # basket must resolve at best available (never grow further).
        if self.daily_loss_cap_reached():
            if proposed_action == "open" or would_add_leg is not None:
                return Decision(
                    action=ACTION_CLOSE_ALL_CAP_STOP if self.state.legs else ACTION_NONE,
                    reason="daily_loss_baskets cap reached — no new baskets or legs today",
                    detail={
                        "daily_resolved_loss_baskets": self.state.daily_resolved_loss_baskets,
                        "cap": self.config.daily_loss_baskets,
                    },
                )

        if not self.state.legs:
            return None  # nothing else to cap-check before a basket exists

        # Time stop — force resolve regardless of any other consideration.
        age = self.basket_age_min(now_min)
        if age >= self.config.time_stop_min:
            return Decision(
                action=ACTION_CLOSE_ALL_CAP_STOP,
                reason=f"time_stop_min reached: age={age:.1f}min >= cap={self.config.time_stop_min}min",
                detail={"age_min": age, "cap_min": self.config.time_stop_min},
            )

        # Max legs — never allow another leg past the cap.
        if would_add_leg is not None and len(self.state.legs) >= self.config.max_legs:
            return Decision(
                action=ACTION_NONE,
                reason=f"max_legs cap reached: legs={len(self.state.legs)} >= cap={self.config.max_legs}",
                detail={"legs": len(self.state.legs), "cap": self.config.max_legs},
            )

        # Max basket risk — never allow a leg whose worst-case risk would
        # breach the cap, even if leg-count still has room.
        if would_add_leg is not None:
            projected_risk = self.current_basket_risk_usd() + would_add_leg.risk_usd
            max_risk = self.max_basket_risk_usd()
            if max_risk > 0 and projected_risk > max_risk:
                return Decision(
                    action=ACTION_NONE,
                    reason=(
                        f"max_basket_risk_mult cap reached: projected={projected_risk:.2f} "
                        f"> cap={max_risk:.2f} ({self.config.max_basket_risk_mult}x base)"
                    ),
                    detail={"projected_risk_usd": projected_risk, "cap_usd": max_risk},
                )

        return None

    # -- lifecycle --------------------------------------------------------

    def on_entry(self, leg: Leg) -> Decision:
        """Register the first leg of a new basket, or reject if capped."""
        if self.state.state != FLAT:
            return Decision(
                action=ACTION_NONE,
                reason=f"on_entry called while basket state={self.state.state} (expected FLAT) — ignored",
                detail={"state": self.state.state},
            )

        cap_decision = self._enforce_caps(proposed_action="open", now_min=leg.opened_at_min, would_add_leg=None)
        if cap_decision is not None:
            return cap_decision

        self._basket_seq += 1
        self.state = BasketState(
            state=OPEN,
            legs=[leg],
            opened_at_min=leg.opened_at_min,
            base_risk_usd=leg.risk_usd,
            daily_resolved_loss_baskets=self.state.daily_resolved_loss_baskets,
            basket_id=self._basket_seq,
        )
        return Decision(
            action=ACTION_NONE,
            reason="basket opened",
            detail={"basket_id": self.state.basket_id, "leg": leg.to_dict()},
        )

    def on_m5_close(
        self,
        bars: list[dict[str, Any]],
        features: dict[str, Any],
        positions_pnl: dict[str, Any],
    ) -> Decision:
        """Evaluate the basket on a new M5 close.

        ``positions_pnl`` must supply at least:
          - ``aggregate_r``: current basket PnL expressed in multiples of
            ``base_risk_usd`` (float; negative = loss).
          - ``now_min``: caller's clock, minutes since epoch (float).
          - ``structure_evidence``: optional dict with ``level_lost`` (bool)
            and ``m5_close_beyond`` (bool) — REQUIRED (both True) to permit
            a repair leg; price distance alone never qualifies.
        """
        now_min = float(positions_pnl.get("now_min", 0.0))

        if self.state.state == FLAT:
            return Decision(action=ACTION_NONE, reason="basket is FLAT — nothing to manage", detail={})

        cap_decision = self._enforce_caps(proposed_action="manage", now_min=now_min, would_add_leg=None)
        if cap_decision is not None:
            if cap_decision.action == ACTION_CLOSE_ALL_CAP_STOP:
                self._resolve(is_loss=True, now_min=now_min, reason=cap_decision.reason)
            return cap_decision

        aggregate_r = float(positions_pnl.get("aggregate_r", 0.0))

        # Resolve-in-profit target takes priority over repair evaluation.
        if aggregate_r >= self.config.resolve_target_r:
            decision = Decision(
                action=ACTION_CLOSE_ALL_IN_PROFIT,
                reason=f"aggregate_r={aggregate_r:.3f} >= resolve_target_r={self.config.resolve_target_r}",
                detail={"aggregate_r": aggregate_r},
            )
            self._resolve(is_loss=False, now_min=now_min, reason=decision.reason)
            return decision

        structure_evidence = positions_pnl.get("structure_evidence") or {}
        level_lost = bool(structure_evidence.get("level_lost"))
        m5_close_beyond = bool(structure_evidence.get("m5_close_beyond"))
        has_repair_evidence = level_lost and m5_close_beyond

        if aggregate_r < 0 and has_repair_evidence:
            proposed_leg = positions_pnl.get("proposed_repair_leg")
            if proposed_leg is None:
                return Decision(
                    action=ACTION_NONE,
                    reason="repair evidence present but no proposed_repair_leg supplied by caller",
                    detail={"level_lost": level_lost, "m5_close_beyond": m5_close_beyond},
                )
            leg = proposed_leg if isinstance(proposed_leg, Leg) else Leg(**proposed_leg)
            cap_decision = self._enforce_caps(
                proposed_action="repair", now_min=now_min, would_add_leg=leg
            )
            if cap_decision is not None:
                if cap_decision.action == ACTION_CLOSE_ALL_CAP_STOP:
                    self._resolve(is_loss=True, now_min=now_min, reason=cap_decision.reason)
                return cap_decision

            self.state.legs.append(leg)
            self.state.state = REPAIR
            return Decision(
                action=ACTION_ADD_REPAIR_LEG,
                reason=(
                    f"structure evidence confirmed (level_lost=True, m5_close_beyond=True), "
                    f"aggregate_r={aggregate_r:.3f} — adding repair leg"
                ),
                detail={"leg": leg.to_dict(), "aggregate_r": aggregate_r, "legs_now": len(self.state.legs)},
            )

        if aggregate_r < 0 and (level_lost or m5_close_beyond) and not has_repair_evidence:
            # Partial evidence only — explicitly refuse to repair on
            # price-distance-alone or a single-flag signal.
            return Decision(
                action=ACTION_NONE,
                reason=(
                    "repair refused: partial structure evidence only "
                    f"(level_lost={level_lost}, m5_close_beyond={m5_close_beyond}) — "
                    "both required, price distance alone is never sufficient"
                ),
                detail={"level_lost": level_lost, "m5_close_beyond": m5_close_beyond},
            )

        return Decision(
            action=ACTION_NONE,
            reason=f"holding: aggregate_r={aggregate_r:.3f}, no repair evidence, target not reached",
            detail={"aggregate_r": aggregate_r, "state": self.state.state},
        )

    def _resolve(self, *, is_loss: bool, now_min: float, reason: str) -> None:
        if is_loss:
            self.state.daily_resolved_loss_baskets += 1
        self.state = BasketState(
            state=FLAT,
            legs=[],
            opened_at_min=None,
            base_risk_usd=0.0,
            daily_resolved_loss_baskets=self.state.daily_resolved_loss_baskets,
            basket_id=self.state.basket_id,
        )

    def reset_daily_counters(self) -> None:
        """Caller-invoked at session/day boundary — resets the daily loss-basket cap."""
        self.state.daily_resolved_loss_baskets = 0

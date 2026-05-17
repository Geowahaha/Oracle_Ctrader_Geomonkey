"""Signal-run basket risk admission helpers.

Pure in-memory registry used by tests and future scheduler integration. It makes
main/canary legs alternatives inside one thesis risk budget instead of additive
full-risk duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    signal_run_id: str
    requested_risk_usd: float
    open_risk_usd: float
    basket_risk_budget: float
    leg_role: str
    reason: str


@dataclass(frozen=True)
class BasketLeg:
    risk_usd: float
    leg_role: str


class BasketRegistry:
    def __init__(self, default_budget_usd: float = 100.0):
        self.default_budget_usd = float(default_budget_usd)
        self._open: Dict[str, List[BasketLeg]] = {}

    def open_risk_for(self, signal_run_id: str) -> float:
        return sum(leg.risk_usd for leg in self._open.get(signal_run_id or "", ()))

    def record_open_leg(self, signal_run_id: str, risk_usd: float, leg_role: str) -> None:
        key = signal_run_id or ""
        self._open.setdefault(key, []).append(BasketLeg(float(risk_usd), str(leg_role or "unknown")))

    def can_admit(
        self,
        signal_run_id: str,
        requested_risk_usd: float,
        leg_role: str,
        basket_risk_budget: float | None = None,
        no_add: bool = True,
    ) -> AdmissionDecision:
        key = signal_run_id or ""
        requested = float(requested_risk_usd)
        budget = float(self.default_budget_usd if basket_risk_budget is None else basket_risk_budget)
        open_risk = self.open_risk_for(key)
        existing_legs = self._open.get(key, [])
        role = str(leg_role or "unknown")

        if key and existing_legs and no_add:
            return AdmissionDecision(False, key, requested, open_risk, budget, role, "duplicate_signal_run_no_add")

        if open_risk + requested > budget:
            return AdmissionDecision(False, key, requested, open_risk, budget, role, "basket_budget_exceeded")

        return AdmissionDecision(True, key, requested, open_risk, budget, role, "admitted")

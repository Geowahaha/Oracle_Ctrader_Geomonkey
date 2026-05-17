"""Kronos-Primary decision logic.

Decision tree:

    if not forecast.is_fresh(now, max_age_sec):
        return technical_primary
    if forecast.uncertainty >= max_uncertainty:
        return technical_primary
    if forecast.direction != bias:
        return technical_primary   # disagreement → stay conservative
    if router.daily_share_exceeded():
        return technical_primary   # cap exceeded — fall back today
    if router.is_locked_out():
        return technical_primary   # 3 fails in 24h → cooled off
    return kronos_primary          # Kronos drives the plan

When Kronos is primary:
- Entry timing = forecast.first_touch_estimate
- Stop = forecast.invalidation_price
- Take profit = forecast.target_band_high
- Risk multiplier = `kronos_risk_multiplier` (default 1.0 — same as technical
  primary; the edge is in the *timing* and *placement*, not in oversizing).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class KronosForecast:
    """Compact representation of a Kronos forecast for routing decisions."""

    symbol: str
    direction: str                       # "long" or "short"
    uncertainty: float                   # 0..1
    first_touch_price: float             # projected entry zone (limit/stop)
    invalidation_price: float            # projected stop loss
    target_band_high: float              # projected take profit (further-out band)
    created_utc: datetime

    def is_fresh(self, *, now: datetime, max_age_sec: float) -> bool:
        return (now - self.created_utc).total_seconds() <= float(max_age_sec)


@dataclass(frozen=True)
class RoutePlan:
    """A concrete entry plan output by the router."""

    entry_price: float
    stop_loss: float
    take_profit: float
    entry_type: str                      # "limit" / "stop" / "market"
    risk_multiplier: float
    reason: str


@dataclass(frozen=True)
class RouteDecision:
    """The router's verdict: primary path and (if Kronos) the plan."""

    primary: str                         # "kronos" or "technical"
    plan: Optional[RoutePlan]
    reasons: tuple[str, ...]
    forecast_uncertainty: float
    bias_aligned: bool


@dataclass
class KronosRouterConfig:
    enabled: bool = False
    max_uncertainty: float = 0.30
    max_age_sec: float = 120.0
    daily_share_cap: float = 0.30
    daily_total_entries_estimate: int = 30
    failure_lockout_threshold: int = 3
    failure_lockout_window_hours: float = 24.0
    kronos_risk_multiplier: float = 1.0
    entry_type_buy: str = "stop"
    entry_type_sell: str = "stop"


class KronosRouter:
    """Decides when Kronos takes the wheel and emits a plan."""

    def __init__(
        self,
        *,
        config: Optional[KronosRouterConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or KronosRouterConfig()
        self._clock = clock or _utc_now
        self._daily_kronos_entries = 0
        self._daily_total_entries = 0
        self._daily_anchor_utc: Optional[datetime] = None
        self._failures: Deque[datetime] = deque()
        self._lock = threading.Lock()

    # ----- decision -----------------------------------------------------
    def decide(
        self,
        *,
        forecast: Optional[KronosForecast],
        bias: str,
        technical_plan: Optional[RoutePlan] = None,
    ) -> RouteDecision:
        bias_n = _norm_dir(bias)
        reasons: list[str] = []
        if not self.config.enabled:
            reasons.append("kronos_router_disabled")
            return RouteDecision(primary="technical", plan=technical_plan, reasons=tuple(reasons),
                                 forecast_uncertainty=getattr(forecast, "uncertainty", 1.0) or 1.0,
                                 bias_aligned=False)
        if forecast is None:
            reasons.append("no_forecast")
            return RouteDecision(primary="technical", plan=technical_plan, reasons=tuple(reasons),
                                 forecast_uncertainty=1.0, bias_aligned=False)

        now = self._clock()
        self._roll_day_if_needed(now)

        if not forecast.is_fresh(now=now, max_age_sec=self.config.max_age_sec):
            reasons.append(f"forecast_stale_age_sec>={self.config.max_age_sec:.0f}")
            return RouteDecision("technical", technical_plan, tuple(reasons), forecast.uncertainty, False)

        if forecast.uncertainty >= self.config.max_uncertainty:
            reasons.append(f"uncertainty>={self.config.max_uncertainty:.2f}")
            return RouteDecision("technical", technical_plan, tuple(reasons), forecast.uncertainty, False)

        if _norm_dir(forecast.direction) != bias_n:
            reasons.append("forecast_disagrees_with_bias")
            return RouteDecision("technical", technical_plan, tuple(reasons), forecast.uncertainty, False)

        if self._daily_share_exceeded():
            reasons.append("daily_share_cap_exceeded")
            return RouteDecision("technical", technical_plan, tuple(reasons), forecast.uncertainty, True)

        if self._is_locked_out(now=now):
            reasons.append("kronos_locked_out_after_failures")
            return RouteDecision("technical", technical_plan, tuple(reasons), forecast.uncertainty, True)

        plan = self._build_plan(forecast=forecast, bias=bias_n)
        reasons.append(f"kronos_primary:uncertainty={forecast.uncertainty:.2f}")
        return RouteDecision("kronos", plan, tuple(reasons), forecast.uncertainty, True)

    # ----- accounting ---------------------------------------------------
    def record_dispatch(self, *, primary: str) -> None:
        """Caller invokes this for every dispatched entry, so daily-share
        accounting reflects reality regardless of which primary won.
        """
        with self._lock:
            self._roll_day_if_needed(self._clock())
            self._daily_total_entries += 1
            if primary == "kronos":
                self._daily_kronos_entries += 1

    def record_outcome(self, *, primary: str, pnl_usd: float) -> None:
        """When a Kronos-primary trade closes negative, count it against the
        failure window. Three losses in 24h triggers a lock-out.
        """
        if primary != "kronos" or pnl_usd >= 0:
            return
        with self._lock:
            now = self._clock()
            self._evict_failures(now)
            self._failures.append(now)

    # ----- internals ----------------------------------------------------
    def _build_plan(self, *, forecast: KronosForecast, bias: str) -> RoutePlan:
        if bias == "long":
            entry_type = self.config.entry_type_buy
        else:
            entry_type = self.config.entry_type_sell
        return RoutePlan(
            entry_price=float(forecast.first_touch_price),
            stop_loss=float(forecast.invalidation_price),
            take_profit=float(forecast.target_band_high),
            entry_type=entry_type,
            risk_multiplier=float(self.config.kronos_risk_multiplier),
            reason=f"kronos_primary:u={forecast.uncertainty:.2f}",
        )

    def _roll_day_if_needed(self, now: datetime) -> None:
        anchor = self._daily_anchor_utc
        if anchor is None or now.date() != anchor.date():
            self._daily_anchor_utc = now
            self._daily_kronos_entries = 0
            self._daily_total_entries = 0

    def _daily_share_exceeded(self) -> bool:
        denom = max(self._daily_total_entries, self.config.daily_total_entries_estimate)
        if denom <= 0:
            return False
        share = self._daily_kronos_entries / denom
        return share >= self.config.daily_share_cap

    def _is_locked_out(self, *, now: datetime) -> bool:
        self._evict_failures(now)
        return len(self._failures) >= self.config.failure_lockout_threshold

    def _evict_failures(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self.config.failure_lockout_window_hours)
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()


__all__ = [
    "KronosForecast",
    "KronosRouter",
    "KronosRouterConfig",
    "RouteDecision",
    "RoutePlan",
]

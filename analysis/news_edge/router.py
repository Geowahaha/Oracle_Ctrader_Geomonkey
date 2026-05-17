"""News-as-Edge router.

Decision tree:

  if now in chaos_window (t-30s..t+30s):     return "kill"   (broker-level chaos)
  if not in opportunity_window (t-15m..t-3m): return "allow" (outside guard span)
  if now in legacy_guard_window without edge: return "kill"   (existing behaviour)
  if drift |x| < min_drift_atr:               return "kill"   (no consensus deviation)
  if drift >= +min_drift_atr and volume_ok:   return "probe_long"
  if drift <= -min_drift_atr and volume_ok:   return "probe_short"
  fallback:                                    return "kill"

Definitions:
- `drift_atr = (close_now - close_60m_ago) / atr_60m`
- `volume_ok = recent_volume_ratio >= min_volume_ratio` (e.g. last 5 bars
  averaged >= 1.0x of the 60-min avg, meaning the move has participation)

The probe lifecycle is the caller's responsibility (the router only emits
decisions). The recommended pattern is:

  1. Receive `probe_long`/`probe_short` at `t-3m`.
  2. Place a small market order (risk multiplier 0.20) immediately.
  3. Cancel and close at `t+exit_minutes` regardless of PnL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


ACTION_KILL = "kill"
ACTION_ALLOW = "allow"
ACTION_PROBE_LONG = "probe_long"
ACTION_PROBE_SHORT = "probe_short"


@dataclass(frozen=True)
class NewsEvent:
    """A scheduled high-impact news event."""

    event_id: str
    name: str            # "NFP", "FOMC", "CPI", ...
    symbol_impacted: str # "XAUUSD", "EURUSD", ...
    scheduled_utc: datetime
    tier: str = "T1"


@dataclass(frozen=True)
class PriceContext:
    """Live price-action context evaluated by the router."""

    close_now: float
    close_60m_ago: float
    atr_60m: float
    recent_volume_ratio: float = 1.0  # last-5-bars / 60-min avg; 1.0 = parity


@dataclass(frozen=True)
class NewsDecision:
    action: str
    drift_atr: float
    volume_ratio: float
    reasons: tuple[str, ...]
    forced_exit_utc: Optional[datetime] = None


@dataclass
class NewsEdgeRouterConfig:
    enabled: bool = False
    legacy_guard_pre_minutes: float = 45.0
    legacy_guard_post_minutes: float = 30.0
    opportunity_window_start_minutes: float = 15.0  # how far before t-0 we start scoring
    opportunity_window_end_minutes: float = 3.0     # how close to t-0 we still consider entry
    chaos_window_seconds: float = 30.0              # +/- t-0 no order period
    min_drift_atr: float = 0.6
    min_volume_ratio: float = 0.85
    probe_risk_multiplier: float = 0.20
    forced_exit_minutes_after_event: float = 5.0


class NewsEdgeRouter:
    """Decides allow/kill/probe per upcoming news event."""

    def __init__(self, *, config: Optional[NewsEdgeRouterConfig] = None) -> None:
        self.config = config or NewsEdgeRouterConfig()

    def decide(
        self,
        *,
        event: Optional[NewsEvent],
        price_context: Optional[PriceContext],
        now: Optional[datetime] = None,
    ) -> NewsDecision:
        cfg = self.config
        now = now or _utc_now()
        reasons: list[str] = []
        if event is None:
            return NewsDecision(ACTION_ALLOW, 0.0, 0.0, ("no_active_event",))
        offset = now - event.scheduled_utc

        # 1) Always-killed window around t-0 (broker-level chaos).
        chaos = timedelta(seconds=cfg.chaos_window_seconds)
        if -chaos <= offset <= chaos:
            return NewsDecision(ACTION_KILL, 0.0, 0.0, ("chaos_window",))

        # 2) Outside the legacy guard span entirely → allow.
        legacy_pre = timedelta(minutes=cfg.legacy_guard_pre_minutes)
        legacy_post = timedelta(minutes=cfg.legacy_guard_post_minutes)
        in_legacy_window = -legacy_pre <= offset <= legacy_post
        if not in_legacy_window:
            return NewsDecision(ACTION_ALLOW, 0.0, 0.0, ("outside_legacy_guard",))

        # 3) Engine disabled → keep current behaviour (kill within legacy window).
        if not cfg.enabled:
            return NewsDecision(ACTION_KILL, 0.0, 0.0, ("news_edge_disabled",))

        # 4) Only the opportunity window before t-0 can produce a probe.
        opp_start = timedelta(minutes=cfg.opportunity_window_start_minutes)
        opp_end = timedelta(minutes=cfg.opportunity_window_end_minutes)
        in_opp_window = -opp_start <= offset <= -opp_end
        if not in_opp_window:
            return NewsDecision(ACTION_KILL, 0.0, 0.0, ("outside_opportunity_window",))

        # 5) Need price context to evaluate drift.
        if price_context is None or price_context.atr_60m <= 0:
            return NewsDecision(ACTION_KILL, 0.0, 0.0, ("missing_price_context",))

        drift = (price_context.close_now - price_context.close_60m_ago) / price_context.atr_60m
        vol_ratio = float(price_context.recent_volume_ratio)

        if vol_ratio < cfg.min_volume_ratio:
            return NewsDecision(
                ACTION_KILL, round(drift, 4), round(vol_ratio, 4),
                (f"volume_too_thin:{vol_ratio:.2f}<{cfg.min_volume_ratio:.2f}",),
            )
        if abs(drift) < cfg.min_drift_atr:
            return NewsDecision(
                ACTION_KILL, round(drift, 4), round(vol_ratio, 4),
                (f"drift_below_threshold:|{drift:.2f}|<{cfg.min_drift_atr:.2f}",),
            )

        forced_exit = event.scheduled_utc + timedelta(minutes=cfg.forced_exit_minutes_after_event)
        if drift >= cfg.min_drift_atr:
            return NewsDecision(
                ACTION_PROBE_LONG,
                round(drift, 4), round(vol_ratio, 4),
                (f"drift_long:+{drift:.2f}>=+{cfg.min_drift_atr:.2f}",
                 f"forced_exit:{forced_exit.isoformat()}"),
                forced_exit_utc=forced_exit,
            )
        # drift <= -min
        return NewsDecision(
            ACTION_PROBE_SHORT,
            round(drift, 4), round(vol_ratio, 4),
            (f"drift_short:{drift:.2f}<=-{cfg.min_drift_atr:.2f}",
             f"forced_exit:{forced_exit.isoformat()}"),
            forced_exit_utc=forced_exit,
        )


__all__ = [
    "NewsEdgeRouter",
    "NewsEdgeRouterConfig",
    "NewsEvent",
    "PriceContext",
    "NewsDecision",
    "ACTION_KILL",
    "ACTION_ALLOW",
    "ACTION_PROBE_LONG",
    "ACTION_PROBE_SHORT",
]

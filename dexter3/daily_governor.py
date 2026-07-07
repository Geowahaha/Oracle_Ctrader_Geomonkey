"""Daily Mission Governor — the prop-firm-style layer over Dexter3's HUNT MODE.

Owner directive (2026-07-07): chase $100/day on a $1000 virtual capital base,
aggressively but survivably. This module is PURE LOGIC ONLY — no I/O, no MCP
calls, no journal writes. It answers exactly three questions:

    1. ``status``          — given today's realized + floating PnL, are we
                              still hunting, locked at target, or stopped at
                              the loss cap?
    2. ``risk_for_entry``  — given the current win streak and session, how
                              much USD should THIS entry risk?
    3. ``win_streak_from_closes`` — derive the current win streak fresh from
                              today's ordered close PnLs (stateless — no
                              fragile running counter that can desync from
                              the broker's own history).

Layering discipline (blueprint-equivalent non-negotiable for this module):
the governor can only (a) refuse to let new entries fire, (b) trigger a
close-all, and (c) shape the USD size of an entry BEFORE it reaches the
executor's own sizing/caps. It NEVER raises any existing cap and holds no
reference to ``BasketConfig``/``ExecutorConfig`` — the wiring layer
(``dexter3/shadow_runner.py``) is responsible for actually enforcing what
this module recommends.

Floating PnL counts BOTH ways: a floating winner that crosses the daily
target locks the day (close-all, then no more entries); a floating loser
that crosses the loss cap stops the day the same way. The day must never be
allowed to bleed past the cap just because the loss was "not realized yet".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATE_HUNTING = "HUNTING"
STATE_TARGET_LOCKED = "TARGET_LOCKED"
STATE_LOSS_STOPPED = "LOSS_STOPPED"

DEFAULT_SESSION_MULT: dict[str, float] = {
    "overlap": 1.2,
    "london": 1.0,
    "ny": 1.1,
    "asian": 0.6,
    "off_hours": 0.5,
    "unknown": 0.8,
}


@dataclass(frozen=True)
class GovernorConfig:
    """All fields are env-overridable by the runner (see
    ``dexter3/shadow_runner.py::_governor_config_from_env``). Defaults below
    are the owner's stated mission parameters for 2026-07-07.
    """

    # The owner's virtual capital base driving all sizing math — NOT the
    # broker/demo account balance, which may differ.
    capital_usd: float = 1000.0
    # Reach this effective (realized + floating) PnL for the UTC day ->
    # TARGET_LOCKED: close everything, no more entries until next UTC day.
    daily_target_usd: float = 100.0
    # Effective PnL falling to/below -daily_loss_usd -> LOSS_STOPPED: close
    # everything, protect capital, no more entries until next UTC day.
    daily_loss_usd: float = 50.0
    # Base risk per entry = capital_usd * base_risk_frac (~$12 at defaults).
    base_risk_frac: float = 0.012
    # Absolute per-entry risk ceiling regardless of streak/session multipliers
    # (~$25 at defaults) — the ONE hard cap this module enforces internally.
    max_risk_frac: float = 0.025
    # Anti-martingale ladder: risk multiplier indexed by current WIN streak
    # (index = min(streak, len(ladder) - 1)). A single LOSS resets the streak
    # to 0 (see win_streak_from_closes). Pressing size on winners with a
    # capped downside is how big green days happen without ruin.
    ladder: tuple[float, ...] = (1.0, 1.3, 1.6, 2.0)
    # Session multiplier — keys MUST match market_lens.session_context()'s
    # exact "value" tag strings (asian/london/overlap/ny/off_hours/unknown).
    session_mult: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SESSION_MULT))


class DailyGovernor:
    """Pure decision surface — construct with a ``GovernorConfig`` and call
    ``status``/``risk_for_entry``/``win_streak_from_closes`` per tick. Holds
    no mutable state of its own; the caller (shadow_runner) persists
    day-state (locked/stopped + which UTC day it applies to) itself."""

    def __init__(self, config: GovernorConfig | None = None) -> None:
        self.config = config or GovernorConfig()

    # -- question 1: are we still allowed to hunt? --------------------------

    def status(self, day_pnl_usd: float, floating_pnl_usd: float) -> dict[str, Any]:
        """Return the governor's read of the day given realized + floating PnL.

        ``effective_pnl = day_pnl_usd + floating_pnl_usd``. TARGET_LOCKED
        fires at ``effective_pnl >= daily_target_usd`` (mission accomplished
        — even if the winning part is still floating, lock it in). LOSS_STOPPED
        fires at ``effective_pnl <= -daily_loss_usd`` (protect capital even
        while the loss is still floating — never let the day bleed past the
        cap because "it hasn't closed yet"). Otherwise HUNTING.
        """
        cfg = self.config
        effective = float(day_pnl_usd) + float(floating_pnl_usd)
        if effective >= cfg.daily_target_usd:
            state = STATE_TARGET_LOCKED
            reason = (
                f"effective_pnl {effective:.2f} >= daily_target {cfg.daily_target_usd:.2f} "
                "-> mission complete, locking profit"
            )
        elif effective <= -cfg.daily_loss_usd:
            state = STATE_LOSS_STOPPED
            reason = (
                f"effective_pnl {effective:.2f} <= -daily_loss {cfg.daily_loss_usd:.2f} "
                "-> daily loss cap breached, protecting capital"
            )
        else:
            state = STATE_HUNTING
            reason = (
                f"effective_pnl {effective:.2f} within [-{cfg.daily_loss_usd:.2f}, "
                f"{cfg.daily_target_usd:.2f}) -> still hunting"
            )
        return {
            "state": state,
            "effective_pnl": round(effective, 4),
            "day_pnl_usd": round(float(day_pnl_usd), 4),
            "floating_pnl_usd": round(float(floating_pnl_usd), 4),
            "target": cfg.daily_target_usd,
            "loss_cap": cfg.daily_loss_usd,
            "reason": reason,
        }

    # -- question 2: how much should THIS entry risk? -----------------------

    def risk_for_entry(self, session_label: str, win_streak: int) -> dict[str, Any]:
        """risk_usd = capital * base_risk_frac * ladder[streak] * session_mult,
        capped at capital * max_risk_frac, floored at 1.0 USD.
        """
        cfg = self.config
        streak = max(0, int(win_streak))
        ladder = cfg.ladder if cfg.ladder else (1.0,)
        ladder_mult = ladder[min(streak, len(ladder) - 1)]
        session_key = str(session_label or "unknown")
        session_mult = cfg.session_mult.get(session_key, cfg.session_mult.get("unknown", 0.8))

        base = cfg.capital_usd * cfg.base_risk_frac
        risk_usd = base * ladder_mult * session_mult
        cap_usd = cfg.capital_usd * cfg.max_risk_frac
        risk_usd = min(risk_usd, cap_usd)
        risk_usd = max(risk_usd, 1.0)

        return {
            "risk_usd": round(risk_usd, 2),
            "ladder_mult": ladder_mult,
            "session_mult": session_mult,
            "streak": streak,
            "session_label": session_key,
            "base_risk_usd": round(base, 4),
            "cap_usd": round(cap_usd, 4),
        }

    # -- question 3: what's the current win streak, derived fresh -----------

    @staticmethod
    def win_streak_from_closes(pnls: list[float]) -> int:
        """Length of the trailing run of wins in today's ordered close PnLs.

        Stateless by design — recomputed fresh from the deals list every
        call, never a running counter that can desync from broker reality.
        ``pnls`` must be ordered oldest -> newest (chronological close order).
        A pnl of exactly 0.0 (breakeven) counts as NOT a win and breaks the
        streak, same conservative posture as a loss.

        Examples: [] -> 0; [w,l,w,w] -> 2; [w,w,l] -> 0.
        """
        streak = 0
        for pnl in reversed(pnls):
            if float(pnl) > 0.0:
                streak += 1
            else:
                break
        return streak

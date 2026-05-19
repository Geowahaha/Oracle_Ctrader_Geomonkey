"""Adversarial Awareness — psychology tagging + cool-down directives.

Two components:

1. ``PsychologyTagger`` is stateless: given a CloseEvent, return a tag.
2. ``AdversarialAwareness`` is stateful: append CloseEvents over time, then
   ``evaluate(now)`` to get CoolDownDirective(s) when patterns trip.

Patterns recognised:
- ``revenge``        : N consecutive same-direction losses within window.
- ``hunt_cluster``   : M STOP_HUNT_FULL_SL within window — the market is
                       reading our SL placement; pause to let liquidity
                       reshuffle.
- ``panic_cluster``  : M PANIC_CLOSE_NOISE in a row — the system itself
                       is fragile; cool off to break the loop.
- ``drawdown_burst`` : cumulative loss over the last N trades > threshold
                       — equity protection.

Each directive carries:
- scope:        "global" | "direction:<long|short>" | "source:<name>"
- cooldown_until_utc
- reason (human-readable)
- triggering_event_ids

The scheduler should consult AdversarialAwareness.is_blocked(...) before
emitting any signal. A winning trade RESETS the streak counters for its
direction/source.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Iterable, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


# --- Psychology tags --------------------------------------------------------

class PsychologyTag:
    WIN_NORMAL = "WIN_NORMAL"
    WIN_CUT_EARLY = "WIN_CUT_EARLY"
    LOSS_NORMAL = "LOSS_NORMAL"
    PANIC_CLOSE_NOISE = "PANIC_CLOSE_NOISE"
    STOP_HUNT_FULL_SL = "STOP_HUNT_FULL_SL"
    MFE_GIVEBACK = "MFE_GIVEBACK"
    WRONG_DIRECTION = "WRONG_DIRECTION"


@dataclass(frozen=True)
class CloseEvent:
    """A closed trade with enough context to tag and reason about."""

    position_id: int
    direction: str
    source: str
    pnl_usd: float
    realised_r: float       # signed (>0 winning, <0 losing)
    mfe_r: float            # peak favourable R (always >=0)
    mae_r: float            # peak adverse R (always >=0)
    closed_utc: datetime
    # Optional — caller may already have a tag (e.g. from offline analysis).
    pre_tagged: Optional[str] = None


class PsychologyTagger:
    """Pure tag function with thresholds tuned to the 2026-05-18/19 dataset."""

    def __init__(
        self,
        *,
        win_cut_early_realised_r_max: float = 0.40,
        win_cut_early_mfe_r_min: float = 1.50,
        panic_close_realised_r_min: float = -0.30,
        panic_close_mae_r_max: float = 0.40,
        mfe_giveback_peak_r_min: float = 1.00,
        mfe_giveback_realised_r_max: float = -0.50,
        stop_hunt_realised_r_max: float = -0.85,
        wrong_direction_realised_r_max: float = -0.50,
        wrong_direction_mae_r_min: float = 1.00,
    ) -> None:
        self.win_cut_early_realised_r_max = win_cut_early_realised_r_max
        self.win_cut_early_mfe_r_min = win_cut_early_mfe_r_min
        self.panic_close_realised_r_min = panic_close_realised_r_min
        self.panic_close_mae_r_max = panic_close_mae_r_max
        self.mfe_giveback_peak_r_min = mfe_giveback_peak_r_min
        self.mfe_giveback_realised_r_max = mfe_giveback_realised_r_max
        self.stop_hunt_realised_r_max = stop_hunt_realised_r_max
        self.wrong_direction_realised_r_max = wrong_direction_realised_r_max
        self.wrong_direction_mae_r_min = wrong_direction_mae_r_min

    def tag(self, event: CloseEvent) -> str:
        if event.pre_tagged:
            return str(event.pre_tagged)
        if event.pnl_usd > 0:
            if (event.realised_r < self.win_cut_early_realised_r_max
                    and event.mfe_r > self.win_cut_early_mfe_r_min):
                return PsychologyTag.WIN_CUT_EARLY
            return PsychologyTag.WIN_NORMAL
        # Losers
        if (event.realised_r > self.panic_close_realised_r_min
                and event.mae_r < self.panic_close_mae_r_max):
            return PsychologyTag.PANIC_CLOSE_NOISE
        if (event.mfe_r > self.mfe_giveback_peak_r_min
                and event.realised_r < self.mfe_giveback_realised_r_max):
            return PsychologyTag.MFE_GIVEBACK
        if event.realised_r < self.stop_hunt_realised_r_max:
            return PsychologyTag.STOP_HUNT_FULL_SL
        if (event.realised_r < self.wrong_direction_realised_r_max
                and event.mae_r > self.wrong_direction_mae_r_min):
            return PsychologyTag.WRONG_DIRECTION
        return PsychologyTag.LOSS_NORMAL


# --- Cool-down directives ---------------------------------------------------

@dataclass(frozen=True)
class CoolDownDirective:
    scope: str                       # "global" | "direction:long" | "source:scalp_xauusd"
    cooldown_until_utc: datetime
    reason: str
    triggering_event_ids: tuple[int, ...]


@dataclass
class AdversarialAwarenessConfig:
    enabled: bool = False
    # Revenge: N consecutive same-direction losses → block that direction
    revenge_loss_count: int = 3
    revenge_window_minutes: float = 60.0
    revenge_cooldown_minutes: float = 30.0
    # Hunt cluster: M STOP_HUNT_FULL_SL in window → cool off all XAU
    hunt_cluster_count: int = 3
    hunt_cluster_window_minutes: float = 90.0
    hunt_cooldown_minutes: float = 20.0
    # Panic cluster: M PANIC_CLOSE_NOISE in a row → cool off source
    panic_cluster_count: int = 4
    panic_cluster_window_minutes: float = 60.0
    panic_cooldown_minutes: float = 15.0
    # Drawdown burst: sum of losses across last N trades worse than -X
    drawdown_window_count: int = 8
    drawdown_threshold_usd: float = -200.0
    drawdown_cooldown_minutes: float = 60.0
    # History cap — keep at most this many events in memory.
    history_max: int = 200


class AdversarialAwareness:
    """Stateful — caller records close events, asks ``active_directives()``."""

    def __init__(
        self,
        *,
        config: Optional[AdversarialAwarenessConfig] = None,
        tagger: Optional[PsychologyTagger] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or AdversarialAwarenessConfig()
        self.tagger = tagger or PsychologyTagger()
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._events: Deque[tuple[CloseEvent, str]] = deque(maxlen=self.config.history_max)
        # Active directives keyed by scope; new directives override.
        self._active: dict[str, CoolDownDirective] = {}

    # --- ingestion ---------------------------------------------------------
    def record_close(self, event: CloseEvent) -> str:
        """Tag and append. Returns the tag assigned."""
        tag = self.tagger.tag(event)
        with self._lock:
            self._events.append((event, tag))
            # Winning trade clears the same-direction revenge cooldown.
            if event.pnl_usd > 0:
                dir_key = f"direction:{_norm_dir(event.direction)}"
                self._active.pop(dir_key, None)
        return tag

    # --- evaluation --------------------------------------------------------
    def evaluate(self, *, now: Optional[datetime] = None) -> list[CoolDownDirective]:
        """Re-evaluate all patterns. Returns currently-active directives."""
        if not self.config.enabled:
            return []
        now = now or self._clock()
        new_directives: list[CoolDownDirective] = []
        with self._lock:
            # Garbage-collect expired directives first.
            for scope in list(self._active.keys()):
                if self._active[scope].cooldown_until_utc <= now:
                    del self._active[scope]

            # Pattern: revenge — consecutive same-direction losses
            revenge = self._detect_revenge(now=now)
            if revenge is not None:
                new_directives.append(revenge)
                self._active[revenge.scope] = revenge

            # Pattern: hunt cluster — block all XAU briefly
            hunt = self._detect_hunt_cluster(now=now)
            if hunt is not None:
                new_directives.append(hunt)
                self._active[hunt.scope] = hunt

            # Pattern: panic cluster — block source
            panic = self._detect_panic_cluster(now=now)
            if panic is not None:
                new_directives.append(panic)
                self._active[panic.scope] = panic

            # Pattern: drawdown burst — global pause
            dd = self._detect_drawdown_burst(now=now)
            if dd is not None:
                new_directives.append(dd)
                self._active[dd.scope] = dd

            return list(self._active.values())

    def is_blocked(
        self,
        *,
        direction: str,
        source: str,
        now: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """Quick check used by the scheduler before emitting a signal."""
        now = now or self._clock()
        # Auto-refresh so the caller sees the latest decisions.
        self.evaluate(now=now)
        direction_n = _norm_dir(direction)
        scope_global = "global"
        scope_dir = f"direction:{direction_n}"
        scope_src = f"source:{str(source or '').strip().lower()}"
        with self._lock:
            for scope in (scope_global, scope_dir, scope_src):
                d = self._active.get(scope)
                if d and d.cooldown_until_utc > now:
                    return True, f"{d.scope}:{d.reason}"
        return False, "ok"

    # --- pattern detectors --------------------------------------------------
    def _events_within(self, now: datetime, minutes: float):
        cutoff = now - timedelta(minutes=float(minutes))
        return [(e, t) for (e, t) in self._events if e.closed_utc >= cutoff]

    def _detect_revenge(self, *, now: datetime) -> Optional[CoolDownDirective]:
        cfg = self.config
        recent = self._events_within(now, cfg.revenge_window_minutes)
        if not recent:
            return None
        # Walk back from newest, count same-direction losses until a winner.
        last_dir: Optional[str] = None
        loss_ids: list[int] = []
        for ev, tag in reversed(recent):
            if ev.pnl_usd > 0:
                break
            d = _norm_dir(ev.direction)
            if last_dir is None:
                last_dir = d
            if d != last_dir:
                break
            loss_ids.append(int(ev.position_id))
        if last_dir is None or len(loss_ids) < cfg.revenge_loss_count:
            return None
        scope = f"direction:{last_dir}"
        until = now + timedelta(minutes=cfg.revenge_cooldown_minutes)
        return CoolDownDirective(
            scope=scope,
            cooldown_until_utc=until,
            reason=f"revenge_pattern:{len(loss_ids)}x_{last_dir}_losses_in_{cfg.revenge_window_minutes:.0f}min",
            triggering_event_ids=tuple(loss_ids),
        )

    def _detect_hunt_cluster(self, *, now: datetime) -> Optional[CoolDownDirective]:
        cfg = self.config
        recent = self._events_within(now, cfg.hunt_cluster_window_minutes)
        hunts = [(e, t) for (e, t) in recent if t == PsychologyTag.STOP_HUNT_FULL_SL]
        if len(hunts) < cfg.hunt_cluster_count:
            return None
        until = now + timedelta(minutes=cfg.hunt_cooldown_minutes)
        return CoolDownDirective(
            scope="global",
            cooldown_until_utc=until,
            reason=f"hunt_cluster:{len(hunts)}_sl_hits_in_{cfg.hunt_cluster_window_minutes:.0f}min",
            triggering_event_ids=tuple(int(e.position_id) for e, _ in hunts),
        )

    def _detect_panic_cluster(self, *, now: datetime) -> Optional[CoolDownDirective]:
        cfg = self.config
        recent = self._events_within(now, cfg.panic_cluster_window_minutes)
        panics_in_row: list[CloseEvent] = []
        for ev, tag in reversed(recent):
            if tag == PsychologyTag.PANIC_CLOSE_NOISE:
                panics_in_row.append(ev)
            else:
                break
        if len(panics_in_row) < cfg.panic_cluster_count:
            return None
        # Source: most-common in cluster
        from collections import Counter
        src_counter = Counter(str(e.source or "").strip().lower() for e in panics_in_row)
        main_src, _ = src_counter.most_common(1)[0]
        until = now + timedelta(minutes=cfg.panic_cooldown_minutes)
        return CoolDownDirective(
            scope=f"source:{main_src}",
            cooldown_until_utc=until,
            reason=f"panic_cluster:{len(panics_in_row)}_noise_closes_source={main_src}",
            triggering_event_ids=tuple(int(e.position_id) for e in panics_in_row),
        )

    def _detect_drawdown_burst(self, *, now: datetime) -> Optional[CoolDownDirective]:
        cfg = self.config
        # Last N trades (any time)
        n = max(1, int(cfg.drawdown_window_count))
        last_n = list(self._events)[-n:]
        if len(last_n) < n:
            return None
        total = sum(float(e.pnl_usd) for e, _ in last_n)
        if total > cfg.drawdown_threshold_usd:
            return None
        until = now + timedelta(minutes=cfg.drawdown_cooldown_minutes)
        return CoolDownDirective(
            scope="global",
            cooldown_until_utc=until,
            reason=f"drawdown_burst:last_{n}_trades_pnl=${total:.2f}<={cfg.drawdown_threshold_usd:.0f}",
            triggering_event_ids=tuple(int(e.position_id) for e, _ in last_n),
        )

    # --- diagnostics --------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            tag_counts: dict[str, int] = {}
            for _, t in self._events:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            return {
                "events_total": len(self._events),
                "tag_counts": tag_counts,
                "active_directives": [
                    {
                        "scope": d.scope,
                        "until": d.cooldown_until_utc.isoformat(),
                        "reason": d.reason,
                    }
                    for d in self._active.values()
                ],
            }


__all__ = [
    "AdversarialAwareness",
    "AdversarialAwarenessConfig",
    "CloseEvent",
    "CoolDownDirective",
    "PsychologyTag",
    "PsychologyTagger",
]

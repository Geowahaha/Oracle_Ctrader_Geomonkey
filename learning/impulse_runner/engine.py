"""Impulse Runner engine — decides whether to extend a winning position.

Design notes:
- **Pure analyzer.** The engine emits a directive; the caller modifies the
  position via the existing executor API. We do not place orders here.
- **Conservative gating.** Three independent guards must all pass before any
  position is modified: MFE threshold, structure_break confirmation, and ATR
  sanity (no zero/negative ATR can produce a positive trail).
- **Per-position cooldown.** Once a directive is issued, the same position
  enters cooldown so we don't tighten the trail every tick.
- **Idempotent.** Re-issuing the same directive is safe — the caller should
  no-op when the proposed TP/SL already match the broker side.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


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
class PositionSnapshot:
    """Live position state we need to evaluate."""

    position_id: int
    symbol: str
    direction: str                # "long" / "short"
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    atr_5m: float
    volume: float
    source: str = ""

    @property
    def risk_distance(self) -> float:
        """Absolute distance from entry to original stop, never zero."""
        d = abs(float(self.entry_price) - float(self.stop_loss))
        return max(d, 1e-9)

    @property
    def mfe_r(self) -> float:
        """Maximum favourable excursion measured in R-multiples."""
        d = _norm_dir(self.direction)
        if d == "long":
            adv = float(self.current_price) - float(self.entry_price)
        elif d == "short":
            adv = float(self.entry_price) - float(self.current_price)
        else:
            return 0.0
        return adv / self.risk_distance


@dataclass(frozen=True)
class StructureBreakSignal:
    """Compact representation of the impulse-leg confirmation."""

    aligned_with_position: bool
    strength: float            # 0..1; higher = cleaner break
    last_5m_close_break: bool  # M5 close beyond prior swing
    delta_confirms: bool       # delta_proxy aligned with direction


@dataclass(frozen=True)
class TrailingDirective:
    """Output — instruction for the executor to modify the position."""

    position_id: int
    new_stop_loss: float
    new_take_profit: float
    reason: str
    issued_utc: datetime


@dataclass
class ImpulseRunnerConfig:
    enabled: bool = False
    min_mfe_r: float = 1.0
    min_break_strength: float = 0.5
    require_delta_confirms: bool = True
    require_5m_break_close: bool = True
    trail_atr_mult: float = 1.20    # new SL is current_price ± mult*ATR
    extend_atr_mult: float = 3.50   # new TP is current_price ± mult*ATR
    cooldown_seconds: float = 90.0
    # Allow per-source override of source filter; empty means "any source".
    allowed_sources_csv: str = ""


def _allowed_source(source: str, csv: str) -> bool:
    csv = (csv or "").strip()
    if not csv:
        return True
    allowed = {s.strip().lower() for s in csv.split(",") if s.strip()}
    return str(source or "").strip().lower() in allowed


class ImpulseRunner:
    """Stateful runner — keeps per-position cooldown so it fires at most once
    every `cooldown_seconds` per `position_id`."""

    def __init__(
        self,
        *,
        config: Optional[ImpulseRunnerConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or ImpulseRunnerConfig()
        self._clock = clock or _utc_now
        self._last_fire: dict[int, datetime] = {}
        self._lock = threading.Lock()

    def evaluate(
        self,
        *,
        position: PositionSnapshot,
        structure: Optional[StructureBreakSignal] = None,
    ) -> Optional[TrailingDirective]:
        cfg = self.config
        if not cfg.enabled:
            return None
        if not _allowed_source(position.source, cfg.allowed_sources_csv):
            return None
        # Hard guards.
        if position.atr_5m <= 0:
            return None
        if position.mfe_r < cfg.min_mfe_r:
            return None
        if structure is None or not structure.aligned_with_position:
            return None
        if structure.strength < cfg.min_break_strength:
            return None
        if cfg.require_delta_confirms and not structure.delta_confirms:
            return None
        if cfg.require_5m_break_close and not structure.last_5m_close_break:
            return None

        # Cooldown.
        now = self._clock()
        with self._lock:
            last = self._last_fire.get(position.position_id)
            if last is not None and (now - last).total_seconds() < cfg.cooldown_seconds:
                return None
            self._last_fire[position.position_id] = now

        direction = _norm_dir(position.direction)
        atr = max(0.0, float(position.atr_5m))
        price = float(position.current_price)
        if direction == "long":
            new_sl = price - cfg.trail_atr_mult * atr
            new_tp = price + cfg.extend_atr_mult * atr
            # Never move SL backward (i.e. closer to losing).
            new_sl = max(new_sl, float(position.stop_loss))
            # Never shrink TP — only extend.
            new_tp = max(new_tp, float(position.take_profit))
        elif direction == "short":
            new_sl = price + cfg.trail_atr_mult * atr
            new_tp = price - cfg.extend_atr_mult * atr
            new_sl = min(new_sl, float(position.stop_loss))
            new_tp = min(new_tp, float(position.take_profit))
        else:
            return None

        reason = (
            f"impulse_runner:mfe_r={position.mfe_r:.2f}>={cfg.min_mfe_r:.2f}"
            f",break_str={structure.strength:.2f},trail={cfg.trail_atr_mult:.2f}x"
            f",extend={cfg.extend_atr_mult:.2f}x"
        )
        return TrailingDirective(
            position_id=int(position.position_id),
            new_stop_loss=round(new_sl, 6),
            new_take_profit=round(new_tp, 6),
            reason=reason,
            issued_utc=now,
        )

    def reset(self) -> None:
        with self._lock:
            self._last_fire.clear()

    def is_on_cooldown(self, position_id: int, *, now: Optional[datetime] = None) -> bool:
        cfg = self.config
        now = now or self._clock()
        with self._lock:
            last = self._last_fire.get(int(position_id))
            if last is None:
                return False
            return (now - last).total_seconds() < cfg.cooldown_seconds


__all__ = [
    "ImpulseRunner",
    "ImpulseRunnerConfig",
    "PositionSnapshot",
    "StructureBreakSignal",
    "TrailingDirective",
]

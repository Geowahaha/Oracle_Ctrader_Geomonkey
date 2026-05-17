"""Pre-Signal Engine — fuses live conviction signals into a tiny pre-arm.

Conviction formula (each component 0..1):
    conviction = (
        vp_weight * vp_alignment
        + dom_weight * dom_alignment
        + sharpness_weight * sharpness_score
    )

The weights default to 0.4 / 0.35 / 0.25 and are individually configurable.
When `conviction >= arm_threshold` AND the conviction is `sustained_seconds`
old (i.e., not a one-tick spike), the engine emits a `PreArmDirective`.

Per-symbol cooldown prevents spam (default: one pre-arm per 60 seconds).
TTL on the resulting order is short (default 90s) — caller cancels if the
real signal doesn't fire by then.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class ConvictionInputs:
    """Live components from the analysis stack, each normalised to [-1, 1]
    where positive favours long, negative favours short. Absolute value
    encodes the strength of the alignment with the bias.
    """

    bias: str                   # "long" or "short"
    vp_alignment: float         # Volume Profile (POC vs price, edge proximity)
    dom_alignment: float        # DOM imbalance
    sharpness_score: float      # Sharpness Feedback Loop score
    bias_atr: float             # current 5-min ATR for sizing the pre-arm
    last_price: float


@dataclass(frozen=True)
class PreArmDirective:
    """Output — instructions to place a tiny pre-arm pending order."""

    symbol: str
    bias: str
    conviction: float
    entry_type: str             # "limit" / "stop"
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_multiplier: float
    ttl_seconds: float
    issued_utc: datetime
    reasons: tuple[str, ...]


@dataclass
class PreSignalEngineConfig:
    enabled: bool = False
    vp_weight: float = 0.40
    dom_weight: float = 0.35
    sharpness_weight: float = 0.25
    arm_threshold: float = 0.70
    sustained_seconds: float = 2.0
    cooldown_seconds: float = 60.0
    pre_arm_risk_multiplier: float = 0.10
    ttl_seconds: float = 90.0
    stop_atr_multiplier: float = 1.2
    take_profit_atr_multiplier: float = 1.6


def _norm_bias(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


def _aligned(component: float, bias: str) -> float:
    """Return a non-negative alignment with the bias, 0..1.

    For a `long` bias, only positive component values count; for `short`, only
    negative component values count. Returning 0 when sign disagrees ensures
    a misaligned input cannot lift conviction.
    """
    c = float(component)
    if bias == "long":
        return _clamp01(c)
    if bias == "short":
        return _clamp01(-c)
    return 0.0


class PreSignalEngine:
    """Sustained conviction → pre-arm directive."""

    def __init__(
        self,
        *,
        config: Optional[PreSignalEngineConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or PreSignalEngineConfig()
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        # Per-symbol rolling conviction history (timestamp, conviction).
        self._history: dict[str, Deque[tuple[datetime, float]]] = {}
        self._last_arm_utc: dict[str, datetime] = {}

    def evaluate(
        self,
        *,
        symbol: str,
        inputs: ConvictionInputs,
        now: Optional[datetime] = None,
    ) -> Optional[PreArmDirective]:
        cfg = self.config
        if not cfg.enabled:
            return None
        now = now or self._clock()
        symbol_n = str(symbol or "").strip().upper()
        bias_n = _norm_bias(inputs.bias)
        if bias_n not in {"long", "short"}:
            return None

        vp_a = _aligned(inputs.vp_alignment, bias_n)
        dom_a = _aligned(inputs.dom_alignment, bias_n)
        sharp = _clamp01(inputs.sharpness_score)
        conviction = (
            cfg.vp_weight * vp_a
            + cfg.dom_weight * dom_a
            + cfg.sharpness_weight * sharp
        )

        # Record into history with eviction of stale entries.
        with self._lock:
            hist = self._history.setdefault(symbol_n, deque())
            cutoff = now - timedelta(seconds=max(cfg.sustained_seconds * 2, 30.0))
            while hist and hist[0][0] < cutoff:
                hist.popleft()
            hist.append((now, conviction))

            # Cooldown gate.
            last = self._last_arm_utc.get(symbol_n)
            if last is not None and (now - last).total_seconds() < cfg.cooldown_seconds:
                return None

            # Sustained-conviction gate: all entries within sustained_seconds
            # must be at or above the threshold.
            sustained_start = now - timedelta(seconds=cfg.sustained_seconds)
            sustained_samples = [v for (t, v) in hist if t >= sustained_start]
            if not sustained_samples or len(sustained_samples) < 2:
                return None
            if min(sustained_samples) < cfg.arm_threshold:
                return None

            self._last_arm_utc[symbol_n] = now

        # Build the directive.
        atr = max(0.0, float(inputs.bias_atr))
        if bias_n == "long":
            entry_price = float(inputs.last_price) + 0.10 * atr  # buy_stop just above
            stop_loss = entry_price - cfg.stop_atr_multiplier * atr
            take_profit = entry_price + cfg.take_profit_atr_multiplier * atr
            entry_type = "stop"
        else:
            entry_price = float(inputs.last_price) - 0.10 * atr  # sell_stop just below
            stop_loss = entry_price + cfg.stop_atr_multiplier * atr
            take_profit = entry_price - cfg.take_profit_atr_multiplier * atr
            entry_type = "stop"

        reasons = (
            f"conviction:{conviction:.3f}>={cfg.arm_threshold:.3f}",
            f"sustained:{len(sustained_samples)}x",
            f"vp_a:{vp_a:.2f}", f"dom_a:{dom_a:.2f}", f"sharp:{sharp:.2f}",
        )
        return PreArmDirective(
            symbol=symbol_n,
            bias=bias_n,
            conviction=round(conviction, 4),
            entry_type=entry_type,
            entry_price=round(entry_price, 6),
            stop_loss=round(stop_loss, 6),
            take_profit=round(take_profit, 6),
            risk_multiplier=float(cfg.pre_arm_risk_multiplier),
            ttl_seconds=float(cfg.ttl_seconds),
            issued_utc=now,
            reasons=reasons,
        )


__all__ = [
    "PreSignalEngine",
    "PreSignalEngineConfig",
    "ConvictionInputs",
    "PreArmDirective",
]

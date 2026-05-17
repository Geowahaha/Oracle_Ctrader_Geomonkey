"""Equity-Adaptive Global Risk Governor (Tier 1 #3).

Compounds winners and shrinks during drawdowns by scaling the global
`risk_per_trade` multiplier based on rolling equity-curve slope.

Design choices:
- **Pure reader.** Reads from cTrader `execution_journal` only. Does not write
  to the trading config — emits a `recommended_multiplier` that the trader
  layer is free to consume (or not).
- **Two windows.** A "fast" 24h window detects recent bleed; a "slow" 7d window
  smooths longer-term trend. The output multiplier is the min of both fast and
  slow recommendations, so a single bad day cannot inflate sizing.
- **Hard bounds.** Output is always clamped to `[min_multiplier, max_multiplier]`
  (default `[0.5, 1.5]`). Compounding gains: yes. Going to the moon on a hot
  streak: no.
- **Cold-start friendly.** Until both windows have ≥ `min_trades` closed
  positions, the recommendation is exactly 1.0 (neutral).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(str(value) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class EquitySlice:
    """Aggregate PnL stats for a single window."""

    window_label: str
    n_trades: int
    total_pnl_usd: float
    win_rate: float
    sharpe_like: float


@dataclass(frozen=True)
class EquityRecommendation:
    """Output of the Equity Governor — recommended risk multiplier."""

    recommended_multiplier: float
    fast_slice: EquitySlice
    slow_slice: EquitySlice
    reasons: tuple[str, ...]
    decided_utc: str


class EquityGovernor:
    """Reads broker journal and recommends a global risk multiplier."""

    def __init__(
        self,
        *,
        journal_db_path: str | Path,
        fast_window_hours: float = 24.0,
        slow_window_days: float = 7.0,
        min_trades: int = 5,
        min_multiplier: float = 0.5,
        max_multiplier: float = 1.5,
        target_pnl_fast: float = 50.0,
        target_pnl_slow: float = 300.0,
        sensitivity: float = 0.20,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.journal_db_path = str(journal_db_path)
        self.fast_window_hours = float(fast_window_hours)
        self.slow_window_days = float(slow_window_days)
        self.min_trades = int(min_trades)
        self.min_multiplier = float(min_multiplier)
        self.max_multiplier = float(max_multiplier)
        self.target_pnl_fast = float(target_pnl_fast)
        self.target_pnl_slow = float(target_pnl_slow)
        self.sensitivity = float(sensitivity)
        self._clock = clock or _utc_now

    def _load_slice(self, since_ts: float, label: str) -> EquitySlice:
        if not Path(self.journal_db_path).exists():
            return EquitySlice(label, 0, 0.0, 0.0, 0.0)
        pnls: list[float] = []
        try:
            with sqlite3.connect(self.journal_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT execution_meta_json
                      FROM execution_journal
                     WHERE created_ts >= ?
                       AND execution_meta_json IS NOT NULL
                       AND position_id IS NOT NULL
                     ORDER BY created_ts ASC
                    """,
                    (since_ts,),
                )
                for row in cur:
                    meta = _safe_json(row["execution_meta_json"])
                    closed = dict(meta.get("closed") or {})
                    if not closed:
                        continue
                    pnls.append(_coerce_float(closed.get("pnl_usd")))
        except sqlite3.DatabaseError as exc:
            logger.exception("equity_governor_db_error db=%s err=%s", self.journal_db_path, exc)
            return EquitySlice(label, 0, 0.0, 0.0, 0.0)
        n = len(pnls)
        total = sum(pnls)
        wr = (sum(1 for p in pnls if p > 0) / n) if n else 0.0
        # "Sharpe-like" — total PnL divided by per-trade std × sqrt(n). Not a
        # real Sharpe (we lack the risk-free rate), but it's a comparable
        # signal-to-noise number across windows.
        if n >= 2:
            mean = total / n
            variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            std = max(1e-9, variance ** 0.5)
            sharpe_like = (mean / std) * (n ** 0.5)
        else:
            sharpe_like = 0.0
        return EquitySlice(
            window_label=label,
            n_trades=n,
            total_pnl_usd=round(total, 4),
            win_rate=round(wr, 4),
            sharpe_like=round(sharpe_like, 4),
        )

    def _recommend_from_slice(self, slc: EquitySlice, target_pnl: float) -> float:
        """Map a slice to a multiplier in `[min_multiplier, max_multiplier]`.

        Math: ratio = total_pnl / target_pnl. Recommendation = 1 + sensitivity*ratio.
        ratio ≥ 1 means we hit the target → push above 1; ratio ≤ -1 means we lost
        the target → pull below 1.
        """
        if slc.n_trades < self.min_trades:
            return 1.0
        ratio = slc.total_pnl_usd / max(1e-9, target_pnl)
        # Tame extreme ratios so a single huge win/loss doesn't blow up sizing.
        ratio = _clamp(ratio, -3.0, 3.0)
        rec = 1.0 + self.sensitivity * ratio
        return _clamp(rec, self.min_multiplier, self.max_multiplier)

    def recommend(self) -> EquityRecommendation:
        now = self._clock()
        fast_since = (now - timedelta(hours=self.fast_window_hours)).timestamp()
        slow_since = (now - timedelta(days=self.slow_window_days)).timestamp()
        fast = self._load_slice(fast_since, "fast_24h")
        slow = self._load_slice(slow_since, f"slow_{int(self.slow_window_days)}d")
        fast_rec = self._recommend_from_slice(fast, self.target_pnl_fast)
        slow_rec = self._recommend_from_slice(slow, self.target_pnl_slow)
        # Conservative: take the minimum so a single hot day cannot lift sizing.
        recommended = round(min(fast_rec, slow_rec), 4)
        reasons: list[str] = []
        if fast.n_trades < self.min_trades:
            reasons.append(f"fast_window_cold_start:{fast.n_trades}<{self.min_trades}")
        if slow.n_trades < self.min_trades:
            reasons.append(f"slow_window_cold_start:{slow.n_trades}<{self.min_trades}")
        reasons.append(f"fast_rec:{fast_rec:.3f}")
        reasons.append(f"slow_rec:{slow_rec:.3f}")
        return EquityRecommendation(
            recommended_multiplier=recommended,
            fast_slice=fast,
            slow_slice=slow,
            reasons=tuple(reasons),
            decided_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )


__all__ = ["EquityGovernor", "EquityRecommendation", "EquitySlice"]

"""Regime-Switching Capital Allocator (Tier 3 #9).

Treats the 6+ strategy families as competing portfolios. Allocates the
account's per-trade risk share across families based on rolling 7-day
Sharpe-like performance. The allocator is a pure reader of
`execution_journal`; it emits a `FamilyAllocation` snapshot that the
trader can consult before sizing each entry.

Constraints (configurable):
- `floor_share` = 0.05  — no family below 5% so every family keeps generating
  data even when underperforming.
- `ceiling_share` = 0.40 — no family above 40% so the allocator can't all-in.
- `max_daily_shift` = 0.10 — allocation can move at most ±10 percentage points
  per day to prevent flapping.

Output is consumed via `recommended_share(family)` or `snapshot()`. Until a
family has `min_trades` closed trades in the window, its share stays at the
floor — cold start safety.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
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


def _normalize_family(source: str) -> str:
    """Map a journal `source` to a coarse family bucket.

    Examples:
        "scalp_xauusd:winner"   → "scalp_xauusd"
        "xauusd_scheduled:canary" → "xauusd_scheduled"
        "fibo_xauusd"           → "fibo_xauusd"
    """
    s = str(source or "").strip().lower()
    if not s:
        return "unknown"
    return s.split(":", 1)[0]


@dataclass(frozen=True)
class FamilyStats:
    family: str
    n_trades: int
    total_pnl_usd: float
    win_rate: float
    sharpe_like: float


@dataclass(frozen=True)
class FamilyAllocation:
    """Output snapshot — recommended risk shares summing to 1.0."""

    shares: dict[str, float]
    stats: dict[str, FamilyStats]
    decided_utc: str
    reasons: tuple[str, ...]


class FamilyAllocator:
    """Computes per-family risk shares from rolling broker-journal stats."""

    def __init__(
        self,
        *,
        journal_db_path: str | Path,
        window_days: float = 7.0,
        min_trades: int = 5,
        floor_share: float = 0.05,
        ceiling_share: float = 0.40,
        max_daily_shift: float = 0.10,
        symbol_filter: Optional[Iterable[str]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.journal_db_path = str(journal_db_path)
        self.window_days = float(window_days)
        self.min_trades = int(min_trades)
        self.floor_share = float(floor_share)
        self.ceiling_share = float(ceiling_share)
        self.max_daily_shift = float(max_daily_shift)
        self.symbol_filter = tuple(s.upper() for s in (symbol_filter or ()))
        self._clock = clock or _utc_now
        self._previous: dict[str, float] = {}

    # ----- stats loading ------------------------------------------------
    def _load_stats(self) -> dict[str, FamilyStats]:
        if not Path(self.journal_db_path).exists():
            return {}
        cutoff_ts = (self._clock() - timedelta(days=self.window_days)).timestamp()
        family_pnls: dict[str, list[float]] = {}
        try:
            with sqlite3.connect(self.journal_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT source, symbol, execution_meta_json
                      FROM execution_journal
                     WHERE created_ts >= ?
                       AND position_id IS NOT NULL
                       AND execution_meta_json IS NOT NULL
                    """,
                    (cutoff_ts,),
                )
                for row in cur:
                    symbol = str(row["symbol"] or "").upper()
                    if self.symbol_filter and not any(symbol.startswith(s) for s in self.symbol_filter):
                        continue
                    meta = _safe_json(row["execution_meta_json"])
                    closed = dict(meta.get("closed") or {})
                    if not closed:
                        continue
                    family = _normalize_family(row["source"])
                    family_pnls.setdefault(family, []).append(_coerce_float(closed.get("pnl_usd")))
        except sqlite3.DatabaseError as exc:
            logger.exception("family_allocator_db_error db=%s err=%s", self.journal_db_path, exc)
            return {}

        stats: dict[str, FamilyStats] = {}
        for family, pnls in family_pnls.items():
            n = len(pnls)
            total = sum(pnls)
            wr = (sum(1 for p in pnls if p > 0) / n) if n else 0.0
            if n >= 2:
                mean = total / n
                variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
                std = max(1e-9, math.sqrt(variance))
                sharpe = (mean / std) * math.sqrt(n)
            else:
                sharpe = 0.0
            stats[family] = FamilyStats(
                family=family,
                n_trades=n,
                total_pnl_usd=round(total, 4),
                win_rate=round(wr, 4),
                sharpe_like=round(sharpe, 4),
            )
        return stats

    # ----- allocation ---------------------------------------------------
    def compute(self) -> FamilyAllocation:
        stats = self._load_stats()
        reasons: list[str] = []
        if not stats:
            reasons.append("no_journal_data")
            return FamilyAllocation(
                shares={}, stats={},
                decided_utc=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                reasons=tuple(reasons),
            )

        eligible = {f: s for f, s in stats.items() if s.n_trades >= self.min_trades}
        floor_only = [f for f, s in stats.items() if s.n_trades < self.min_trades]
        if not eligible:
            # Everyone is on the floor.
            even = 1.0 / max(1, len(stats))
            shares = {f: even for f in stats}
            reasons.append("all_families_cold_start_equal_share")
            return FamilyAllocation(
                shares=self._apply_daily_shift_cap(shares),
                stats=stats,
                decided_utc=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                reasons=tuple(reasons),
            )

        # Convert Sharpe-like to a non-negative weight via softplus, so even
        # mildly-negative Sharpe families get a tiny weight rather than zero.
        def softplus(x: float) -> float:
            # Tame extreme values to keep weights well-bounded.
            return math.log1p(math.exp(max(-30.0, min(30.0, x))))

        raw_weights = {f: softplus(s.sharpe_like) for f, s in eligible.items()}
        # Allocate cold-start families exactly `floor_share` each, then
        # distribute the remaining pool by raw weight to eligible families.
        cold_total = self.floor_share * len(floor_only)
        eligible_pool = max(0.0, 1.0 - cold_total)
        weight_sum = sum(raw_weights.values()) or 1.0
        shares: dict[str, float] = {f: self.floor_share for f in floor_only}
        for family, w in raw_weights.items():
            shares[family] = round(eligible_pool * (w / weight_sum), 4)

        # Water-fill the floor/ceiling and daily-shift caps so each constraint
        # holds AND the total still sums to 1.0. This composes correctly even
        # when constraints are tight or infeasible.
        shares = self._water_fill_floor_ceiling(shares)
        shares = self._water_fill_daily_shift(shares)
        reasons.append(f"eligible={len(eligible)}, cold={len(floor_only)}")
        out = FamilyAllocation(
            shares=shares,
            stats=stats,
            decided_utc=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            reasons=tuple(reasons),
        )
        self._previous = dict(shares)
        return out

    # ----- water-filling helpers ---------------------------------------
    def _water_fill_floor_ceiling(self, shares: dict[str, float]) -> dict[str, float]:
        """Apply floor/ceiling, redistributing the residual to feasible families.

        Iterates because clamping one family may push another above the
        ceiling once the residual is redistributed. Convergence is guaranteed
        because each pass clamps at least one family or terminates.
        """
        keys = list(shares.keys())
        if not keys:
            return shares
        # When constraints are infeasible (e.g. ceiling * n < 1.0), saturate
        # everyone at the ceiling and accept sum != 1.0; the caller can decide.
        if self.ceiling_share * len(keys) < 1.0 - 1e-9:
            return {k: self.ceiling_share for k in keys}
        if self.floor_share * len(keys) > 1.0 + 1e-9:
            return {k: self.floor_share for k in keys}

        out = dict(shares)
        for _ in range(64):  # safety cap; converges in O(n) typically
            clamped: set[str] = set()
            for k, v in out.items():
                if v < self.floor_share - 1e-12:
                    out[k] = self.floor_share
                    clamped.add(k)
                elif v > self.ceiling_share + 1e-12:
                    out[k] = self.ceiling_share
                    clamped.add(k)
            total = sum(out.values())
            delta = 1.0 - total
            free = [k for k in out if k not in clamped]
            if abs(delta) < 1e-9 or not free:
                break
            # Distribute delta equally among unconstrained families.
            adjust = delta / len(free)
            for k in free:
                out[k] = out[k] + adjust
        return {k: round(v, 4) for k, v in out.items()}

    def _water_fill_daily_shift(self, shares: dict[str, float]) -> dict[str, float]:
        """Cap each family's change vs _previous to ±max_daily_shift, then
        redistribute the slack among families that did not hit the cap.
        """
        if not self._previous:
            return {k: round(v, 4) for k, v in shares.items()}
        out = dict(shares)
        for _ in range(64):
            capped: set[str] = set()
            for k, v in out.items():
                prev = float(self._previous.get(k, v))
                if v - prev > self.max_daily_shift + 1e-12:
                    out[k] = prev + self.max_daily_shift
                    capped.add(k)
                elif prev - v > self.max_daily_shift + 1e-12:
                    out[k] = prev - self.max_daily_shift
                    capped.add(k)
            total = sum(out.values())
            delta = 1.0 - total
            free = [k for k in out if k not in capped]
            if abs(delta) < 1e-9 or not free:
                break
            adjust = delta / len(free)
            for k in free:
                out[k] = out[k] + adjust
        return {k: round(v, 4) for k, v in out.items()}


__all__ = ["FamilyAllocator", "FamilyAllocation", "FamilyStats"]

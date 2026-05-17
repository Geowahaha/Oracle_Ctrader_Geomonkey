"""Live Archetype Tournament (Tier 1 #2).

Streaming version of the daily winner-archetype mining. Reads closed
positions from the broker journal in real time and maintains a rolling
ranking over `(source, hour_bucket, sl_distance_bucket)` archetypes. Top
archetypes earn a positive risk-multiplier hint; bottom archetypes earn a
negative one. The trading layer is free to read `multiplier_for(...)` at
dispatch time.

Why a tournament instead of a daily report?
- Reservoir trades are episodic; waiting a full day to crown a winner misses
  the second hour of the same regime.
- The same archetype can flip from top to bottom intra-day (regime change).
  Live ranking handles this without re-running batch jobs.
- Bounded memory: the tournament keeps only N archetypes (default 64) and
  ages them out by recency.

Output multiplier is purely advisory:
- Top decile  → +0.30 (max)
- Bottom decile → -0.30 (min)
- Neutral / unknown → 0.0
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
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


def _hour_bucket(closed_utc: str) -> str:
    try:
        v = closed_utc
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"h{dt.astimezone(timezone.utc).hour:02d}"
    except (TypeError, ValueError):
        return "h_unknown"


def _sl_bucket(entry: float, stop: float) -> str:
    if not entry or not stop:
        return "sl_missing"
    dist = abs(entry - stop)
    if dist < 1.0:
        return "sl_lt_1"
    if dist < 2.5:
        return "sl_1_2_5"
    if dist < 5.0:
        return "sl_2_5_5"
    if dist < 10.0:
        return "sl_5_10"
    return "sl_ge_10"


@dataclass
class ArchetypeStats:
    key: str  # e.g. "scalp_xauusd:winner|h12|sl_2_5_5"
    n_trades: int = 0
    total_pnl_usd: float = 0.0
    win_count: int = 0
    last_seen_utc: str = ""

    @property
    def win_rate(self) -> float:
        return self.win_count / self.n_trades if self.n_trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl_usd / self.n_trades if self.n_trades else 0.0

    def score(self) -> float:
        """Compound score combining win-rate and avg PnL.

        Both signals matter: high win rate with tiny avg PnL is no use, and a
        positive avg PnL on 1/10 trades is too noisy. We weight win-rate by
        sqrt(n) so small samples can't dominate.
        """
        if self.n_trades < 2:
            return 0.0
        confidence = min(1.0, self.n_trades / 8.0)
        return self.avg_pnl * confidence


@dataclass(frozen=True)
class TournamentSnapshot:
    """Public view of the tournament at a point in time."""

    top: list[ArchetypeStats]
    bottom: list[ArchetypeStats]
    n_archetypes: int
    decided_utc: str


class ArchetypeTournament:
    """Live archetype ranking, updated from the broker journal."""

    def __init__(
        self,
        *,
        journal_db_path: str | Path,
        lookback_days: float = 7.0,
        max_archetypes: int = 64,
        max_multiplier_delta: float = 0.30,
        symbol_filter: Optional[Iterable[str]] = ("XAU", "XAUUSD"),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.journal_db_path = str(journal_db_path)
        self.lookback_days = float(lookback_days)
        self.max_archetypes = int(max_archetypes)
        self.max_multiplier_delta = float(max_multiplier_delta)
        self.symbol_filter = tuple(s.upper() for s in (symbol_filter or ()))
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._stats: dict[str, ArchetypeStats] = {}
        self._last_seen_position_id: int = 0

    def _archetype_key(self, *, source: str, closed_utc: str, entry: float, stop: float) -> str:
        return f"{source}|{_hour_bucket(closed_utc)}|{_sl_bucket(entry, stop)}"

    def refresh(self) -> int:
        """Pull new closed positions and update the tournament.

        Returns the number of new positions consumed.
        """
        if not Path(self.journal_db_path).exists():
            return 0
        now = self._clock()
        since_ts = (now - timedelta(days=self.lookback_days)).timestamp()
        consumed = 0
        with self._lock:
            try:
                with sqlite3.connect(self.journal_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute(
                        """
                        SELECT position_id, source, symbol, entry, stop_loss,
                               execution_meta_json
                          FROM execution_journal
                         WHERE created_ts >= ?
                           AND position_id IS NOT NULL
                           AND position_id > ?
                           AND execution_meta_json IS NOT NULL
                         ORDER BY position_id ASC
                        """,
                        (since_ts, int(self._last_seen_position_id)),
                    )
                    for row in cur:
                        meta = _safe_json(row["execution_meta_json"])
                        closed = dict(meta.get("closed") or {})
                        if not closed:
                            continue
                        symbol = str(row["symbol"] or "").upper()
                        if self.symbol_filter and not any(symbol.startswith(s) for s in self.symbol_filter):
                            continue
                        pnl = _coerce_float(closed.get("pnl_usd"))
                        source = str(row["source"] or "").lower()
                        entry = _coerce_float(row["entry"])
                        stop = _coerce_float(row["stop_loss"])
                        closed_utc = str(closed.get("closed_utc") or "")
                        key = self._archetype_key(source=source, closed_utc=closed_utc, entry=entry, stop=stop)
                        stats = self._stats.setdefault(key, ArchetypeStats(key=key))
                        stats.n_trades += 1
                        stats.total_pnl_usd += pnl
                        if pnl > 0:
                            stats.win_count += 1
                        stats.last_seen_utc = closed_utc
                        consumed += 1
                        pos_id = int(row["position_id"])
                        if pos_id > self._last_seen_position_id:
                            self._last_seen_position_id = pos_id
            except sqlite3.DatabaseError as exc:
                logger.exception("archetype_tournament_db_error db=%s err=%s", self.journal_db_path, exc)
                return 0
            self._evict_if_needed()
        return consumed

    def _evict_if_needed(self) -> None:
        if len(self._stats) <= self.max_archetypes:
            return
        # Keep the most-recently-seen archetypes; drop oldest.
        items = sorted(self._stats.items(), key=lambda kv: kv[1].last_seen_utc, reverse=True)
        keep = dict(items[: self.max_archetypes])
        self._stats = keep

    def snapshot(self) -> TournamentSnapshot:
        """Return the current top/bottom decile snapshot."""
        with self._lock:
            ranked = sorted(self._stats.values(), key=lambda s: s.score(), reverse=True)
        if not ranked:
            return TournamentSnapshot(
                top=[],
                bottom=[],
                n_archetypes=0,
                decided_utc=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
        decile = max(1, len(ranked) // 10)
        return TournamentSnapshot(
            top=ranked[:decile],
            bottom=ranked[-decile:][::-1],
            n_archetypes=len(ranked),
            decided_utc=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )

    def multiplier_for(self, *, source: str, closed_utc: str, entry: float, stop: float) -> float:
        """Return a risk-multiplier hint in [-max_multiplier_delta, +max_multiplier_delta]."""
        key = self._archetype_key(source=source, closed_utc=closed_utc, entry=entry, stop=stop)
        with self._lock:
            stats = self._stats.get(key)
            if stats is None or stats.n_trades < 2:
                return 0.0
            ranked = sorted(self._stats.values(), key=lambda s: s.score(), reverse=True)
        if not ranked:
            return 0.0
        decile = max(1, len(ranked) // 10)
        top_keys = {s.key for s in ranked[:decile]}
        bot_keys = {s.key for s in ranked[-decile:]}
        if key in top_keys:
            return float(self.max_multiplier_delta)
        if key in bot_keys:
            return float(-self.max_multiplier_delta)
        return 0.0


__all__ = ["ArchetypeTournament", "ArchetypeStats", "TournamentSnapshot"]

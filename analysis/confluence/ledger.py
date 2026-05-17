"""Rolling per-(symbol, side) vote ledger for the Confluence Booster.

Each family that issues a signal records a vote. Votes age out of the rolling
window automatically. The ledger is thread-safe and pure-Python (no SQLite —
volatile state, recomputed every process restart, which is desirable for a
short window).
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Iterable, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _norm_side(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class Vote:
    family: str
    symbol: str
    side: str
    cast_utc: datetime


@dataclass(frozen=True)
class VoteCount:
    long: int
    short: int
    families_long: tuple[str, ...]
    families_short: tuple[str, ...]


class ConfluenceLedger:
    """Per-(symbol, side) rolling vote ledger.

    Each `record_vote` is O(1); each `count_for` is O(n) where n is the number
    of votes in the symbol's window. With <10 families voting at most every
    few seconds, n stays tiny — no need for a fancy data structure.

    Same `(family, symbol, side)` vote within the dedupe window is collapsed
    to one — a family that re-fires a long signal twice in the window still
    counts once toward consensus.
    """

    def __init__(
        self,
        *,
        window_minutes: float = 10.0,
        dedupe_seconds: float = 60.0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.window = timedelta(minutes=float(window_minutes))
        self.dedupe = timedelta(seconds=float(dedupe_seconds))
        self._clock = clock or _utc_now
        self._votes: dict[str, Deque[Vote]] = defaultdict(deque)
        self._lock = threading.Lock()

    def record_vote(self, *, family: str, symbol: str, side: str) -> bool:
        """Record one vote. Returns True if accepted, False if deduped."""
        symbol_n = _norm_symbol(symbol)
        side_n = _norm_side(side)
        if not symbol_n or side_n not in {"long", "short"} or not family:
            return False
        now = self._clock()
        with self._lock:
            queue = self._votes[symbol_n]
            self._evict(queue, now)
            # Dedupe: same family + symbol + side within dedupe window.
            for existing in reversed(queue):
                if (now - existing.cast_utc) > self.dedupe:
                    break
                if existing.family == family and existing.side == side_n:
                    return False
            queue.append(Vote(family=family, symbol=symbol_n, side=side_n, cast_utc=now))
            return True

    def count_for(self, *, symbol: str, now: Optional[datetime] = None) -> VoteCount:
        symbol_n = _norm_symbol(symbol)
        now = now or self._clock()
        with self._lock:
            queue = self._votes.get(symbol_n)
            if not queue:
                return VoteCount(0, 0, (), ())
            self._evict(queue, now)
            longs: set[str] = set()
            shorts: set[str] = set()
            for vote in queue:
                if vote.side == "long":
                    longs.add(vote.family)
                elif vote.side == "short":
                    shorts.add(vote.family)
        return VoteCount(
            long=len(longs),
            short=len(shorts),
            families_long=tuple(sorted(longs)),
            families_short=tuple(sorted(shorts)),
        )

    def reset(self, symbol: Optional[str] = None) -> None:
        with self._lock:
            if symbol is None:
                self._votes.clear()
            else:
                self._votes.pop(_norm_symbol(symbol), None)

    # ----- internal -----------------------------------------------------
    def _evict(self, queue: Deque[Vote], now: datetime) -> None:
        cutoff = now - self.window
        while queue and queue[0].cast_utc < cutoff:
            queue.popleft()


__all__ = ["ConfluenceLedger", "Vote", "VoteCount"]

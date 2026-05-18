"""Missed Opportunity Detector implementation.

Algorithm:
  1. Pull closed positions from the broker journal in the last `lookback_hours`.
  2. For each closed position with a positive PnL (winners only — the scope
     here is "early exit on a runner that kept running"), look at the price
     `evaluation_minutes_after_close` minutes after the close timestamp.
  3. Compute:
        captured     = |exit_price - entry_price|
        continuation = |post_exit_price - exit_price|
        if continuation_aligned_with_direction:
            missed_factor = continuation / max(captured, 1e-9)
        else:
            missed_factor = 0.0
  4. When `missed_factor >= min_missed_factor`, record a `MissedRunnerEvent`.

Edge cases:
- Losing trades are NOT scanned — those are handled by the loss-event trigger.
- "open?" / canceled trades are skipped.
- Trades younger than `evaluation_minutes_after_close` are skipped — we
  haven't yet seen enough continuation.
- The same `position_id` is recorded once (idempotent).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_json(v: object) -> dict:
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        return json.loads(str(v) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _coerce_float(v: object, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_dir(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in {"buy", "long"}:
        return "long"
    if v in {"sell", "short"}:
        return "short"
    return v


@dataclass(frozen=True)
class MissedRunnerEvent:
    position_id: int
    symbol: str
    direction: str
    source: str
    entry_price: float
    exit_price: float
    captured_pts: float
    continuation_pts: float
    missed_factor: float       # continuation / captured
    closed_utc: str
    detected_utc: str
    notes: str = ""


class MissedRunnerStore:
    """Tiny SQLite store for detected missed-runner events."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_runners (
                    position_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    source TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    captured_pts REAL NOT NULL,
                    continuation_pts REAL NOT NULL,
                    missed_factor REAL NOT NULL,
                    closed_utc TEXT NOT NULL,
                    detected_utc TEXT NOT NULL,
                    notes TEXT DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_closed ON missed_runners(closed_utc DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_factor ON missed_runners(missed_factor DESC)")

    def record(self, event: MissedRunnerEvent) -> bool:
        """Insert; returns True when this is a new event."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO missed_runners(
                    position_id, symbol, direction, source, entry_price, exit_price,
                    captured_pts, continuation_pts, missed_factor,
                    closed_utc, detected_utc, notes
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(event.position_id),
                    event.symbol,
                    event.direction,
                    event.source,
                    float(event.entry_price),
                    float(event.exit_price),
                    float(event.captured_pts),
                    float(event.continuation_pts),
                    float(event.missed_factor),
                    event.closed_utc,
                    event.detected_utc,
                    event.notes,
                ),
            )
            return cur.rowcount > 0

    def recent(self, limit: int = 20) -> list[MissedRunnerEvent]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM missed_runners ORDER BY closed_utc DESC LIMIT ?", (int(limit),),
            ).fetchall()
        out: list[MissedRunnerEvent] = []
        for r in rows:
            out.append(
                MissedRunnerEvent(
                    position_id=int(r["position_id"]),
                    symbol=str(r["symbol"]),
                    direction=str(r["direction"]),
                    source=str(r["source"]),
                    entry_price=float(r["entry_price"]),
                    exit_price=float(r["exit_price"]),
                    captured_pts=float(r["captured_pts"]),
                    continuation_pts=float(r["continuation_pts"]),
                    missed_factor=float(r["missed_factor"]),
                    closed_utc=str(r["closed_utc"]),
                    detected_utc=str(r["detected_utc"]),
                    notes=str(r["notes"] or ""),
                )
            )
        return out


class MissedOpportunityDetector:
    """Scans broker journal for runners that kept running after exit."""

    def __init__(
        self,
        *,
        journal_db_path: str | Path,
        store: MissedRunnerStore,
        lookback_hours: float = 24.0,
        evaluation_minutes_after_close: float = 60.0,
        min_missed_factor: float = 3.0,
        min_captured_pts: float = 0.5,
        symbol_filter: Optional[Iterable[str]] = ("XAU", "XAUUSD"),
    ) -> None:
        self.journal_db_path = str(journal_db_path)
        self.store = store
        self.lookback_hours = float(lookback_hours)
        self.evaluation_minutes_after_close = float(evaluation_minutes_after_close)
        self.min_missed_factor = float(min_missed_factor)
        self.min_captured_pts = float(min_captured_pts)
        self.symbol_filter = tuple(s.upper() for s in (symbol_filter or ()))

    # ----- main entry --------------------------------------------------
    def scan(self, *, now: Optional[datetime] = None) -> list[MissedRunnerEvent]:
        if not Path(self.journal_db_path).exists():
            return []
        now = now or datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(hours=self.lookback_hours)).timestamp()
        eval_cutoff = now - timedelta(minutes=self.evaluation_minutes_after_close)
        eval_cutoff_iso = eval_cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        emitted: list[MissedRunnerEvent] = []
        try:
            with sqlite3.connect(self.journal_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT position_id, source, symbol, direction, entry,
                           execution_meta_json, created_ts, created_utc
                      FROM execution_journal
                     WHERE created_ts >= ?
                       AND position_id IS NOT NULL
                       AND execution_meta_json IS NOT NULL
                     ORDER BY id ASC
                    """,
                    (cutoff_ts,),
                )
                rows = list(cur)
        except sqlite3.DatabaseError as exc:
            logger.exception("missed_opportunity_db_error db=%s err=%s", self.journal_db_path, exc)
            return []

        for row in rows:
            pos_id = row["position_id"]
            if not pos_id:
                continue
            symbol = str(row["symbol"] or "").upper()
            if self.symbol_filter and not any(symbol.startswith(s) for s in self.symbol_filter):
                continue
            meta = _safe_json(row["execution_meta_json"])
            closed = dict(meta.get("closed") or {})
            if not closed:
                continue
            closed_utc = str(closed.get("closed_utc") or "")
            if not closed_utc or closed_utc > eval_cutoff_iso:
                # Not enough time since close to evaluate continuation.
                continue
            pnl = _coerce_float(closed.get("pnl_usd"))
            if pnl <= 0:
                continue  # losers go through the loss-event trigger
            entry = _coerce_float(row["entry"])
            exit_price = _coerce_float(closed.get("exit_price"))
            if not entry or not exit_price:
                continue
            direction = _norm_dir(row["direction"])
            captured = abs(exit_price - entry)
            if captured < self.min_captured_pts:
                continue

            post_price = _coerce_float(closed.get("post_close_price_60m"))
            if not post_price:
                # No 60m-after price stamped — best-effort: skip silently.
                continue
            if direction == "short":
                continuation = exit_price - post_price
            elif direction == "long":
                continuation = post_price - exit_price
            else:
                continue
            if continuation <= 0:
                continue
            missed_factor = continuation / max(captured, 1e-9)
            if missed_factor < self.min_missed_factor:
                continue

            event = MissedRunnerEvent(
                position_id=int(pos_id),
                symbol=symbol,
                direction=direction,
                source=str(row["source"] or ""),
                entry_price=entry,
                exit_price=exit_price,
                captured_pts=round(captured, 4),
                continuation_pts=round(continuation, 4),
                missed_factor=round(missed_factor, 4),
                closed_utc=closed_utc,
                detected_utc=_utc_now_iso(),
                notes=f"pnl_usd={pnl:.2f}",
            )
            if self.store.record(event):
                emitted.append(event)
        return emitted


__all__ = ["MissedOpportunityDetector", "MissedRunnerEvent", "MissedRunnerStore"]

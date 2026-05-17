"""Loss-event trigger — observes the broker journal and emits LossEvents.

The trigger is read-only with respect to the trading system. It polls the
cTrader `execution_journal` table for closed positions where the realised PnL
is below `-loss_threshold_usd`, deduplicates against the Self-Mutation ledger,
and returns the new events for the governor to process.

The trigger never opens orders, never writes to the trading journal, and never
blocks. It is safe to call from any context (cron, ad-hoc, tests).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from learning.self_mutation.ledger import Ledger
from learning.self_mutation.types import LossEvent

logger = logging.getLogger(__name__)


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


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


class LossEventTrigger:
    """Polls the cTrader journal for losses worth investigating."""

    def __init__(
        self,
        *,
        journal_db_path: str | Path,
        ledger: Ledger,
        loss_threshold_usd: float = 20.0,
        lookback_hours: float = 24.0,
        symbol_filter: Optional[Iterable[str]] = ("XAU", "XAUUSD"),
    ) -> None:
        self.journal_db_path = str(journal_db_path)
        self.ledger = ledger
        self.loss_threshold_usd = float(loss_threshold_usd)
        self.lookback_hours = float(lookback_hours)
        self.symbol_filter = tuple(_normalize_symbol(s) for s in (symbol_filter or ()))

    def fetch_new_loss_events(self, *, now: Optional[datetime] = None) -> list[LossEvent]:
        """Return new (un-recorded) loss events from the journal."""
        if not Path(self.journal_db_path).exists():
            return []
        now = now or datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(hours=self.lookback_hours)).timestamp()
        events: list[LossEvent] = []
        try:
            with sqlite3.connect(self.journal_db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT created_ts, source, symbol, direction, signal_run_id,
                           position_id, request_json, execution_meta_json
                      FROM execution_journal
                     WHERE created_ts >= ?
                       AND position_id IS NOT NULL
                       AND execution_meta_json IS NOT NULL
                     ORDER BY created_ts ASC
                    """,
                    (cutoff_ts,),
                )
                rows = list(cur)
        except sqlite3.DatabaseError as exc:
            logger.exception("self_mutation_trigger_db_error db=%s err=%s", self.journal_db_path, exc)
            return []

        seen: set[int] = set()
        for row in rows:
            pos_id = row["position_id"]
            if not pos_id or int(pos_id) in seen:
                continue
            seen.add(int(pos_id))
            meta = _safe_json(row["execution_meta_json"])
            closed = dict(meta.get("closed") or {})
            if not closed:
                continue
            pnl = _coerce_float(closed.get("pnl_usd"))
            if pnl > -self.loss_threshold_usd:
                continue
            symbol = _normalize_symbol(row["symbol"])
            if self.symbol_filter and not any(symbol.startswith(s) for s in self.symbol_filter):
                continue
            if self.ledger.has_loss_event(int(pos_id)):
                continue
            request = _safe_json(row["request_json"])
            raw_scores = dict(request.get("raw_scores") or meta.get("raw_scores") or {})
            router = dict(raw_scores.get("xau_openapi_entry_router") or {})
            raw_meta = {
                "mode": str(router.get("mode") or raw_scores.get("mode") or ""),
                "entry_type": str(request.get("entry_type") or "").lower(),
                "continuation_score": _coerce_float(router.get("continuation_score") or raw_scores.get("continuation_score")),
                "continuation_bias": _coerce_float(router.get("continuation_bias") or raw_scores.get("continuation_bias")),
                "mfe_r": _coerce_float(closed.get("mfe_r")),
                "xau_pair_risk_cap_applied": bool(raw_scores.get("xau_pair_risk_cap_applied")),
            }
            events.append(
                LossEvent(
                    position_id=int(pos_id),
                    source=str(row["source"] or "").lower(),
                    symbol=symbol,
                    direction=str(row["direction"] or "").lower(),
                    pnl_usd=pnl,
                    closed_utc=str(closed.get("closed_utc") or ""),
                    signal_run_id=str(row["signal_run_id"] or ""),
                    raw_meta=raw_meta,
                )
            )
        return events


__all__ = ["LossEventTrigger"]

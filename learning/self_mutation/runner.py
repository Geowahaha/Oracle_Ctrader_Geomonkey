"""Counterfactual backtest runner.

Replays the broker `execution_journal` over a lookback window and answers
"what would have happened if knob X had been set to value Y?" — without
placing any orders.

The runner deliberately implements a small, exact set of counterfactual
transforms (one per knob family). When a knob's effect is too entangled with
live execution to be modelled from journal rows alone, the runner returns
`KIND_NEEDS_PTS` and the Verdict engine treats the mutation as inconclusive.

Why counterfactual replay instead of a real backtester?
- It uses the *same data the system traded on*, so the verdict reflects the
  operator's actual exposure, not a synthetic market.
- It runs in milliseconds, so the Self-Mutation Loop can react to every loss.
- It is reproducible — given the same journal rows, the output is deterministic.

Limitations are explicit: knobs that change strategy/route/entry placement need
the real PTS backtester (out of scope for v1) and are routed to `needs_pts`.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from learning.self_mutation.types import (
    KIND_ERROR,
    KIND_INSUFFICIENT_DATA,
    KIND_NEEDS_PTS,
    KIND_OK,
    BacktestOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Knob classification — how each whitelisted knob maps to a counterfactual.
# ---------------------------------------------------------------------------

RISK_MULTIPLIER_KNOBS = frozenset({
    "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER",
    "XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO",
    "RISK_PER_TRADE",
})

THRESHOLD_FILTER_KNOBS = frozenset({
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE",
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS",
})

PAIR_RISK_CAP_KNOBS = frozenset({
    "CTRADER_XAU_PAIR_RISK_MAX_USD",
})

NEEDS_PTS_KNOBS = frozenset({
    "XAU_GUARDIAN_RUNNER_PRESERVE_R",
})


# Mapping of threshold knobs to the raw_scores field they should be compared with.
THRESHOLD_FIELD = {
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE": "continuation_score",
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS": "continuation_bias",
}

# Mode tag identifying trades that flow through the gated path.
THRESHOLD_MODE_TAG = {
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE": "signal_market",
    "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS": "signal_market",
}

# Mode tag for risk-multiplier knobs that only apply to specific entry paths.
RISK_MULTIPLIER_MODE_TAG = {
    "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER": "wait_break_probe_stop",
    "XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO": None,  # all limit retests; entry_type=limit
}


# ---------------------------------------------------------------------------
# Internal row shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JournalRow:
    """Compact representation of a closed broker journal row used by the runner."""

    position_id: int
    created_ts: float
    symbol: str
    direction: str
    source: str
    entry_type: str
    mode: str
    realized_pnl_usd: float
    continuation_score: float
    continuation_bias: float
    risk_usd: float
    pair_risk_cap_applied: bool


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


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def _extract_row(raw: sqlite3.Row | dict) -> JournalRow | None:
    """Parse a journal row into JournalRow, or None if it's unclosed/invalid."""
    request = _safe_json(raw["request_json"] if "request_json" in raw.keys() else raw.get("request_json"))
    meta = _safe_json(raw["execution_meta_json"] if "execution_meta_json" in raw.keys() else raw.get("execution_meta_json"))
    closed = dict(meta.get("closed") or {})
    if not closed:
        return None
    pnl = closed.get("pnl_usd")
    if pnl is None:
        return None
    raw_scores = dict(request.get("raw_scores") or meta.get("raw_scores") or {})
    router = dict(raw_scores.get("xau_openapi_entry_router") or {})
    mode = str(router.get("mode") or raw_scores.get("mode") or "").strip().lower()
    pos_id = raw["position_id"] if "position_id" in raw.keys() else raw.get("position_id")
    if not pos_id:
        return None
    return JournalRow(
        position_id=int(pos_id),
        created_ts=_coerce_float(raw["created_ts"] if "created_ts" in raw.keys() else raw.get("created_ts")),
        symbol=str(raw["symbol"] if "symbol" in raw.keys() else raw.get("symbol") or "").upper(),
        direction=str(raw["direction"] if "direction" in raw.keys() else raw.get("direction") or "").lower(),
        source=str(raw["source"] if "source" in raw.keys() else raw.get("source") or "").lower(),
        entry_type=str(raw["entry_type"] if "entry_type" in raw.keys() else raw.get("entry_type") or "").lower(),
        mode=mode,
        realized_pnl_usd=_coerce_float(pnl),
        continuation_score=_coerce_float(router.get("continuation_score") or raw_scores.get("continuation_score")),
        continuation_bias=_coerce_float(router.get("continuation_bias") or raw_scores.get("continuation_bias")),
        risk_usd=_coerce_float(request.get("risk_usd") or raw_scores.get("risk_usd")),
        pair_risk_cap_applied=_coerce_bool(raw_scores.get("xau_pair_risk_cap_applied")),
    )


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate(pnls: list[float]) -> BacktestOutcome:
    if not pnls:
        return BacktestOutcome.empty(KIND_INSUFFICIENT_DATA, reason="no_closed_trades")
    n = len(pnls)
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    win_rate = wins / n if n else 0.0
    avg = total / n if n else 0.0
    # Cumulative max-drawdown over the equity curve, in USD.
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    # Std of per-trade PnL (sample).
    if n > 1:
        mean = avg
        variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(max(0.0, variance))
    else:
        std = 0.0
    return BacktestOutcome(
        kind=KIND_OK,
        n_trades=n,
        total_pnl_usd=round(total, 4),
        win_count=wins,
        loss_count=losses,
        win_rate=round(win_rate, 4),
        max_drawdown_usd=round(max_dd, 4),
        avg_pnl_per_trade=round(avg, 4),
        pnl_std=round(std, 4),
    )


# ---------------------------------------------------------------------------
# Counterfactual transforms
# ---------------------------------------------------------------------------

def _applies_risk_multiplier(row: JournalRow, knob: str) -> bool:
    mode_tag = RISK_MULTIPLIER_MODE_TAG.get(knob)
    if mode_tag is None:
        # Knob applies to every row in scope (e.g. RISK_PER_TRADE globally or
        # any limit-retest row when scoped by entry_type below).
        if knob == "XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO":
            return row.symbol.startswith("XAU") and row.entry_type == "limit"
        if knob == "RISK_PER_TRADE":
            return True
        return False
    return row.mode == mode_tag


def _applies_threshold_filter(row: JournalRow, knob: str) -> bool:
    mode_tag = THRESHOLD_MODE_TAG.get(knob)
    return bool(mode_tag) and row.mode == mode_tag


def _applies_pair_risk_cap(row: JournalRow, knob: str) -> bool:
    return row.pair_risk_cap_applied and row.symbol.startswith("XAU")


def _transform_pnl_for_mutation(
    row: JournalRow,
    knob: str,
    baseline: float,
    proposed: float,
) -> tuple[float, bool]:
    """Apply the counterfactual transform.

    Returns `(new_pnl_usd, included)`. `included=False` means the mutation
    effectively removes this trade (filtered out by a threshold). The aggregator
    treats such rows as if they did not happen.
    """
    if knob in RISK_MULTIPLIER_KNOBS:
        if not _applies_risk_multiplier(row, knob):
            return row.realized_pnl_usd, True
        if baseline <= 0:
            return row.realized_pnl_usd, True
        scale = float(proposed) / float(baseline)
        return row.realized_pnl_usd * scale, True

    if knob in THRESHOLD_FILTER_KNOBS:
        if not _applies_threshold_filter(row, knob):
            return row.realized_pnl_usd, True
        field = THRESHOLD_FIELD.get(knob, "")
        observed = getattr(row, field, 0.0) if field else 0.0
        # Trade only happens under mutation if observed >= proposed threshold.
        if observed >= float(proposed):
            return row.realized_pnl_usd, True
        return 0.0, False

    if knob in PAIR_RISK_CAP_KNOBS:
        if not _applies_pair_risk_cap(row, knob):
            return row.realized_pnl_usd, True
        # When the cap was applied, the trade's effective risk was capped by
        # baseline. Under mutation, the cap is `proposed`. PnL scales by the
        # cap ratio (a lower cap reduces both win and loss magnitude).
        if baseline <= 0:
            return row.realized_pnl_usd, True
        scale = float(proposed) / float(baseline)
        return row.realized_pnl_usd * scale, True

    # Anything else: we cannot model — caller should not have reached here.
    return row.realized_pnl_usd, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CounterfactualRunner:
    """Runs counterfactual backtests against the cTrader execution journal."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        lookback_days: int = 14,
        symbol_filter: Optional[Iterable[str]] = ("XAU", "XAUUSD"),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db_path = str(db_path)
        self.lookback_days = max(1, int(lookback_days))
        self.symbol_filter = tuple(s.upper() for s in (symbol_filter or ()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ----- row loading --------------------------------------------------
    def _load_rows(self) -> list[JournalRow]:
        if not Path(self.db_path).exists():
            return []
        now = self._clock()
        cutoff_ts = (now - timedelta(days=self.lookback_days)).timestamp()
        rows: list[JournalRow] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT created_ts, source, symbol, direction, entry_type,
                           request_json, execution_meta_json, position_id
                      FROM execution_journal
                     WHERE created_ts >= ?
                       AND position_id IS NOT NULL
                       AND execution_meta_json IS NOT NULL
                     ORDER BY created_ts ASC
                    """,
                    (cutoff_ts,),
                )
                for raw in cur:
                    row = _extract_row(raw)
                    if row is None:
                        continue
                    if self.symbol_filter and not any(row.symbol.startswith(s) for s in self.symbol_filter):
                        continue
                    rows.append(row)
        except sqlite3.DatabaseError as exc:
            logger.exception("self_mutation_runner_db_error db=%s err=%s", self.db_path, exc)
            return []
        # Deduplicate by position_id keeping latest row (closed event).
        deduped: dict[int, JournalRow] = {}
        for row in rows:
            deduped[row.position_id] = row
        return list(deduped.values())

    # ----- baseline + mutation outcomes ---------------------------------
    def run_baseline(self) -> BacktestOutcome:
        rows = self._load_rows()
        return _aggregate([r.realized_pnl_usd for r in rows])

    def run_mutation(
        self,
        knob: str,
        baseline_value: float,
        proposed_value: float,
        *,
        rows: Optional[list[JournalRow]] = None,
    ) -> BacktestOutcome:
        if knob in NEEDS_PTS_KNOBS:
            return BacktestOutcome.empty(KIND_NEEDS_PTS, reason=f"knob_requires_pts:{knob}")
        if knob not in RISK_MULTIPLIER_KNOBS | THRESHOLD_FILTER_KNOBS | PAIR_RISK_CAP_KNOBS:
            return BacktestOutcome.empty(KIND_ERROR, reason=f"knob_unknown_to_runner:{knob}")
        if rows is None:
            rows = self._load_rows()
        new_pnls: list[float] = []
        for row in rows:
            new_pnl, included = _transform_pnl_for_mutation(row, knob, baseline_value, proposed_value)
            if included:
                new_pnls.append(new_pnl)
        return _aggregate(new_pnls)


__all__ = [
    "CounterfactualRunner",
    "JournalRow",
    "RISK_MULTIPLIER_KNOBS",
    "THRESHOLD_FILTER_KNOBS",
    "PAIR_RISK_CAP_KNOBS",
    "NEEDS_PTS_KNOBS",
]

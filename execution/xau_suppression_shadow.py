"""
execution/xau_suppression_shadow.py — Shadow audit logger for XAU
high-confidence suppression.

Purpose: produce evidence for the question "is winner-logic confidence
suppression killing profitable XAU opportunity?" WITHOUT modifying XAU
scanner logic, XAU signal scoring, XAU direct-lane gates, or any live
decision surface.

Design invariants:
- Pure append-only JSONL. Never reads or influences a decision.
- Symbol-scoped: XAU only. No-op for BTC / ETH / FX.
- Bounded growth via size-based rotation.
- All I/O wrapped — never raises into the caller.
- Feature-flagged via XAU_CONF_SUPPRESSION_SHADOW_ENABLED.

Consumers: ops/analyze_xau_suppression.py (offline, post-hoc) joins these
events by signal_run_id / timestamp to ctrader_deals to report per-regime
win rate + implied opportunity cost of the severe-regime hard-block.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_XAU_SYMBOLS = {"XAUUSD", "GOLD", "XAU"}

# Fields we copy verbatim from signal.raw_scores when present. Defined as a
# frozen list so the JSONL schema is stable across code evolutions.
_RAW_SCORE_FIELDS = (
    "winner_logic_enabled",
    "winner_logic_scope",
    "winner_logic_regime",
    "winner_logic_session",
    "winner_logic_resolved",
    "winner_logic_win_rate",
    "winner_logic_avg_pnl",
    "winner_logic_applied",
    "winner_logic_confidence_after",
    "winner_logic_rr_after",
    "high_confidence_bridge",
    "entry_sharpness_composite",
    "entry_sharpness_band",
)


def _log_path() -> Path:
    base = os.environ.get("XAU_CONF_SUPPRESSION_SHADOW_PATH", "").strip()
    if base:
        return Path(base)
    return Path("data/runtime/xau_conf_suppression_shadow.jsonl")


def _rotate_if_needed(path: Path, max_mb: float) -> None:
    try:
        if max_mb <= 0:
            return
        if not path.exists():
            return
        size_mb = path.stat().st_size / (1024.0 * 1024.0)
        if size_mb < max_mb:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rotated = path.with_name(f"{path.stem}.{ts}{path.suffix}")
        path.replace(rotated)
    except Exception:
        pass


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_xau(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in _XAU_SYMBOLS


def log_signal_event(
    *,
    enabled: bool,
    symbol: str,
    direction: str,
    source: str,
    confidence: float,
    entry: float,
    stop_loss: float,
    take_profit: float,
    raw_scores: Optional[Mapping[str, Any]],
    decision: str,
    reason: str = "",
    signal_run_id: str = "",
    signal_run_no: int = 0,
    pattern: str = "",
    session: str = "",
    max_file_mb: float = 10.0,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append one shadow event. Completely silent on any failure.

    `decision` values:
      - "arrived"   — signal reached executor, pre-gate decision
      - "executed"  — executor accepted and placed (real or dry-run)
      - "rejected"  — executor short-circuited (filtered / disabled / health)
      - "health_warn" — gate produced a warn-only signal
    """
    try:
        if not enabled:
            return
        if not _is_xau(symbol):
            return
        path = _log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        _rotate_if_needed(path, float(max_file_mb or 0.0))

        raw = dict(raw_scores or {})
        record = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": str(symbol or "").upper(),
            "direction": str(direction or "").lower(),
            "source": str(source or ""),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "confidence": round(_safe_float(confidence), 2),
            "entry": round(_safe_float(entry), 6),
            "stop_loss": round(_safe_float(stop_loss), 6),
            "take_profit": round(_safe_float(take_profit), 6),
            "pattern": str(pattern or ""),
            "session": str(session or ""),
            "signal_run_id": str(signal_run_id or ""),
            "signal_run_no": int(signal_run_no or 0),
            "raw": {k: raw.get(k) for k in _RAW_SCORE_FIELDS if k in raw},
        }
        if extra:
            try:
                record["extra"] = dict(extra)
            except Exception:
                pass

        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # Observer MUST NOT raise back into the executor.
        logger.debug("[xau_suppression_shadow] log failed", exc_info=True)

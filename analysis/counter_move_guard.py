"""Counter-move guard with ghost-trade logging (Option B).

When the last N M1 candles strongly contradict the intended trade direction,
we SKIP the live order (cTrader minimum lot 0.01 = can't size below floor)
but write a ghost trade into xau_shadow_journal so we can later compare
"would-have outcomes" against trades we actually took.

Design:
- 5-bar M1 window before entry
- count opposite-color bars
- ≥4/5 opposite → skip + ghost log (strong counter-move)
- 3/5 → log warning tag, but still trade
- ≤2/5 → trade normally

Per project rules:
- Demo-account safe: skip is the smallest possible block (replaces
  size-tilt below lot floor); shadow data is collected so the gate is
  evidence-driven and reversible.
- Additive: no family code touched. Single chokepoint in ctrader_executor.
- Fail-silent: errors → no skip (trade goes through normally).
"""
from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ctrader_openapi.db"


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _normalize(c: Any) -> Optional[dict]:
    if c is None:
        return None
    if isinstance(c, dict):
        o = _f(c.get("open", c.get("o")))
        cl = _f(c.get("close", c.get("c")))
    else:
        o = _f(getattr(c, "open", None))
        cl = _f(getattr(c, "close", None))
    if o <= 0 or cl <= 0:
        return None
    return {"open": o, "close": cl}


def detect_counter_move(direction: str, m1_candles: Any, *,
                        window: int = 5,
                        skip_threshold: int = 4) -> dict:
    """Return {strength: int 0..window, skip: bool, would_skip: bool, reason: str}.

    direction long/buy: counter-move = bearish (close<open) bars
    direction short/sell: counter-move = bullish bars
    """
    direction = str(direction or "").strip().lower()
    is_long = direction in ("long", "buy")
    is_short = direction in ("short", "sell")
    if not (is_long or is_short):
        return {"strength": 0, "skip": False, "would_skip": False, "reason": "bad_direction"}

    norm: list[dict] = []
    try:
        for c in (m1_candles or [])[-window:]:
            n = _normalize(c)
            if n is not None:
                norm.append(n)
    except TypeError:
        return {"strength": 0, "skip": False, "would_skip": False, "reason": "candles_not_iterable"}

    if len(norm) < window:
        return {"strength": 0, "skip": False, "would_skip": False, "reason": "insufficient_data"}

    if is_long:
        opposite = sum(1 for c in norm if c["close"] < c["open"])
    else:
        opposite = sum(1 for c in norm if c["close"] > c["open"])

    skip = opposite >= skip_threshold
    return {
        "strength": opposite,
        "window": len(norm),
        "skip": skip,
        "would_skip": skip,
        "reason": f"counter_move:{opposite}_{len(norm)}" if skip else f"normal:{opposite}_{len(norm)}",
        "threshold": skip_threshold,
    }


def log_ghost_trade(*, signal: Any, block_reason: str, db_path: Optional[Path] = None) -> bool:
    """Write skipped trade to xau_shadow_journal. Fail-silent."""
    try:
        path = Path(db_path) if db_path else DB_PATH
        if not path.exists():
            return False
        symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
        if symbol != "XAUUSD":
            return False
        raw = getattr(signal, "raw_scores", {}) or {}
        try:
            raw_json = json.dumps(raw, default=str, ensure_ascii=True)
        except Exception:
            raw_json = "{}"
        con = sqlite3.connect(str(path), timeout=5.0)
        try:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO xau_shadow_journal
                  (signal_utc, symbol, direction, confidence, entry, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   block_reason, raw_scores_json, shadow_outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                symbol,
                str(getattr(signal, "direction", "") or "").strip().lower(),
                _f(getattr(signal, "confidence", 0.0)),
                _f(getattr(signal, "entry", 0.0)),
                _f(getattr(signal, "stop_loss", 0.0)),
                _f(getattr(signal, "take_profit_1", 0.0)),
                _f(getattr(signal, "take_profit_2", 0.0)),
                _f(getattr(signal, "take_profit_3", 0.0)),
                str(block_reason or ""),
                raw_json,
                "pending",
            ))
            con.commit()
            return True
        finally:
            con.close()
    except Exception:
        return False


def fetch_m1_for_xau(bars: int = 5) -> list[dict]:
    """Best-effort M1 fetch for live evaluation. Returns [] on failure."""
    try:
        from market.data_fetcher import XAUUSDProvider
        df = XAUUSDProvider().fetch(timeframe="1m", bars=bars)
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            try:
                out.append({"open": float(row["open"]), "close": float(row["close"])})
            except Exception:
                continue
        return out
    except Exception:
        return []


def evaluate(signal: Any, *, enabled: bool = True) -> dict:
    """One-call helper used by executor chokepoint.

    Returns dict with keys:
      - skip: bool — caller should skip live order if True
      - tag: dict — write into raw_scores["counter_move_tag"]
      - block_reason: str — for shadow journal entry
    """
    out = {"skip": False, "tag": {"checked": False}, "block_reason": ""}
    try:
        if not enabled or signal is None:
            return out
        symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
        if symbol != "XAUUSD":
            return out
        direction = str(getattr(signal, "direction", "") or "").strip().lower()
        m1 = fetch_m1_for_xau(bars=5)
        result = detect_counter_move(direction, m1, window=5, skip_threshold=4)
        out["tag"] = {
            "checked": True,
            "strength": result["strength"],
            "window": result.get("window", 0),
            "would_skip": result["would_skip"],
            "reason": result["reason"],
        }
        if result["skip"]:
            out["skip"] = True
            out["block_reason"] = f"counter_move_skip:{result['strength']}_{result.get('window', 5)}"
        return out
    except Exception:
        return out

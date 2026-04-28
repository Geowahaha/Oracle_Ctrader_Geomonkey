"""Reversal setup live runner — emits real signals when 5-layer score qualifies.

Called by scheduler every N minutes. Fetches XAUUSD M5+M1, evaluates the
5-layer reversal setup detector, and:
  - score≥4 + L5 confirmed → emits a TradeSignal with entry_type="market"
  - score≥3 + at_overlap   → emits a TradeSignal with entry_type="limit"
  - score==2               → writes to xau_shadow_journal (ghost only)
  - score≤1                → no action

The signal then goes through the executor's full chokepoint chain
(overlap_tagger, trend_rider, rr_bucket sizing, counter_move_guard).

Per project rules: additive (new signal source, doesn't replace any),
demo-safe (live entry only on highest conviction), fail-silent.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

# Throttle: minimum minutes between live emissions (to avoid spam)
_LAST_FIRE_TS: dict[str, float] = {}
_THROTTLE_MIN = 20


def _within_throttle(key: str) -> bool:
    last = _LAST_FIRE_TS.get(key, 0.0)
    return (time.time() - last) < (_THROTTLE_MIN * 60)


def _mark_fired(key: str) -> None:
    _LAST_FIRE_TS[key] = time.time()


def _fetch(symbol: str, tf: str, bars: int) -> list[dict]:
    try:
        from market.data_fetcher import XAUUSDProvider
        if symbol != "XAUUSD":
            return []
        df = XAUUSDProvider().fetch(timeframe=tf, bars=bars)
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            try:
                out.append({
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0) or 0.0),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


def _h1_trend_label(h1: list[dict]) -> str:
    if len(h1) < 30:
        return ""
    closes = [c["close"] for c in h1[-30:]]
    drift = closes[-1] - closes[0]
    rng = max(c["high"] for c in h1[-30:]) - min(c["low"] for c in h1[-30:])
    if rng <= 0:
        return ""
    ratio = drift / rng
    if ratio > 0.3:
        return "up"
    if ratio < -0.3:
        return "down"
    return "flat"


def _build_signal(eval_out: dict, m5: list[dict]) -> Optional[Any]:
    """Build a TradeSignal-compatible object from evaluator output."""
    try:
        from analysis.signals import TradeSignal
    except Exception:
        return None
    if not m5:
        return None
    try:
        atr = 0.0
        if len(m5) >= 15:
            trs = []
            for i in range(1, len(m5)):
                trs.append(max(
                    m5[i]["high"] - m5[i]["low"],
                    abs(m5[i]["high"] - m5[i-1]["close"]),
                    abs(m5[i]["low"] - m5[i-1]["close"]),
                ))
            atr = sum(trs[-14:]) / 14 if trs else 0.0
        entry = float(eval_out.get("planned_entry") or m5[-1]["close"])
        sl = float(eval_out.get("planned_sl") or 0.0)
        tp1 = float(eval_out.get("planned_tp_1r") or 0.0)
        tp2 = float(eval_out.get("planned_tp_2r") or 0.0)
        tp3 = entry + (tp2 - entry) * 1.5 if eval_out["bias"] == "long" else entry - (entry - tp2) * 1.5
        risk = abs(entry - sl)
        rr = abs(tp2 - entry) / risk if risk > 0 else 0.0
        sig = TradeSignal(
            symbol="XAUUSD",
            direction=eval_out["bias"],
            confidence=70.0 + (eval_out.get("score", 0) * 4),  # 70-90 by score
            entry=round(entry, 5),
            stop_loss=round(sl, 5),
            take_profit_1=round(tp1, 5),
            take_profit_2=round(tp2, 5),
            take_profit_3=round(tp3, 5),
            risk_reward=round(rr, 2),
            timeframe="M5",
            session="",
            trend="reversal_setup",
            rsi=50.0,
            atr=round(atr, 5),
            pattern=f"REVERSAL_SETUP_5L_{eval_out.get('score', 0)}",
            entry_type=eval_out.get("entry_type", "limit"),
            sl_type="structure",
            sl_reason="recent_extreme_minus_0.5atr",
            tp_type="rr",
            tp_reason="2R_target",
        )
        sig.raw_scores = {
            "reversal_setup": {
                "score": eval_out.get("score"),
                "max_score": 5,
                "layers": eval_out.get("layers", {}),
                "decision_reason": eval_out.get("decision_reason"),
                "htf_aligned": eval_out.get("htf_aligned"),
                "sr_top3": eval_out.get("sr_levels_top3", []),
            },
        }
        # Add a small risk override hint (cTrader risk USD); executor's overlap
        # tagger + rr bucket will further adjust. Score 4-5 = full size.
        sig.raw_scores["ctrader_risk_usd_override"] = 1.5
        return sig
    except Exception:
        return None


def _log_shadow(eval_out: dict) -> bool:
    """Write tentative (score=2) result to xau_shadow_journal as ghost."""
    try:
        db = ROOT / "data" / "ctrader_openapi.db"
        if not db.exists():
            return False
        con = sqlite3.connect(str(db), timeout=5.0)
        try:
            con.execute("""
                INSERT INTO xau_shadow_journal
                  (signal_utc, symbol, direction, confidence, entry, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   block_reason, raw_scores_json, shadow_outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                "XAUUSD",
                eval_out.get("bias", ""),
                70.0,
                float(eval_out.get("planned_entry") or 0.0),
                float(eval_out.get("planned_sl") or 0.0),
                float(eval_out.get("planned_tp_1r") or 0.0),
                float(eval_out.get("planned_tp_2r") or 0.0),
                0.0,
                f"reversal_setup_score_{eval_out.get('score', 0)}",
                json.dumps({"layers": eval_out.get("layers", {}),
                            "sr_top3": eval_out.get("sr_levels_top3", [])},
                           default=str, ensure_ascii=True),
                "pending",
            ))
            con.commit()
            return True
        finally:
            con.close()
    except Exception:
        return False


def run_xau_reversal_setup_scan(executor: Any | None = None, *, logger: Any = None) -> dict:
    """Single scan tick. Returns {'action': str, 'score': int, 'reason': str}."""
    out = {"action": "noop", "score": 0, "reason": "init"}
    try:
        from analysis.reversal_setup_detector import evaluate
    except Exception:
        out["reason"] = "import_error"
        return out

    m5 = _fetch("XAUUSD", "5m", bars=120)
    if len(m5) < 60:
        out["reason"] = "no_m5_data"
        return out
    h1 = _fetch("XAUUSD", "1h", bars=60)
    htf = _h1_trend_label(h1) if h1 else ""

    ev = evaluate(m5, h1_trend=htf)
    out["score"] = ev.get("score", 0)
    entry_type = ev.get("entry_type", "none")
    bias = ev.get("bias", "neutral")

    if entry_type == "none":
        out["reason"] = ev.get("decision_reason", "score_too_low")
        return out

    throttle_key = f"{entry_type}:{bias}"
    if entry_type in ("market", "limit") and _within_throttle(throttle_key):
        out["action"] = "throttled"
        out["reason"] = f"throttled_{_THROTTLE_MIN}m"
        return out

    if entry_type == "shadow":
        if _log_shadow(ev):
            out["action"] = "shadow_logged"
            out["reason"] = "score_2_shadow"
        else:
            out["action"] = "shadow_skip"
            out["reason"] = "log_failed"
        return out

    # market or limit → emit live signal through executor
    if executor is None:
        out["action"] = "no_executor"
        return out

    sig = _build_signal(ev, m5)
    if sig is None:
        out["reason"] = "build_failed"
        return out

    try:
        source = f"xau_reversal_setup:{entry_type}"
        result = executor.execute_signal(sig, source=source)
        out["action"] = "emitted"
        out["reason"] = f"{entry_type}_score_{ev.get('score', 0)}"
        out["status"] = getattr(result, "status", "")
        out["message"] = getattr(result, "message", "")
        _mark_fired(throttle_key)
        if logger is not None:
            try:
                logger.info("[reversal_setup] %s emitted score=%s status=%s",
                            entry_type, ev.get("score"), getattr(result, "status", ""))
            except Exception:
                pass
    except Exception as e:
        out["action"] = "execute_error"
        out["reason"] = str(e)[:80]
    return out

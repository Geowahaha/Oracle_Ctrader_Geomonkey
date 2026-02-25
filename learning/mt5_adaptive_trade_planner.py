"""
learning/mt5_adaptive_trade_planner.py
Adaptive MT5 execution planner (bounded, explainable).

Purpose:
- Replace fixed TP/SL/RR assumptions with symbol-aware, regime-aware execution planning.
- Use both live execution context (spread, tick price) and recent realized outcomes
  from mt5_autopilot forward-test journal.
- Keep changes bounded to avoid destabilizing the strategy.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    src = dt if isinstance(dt, datetime) else _utc_now()
    if src.tzinfo is None:
        src = src.replace(tzinfo=timezone.utc)
    return src.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(float(lo), min(float(hi), float(v)))


@dataclass
class AdaptiveExecutionPlan:
    ok: bool
    applied: bool
    reason: str
    signal_symbol: str = ""
    broker_symbol: str = ""
    account_key: str = ""
    rr_target: Optional[float] = None
    rr_base: Optional[float] = None
    stop_scale: float = 1.0
    size_multiplier: float = 1.0
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    factors: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "applied": bool(self.applied),
            "reason": str(self.reason or ""),
            "signal_symbol": str(self.signal_symbol or ""),
            "broker_symbol": str(self.broker_symbol or ""),
            "account_key": str(self.account_key or ""),
            "rr_target": self.rr_target,
            "rr_base": self.rr_base,
            "stop_scale": self.stop_scale,
            "size_multiplier": self.size_multiplier,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "take_profit_3": self.take_profit_3,
            "factors": dict(self.factors or {}),
        }


class MT5AdaptiveTradePlanner:
    def __init__(self, db_path: Optional[str] = None):
        data_dir = Path(__file__).resolve().parent.parent / "data"
        cfg = str(getattr(config, "MT5_AUTOPILOT_DB_PATH", "") or "").strip()
        self.db_path = Path(db_path or cfg or (data_dir / "mt5_autopilot.db"))
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl_sec = 60.0

    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "MT5_ADAPTIVE_EXECUTION_ENABLED", True))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA query_only=1")
        return conn

    @staticmethod
    def _symbol_family(signal_symbol: str, broker_symbol: str) -> str:
        s = str(signal_symbol or broker_symbol or "").upper()
        b = str(broker_symbol or signal_symbol or "").upper()
        if "XAU" in s or "XAU" in b or "XAG" in s or "XAG" in b:
            return "metal"
        if "/" in s and s.endswith("/USDT"):
            return "crypto"
        if any(b.endswith(x) for x in ("USD", "USDT")) and any(b.startswith(x) for x in (
            "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "BNB", "LTC", "BCH", "DOT", "LINK", "TRX", "UNI", "ATOM", "POL", "HBAR", "PEPE", "SHIB", "PAXG",
        )):
            return "crypto"
        if b in {"US500", "US30", "USTEC", "US2000", "UK100", "JP225", "DE40", "GER40", "SPX500", "NAS100"}:
            return "index"
        if len(b) in (6, 7) and any(b.endswith(x) for x in ("USD", "JPY", "EUR", "GBP", "CHF", "AUD", "NZD", "CAD")):
            return "fx"
        if "." in s or "." in b:
            return "stock"
        return "other"

    def symbol_family(self, signal_symbol: str, broker_symbol: str) -> str:
        return self._symbol_family(signal_symbol, broker_symbol)

    def _symbol_stats(self, account_key: str, signal_symbol: str, broker_symbol: str, lookback_days: int) -> dict:
        if not account_key or not self.db_path.exists():
            return {"samples": 0}
        key = f"{account_key}|{str(signal_symbol).upper()}|{str(broker_symbol).upper()}|{int(lookback_days)}"
        now_ts = _utc_now().timestamp()
        cached = self._cache.get(key)
        if cached and (now_ts - float(cached[0])) <= self._cache_ttl_sec:
            return dict(cached[1] or {})

        since = _iso(_utc_now() - timedelta(days=max(1, int(lookback_days))))
        out = {
            "samples": 0,
            "win_rate": None,
            "tp_rate": None,
            "sl_rate": None,
            "mae": None,
            "avg_rr": None,
            "avg_conf": None,
            "avg_pnl": None,
        }
        try:
            with self._lock:
                with closing(self._connect()) as conn:
                    row = conn.execute(
                        """
                        SELECT COUNT(*),
                               AVG(CASE WHEN outcome IS NOT NULL THEN outcome END),
                               AVG(CASE WHEN close_reason='TP' THEN 1.0 ELSE 0.0 END),
                               AVG(CASE WHEN close_reason='SL' THEN 1.0 ELSE 0.0 END),
                               AVG(CASE WHEN prediction_error IS NOT NULL THEN ABS(prediction_error) END),
                               AVG(CASE WHEN risk_reward IS NOT NULL THEN risk_reward END),
                               AVG(CASE WHEN confidence IS NOT NULL THEN confidence END),
                               AVG(CASE WHEN pnl IS NOT NULL THEN pnl END)
                          FROM mt5_execution_journal
                         WHERE account_key=?
                           AND resolved=1
                           AND closed_at>=?
                           AND (
                                UPPER(COALESCE(broker_symbol,''))=?
                                OR UPPER(COALESCE(signal_symbol,''))=?
                           )
                        """,
                        (
                            account_key,
                            since,
                            str(broker_symbol or "").upper(),
                            str(signal_symbol or "").upper(),
                        ),
                    ).fetchone()
            if row:
                out["samples"] = _safe_int(row[0], 0)
                out["win_rate"] = (None if row[1] is None else round(_safe_float(row[1], 0.0), 4))
                out["tp_rate"] = (None if row[2] is None else round(_safe_float(row[2], 0.0), 4))
                out["sl_rate"] = (None if row[3] is None else round(_safe_float(row[3], 0.0), 4))
                out["mae"] = (None if row[4] is None else round(_safe_float(row[4], 0.0), 4))
                out["avg_rr"] = (None if row[5] is None else round(_safe_float(row[5], 0.0), 4))
                out["avg_conf"] = (None if row[6] is None else round(_safe_float(row[6], 0.0), 2))
                out["avg_pnl"] = (None if row[7] is None else round(_safe_float(row[7], 0.0), 6))
        except Exception as e:
            logger.debug("[MT5AdaptiveExec] stats query failed: %s", e)

        self._cache[key] = (now_ts, dict(out))
        return out

    def symbol_behavior_stats(self, account_key: str, signal_symbol: str, broker_symbol: str, lookback_days: int = 45) -> dict:
        return self._symbol_stats(
            account_key=str(account_key or ""),
            signal_symbol=str(signal_symbol or ""),
            broker_symbol=str(broker_symbol or ""),
            lookback_days=max(1, int(lookback_days or 45)),
        )

    @staticmethod
    def _session_score(session: str) -> float:
        s = str(session or "").lower()
        score = 0.0
        if "overlap" in s:
            score += 0.12
        if "new_york" in s:
            score += 0.05
        if "london" in s:
            score += 0.05
        if "asian" in s and "crypto" not in s:
            score -= 0.03
        return score

    def plan_execution(
        self,
        *,
        signal,
        account_key: str,
        broker_symbol: str,
        execution_price: float,
        bid: float,
        ask: float,
        point: float,
    ) -> AdaptiveExecutionPlan:
        sig_symbol = str(getattr(signal, "symbol", "") or "")
        if not self.enabled:
            return AdaptiveExecutionPlan(False, False, "disabled", signal_symbol=sig_symbol, broker_symbol=broker_symbol, account_key=account_key)
        try:
            direction = str(getattr(signal, "direction", "") or "").lower()
            if direction not in {"long", "short"}:
                return AdaptiveExecutionPlan(False, False, "invalid_direction", signal_symbol=sig_symbol, broker_symbol=broker_symbol, account_key=account_key)
            price = _safe_float(execution_price, 0.0)
            entry0 = _safe_float(getattr(signal, "entry", price), price)
            sl0 = _safe_float(getattr(signal, "stop_loss", 0.0), 0.0)
            rr0 = _safe_float(getattr(signal, "risk_reward", 2.0), 2.0)
            if price <= 0 or sl0 <= 0:
                return AdaptiveExecutionPlan(False, False, "invalid_prices", signal_symbol=sig_symbol, broker_symbol=broker_symbol, account_key=account_key)
            base_risk = abs(entry0 - sl0)
            if base_risk <= max(1e-12, float(point or 0.0)):
                return AdaptiveExecutionPlan(False, False, "tiny_base_risk", signal_symbol=sig_symbol, broker_symbol=broker_symbol, account_key=account_key)

            family = self._symbol_family(sig_symbol, broker_symbol)
            atr = abs(_safe_float(getattr(signal, "atr", 0.0), 0.0))
            atr_pct = (atr / price * 100.0) if (atr > 0 and price > 0) else 0.0
            spread = max(0.0, _safe_float(ask, 0.0) - _safe_float(bid, 0.0))
            mid = ((ask + bid) / 2.0) if (_safe_float(ask, 0.0) > 0 and _safe_float(bid, 0.0) > 0) else price
            spread_pct = (spread / mid * 100.0) if mid > 0 else 0.0
            conf = _clamp(_safe_float(getattr(signal, "confidence", 0.0), 0.0) / 100.0, 0.0, 1.0)
            trend = str(getattr(signal, "trend", "") or "").lower()
            trend_aligned = (direction == "long" and "bull" in trend) or (direction == "short" and "bear" in trend)
            session = str(getattr(signal, "session", "") or "")
            stats = self._symbol_stats(
                account_key=str(account_key or ""),
                signal_symbol=sig_symbol,
                broker_symbol=broker_symbol,
                lookback_days=max(7, int(getattr(config, "MT5_ADAPTIVE_EXECUTION_LOOKBACK_DAYS", 45))),
            )
            samples = _safe_int(stats.get("samples", 0), 0)

            # Family baselines and spread tolerances (heuristic, bounded).
            family_cfg = {
                "crypto": {"rr_min": 1.4, "rr_max": 2.8, "atr_ref": 1.8, "spread_warn": 0.10},
                "metal": {"rr_min": 1.4, "rr_max": 2.4, "atr_ref": 0.55, "spread_warn": 0.06},
                "fx":    {"rr_min": 1.2, "rr_max": 2.1, "atr_ref": 0.35, "spread_warn": 0.03},
                "index": {"rr_min": 1.3, "rr_max": 2.3, "atr_ref": 0.60, "spread_warn": 0.05},
                "stock": {"rr_min": 1.2, "rr_max": 2.0, "atr_ref": 1.20, "spread_warn": 0.08},
                "other": {"rr_min": 1.2, "rr_max": 2.2, "atr_ref": 1.00, "spread_warn": 0.08},
            }.get(family, {"rr_min": 1.2, "rr_max": 2.2, "atr_ref": 1.0, "spread_warn": 0.08})

            rr_floor = max(float(family_cfg["rr_min"]), float(getattr(config, "MT5_ADAPTIVE_EXECUTION_RR_MIN", 1.2)))
            rr_cap = min(float(family_cfg["rr_max"]), float(getattr(config, "MT5_ADAPTIVE_EXECUTION_RR_MAX", 2.8)))
            if rr_cap < rr_floor:
                rr_cap = rr_floor

            rr_adj = 0.0
            rr_adj += (conf - 0.70) * 0.60     # confidence quality
            rr_adj += (0.05 if trend_aligned else -0.03)
            rr_adj += self._session_score(session)
            rr_adj -= max(0.0, (spread_pct - float(family_cfg["spread_warn"])) * 1.5)

            # Volatility regime: if ATR% is high, widen stop and moderate RR to reduce premature stopouts.
            atr_ref = max(0.05, float(family_cfg["atr_ref"]))
            vol_ratio = (atr_pct / atr_ref) if atr_ref > 0 else 1.0
            stop_scale = 1.0 + _clamp((vol_ratio - 1.0) * 0.12, -0.10, 0.18)
            rr_adj -= _clamp((vol_ratio - 1.3) * 0.12, 0.0, 0.12)

            min_samples = max(1, int(getattr(config, "MT5_ADAPTIVE_EXECUTION_MIN_SYMBOL_TRADES", 6)))
            hist_bonus = 0.0
            size_mult = 1.0
            if samples >= min_samples:
                win_rate = _safe_float(stats.get("win_rate", 0.5), 0.5)
                mae = _safe_float(stats.get("mae", 0.35), 0.35)
                tp_rate = _safe_float(stats.get("tp_rate", win_rate), win_rate)
                sl_rate = _safe_float(stats.get("sl_rate", 1.0 - win_rate), 1.0 - win_rate)
                hist_bonus += _clamp((win_rate - 0.52) * 0.55, -0.12, 0.12)
                hist_bonus += _clamp((tp_rate - sl_rate) * 0.12, -0.08, 0.08)
                hist_bonus -= _clamp((mae - 0.35) * 0.20, 0.0, 0.10)
                rr_adj += hist_bonus
                size_mult *= 1.0 + _clamp((win_rate - 0.52) * 0.75, -0.12, 0.12)
                size_mult *= 1.0 - _clamp((mae - 0.35) * 0.25, 0.0, 0.12)

            # Keep dollar risk roughly stable when widening stops; amplify only modestly when conditions improve.
            size_mult *= (1.0 / max(0.70, stop_scale)) ** 0.55
            size_mult *= 1.0 + _clamp((conf - 0.72) * 0.22, -0.05, 0.06)

            stop_scale = _clamp(
                stop_scale,
                float(getattr(config, "MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MIN", 0.85)),
                float(getattr(config, "MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MAX", 1.35)),
            )
            size_mult = _clamp(
                size_mult,
                float(getattr(config, "MT5_ADAPTIVE_EXECUTION_SIZE_MIN", 0.70)),
                float(getattr(config, "MT5_ADAPTIVE_EXECUTION_SIZE_MAX", 1.10)),
            )
            rr_target = _clamp(rr0 * (1.0 + rr_adj), rr_floor, rr_cap)
            rr_target = round(float(rr_target), 2)

            is_long = direction == "long"
            new_risk = max(float(point or 0.0) * 2.0, float(base_risk) * float(stop_scale))
            if is_long:
                sl = price - new_risk
                tp1 = price + new_risk * 1.0
                tp2 = price + new_risk * rr_target
                tp3 = price + new_risk * max(rr_target + 1.0, 3.0)
            else:
                sl = price + new_risk
                tp1 = price - new_risk * 1.0
                tp2 = price - new_risk * rr_target
                tp3 = price - new_risk * max(rr_target + 1.0, 3.0)

            # Materiality threshold: avoid churn for microscopic changes.
            changed = (
                abs(sl - sl0) > max(float(point or 0.0) * 2.0, abs(sl0) * 0.00002)
                or abs(_safe_float(getattr(signal, "take_profit_2", 0.0), 0.0) - tp2) > max(float(point or 0.0) * 2.0, abs(tp2) * 0.00002)
                or abs(rr_target - rr0) >= 0.05
                or abs(size_mult - 1.0) >= 0.03
            )

            factors = {
                "family": family,
                "atr_pct": round(float(atr_pct), 5),
                "spread_pct": round(float(spread_pct), 6),
                "confidence": round(float(conf), 4),
                "trend_aligned": bool(trend_aligned),
                "session": session,
                "vol_ratio": round(float(vol_ratio), 4),
                "samples": int(samples),
                "win_rate": stats.get("win_rate"),
                "tp_rate": stats.get("tp_rate"),
                "sl_rate": stats.get("sl_rate"),
                "mae": stats.get("mae"),
                "rr_adj": round(float(rr_adj), 4),
                "hist_bonus": round(float(hist_bonus), 4),
            }
            return AdaptiveExecutionPlan(
                ok=True,
                applied=bool(changed),
                reason=("adaptive_applied" if changed else "adaptive_neutral"),
                signal_symbol=sig_symbol,
                broker_symbol=str(broker_symbol or ""),
                account_key=str(account_key or ""),
                rr_target=float(rr_target),
                rr_base=float(rr0),
                stop_scale=round(float(stop_scale), 4),
                size_multiplier=round(float(size_mult), 4),
                entry=float(price),
                stop_loss=float(sl),
                take_profit_1=float(tp1),
                take_profit_2=float(tp2),
                take_profit_3=float(tp3),
                factors=factors,
            )
        except Exception as e:
            logger.debug("[MT5AdaptiveExec] planner error: %s", e, exc_info=True)
            return AdaptiveExecutionPlan(False, False, f"planner_error:{e}")


mt5_adaptive_trade_planner = MT5AdaptiveTradePlanner()

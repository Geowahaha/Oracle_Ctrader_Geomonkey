"""Non-Fibo XAU family protection helpers.

These helpers are intentionally pure/data-driven and feature-flag friendly. They
convert recent cTrader deal evidence into opportunity-first routing hints:
raise confidence floors and reduce risk for the exact side/family currently
bleeding, instead of turning the whole family off.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time
from typing import Iterable


NONFIBO_XAU_PREFIXES = ("scalp_xauusd", "xauusd_scheduled")
_READ_DEALS_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
READ_DEALS_TTL_SEC = 60.0


@dataclass(frozen=True)
class SideThrottle:
    active: bool
    size_mult: float = 1.0
    reason: str = "ok"
    trades: int = 0
    losses: int = 0
    wins: int = 0
    pnl: float = 0.0
    consecutive_losses: int = 0

    def as_dict(self) -> dict:
        return {
            "active": bool(self.active),
            "size_mult": round(float(self.size_mult), 4),
            "reason": str(self.reason),
            "trades": int(self.trades),
            "losses": int(self.losses),
            "wins": int(self.wins),
            "pnl": round(float(self.pnl), 4),
            "consecutive_losses": int(self.consecutive_losses),
        }


@dataclass(frozen=True)
class DynamicFloor:
    active: bool
    floor: float
    base_floor: float
    delta: float = 0.0
    reason: str = "ok"
    trades: int = 0
    wins: int = 0
    winrate: float = 0.0
    pnl: float = 0.0

    def as_dict(self) -> dict:
        return {
            "active": bool(self.active),
            "floor": round(float(self.floor), 3),
            "base_floor": round(float(self.base_floor), 3),
            "delta": round(float(self.delta), 3),
            "reason": str(self.reason),
            "trades": int(self.trades),
            "wins": int(self.wins),
            "winrate": round(float(self.winrate), 4),
            "pnl": round(float(self.pnl), 4),
        }


def _utc_cutoff(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(hours or 0.0)))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_db_path(db_path: str | Path | None) -> Path | None:
    if not db_path:
        return None
    try:
        p = Path(db_path).expanduser().resolve()
        return p if p.exists() else None
    except Exception:
        return None


def is_nonfibo_xau_source(source: str) -> bool:
    src = str(source or "").strip().lower()
    return bool(src) and (not src.startswith("fibo")) and src.startswith(NONFIBO_XAU_PREFIXES)


def _source_predicates(source: str) -> tuple[str, list[str]]:
    """Return SQL predicate and params matching source + its base/winner/canary lanes."""
    src = str(source or "").strip().lower()
    base = src.split(":", 1)[0]
    if not src:
        return "1=0", []
    if base and base != src:
        return "(LOWER(COALESCE(source,''))=? OR LOWER(COALESCE(source,''))=?)", [src, base]
    return "(LOWER(COALESCE(source,''))=? OR LOWER(COALESCE(source,'')) LIKE ?)", [src, f"{src}:%"]


def _read_deals(db_path: str | Path | None, *, source: str, symbol: str, direction: str, lookback_hours: float, limit: int = 50) -> list[dict]:
    p = _safe_db_path(db_path)
    if p is None:
        return []
    side = str(direction or "").strip().lower()
    sym = str(symbol or "").strip().upper()
    if side not in {"long", "short"} or not sym:
        return []
    cache_key = (str(p), str(source or "").strip().lower(), sym, side, round(float(lookback_hours or 0.0), 3), int(limit or 50))
    now = time.monotonic()
    cached = _READ_DEALS_CACHE.get(cache_key)
    if cached and now - float(cached[0]) <= READ_DEALS_TTL_SEC:
        return [dict(row) for row in cached[1]]
    source_pred, params = _source_predicates(source)
    if not params:
        return []
    cutoff = _utc_cutoff(lookback_hours)
    sql = f"""
        SELECT execution_utc, source, direction, pnl_usd AS pnl
          FROM ctrader_deals
         WHERE UPPER(COALESCE(symbol,''))=?
           AND LOWER(COALESCE(direction,''))=?
           AND {source_pred}
           AND COALESCE(pnl_usd,0) != 0
           AND COALESCE(execution_utc,'') >= ?
         ORDER BY execution_utc DESC
         LIMIT ?
    """
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, [sym, side, *params, cutoff, int(limit or 50)]).fetchall()
        conn.close()
        out = [dict(r) for r in rows]
        _READ_DEALS_CACHE[cache_key] = (now, [dict(r) for r in out])
        if len(_READ_DEALS_CACHE) > 256:
            for old_key in list(_READ_DEALS_CACHE.keys())[:64]:
                _READ_DEALS_CACHE.pop(old_key, None)
        return out
    except Exception:
        return []


def consecutive_losses_from_latest(deals: Iterable[dict]) -> int:
    streak = 0
    for row in list(deals or []):
        if _safe_float(row.get("pnl"), 0.0) < 0.0:
            streak += 1
        else:
            break
    return streak


def compute_side_throttle(
    db_path: str | Path | None,
    *,
    source: str,
    symbol: str = "XAUUSD",
    direction: str,
    lookback_hours: float = 6.0,
    min_trades: int = 3,
    max_consecutive_losses: int = 3,
    loss_usd_trigger: float = 8.0,
    throttle_mult: float = 0.30,
) -> SideThrottle:
    if not is_nonfibo_xau_source(source):
        return SideThrottle(False, reason="not_nonfibo_xau")
    deals = _read_deals(db_path, source=source, symbol=symbol, direction=direction, lookback_hours=lookback_hours, limit=50)
    n = len(deals)
    if n < max(1, int(min_trades or 1)):
        return SideThrottle(False, reason="insufficient_recent_trades", trades=n)
    pnls = [_safe_float(row.get("pnl"), 0.0) for row in deals]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    pnl = sum(pnls)
    streak = consecutive_losses_from_latest(deals)
    active = streak >= max(1, int(max_consecutive_losses or 1)) or pnl <= -abs(float(loss_usd_trigger or 0.0))
    if not active:
        return SideThrottle(False, reason="within_recent_side_tolerance", trades=n, losses=losses, wins=wins, pnl=pnl, consecutive_losses=streak)
    reason = f"rolling_side_bleed:streak={streak}:pnl={pnl:.2f}"
    mult = max(0.05, min(1.0, float(throttle_mult or 0.30)))
    return SideThrottle(True, size_mult=mult, reason=reason, trades=n, losses=losses, wins=wins, pnl=pnl, consecutive_losses=streak)


def compute_dynamic_confidence_floor(
    db_path: str | Path | None,
    *,
    source: str,
    symbol: str = "XAUUSD",
    direction: str = "",
    base_floor: float = 70.0,
    lookback_hours: float = 336.0,
    window: int = 10,
    min_trades: int = 5,
    low_wr: float = 0.35,
    high_wr: float = 0.55,
    raise_delta: float = 5.0,
    lower_delta: float = -3.0,
    max_delta: float = 5.0,
) -> DynamicFloor:
    if not is_nonfibo_xau_source(source):
        return DynamicFloor(False, floor=base_floor, base_floor=base_floor, reason="not_nonfibo_xau")
    # Direction-specific floors if a direction is supplied; otherwise caller can pass any side.
    side = str(direction or "").strip().lower()
    if side not in {"long", "short"}:
        return DynamicFloor(False, floor=base_floor, base_floor=base_floor, reason="direction_missing")
    deals = _read_deals(db_path, source=source, symbol=symbol, direction=side, lookback_hours=lookback_hours, limit=max(1, int(window or 10)))
    n = len(deals)
    if n < max(1, int(min_trades or 1)):
        return DynamicFloor(False, floor=base_floor, base_floor=base_floor, reason="insufficient_window", trades=n)
    pnls = [_safe_float(row.get("pnl"), 0.0) for row in deals]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / max(1, n)
    pnl = sum(pnls)
    delta = 0.0
    reason = "neutral_window"
    if wr < float(low_wr or 0.35) or pnl < 0.0 and wr <= 0.40:
        delta = abs(float(raise_delta or 0.0))
        reason = f"weak_recent_wr:{wr:.2f}:pnl={pnl:.2f}"
    elif wr > float(high_wr or 0.55) and pnl > 0.0:
        delta = float(lower_delta or 0.0)
        reason = f"strong_recent_wr:{wr:.2f}:pnl={pnl:.2f}"
    delta = max(-abs(float(max_delta or 0.0)), min(abs(float(max_delta or 0.0)), delta))
    return DynamicFloor(True, floor=max(0.0, float(base_floor or 0.0) + delta), base_floor=base_floor, delta=delta, reason=reason, trades=n, wins=wins, winrate=wr, pnl=pnl)


def planned_rr(entry: float, stop_loss: float, take_profit: float, direction: str) -> float:
    entry_f = _safe_float(entry, 0.0)
    sl_f = _safe_float(stop_loss, 0.0)
    tp_f = _safe_float(take_profit, 0.0)
    if entry_f <= 0.0 or sl_f <= 0.0 or tp_f <= 0.0:
        return 0.0
    side = str(direction or "").strip().lower()
    if side == "long":
        if sl_f >= entry_f or tp_f <= entry_f:
            return 0.0
        risk = entry_f - sl_f
        reward = tp_f - entry_f
    elif side == "short":
        if sl_f <= entry_f or tp_f >= entry_f:
            return 0.0
        risk = sl_f - entry_f
        reward = entry_f - tp_f
    else:
        return 0.0
    if risk <= 0.0:
        return 0.0
    return max(0.0, reward / risk)


def apply_size_multiplier_to_signal(signal, *, multiplier: float, reason: str, default_risk_usd: float = 10.0) -> dict:
    mult = max(0.05, min(1.0, float(multiplier or 1.0)))
    raw = dict(getattr(signal, "raw_scores", {}) or {})
    before = _safe_float(raw.get("ctrader_risk_usd_override", default_risk_usd), default_risk_usd)
    after = round(max(0.05, before * mult), 4)
    raw["ctrader_risk_usd_override"] = after
    raw["xau_rasg_risk_multiplier"] = round(mult, 4)
    raw["xau_rasg_risk_before_usd"] = round(before, 4)
    raw["xau_rasg_risk_after_usd"] = after
    raw["xau_rasg_reason"] = str(reason or "")
    try:
        signal.raw_scores = raw
    except Exception:
        pass
    return raw

"""
learning/mt5_autopilot_core.py
Multi-account-ready MT5 risk governor + forward-test journal.

Goals:
- Risk-first gating before execution (daily loss, loss streak cooldown, rejection storm).
- Persist execution attempts/outcomes in SQLite (forward-test journal).
- Sync closed MT5 trades and compute prediction error / calibration stats.
- Future-ready account_key design: "{server}|{login}".
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
from execution.mt5_executor import mt5_executor

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    src = dt if isinstance(dt, datetime) else _utc_now()
    if src.tzinfo is None:
        src = src.replace(tzinfo=timezone.utc)
    return src.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(v: str) -> Optional[datetime]:
    raw = str(v or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


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


@dataclass
class GateDecision:
    allow: bool
    status: str
    reason: str
    account_key: str = ""
    snapshot: Optional[dict] = None


class MT5AutopilotCore:
    def __init__(self, db_path: Optional[str] = None):
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg = str(getattr(config, "MT5_AUTOPILOT_DB_PATH", "") or "").strip()
        self.db_path = Path(db_path or cfg or (data_dir / "mt5_autopilot.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_gate_snapshot: dict[str, tuple[float, dict]] = {}
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "MT5_AUTOPILOT_ENABLED", True)) and bool(getattr(config, "MT5_ENABLED", False))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mt5_accounts (
                        account_key TEXT PRIMARY KEY,
                        account_login INTEGER,
                        account_server TEXT,
                        currency TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mt5_execution_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        source TEXT,
                        signal_symbol TEXT,
                        broker_symbol TEXT,
                        direction TEXT,
                        confidence REAL,
                        neural_prob REAL,
                        risk_reward REAL,
                        entry REAL,
                        stop_loss REAL,
                        take_profit_2 REAL,
                        order_volume REAL,
                        risk_multiplier REAL,
                        canary_mode INTEGER,
                        mt5_status TEXT NOT NULL,
                        mt5_message TEXT,
                        ticket INTEGER,
                        position_id INTEGER,
                        account_balance REAL,
                        account_equity REAL,
                        account_free_margin REAL,
                        resolved INTEGER NOT NULL DEFAULT 0,
                        outcome INTEGER,
                        pnl REAL,
                        close_reason TEXT,
                        closed_at TEXT,
                        prediction_error REAL,
                        extra_json TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt5_exec_acct_created ON mt5_execution_journal(account_key, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt5_exec_unresolved ON mt5_execution_journal(resolved, account_key, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt5_exec_pos ON mt5_execution_journal(position_id, ticket)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mt5_risk_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        category TEXT NOT NULL,
                        action TEXT NOT NULL,
                        reason TEXT,
                        payload_json TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mt5_risk_events_acct_time ON mt5_risk_events(account_key, created_at)"
                )
                cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(mt5_execution_journal)").fetchall()}
                if "order_volume" not in cols:
                    conn.execute("ALTER TABLE mt5_execution_journal ADD COLUMN order_volume REAL")
                if "risk_multiplier" not in cols:
                    conn.execute("ALTER TABLE mt5_execution_journal ADD COLUMN risk_multiplier REAL")
                if "canary_mode" not in cols:
                    conn.execute("ALTER TABLE mt5_execution_journal ADD COLUMN canary_mode INTEGER")
                conn.commit()

    @staticmethod
    def _account_key_from_status(st: dict) -> str:
        login = _safe_int(st.get("account_login", 0), 0)
        server = str(st.get("account_server", "") or "")
        if not login or not server:
            return ""
        return f"{server}|{login}"

    def _upsert_account(self, st: dict) -> str:
        account_key = self._account_key_from_status(st)
        if not account_key:
            return ""
        now = _iso(_utc_now())
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT INTO mt5_accounts(account_key, account_login, account_server, currency, created_at, updated_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET
                        account_login=excluded.account_login,
                        account_server=excluded.account_server,
                        currency=excluded.currency,
                        updated_at=excluded.updated_at,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        account_key,
                        _safe_int(st.get("account_login", 0), 0),
                        str(st.get("account_server", "") or ""),
                        str(st.get("currency", "") or ""),
                        now,
                        now,
                        json.dumps(
                            {
                                "host": st.get("host"),
                                "port": st.get("port"),
                                "leverage": st.get("leverage"),
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                conn.commit()
        return account_key

    def _prediction_prob(self, signal) -> float:
        raw = dict(getattr(signal, "raw_scores", {}) or {})
        p = raw.get("neural_probability")
        if p is not None:
            try:
                return max(0.0, min(1.0, float(p)))
            except Exception:
                pass
        return max(0.0, min(1.0, _safe_float(getattr(signal, "confidence", 0.0), 0.0) / 100.0))

    def _compute_gate_snapshot(self, *, ttl_sec: int = 5) -> dict:
        now_ts = _utc_now().timestamp()
        st = mt5_executor.status()
        account_key = self._account_key_from_status(st)
        if not account_key:
            return {
                "ok": False,
                "account_key": "",
                "reason": "mt5_not_connected",
                "status": st,
            }
        cached = self._last_gate_snapshot.get(account_key)
        if cached and (now_ts - float(cached[0])) <= max(1, int(ttl_sec)):
            snap = dict(cached[1] or {})
            snap["cache_hit"] = True
            return snap

        open_snap = mt5_executor.open_positions_snapshot(limit=50)
        closed_snap = mt5_executor.closed_trades_snapshot(hours=24, limit=200)
        now_dt = _utc_now()
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        closed = list(closed_snap.get("closed_trades", []) or [])
        daily_pnl = 0.0
        win_count = 0
        loss_count = 0
        last_loss_ts = 0
        ordered = []
        for row in closed:
            pnl = _safe_float(row.get("pnl", 0.0), 0.0)
            cts = _safe_int(row.get("close_time", 0), 0)
            if cts <= 0:
                continue
            try:
                cdt = datetime.fromtimestamp(cts, tz=timezone.utc)
            except Exception:
                continue
            if cdt < today_start:
                continue
            daily_pnl += pnl
            if pnl > 1e-12:
                win_count += 1
            elif pnl < -1e-12:
                loss_count += 1
                last_loss_ts = max(last_loss_ts, cts)
            ordered.append((cts, pnl))

        ordered.sort(reverse=True)
        consecutive_losses = 0
        for _cts, pnl in ordered:
            if pnl < -1e-12:
                consecutive_losses += 1
            elif pnl > 1e-12:
                break
            else:
                # flat trade breaks the streak conservatively
                break

        # Recent rejection storm (journal-based; only this account)
        rejection_1h = 0
        with self._lock:
            with closing(self._connect()) as conn:
                cur = conn.execute(
                    """
                    SELECT COUNT(*) FROM mt5_execution_journal
                    WHERE account_key = ?
                      AND created_at >= ?
                      AND mt5_status IN ('rejected','error','invalid_stops')
                    """,
                    (account_key, _iso(now_dt - timedelta(hours=1))),
                )
                row = cur.fetchone()
                rejection_1h = _safe_int((row[0] if row else 0), 0)

        snapshot = {
            "ok": True,
            "account_key": account_key,
            "status": st,
            "open_positions": len(list(open_snap.get("positions", []) or [])),
            "pending_orders": len(list(open_snap.get("orders", []) or [])),
            "daily_realized_pnl": round(daily_pnl, 4),
            "daily_loss_abs": round(abs(min(0.0, daily_pnl)), 4),
            "daily_win_count": win_count,
            "daily_loss_count": loss_count,
            "consecutive_losses": consecutive_losses,
            "last_loss_ts": last_loss_ts,
            "recent_rejections_1h": rejection_1h,
            "closed_history_query_mode": str(closed_snap.get("history_query_mode", "") or ""),
            "cache_hit": False,
            "computed_at": _iso(now_dt),
        }
        self._last_gate_snapshot[account_key] = (now_ts, dict(snapshot))
        return snapshot

    def _log_risk_event(self, account_key: str, category: str, action: str, reason: str, payload: Optional[dict] = None) -> None:
        if not account_key:
            return
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT INTO mt5_risk_events(created_at, account_key, category, action, reason, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _iso(_utc_now()),
                        account_key,
                        str(category or ""),
                        str(action or ""),
                        str(reason or "")[:300],
                        json.dumps(payload or {}, ensure_ascii=True, separators=(",", ":")),
                    ),
                )
                conn.commit()

    def pre_trade_gate(self, signal, source: str = "", policy_overrides: Optional[dict] = None) -> GateDecision:
        if not self.enabled:
            return GateDecision(True, "disabled", "autopilot disabled")

        snap = self._compute_gate_snapshot(ttl_sec=int(getattr(config, "MT5_AUTOPILOT_GATE_CACHE_SEC", 5)))
        if not snap.get("ok"):
            # Fail-open by default to avoid breaking trading if telemetry fails.
            return GateDecision(True, "telemetry_unavailable", str(snap.get("reason", "unknown")), snapshot=snap)

        st = dict(snap.get("status") or {})
        account_key = str(snap.get("account_key") or "")
        overrides = dict(policy_overrides or {})
        equity = max(
            _safe_float(st.get("equity", 0.0), 0.0),
            _safe_float(st.get("balance", 0.0), 0.0),
            0.0,
        )
        daily_loss_abs = _safe_float(snap.get("daily_loss_abs", 0.0), 0.0)
        daily_loss_limit_usd = max(
            0.0,
            _safe_float(
                overrides.get("daily_loss_limit_usd", getattr(config, "MT5_RISK_GOV_DAILY_LOSS_LIMIT_USD", 2.0)),
                2.0,
            ),
        )
        daily_loss_limit_pct = max(
            0.0,
            _safe_float(
                overrides.get("daily_loss_limit_pct", getattr(config, "MT5_RISK_GOV_DAILY_LOSS_LIMIT_PCT", 15.0)),
                15.0,
            ),
        )
        max_cons_losses = max(
            0,
            _safe_int(
                overrides.get("max_consecutive_losses", getattr(config, "MT5_RISK_GOV_MAX_CONSECUTIVE_LOSSES", 2)),
                2,
            ),
        )
        cooldown_min = max(
            0,
            _safe_int(
                overrides.get("loss_cooldown_min", getattr(config, "MT5_RISK_GOV_LOSS_COOLDOWN_MIN", 30)),
                30,
            ),
        )
        reject_limit = max(
            0,
            _safe_int(
                overrides.get("max_rejections_1h", getattr(config, "MT5_RISK_GOV_MAX_REJECTIONS_1H", 5)),
                5,
            ),
        )

        # Hard stop: daily realized loss in USD.
        if daily_loss_limit_usd > 0 and daily_loss_abs >= daily_loss_limit_usd:
            reason = f"risk governor: daily realized loss {daily_loss_abs:.2f} >= ${daily_loss_limit_usd:.2f}"
            self._log_risk_event(account_key, "daily_loss", "block", reason, {"snapshot": snap, "source": source})
            return GateDecision(False, "guard_blocked", reason, account_key=account_key, snapshot=snap)

        # Hard stop: daily realized loss as % equity.
        if equity > 0 and daily_loss_limit_pct > 0:
            loss_pct = (daily_loss_abs / equity) * 100.0
            if loss_pct >= daily_loss_limit_pct:
                reason = f"risk governor: daily realized loss {loss_pct:.1f}% >= {daily_loss_limit_pct:.1f}% of equity"
                self._log_risk_event(account_key, "daily_loss_pct", "block", reason, {"snapshot": snap, "source": source})
                return GateDecision(False, "guard_blocked", reason, account_key=account_key, snapshot=snap)

        # Cooldown after consecutive losses.
        cons_losses = _safe_int(snap.get("consecutive_losses", 0), 0)
        last_loss_ts = _safe_int(snap.get("last_loss_ts", 0), 0)
        if max_cons_losses > 0 and cons_losses >= max_cons_losses and cooldown_min > 0 and last_loss_ts > 0:
            age_min = max(0.0, (_utc_now().timestamp() - float(last_loss_ts)) / 60.0)
            if age_min < float(cooldown_min):
                reason = (
                    f"risk governor: cooldown after {cons_losses} consecutive losses "
                    f"({age_min:.0f}m < {cooldown_min}m)"
                )
                self._log_risk_event(account_key, "loss_streak", "cooldown", reason, {"snapshot": snap, "source": source})
                return GateDecision(False, "guard_blocked", reason, account_key=account_key, snapshot=snap)

        if reject_limit > 0 and _safe_int(snap.get("recent_rejections_1h", 0), 0) >= reject_limit:
            reason = f"risk governor: recent MT5 rejections/errors >= {reject_limit}/h"
            self._log_risk_event(account_key, "reject_storm", "cooldown", reason, {"snapshot": snap, "source": source})
            return GateDecision(False, "guard_blocked", reason, account_key=account_key, snapshot=snap)

        return GateDecision(True, "allowed", "ok", account_key=account_key, snapshot=snap)

    def record_execution(self, signal, result, source: str = "") -> None:
        if not self.enabled:
            return
        try:
            st = mt5_executor.status()
        except Exception as e:
            logger.debug("[MT5Autopilot] status snapshot failed during record_execution: %s", e)
            st = {}
        account_key = self._upsert_account(st) or "unknown"

        raw_scores = dict(getattr(signal, "raw_scores", {}) or {}) if signal is not None else {}
        extra = {
            "raw_scores": raw_scores,
            "pattern": str(getattr(signal, "pattern", "") or "") if signal is not None else "",
            "session": str(getattr(signal, "session", "") or "") if signal is not None else "",
            "timeframe": str(getattr(signal, "timeframe", "") or "") if signal is not None else "",
        }
        try:
            exec_meta = dict(getattr(result, "execution_meta", {}) or {}) if result is not None else {}
            if exec_meta:
                extra["adaptive_execution"] = exec_meta
        except Exception:
            pass
        risk_mult = _safe_float(raw_scores.get("mt5_risk_multiplier", 1.0), 1.0)
        canary_mode = 1 if bool(raw_scores.get("mt5_canary_mode", False)) else 0
        created_at = _iso(_utc_now())
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT INTO mt5_execution_journal(
                        created_at, account_key, source, signal_symbol, broker_symbol, direction,
                        confidence, neural_prob, risk_reward, entry, stop_loss, take_profit_2,
                        order_volume, risk_multiplier, canary_mode,
                        mt5_status, mt5_message, ticket, position_id,
                        account_balance, account_equity, account_free_margin,
                        resolved, outcome, pnl, close_reason, closed_at, prediction_error, extra_json
                    )
                    VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        0, NULL, NULL, NULL, NULL, NULL, ?
                    )
                    """,
                    (
                        created_at,
                        account_key,
                        str(source or ""),
                        str(getattr(signal, "symbol", "") or "") if signal is not None else "",
                        str(getattr(result, "broker_symbol", "") or "") if result is not None else "",
                        str(getattr(signal, "direction", "") or "") if signal is not None else "",
                        _safe_float(getattr(signal, "confidence", 0.0) if signal is not None else 0.0, 0.0),
                        self._prediction_prob(signal) if signal is not None else None,
                        _safe_float(getattr(signal, "risk_reward", 0.0) if signal is not None else 0.0, 0.0),
                        _safe_float(getattr(signal, "entry", 0.0) if signal is not None else 0.0, 0.0),
                        _safe_float(getattr(signal, "stop_loss", 0.0) if signal is not None else 0.0, 0.0),
                        _safe_float(getattr(signal, "take_profit_2", 0.0) if signal is not None else 0.0, 0.0),
                        (_safe_float(getattr(result, "volume", None), 0.0) if getattr(result, "volume", None) is not None else None),
                        risk_mult,
                        canary_mode,
                        str(getattr(result, "status", "") or "") if result is not None else "",
                        str(getattr(result, "message", "") or "")[:400] if result is not None else "",
                        (_safe_int(getattr(result, "ticket", None), 0) or None) if result is not None else None,
                        (_safe_int(getattr(result, "position_id", None), 0) or None) if result is not None else None,
                        _safe_float(st.get("balance", 0.0), 0.0),
                        _safe_float(st.get("equity", 0.0), 0.0),
                        _safe_float(st.get("margin_free", 0.0), 0.0),
                        json.dumps(extra, ensure_ascii=True, separators=(",", ":")),
                    ),
                )
                conn.commit()

    def _match_and_update_outcomes(self, account_key: str, closed_rows: list[dict]) -> dict:
        updated = 0
        unresolved = 0
        matched = 0
        now_iso = _iso(_utc_now())
        with self._lock:
            with closing(self._connect()) as conn:
                unresolved_rows = conn.execute(
                    """
                    SELECT id, broker_symbol, signal_symbol, position_id, ticket, created_at, neural_prob
                    FROM mt5_execution_journal
                    WHERE account_key=? AND resolved=0 AND mt5_status IN ('filled','dry_run')
                    ORDER BY id DESC
                    LIMIT 1000
                    """,
                    (account_key,),
                ).fetchall()
                unresolved = len(unresolved_rows)
                # Build index from closed trades by position_id then symbol.
                by_pos = {}
                by_symbol = {}
                for row in closed_rows:
                    pos = row.get("position_id")
                    if pos:
                        by_pos[int(pos)] = row
                    sym = str(row.get("symbol", "") or "").upper()
                    by_symbol.setdefault(sym, []).append(row)
                for sym_list in by_symbol.values():
                    sym_list.sort(key=lambda r: _safe_int(r.get("close_time", 0), 0), reverse=True)

                for rid, broker_symbol, signal_symbol, position_id, ticket, created_at, neural_prob in unresolved_rows:
                    match = None
                    pos_i = _safe_int(position_id, 0)
                    if pos_i > 0:
                        match = by_pos.get(pos_i)
                    if match is None:
                        sym_key = str(broker_symbol or signal_symbol or "").upper()
                        created_dt = _parse_iso(created_at) or (_utc_now() - timedelta(days=365))
                        for cand in by_symbol.get(sym_key, []):
                            cts = _safe_int(cand.get("close_time", 0), 0)
                            if cts <= 0:
                                continue
                            cdt = datetime.fromtimestamp(cts, tz=timezone.utc)
                            if cdt >= created_dt:
                                match = cand
                                break
                    if match is None:
                        continue

                    pnl = _safe_float(match.get("pnl", 0.0), 0.0)
                    outcome = 1 if pnl > 1e-12 else (0 if pnl < -1e-12 else None)
                    pred = None if neural_prob is None else _safe_float(neural_prob, 0.0)
                    pred_err = None
                    if outcome is not None and pred is not None:
                        pred_err = float(outcome) - float(pred)
                    conn.execute(
                        """
                        UPDATE mt5_execution_journal
                           SET resolved=1,
                               outcome=?,
                               pnl=?,
                               close_reason=?,
                               closed_at=?,
                               prediction_error=?
                         WHERE id=?
                        """,
                        (
                            outcome,
                            pnl,
                            str(match.get("reason", "") or "")[:60],
                            _iso(datetime.fromtimestamp(_safe_int(match.get("close_time", 0), 0), tz=timezone.utc)),
                            pred_err,
                            int(rid),
                        ),
                    )
                    updated += 1
                    matched += 1
                conn.commit()

                stats_row = conn.execute(
                    """
                    SELECT COUNT(*),
                           AVG(CASE WHEN outcome IS NOT NULL THEN outcome END),
                           AVG(CASE WHEN prediction_error IS NOT NULL THEN ABS(prediction_error) END)
                      FROM mt5_execution_journal
                     WHERE account_key=? AND resolved=1 AND closed_at>=?
                    """,
                    (account_key, _iso(_utc_now() - timedelta(days=7))),
                ).fetchone()
        total_labeled = _safe_int(stats_row[0] if stats_row else 0, 0)
        win_rate = _safe_float(stats_row[1] if stats_row else 0.0, 0.0)
        mae = _safe_float(stats_row[2] if stats_row else 0.0, 0.0)
        return {
            "updated": updated,
            "matched": matched,
            "unresolved_scanned": unresolved,
            "labeled_7d": total_labeled,
            "win_rate_7d": round(win_rate, 4),
            "mae_7d": round(mae, 4) if total_labeled > 0 else None,
            "as_of": now_iso,
        }

    def sync_outcomes_from_mt5(self, hours: int = 72) -> dict:
        if not self.enabled:
            return {"ok": False, "message": "disabled"}
        st = mt5_executor.status()
        account_key = self._upsert_account(st)
        if not account_key:
            return {"ok": False, "message": "mt5 not connected"}
        closed = mt5_executor.closed_trades_snapshot(hours=max(1, int(hours or 72)), limit=500)
        if not bool(closed.get("connected")):
            return {"ok": False, "message": str(closed.get("error") or "closed snapshot failed")}
        rows = list(closed.get("closed_trades", []) or [])
        out = self._match_and_update_outcomes(account_key, rows)
        out.update(
            {
                "ok": True,
                "account_key": account_key,
                "closed_rows_seen": len(rows),
                "history_query_mode": str(closed.get("history_query_mode", "") or ""),
            }
        )
        return out

    @staticmethod
    def _quantile(vals: list[float], q: float) -> Optional[float]:
        if not vals:
            return None
        qv = max(0.0, min(1.0, float(q)))
        arr = sorted(float(v) for v in vals)
        if len(arr) == 1:
            return arr[0]
        pos = (len(arr) - 1) * qv
        lo = int(pos)
        hi = min(lo + 1, len(arr) - 1)
        frac = pos - lo
        return arr[lo] * (1.0 - frac) + arr[hi] * frac

    def fx_learned_neural_threshold(self, symbol: str, *, base_threshold: float) -> dict:
        out = {
            'ok': False,
            'applied': False,
            'symbol': str(symbol or '').upper(),
            'threshold': float(base_threshold),
            'base_threshold': float(base_threshold),
            'samples': 0,
            'wins': 0,
            'losses': 0,
            'reason': 'disabled',
            'details': {},
        }
        if not self.enabled:
            out['reason'] = 'disabled'
            return out
        if not bool(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_ENABLED', False)):
            out['reason'] = 'feature_off'
            return out
        sym = str(symbol or '').strip().upper()
        if not sym:
            out['reason'] = 'no_symbol'
            return out
        lookback_days = max(7, int(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_LOOKBACK_DAYS', 60) or 60))
        min_resolved = max(4, int(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_RESOLVED', 8) or 8))
        lo_bound = float(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_PROB', 0.40) or 0.40)
        hi_bound = float(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MAX_PROB', 0.55) or 0.55)
        blend = max(0.0, min(1.0, float(getattr(config, 'NEURAL_BRAIN_FX_LEARNED_THRESHOLD_BLEND', 0.5) or 0.5)))
        aliases = {sym}
        if sym.endswith('USD'):
            aliases.add(sym[:-3] + '/USDT')
        if sym.endswith('USDT'):
            aliases.add(sym[:-4] + 'USD')
        if '/' in sym:
            aliases.add(sym.replace('/', ''))
        since = _iso(_utc_now() - timedelta(days=lookback_days))
        gate = self.pre_trade_gate(signal=None, source='fx_learned_threshold')
        account_key = str(gate.account_key or '').strip()
        rows = []
        ph = ','.join(['?'] * len(aliases))
        params = [since]
        where = ["source='fx'", "created_at>=?", "resolved=1", "mt5_status IN ('filled','dry_run')", "neural_prob IS NOT NULL"]
        if account_key:
            where.append('account_key=?')
            params.append(account_key)
        where.append(f"(UPPER(COALESCE(NULLIF(broker_symbol,''),'')) IN ({ph}) OR UPPER(COALESCE(NULLIF(signal_symbol,''),'')) IN ({ph}))")
        aa = sorted(aliases)
        params.extend(aa)
        params.extend(aa)
        sql = f"SELECT neural_prob, COALESCE(pnl,0.0) as pnl FROM mt5_execution_journal WHERE {' AND '.join(where)}"
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(sql, params).fetchall()
        probs = []
        wins = []
        losses = []
        for r in rows:
            p = _safe_float(r[0], -1.0)
            if p < 0 or p > 1:
                continue
            probs.append(p)
            pnl = _safe_float(r[1], 0.0)
            if pnl > 0:
                wins.append(p)
            else:
                losses.append(p)
        n = len(probs)
        out['samples'] = n
        out['wins'] = len(wins)
        out['losses'] = len(losses)
        out['ok'] = True
        if n < min_resolved:
            out['reason'] = f'need_more_resolved:{n}/{min_resolved}'
            return out
        if len(wins) < 2 or len(losses) < 2:
            out['reason'] = 'insufficient_class_balance'
            return out
        win_mean = sum(wins) / len(wins)
        loss_mean = sum(losses) / len(losses)
        win_q25 = self._quantile(wins, 0.25)
        loss_q75 = self._quantile(losses, 0.75)
        if win_q25 is None or loss_q75 is None:
            out['reason'] = 'quantile_unavailable'
            return out
        raw_candidate = (float(win_q25) + float(loss_q75)) / 2.0
        if win_mean <= loss_mean:
            # No separation; keep base and report.
            out['reason'] = 'no_separation'
            out['details'] = {
                'win_mean': round(win_mean, 4), 'loss_mean': round(loss_mean, 4),
                'win_q25': round(float(win_q25), 4), 'loss_q75': round(float(loss_q75), 4),
            }
            return out
        sample_factor = min(1.0, max(0.0, (n - min_resolved + 1) / float(max(min_resolved, 1) * 2)))
        alpha = min(1.0, blend * sample_factor)
        candidate = max(lo_bound, min(hi_bound, float(raw_candidate)))
        threshold = (1.0 - alpha) * float(base_threshold) + alpha * candidate
        threshold = max(lo_bound, min(hi_bound, float(threshold)))
        out.update({
            'applied': True,
            'threshold': round(float(threshold), 4),
            'reason': 'learned_applied',
            'details': {
                'win_mean': round(win_mean, 4),
                'loss_mean': round(loss_mean, 4),
                'win_q25': round(float(win_q25), 4),
                'loss_q75': round(float(loss_q75), 4),
                'raw_candidate': round(float(raw_candidate), 4),
                'bounded_candidate': round(float(candidate), 4),
                'alpha': round(float(alpha), 4),
                'lookback_days': lookback_days,
                'min_resolved': min_resolved,
            },
        })
        return out


    def _fx_learned_metric_band(self, symbol: str, *, metric_col: str, base_low: float, base_high: float,
                                lookback_days: int = 60, min_resolved: int = 8, blend: float = 0.5,
                                hard_low: float = 0.0, hard_high: float = 1.0) -> dict:
        out = {
            'ok': False, 'applied': False, 'symbol': str(symbol or '').upper(),
            'low': float(base_low), 'high': float(base_high), 'samples': 0, 'wins': 0, 'losses': 0,
            'reason': 'disabled', 'details': {}
        }
        if not self.enabled:
            return out
        sym = str(symbol or '').strip().upper()
        if not sym:
            out['reason'] = 'no_symbol'
            return out
        aliases = {sym}
        if sym.endswith('USD'):
            aliases.add(sym[:-3] + '/USDT')
        if sym.endswith('USDT'):
            aliases.add(sym[:-4] + 'USD')
        if '/' in sym:
            aliases.add(sym.replace('/', ''))
        since = _iso(_utc_now() - timedelta(days=max(7, int(lookback_days or 60))))
        gate = self.pre_trade_gate(signal=None, source='fx_metric_band')
        account_key = str(gate.account_key or '').strip()
        where = [
            "source='fx'", "created_at>=?", "resolved=1",
            "mt5_status IN ('filled','dry_run')", f"{metric_col} IS NOT NULL"
        ]
        params = [since]
        if account_key:
            where.append('account_key=?')
            params.append(account_key)
        ph = ','.join(['?'] * len(aliases))
        where.append(f"(UPPER(COALESCE(NULLIF(broker_symbol,''),'')) IN ({ph}) OR UPPER(COALESCE(NULLIF(signal_symbol,''),'')) IN ({ph}))")
        aa = sorted(aliases)
        params.extend(aa); params.extend(aa)
        sql = f"SELECT {metric_col}, COALESCE(pnl,0.0) FROM mt5_execution_journal WHERE {' AND '.join(where)}"
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(sql, params).fetchall()
        vals, wins, losses = [], [], []
        for r in rows:
            v = _safe_float(r[0], None)
            if v is None:
                continue
            try:
                fv = float(v)
            except Exception:
                continue
            vals.append(fv)
            pnl = _safe_float(r[1], 0.0)
            if pnl > 0:
                wins.append(fv)
            else:
                losses.append(fv)
        n = len(vals)
        out.update({'ok': True, 'samples': n, 'wins': len(wins), 'losses': len(losses)})
        if n < max(4, int(min_resolved or 8)):
            out['reason'] = f'need_more_resolved:{n}/{max(4, int(min_resolved or 8))}'
            return out
        if len(wins) < 2 or len(losses) < 2:
            out['reason'] = 'insufficient_class_balance'
            return out
        win_mean = sum(wins) / len(wins)
        loss_mean = sum(losses) / len(losses)
        win_q25 = self._quantile(wins, 0.25)
        loss_q75 = self._quantile(losses, 0.75)
        if win_q25 is None or loss_q75 is None:
            out['reason'] = 'quantile_unavailable'
            return out
        if float(win_mean) <= float(loss_mean) or float(win_q25) <= float(loss_q75):
            out['reason'] = 'no_separation'
            out['details'] = {'win_mean': round(float(win_mean),4), 'loss_mean': round(float(loss_mean),4), 'win_q25': round(float(win_q25),4), 'loss_q75': round(float(loss_q75),4)}
            return out
        raw_low = float(loss_q75)
        raw_high = float((float(loss_q75) + float(win_q25)) / 2.0)
        if raw_high <= raw_low:
            raw_high = raw_low + max(0.01, (float(base_high) - float(base_low)) * 0.25)
        sample_factor = min(1.0, max(0.0, (n - max(4, int(min_resolved or 8)) + 1) / float(max(max(4, int(min_resolved or 8)), 1) * 2)))
        alpha = min(1.0, max(0.0, float(blend or 0.0)) * sample_factor)
        low = (1.0 - alpha) * float(base_low) + alpha * float(raw_low)
        high = (1.0 - alpha) * float(base_high) + alpha * float(raw_high)
        low = max(float(hard_low), min(float(hard_high), float(low)))
        high = max(low + 1e-6, min(float(hard_high), float(high)))
        out.update({
            'applied': True, 'low': round(float(low),4), 'high': round(float(high),4), 'reason': 'learned_applied',
            'details': {
                'win_mean': round(float(win_mean),4), 'loss_mean': round(float(loss_mean),4),
                'win_q25': round(float(win_q25),4), 'loss_q75': round(float(loss_q75),4),
                'raw_low': round(float(raw_low),4), 'raw_high': round(float(raw_high),4), 'alpha': round(float(alpha),4)
            }
        })
        return out

    def fx_learned_neural_soft_band(self, symbol: str, *, base_low: float, base_high: float, ref_threshold: float) -> dict:
        out = self._fx_learned_metric_band(
            symbol, metric_col='neural_prob', base_low=float(base_low), base_high=float(base_high),
            lookback_days=int(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_LOOKBACK_DAYS', 60) or 60),
            min_resolved=int(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_RESOLVED', 8) or 8),
            blend=float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BLEND', 0.5) or 0.5),
            hard_low=float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_LOW', 0.35) or 0.35),
            hard_high=float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MAX_HIGH', 0.52) or 0.52),
        )
        # Keep soft-band upper bound below the active neural threshold.
        try:
            if out.get('applied'):
                hi = min(float(out.get('high', base_high) or base_high), max(float(out.get('low', base_low) or base_low) + 0.005, float(ref_threshold) - 0.005))
                out['high'] = round(float(hi), 4)
                if float(out['high']) <= float(out['low']):
                    out['applied'] = False
                    out['reason'] = 'collapsed_by_threshold'
        except Exception:
            pass
        return out

    def fx_learned_confidence_soft_floor(self, symbol: str, *, min_conf: float, base_band_pts: float) -> dict:
        base_low = float(min_conf) - max(0.5, float(base_band_pts))
        out = self._fx_learned_metric_band(
            symbol, metric_col='confidence', base_low=float(base_low), base_high=float(min_conf),
            lookback_days=int(getattr(config, 'MT5_FX_CONF_SOFT_FILTER_LEARNED_LOOKBACK_DAYS', 60) or 60),
            min_resolved=int(getattr(config, 'MT5_FX_CONF_SOFT_FILTER_LEARNED_MIN_RESOLVED', 8) or 8),
            blend=float(getattr(config, 'MT5_FX_CONF_SOFT_FILTER_LEARNED_BLEND', 0.5) or 0.5),
            hard_low=float(getattr(config, 'MT5_FX_CONF_SOFT_FILTER_MIN_LOW', 55) or 55),
            hard_high=float(min_conf),
        )
        if out.get('applied'):
            low = float(out.get('low', base_low) or base_low)
            hi = min(float(min_conf), float(out.get('high', min_conf) or min_conf))
            gap = max(float(getattr(config, 'MT5_FX_CONF_SOFT_FILTER_MIN_GAP', 1.0) or 1.0), hi - low)
            out['soft_floor'] = round(float(hi - gap), 3)
            out['soft_ceiling'] = round(float(hi), 3)
        return out

    @staticmethod
    def _exec_reason_bucket(status: str, message: str) -> str:
        st = str(status or '').lower()
        msg = str(message or '').lower()
        if st in {'filled', 'dry_run'}:
            return 'filled'
        if 'retcode=10027' in msg or 'autotrading blocked' in msg:
            return 'terminal_autotrade_block'
        if 'margin guard' in msg or 'deny_margin' in msg:
            return 'margin_guard'
        if 'single open position only' in msg or 'single bot position only' in msg:
            return 'single_position_guard'
        if 'neural filter:' in msg:
            return 'neural_filter'
        if 'below mt5 confidence threshold' in msg:
            return 'confidence_threshold'
        if 'spread filter' in msg:
            return 'spread_filter'
        if st == 'guard_blocked':
            return 'risk_guard'
        if st in {'rejected', 'error', 'invalid_stops'}:
            return 'broker_or_exec_error'
        return st or 'other'

    def _augment_exec_reasons_delta_and_reco(self, out: dict, *, since_iso: str, aliases: set[str]) -> None:
        marker = str(config.get_exec_reasons_delta_marker_utc() or '').strip()
        if not marker:
            out['delta'] = {'marker_utc': '', 'enabled': False}
        else:
            out['delta'] = {'marker_utc': marker, 'enabled': True}
        rows = []
        acct = str(out.get('account_key') or '').strip()
        where = ["created_at>=?"]
        params = [since_iso]
        if acct:
            where.append('account_key=?')
            params.append(acct)
        if aliases:
            ph = ','.join(['?'] * len(aliases))
            where.append(f"(UPPER(COALESCE(signal_symbol,'')) IN ({ph}) OR UPPER(COALESCE(broker_symbol,'')) IN ({ph}))")
            aa = sorted(aliases)
            params.extend(aa); params.extend(aa)
        sql = f"SELECT created_at, COALESCE(mt5_status,''), COALESCE(mt5_message,''), COALESCE(signal_symbol,''), COALESCE(broker_symbol,'') FROM mt5_execution_journal WHERE {' AND '.join(where)}"
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(sql, params).fetchall()
        pre = {'total': 0}; post = {'total': 0}
        pre_b = {}; post_b = {}
        for created_at, st, msg, ss, bs in rows:
            ts = str(created_at or '')
            is_post = bool(marker and ts >= marker)
            tgt = post if is_post else pre
            tgt['total'] = int(tgt.get('total', 0)) + 1
            bucket = self._exec_reason_bucket(st, msg)
            bd = post_b if is_post else pre_b
            bd[bucket] = int(bd.get(bucket, 0)) + 1
        if out.get('delta', {}).get('enabled'):
            keys = sorted(set(pre_b) | set(post_b))
            changes = []
            for k in keys:
                pv = int(pre_b.get(k, 0)); qv = int(post_b.get(k, 0))
                changes.append({'bucket': k, 'before': pv, 'after': qv, 'delta': (qv - pv)})
            out['delta'].update({'pre': {'total': pre.get('total',0), 'buckets': pre_b}, 'post': {'total': post.get('total',0), 'buckets': post_b}, 'changes': changes})
        recs = []
        # recommendations based on top blockers in current lookback
        bmap = {}
        for r in list(out.get('reasons') or []):
            b = self._exec_reason_bucket(r.get('status',''), r.get('message',''))
            bmap[b] = int(bmap.get(b, 0)) + int(r.get('count', 0) or 0)
        total = int((out.get('summary') or {}).get('total', 0) or 0)
        sym = str(out.get('symbol') or '').upper()
        if bmap.get('terminal_autotrade_block', 0) > 0:
            recs.append({'priority': 'high', 'code': 'terminal_autotrade_block', 'action': 'Check MT5 terminal Algo Trading ON and Expert Advisors settings; remove auto-disable-on-account/profile-change if testing long sessions.'})
        if bmap.get('single_position_guard', 0) > 0:
            recs.append({'priority': 'high', 'code': 'single_position_guard', 'action': 'Close manual positions or keep MT5_POSITION_LIMITS_BOT_ONLY=1 and restart monitor so bot ignores manual trades in position limits.'})
        if bmap.get('margin_guard', 0) > 0:
            recs.append({'priority': 'medium', 'code': 'margin_guard', 'action': 'Increase capital or raise FX-only margin budget slightly (current FX-specific budget already supported) for this pair if repeated valid signals are blocked.'})
        if bmap.get('neural_filter', 0) > 0:
            if sym:
                recs.append({'priority': 'medium', 'code': 'neural_filter', 'action': f'{sym}: keep pair-specific neural override and soft band enabled; if blocks persist after more fills, lower learned/override threshold gradually (e.g. 0.45→0.43) not globally.'})
            else:
                recs.append({'priority': 'medium', 'code': 'neural_filter', 'action': 'Use FX pair-specific neural overrides/soft bands, not global threshold cuts, and collect more resolved FX fills for learned thresholds.'})
        if bmap.get('confidence_threshold', 0) > 0:
            recs.append({'priority': 'medium', 'code': 'confidence_threshold', 'action': 'Pair-specific confidence soft band can reduce size instead of hard block near threshold; tune per-pair only if repeated borderline setups show positive outcomes.'})
        if total == 0:
            recs.append({'priority': 'info', 'code': 'no_attempts', 'action': 'No execution attempts in lookback. Check scanner schedule, allowlist, and whether signals occurred for this symbol.'})
        out['recommendations'] = recs[:6]

    def execution_reasons_report(self, hours: int = 24, symbol: str = "") -> dict:
        out = {
            'ok': False,
            'enabled': self.enabled,
            'hours': max(1, int(hours or 24)),
            'symbol': str(symbol or '').upper(),
            'account_key': '',
            'summary': {},
            'reasons': [],
            'by_symbol': [],
            'samples': [],
            'delta': {},
            'recommendations': [],
            'message': '',
        }
        if not self.enabled:
            out['message'] = 'disabled'
            return out
        hrs = max(1, min(24 * 14, int(hours or 24)))
        sym = str(symbol or '').strip().upper()
        aliases = set()
        if sym:
            aliases.add(sym)
            if '/' in sym:
                base = sym.split('/', 1)[0]
                aliases.update({f'{base}/USDT', f'{base}USD'})
            elif sym.endswith('USDT'):
                base = sym[:-4]
                aliases.update({f'{base}/USDT', f'{base}USD'})
            elif sym.endswith('USD'):
                base = sym[:-3]
                aliases.update({f'{base}/USDT', f'{base}USD'})
        gate = self.pre_trade_gate(signal=None, source='exec_reasons')
        account_key = str(gate.account_key or '').strip()
        out['account_key'] = account_key
        since = _iso(_utc_now() - timedelta(hours=hrs))
        where = ["created_at >= ?"]
        params = [since]
        if account_key:
            where.append("account_key = ?")
            params.append(account_key)
        if aliases:
            ph = ','.join(['?'] * len(aliases))
            where.append(f"(UPPER(COALESCE(signal_symbol,'')) IN ({ph}) OR UPPER(COALESCE(broker_symbol,'')) IN ({ph}))")
            aa = sorted(aliases)
            params.extend(aa)
            params.extend(aa)
        where_sql = ' AND '.join(where)
        with self._lock:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*),
                           SUM(CASE WHEN mt5_status IN ('filled','dry_run') THEN 1 ELSE 0 END),
                           SUM(CASE WHEN mt5_status='skipped' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN mt5_status='guard_blocked' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN mt5_status IN ('rejected','error','invalid_stops') THEN 1 ELSE 0 END)
                      FROM mt5_execution_journal
                     WHERE {where_sql}
                    """,
                    params,
                ).fetchone()
                out['summary'] = {
                    'total': _safe_int(row[0] if row else 0, 0),
                    'filled': _safe_int(row[1] if row else 0, 0),
                    'skipped': _safe_int(row[2] if row else 0, 0),
                    'guard_blocked': _safe_int(row[3] if row else 0, 0),
                    'errors': _safe_int(row[4] if row else 0, 0),
                }
                reasons = conn.execute(
                    f"""
                    SELECT COALESCE(mt5_status,'-') AS status,
                           COALESCE(mt5_message,'-') AS msg,
                           COUNT(*) AS c
                      FROM mt5_execution_journal
                     WHERE {where_sql}
                     GROUP BY status, msg
                     ORDER BY c DESC, status ASC
                     LIMIT 20
                    """,
                    params,
                ).fetchall()
                out['reasons'] = [
                    {'status': str(r[0] or '-'), 'message': str(r[1] or '-'), 'count': _safe_int(r[2], 0)}
                    for r in reasons
                ]
                bysym = conn.execute(
                    f"""
                    SELECT UPPER(COALESCE(NULLIF(broker_symbol,''), NULLIF(signal_symbol,''), '-')) AS sym,
                           COUNT(*) AS sent,
                           SUM(CASE WHEN mt5_status IN ('filled','dry_run') THEN 1 ELSE 0 END) AS filled,
                           SUM(CASE WHEN mt5_status='skipped' THEN 1 ELSE 0 END) AS skipped,
                           SUM(CASE WHEN mt5_status='guard_blocked' THEN 1 ELSE 0 END) AS blocked
                      FROM mt5_execution_journal
                     WHERE {where_sql}
                     GROUP BY sym
                     ORDER BY sent DESC, sym ASC
                     LIMIT 10
                    """,
                    params,
                ).fetchall()
                out['by_symbol'] = [
                    {'symbol': str(r[0] or '-'), 'sent': _safe_int(r[1], 0), 'filled': _safe_int(r[2], 0), 'skipped': _safe_int(r[3], 0), 'guard_blocked': _safe_int(r[4], 0)}
                    for r in bysym
                ]
                samples = conn.execute(
                    f"""
                    SELECT created_at, COALESCE(signal_symbol,'-'), COALESCE(broker_symbol,'-'), COALESCE(mt5_status,'-'), COALESCE(mt5_message,'-'),
                           confidence, neural_prob
                      FROM mt5_execution_journal
                     WHERE {where_sql}
                     ORDER BY id DESC
                     LIMIT 8
                    """,
                    params,
                ).fetchall()
                out['samples'] = [
                    {
                        'created_at': str(r[0] or ''), 'signal_symbol': str(r[1] or '-'), 'broker_symbol': str(r[2] or '-'),
                        'status': str(r[3] or '-'), 'message': str(r[4] or '-'),
                        'confidence': (_safe_float(r[5], 0.0) if r[5] is not None else None),
                        'neural_prob': (_safe_float(r[6], 0.0) if r[6] is not None else None),
                    }
                    for r in samples
                ]
        try:
            self._augment_exec_reasons_delta_and_reco(out, since_iso=since, aliases=aliases)
        except Exception as e:
            out.setdefault('delta', {})
            out['delta']['error'] = str(e)
        out['ok'] = True
        out['message'] = 'ok'
        return out

    def status(self) -> dict:
        out = {
            "enabled": self.enabled,
            "db_path": str(self.db_path),
            "account_key": "",
            "risk_gate": {"allow": True, "status": "unknown", "reason": "not evaluated"},
            "journal": {"total": 0, "resolved": 0, "open_forward_tests": 0, "rejected_24h": 0},
            "calibration": {"labeled_7d": 0, "win_rate_7d": 0.0, "mae_7d": None},
        }
        if not self.enabled:
            return out
        gate = self.pre_trade_gate(signal=None, source="status")
        out["account_key"] = gate.account_key or ""
        out["risk_gate"] = {"allow": gate.allow, "status": gate.status, "reason": gate.reason}
        snap = dict(gate.snapshot or {})
        if snap:
            out["risk_snapshot"] = {
                "daily_realized_pnl": snap.get("daily_realized_pnl"),
                "daily_loss_abs": snap.get("daily_loss_abs"),
                "consecutive_losses": snap.get("consecutive_losses"),
                "recent_rejections_1h": snap.get("recent_rejections_1h"),
                "open_positions": snap.get("open_positions"),
                "pending_orders": snap.get("pending_orders"),
            }
        account_key = out["account_key"]
        if account_key:
            with self._lock:
                with closing(self._connect()) as conn:
                    row = conn.execute(
                        """
                        SELECT COUNT(*),
                               SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END),
                               SUM(CASE WHEN resolved=0 AND mt5_status IN ('filled','dry_run') THEN 1 ELSE 0 END),
                               SUM(CASE WHEN created_at>=? AND mt5_status IN ('rejected','error','invalid_stops') THEN 1 ELSE 0 END)
                          FROM mt5_execution_journal
                         WHERE account_key=?
                        """,
                        (_iso(_utc_now() - timedelta(hours=24)), account_key),
                    ).fetchone()
                    out["journal"] = {
                        "total": _safe_int(row[0] if row else 0, 0),
                        "resolved": _safe_int(row[1] if row else 0, 0),
                        "open_forward_tests": _safe_int(row[2] if row else 0, 0),
                        "rejected_24h": _safe_int(row[3] if row else 0, 0),
                    }
                    c = conn.execute(
                        """
                        SELECT COUNT(*),
                               AVG(CASE WHEN outcome IS NOT NULL THEN outcome END),
                               AVG(CASE WHEN prediction_error IS NOT NULL THEN ABS(prediction_error) END)
                          FROM mt5_execution_journal
                         WHERE account_key=? AND resolved=1 AND closed_at>=?
                        """,
                        (account_key, _iso(_utc_now() - timedelta(days=7))),
                    ).fetchone()
                    out["calibration"] = {
                        "labeled_7d": _safe_int(c[0] if c else 0, 0),
                        "win_rate_7d": round(_safe_float(c[1] if c else 0.0, 0.0), 4),
                        "mae_7d": (round(_safe_float(c[2], 0.0), 4) if (c and c[2] is not None) else None),
                    }
        return out


mt5_autopilot_core = MT5AutopilotCore()

"""
copy_trade/manager.py — Core copy trade dispatcher.

After master account executes a trade, this dispatches the same signal
to all active follower accounts with per-account risk scaling.

Architecture:
  Master execution (ctrader_executor.execute_signal)
       │
       ▼
  CopyTradeManager.dispatch(payload, result)
       │
       ├── cTrader followers: re-run ops/ctrader_execute_once.py
       │   with follower account_id + scaled risk_usd
       │
       └── MT5 followers: send via RPyC bridge (same signal, different magic)

All follower dispatches run in background threads so master execution
is never delayed.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config
from copy_trade.accounts import FollowerAccount, account_registry

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_WORKER_PATH = _ROOT / "ops" / "ctrader_execute_once.py"


@dataclass
class CopyTradeResult:
    account_id: str
    label: str
    broker: str
    ok: bool
    status: str
    message: str
    order_id: Optional[int] = None
    elapsed_ms: float = 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CopyTradeManager:
    """Dispatches executed master signals to follower accounts."""

    def __init__(self):
        self.enabled: bool = str(
            os.getenv("COPY_TRADE_ENABLED", "0")
        ).strip().lower() in ("1", "true", "yes", "on")
        self._dispatch_log: list[dict] = []
        self._lock = threading.Lock()

    def dispatch(
        self,
        master_payload: dict,
        master_result: dict,
        *,
        source: str = "",
    ) -> list[CopyTradeResult]:
        """
        Dispatch a successfully executed signal to all active followers.

        Args:
            master_payload: The payload dict sent to ctrader_execute_once.py
            master_result: The worker result dict (must have ok=True)
            source: signal source string for filtering
        """
        if not self.enabled:
            return []

        if not master_payload or not master_result:
            return []

        if not master_result.get("ok"):
            return []

        symbol = str(master_payload.get("symbol", "")).strip().upper()
        source = source or str(master_payload.get("source", ""))

        followers = account_registry.list_accounts()
        active = [
            f for f in followers
            if f.enabled
            and f.is_symbol_allowed(symbol)
            and f.is_source_allowed(source)
        ]

        if not active:
            return []

        logger.info(
            "[CopyTrade] dispatching %s %s to %d followers",
            symbol,
            master_payload.get("direction", ""),
            len(active),
        )

        results: list[CopyTradeResult] = []
        threads: list[threading.Thread] = []

        for follower in active:
            t = threading.Thread(
                target=self._dispatch_one,
                args=(follower, master_payload, results),
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        self._log_dispatch(master_payload, results)
        return results

    def dispatch_async(
        self,
        master_payload: dict,
        master_result: dict,
        *,
        source: str = "",
    ) -> None:
        """Fire-and-forget version of dispatch (runs in background thread)."""
        if not self.enabled:
            return
        t = threading.Thread(
            target=self.dispatch,
            args=(master_payload, master_result),
            kwargs={"source": source},
            daemon=True,
        )
        t.start()

    def _dispatch_one(
        self,
        follower: FollowerAccount,
        master_payload: dict,
        results: list[CopyTradeResult],
    ) -> None:
        t0 = time.time()
        try:
            if follower.broker == "ctrader":
                result = self._execute_ctrader(follower, master_payload)
            elif follower.broker == "mt5":
                result = self._execute_mt5(follower, master_payload)
            else:
                result = CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker=follower.broker,
                    ok=False,
                    status="unsupported_broker",
                    message=f"broker '{follower.broker}' not supported",
                )
        except Exception as e:
            result = CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker=follower.broker,
                ok=False,
                status="exception",
                message=str(e),
            )

        result.elapsed_ms = (time.time() - t0) * 1000
        with self._lock:
            results.append(result)

        if result.ok:
            account_registry.update_trade_stats(follower.account_id)
            logger.info(
                "[CopyTrade] %s (%s) -> %s order=%s %.0fms",
                follower.label,
                follower.broker,
                result.status,
                result.order_id,
                result.elapsed_ms,
            )
        else:
            logger.warning(
                "[CopyTrade] %s (%s) FAILED: %s — %s %.0fms",
                follower.label,
                follower.broker,
                result.status,
                result.message[:200],
                result.elapsed_ms,
            )

    def _execute_ctrader(
        self,
        follower: FollowerAccount,
        master_payload: dict,
    ) -> CopyTradeResult:
        """Execute on a cTrader follower account via the existing worker."""
        if not follower.ctrader_account_id:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="ctrader",
                ok=False,
                status="missing_account_id",
                message="ctrader_account_id not configured",
            )

        payload = copy.deepcopy(master_payload)
        payload["account_id"] = int(follower.ctrader_account_id)
        payload["account_reason"] = f"copy_trade:{follower.label}"

        master_risk = float(payload.get("risk_usd", 0) or 0)
        payload["risk_usd"] = round(follower.scale_risk(master_risk), 4)

        payload["label"] = (
            f"dxcp:{payload.get('symbol', '')}:"
            f"{str(payload.get('source', ''))[:12]}:"
            f"{follower.label[:8]}"
        )[:64]
        payload["comment"] = (
            f"dexter_copy|{follower.label}|{payload.get('symbol', '')}"
        )[:128]

        if follower.ctrader_access_token:
            payload["access_token_override"] = follower.ctrader_access_token

        payload.pop("raw_scores", None)

        return self._run_ctrader_worker(follower, payload)

    def _run_ctrader_worker(
        self,
        follower: FollowerAccount,
        payload: dict,
    ) -> CopyTradeResult:
        """Run ops/ctrader_execute_once.py for a follower."""
        if not _WORKER_PATH.exists():
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="ctrader",
                ok=False,
                status="worker_missing",
                message=f"worker not found: {_WORKER_PATH}",
            )

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as fh:
                json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"))
                tmp_path = fh.name

            cmd = [
                sys.executable,
                str(_WORKER_PATH),
                "--mode", "execute",
                "--payload-file", tmp_path,
            ]

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            if follower.ctrader_access_token:
                env["CTRADER_OPENAPI_ACCESS_TOKEN"] = follower.ctrader_access_token

            timeout = int(os.getenv("COPY_TRADE_WORKER_TIMEOUT_SEC", "25"))
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(_ROOT),
                env=env,
            )

            parsed = self._extract_json(proc.stdout)
            if parsed and parsed.get("ok"):
                return CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker="ctrader",
                    ok=True,
                    status=str(parsed.get("status", "ok")),
                    message=str(parsed.get("message", "")),
                    order_id=parsed.get("order_id"),
                )

            err_msg = ""
            if parsed:
                err_msg = str(parsed.get("message", parsed.get("status", "")))
            if not err_msg:
                err_msg = (proc.stderr or proc.stdout or "").strip()[-500:]

            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="ctrader",
                ok=False,
                status=str(parsed.get("status", "worker_error")) if parsed else "worker_error",
                message=err_msg or f"exit code {proc.returncode}",
            )

        except subprocess.TimeoutExpired:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="ctrader",
                ok=False,
                status="timeout",
                message=f"worker timeout",
            )
        except Exception as e:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="ctrader",
                ok=False,
                status="exception",
                message=str(e),
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _execute_mt5(
        self,
        follower: FollowerAccount,
        master_payload: dict,
    ) -> CopyTradeResult:
        """Execute on an MT5 follower account via RPyC bridge."""
        if not follower.mt5_login:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="mt5",
                ok=False,
                status="missing_login",
                message="mt5_login not configured",
            )

        try:
            import rpyc
        except ImportError:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="mt5",
                ok=False,
                status="rpyc_missing",
                message="rpyc not installed",
            )

        host = follower.mt5_host or str(os.getenv("MT5_HOST", "localhost"))
        port = follower.mt5_port or int(os.getenv("MT5_PORT", "18812"))
        magic = follower.mt5_magic or int(os.getenv("MT5_MAGIC", "123456"))

        symbol = str(master_payload.get("symbol", ""))
        direction = str(master_payload.get("direction", ""))
        entry = float(master_payload.get("entry", 0))
        stop_loss = float(master_payload.get("stop_loss", 0))
        take_profit = float(master_payload.get("take_profit", 0))
        entry_type = str(master_payload.get("entry_type", "market"))

        master_risk = float(master_payload.get("risk_usd", 0) or 0)
        risk_usd = follower.scale_risk(master_risk)

        try:
            conn = rpyc.connect(host, port, config={"allow_pickle": True})
            mt5 = conn.root.get_mt5()

            if not mt5.initialize():
                return CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker="mt5",
                    ok=False,
                    status="mt5_init_failed",
                    message="MT5 initialize() failed",
                )

            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                return CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker="mt5",
                    ok=False,
                    status="symbol_not_found",
                    message=f"symbol {symbol} not found on MT5",
                )

            point = symbol_info.point
            if point <= 0:
                point = 0.01

            risk_points = abs(entry - stop_loss) / point
            if risk_points <= 0:
                risk_points = 100

            tick_value = getattr(symbol_info, "trade_tick_value", 1.0) or 1.0
            volume_step = getattr(symbol_info, "volume_step", 0.01) or 0.01
            volume_min = getattr(symbol_info, "volume_min", 0.01) or 0.01
            volume_max = getattr(symbol_info, "volume_max", 100.0) or 100.0

            raw_volume = risk_usd / (risk_points * tick_value) if (risk_points * tick_value) > 0 else volume_min
            volume = max(volume_min, min(volume_max, round(raw_volume / volume_step) * volume_step))

            if direction == "long":
                order_type = mt5.ORDER_TYPE_BUY if entry_type == "market" else mt5.ORDER_TYPE_BUY_LIMIT
            else:
                order_type = mt5.ORDER_TYPE_SELL if entry_type == "market" else mt5.ORDER_TYPE_SELL_LIMIT

            price = entry if entry_type != "market" else (
                symbol_info.ask if direction == "long" else symbol_info.bid
            )

            request = {
                "action": mt5.TRADE_ACTION_DEAL if entry_type == "market" else mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": stop_loss,
                "tp": take_profit,
                "magic": magic,
                "comment": f"dxcp|{follower.label}|{symbol}"[:31],
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            conn.close()

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                return CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker="mt5",
                    ok=True,
                    status="filled",
                    message=f"order={result.order} volume={volume}",
                    order_id=int(result.order),
                )
            else:
                retcode = result.retcode if result else -1
                comment = result.comment if result else "no result"
                return CopyTradeResult(
                    account_id=follower.account_id,
                    label=follower.label,
                    broker="mt5",
                    ok=False,
                    status="mt5_rejected",
                    message=f"retcode={retcode} {comment}",
                )

        except Exception as e:
            return CopyTradeResult(
                account_id=follower.account_id,
                label=follower.label,
                broker="mt5",
                ok=False,
                status="exception",
                message=str(e),
            )

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        for line in reversed((text or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        return None

    def _log_dispatch(self, master_payload: dict, results: list[CopyTradeResult]) -> None:
        entry = {
            "ts": _utc_now_iso(),
            "symbol": master_payload.get("symbol", ""),
            "direction": master_payload.get("direction", ""),
            "source": master_payload.get("source", ""),
            "master_risk_usd": master_payload.get("risk_usd", 0),
            "followers": len(results),
            "success": sum(1 for r in results if r.ok),
            "failed": sum(1 for r in results if not r.ok),
            "details": [
                {
                    "account": r.account_id,
                    "label": r.label,
                    "broker": r.broker,
                    "ok": r.ok,
                    "status": r.status,
                    "order_id": r.order_id,
                    "elapsed_ms": round(r.elapsed_ms),
                }
                for r in results
            ],
        }
        with self._lock:
            self._dispatch_log.append(entry)
            if len(self._dispatch_log) > 500:
                self._dispatch_log = self._dispatch_log[-200:]

        try:
            log_path = _ROOT / "data" / "copy_trade_log.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def get_recent_log(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._dispatch_log[-n:])

    def status_summary(self) -> dict:
        accounts = account_registry.list_accounts()
        return {
            "enabled": self.enabled,
            "total_accounts": len(accounts),
            "active_accounts": sum(1 for a in accounts if a.enabled),
            "ctrader_accounts": sum(1 for a in accounts if a.broker == "ctrader" and a.enabled),
            "mt5_accounts": sum(1 for a in accounts if a.broker == "mt5" and a.enabled),
            "recent_dispatches": len(self._dispatch_log),
        }

    def format_telegram_status(self) -> str:
        s = self.status_summary()
        accounts = account_registry.list_accounts()
        lines = [
            "📋 *Copy Trade Status*",
            f"Enabled: {'✅' if s['enabled'] else '❌'}",
            f"Accounts: {s['active_accounts']}/{s['total_accounts']} active",
            f"  cTrader: {s['ctrader_accounts']}",
            f"  MT5: {s['mt5_accounts']}",
            "",
        ]
        for acc in accounts:
            status = "✅" if acc.enabled else "⏸"
            lines.append(
                f"{status} *{acc.label}* ({acc.broker}) "
                f"risk={acc.risk_multiplier}x max={acc.max_risk_usd}$ "
                f"trades={acc.total_trades}"
            )
        if not accounts:
            lines.append("No follower accounts configured.")
        return "\n".join(lines)


copy_trade_manager = CopyTradeManager()

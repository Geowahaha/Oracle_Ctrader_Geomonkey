"""cTrader OpenAPI-backed client for Dexter3 (VM migration Phase 1).

``Dexter3OpenApiClient`` exposes the EXACT method surface and return-dict
shapes of ``dexter3.mcp_client.Dexter3McpClient`` (read that module first —
this one mirrors its docstrings field-for-field) so that
``dexter3/transport.py::make_client()`` can hand either object to the same
caller with zero branching downstream. See
``docs/DEXTER3_VM_MIGRATION_DESIGN.md`` for the migration rationale.

This module does NOT implement the OpenAPI protocol itself. It shells out to
``ops/ctrader_execute_once.py`` — the SAME one-shot worker
``execution/ctrader_executor.py`` (the main, live-trading system) already
uses in production (proven connection/auth/reconcile/order-placement code).
Every read/write below is one of that worker's existing ``--mode`` values;
nothing here talks TCP/protobuf directly, and this module never modifies
``ops/ctrader_execute_once.py`` or ``execution/ctrader_executor.py`` — both
are shared with the live main system and are explicitly out of scope.

GAPS (read before relying on any of these in a live loop):

1. ``get_symbol_details`` raises ``Dexter3OpenApiNotImplementedError``
   (subclasses both ``McpClientError`` — so existing
   ``except (McpClientError, McpZombieError)`` call sites in
   ``dexter3/executor.py``/``dexter3/shadow_runner.py`` still catch it and
   degrade to a refusal instead of crashing — and ``NotImplementedError``).
   No existing worker ``--mode`` surfaces ``minVolume``/``stepVolume``/
   ``maxVolume``/``lotSize`` back to the caller (``get_trendbars`` and
   ``capture_market`` both fetch ``ProtoOASymbolByIdReq`` internally but
   only ``get_trendbars`` returns ``digits``, nothing else). Fixing this
   needs either a new worker mode (out of scope: shared live file) or a
   confirmed-live static per-symbol table. Blocks live ``execute_entry`` via
   this transport (Phase 3 concern only — P1/P2 are read-only shadow use).

2. ``get_balance()``: ``equity``, ``margin``, ``accountType`` are always
   ``None``. cTrader's ``ProtoOATrader`` message (what the worker's
   ``health`` mode reads) carries none of them — equity/margin need a
   separate ``ProtoOAGetPositionUnrealizedPnLReq`` per position (used
   internally by ``execution/ctrader_stream.py``'s margin poll, not exposed
   by any worker mode); ``accountType`` IS a real ``ProtoOATrader`` field
   but the ``health`` branch doesn't return it. Keys are present for shape
   parity; values are honestly absent, never guessed. ``balance`` and
   ``traderId`` (== ``ctidTraderAccountId``, the only field
   ``dexter3.shadow_runner``/``dexter3.executor`` actually read today) ARE
   real and correctly scaled by ``moneyDigits`` (the worker's ``health``
   branch returns raw, unscaled ``trader.balance`` — this client re-derives
   it correctly using the ``money_digits`` the same response already
   carries).

3. ``get_positions()``: ``ProtoOAPosition`` has no PnL field, so daemon mode
   computes ``netProfit`` from a fresh standing spot quote, position volume,
   swap and commission. When the quote is stale/unavailable (for example,
   market close), it deliberately leaves PnL unreadable and the basket holds
   fail-closed. The one-shot subprocess transport has the same safe degrade.

4. ``get_deals()``: label recovery is daemon-only.
   ``ProtoOADeal`` (confirmed against the installed protobuf descriptor) has
   no label field at all — labels live on ``ProtoOATradeData`` (orders/
   positions only). Reconcile's live "orders" list could join a still-open
   order's label, but a DEAL's originating order has usually already been
   consumed by the time the deal exists, and closed/historical positions
   drop out of "reconcile" entirely, so no reliable join exists via any
   currently-used worker mode. **This means
   ``dexter3.shadow_runner._lane_realized_today``'s label filter
   (``"dexter3:fable" in label``) will match ZERO deals via this
   transport** unless it uses the daemon's enabled ``ProtoOAOrderListReq``
   join. The one-shot subprocess transport still has no reliable historical
   label join, therefore its governor remains fail-closed.

5. ``get_pending_orders()`` is best-effort: normalized from
   ``ProtoOAReconcileReq``'s live "order" list using the same
   ``tradeData`` parsing pattern already confirmed for positions. It is
   NOT golden-sample-verified against a real pending order response (no
   current dexter3 caller exists to cross-check field names against) —
   verify with ``scripts/dexter3_transport_parity.py`` before depending on
   it.

6. ``place_market_order()`` needs a pip size to translate
   ``stop_loss_pips``/``take_profit_pips`` into the absolute prices the
   worker's order-placement path requires. Pending real
   ``get_symbol_details`` support (gap #1), this uses a small hardcoded
   per-symbol table (``_PIP_SIZE_BY_SYMBOL`` — XAUUSD=0.01 only, the
   universal 2-decimal gold quoting convention, not a broker-specific
   guess). An unrecognized symbol raises a clear error rather than assuming
   a pip size.

7. By default every call here is a fresh subprocess (full OpenAPI TCP+auth
   handshake, ~18s measured live on the VM) vs the local MCP's persistent
   HTTP session (~60-200ms). This was accepted as a latency difference for
   P1/P2, but flagged in ``docs/DEXTER3_VM_MIGRATION_DESIGN.md`` gap #4 as a
   connection-churn antipattern unusable at the lanes' 8-20s cadence.
   FIXED (additive): set ``DEXTER3_OPENAPI_DAEMON_URL`` (e.g.
   ``http://127.0.0.1:9877``) to route every call through
   ``dexter3/openapi_daemon.py`` instead — ONE persistent, already-
   authenticated connection serving many requests, no per-call handshake.
   Unset = unchanged subprocess behavior (this module's historical default).

8. Volume units: dexter3 volume is in "units" (1 unit = 0.01 lot XAU = 1 oz,
   confirmed against ``tests/test_dexter3_wiring.py``'s
   ``XAU_DETAILS`` fixture: minVolume=1.0/volumeStep=1.0/lotSize=100.0).
   cTrader OpenAPI's raw ``volume``/``minVolume``/``stepVolume`` fields are
   in hundredths of that same unit (confirmed empirically against this
   repo's own live journal: ``data/ctrader_openapi.db``'s
   ``execution_journal``/``ctrader_positions`` show XAUUSD trades
   consistently clamped to raw volume=100, i.e. exactly 1.0 oz at
   minVolume — the standard cTrader "centiunits" convention). This module
   converts at every OpenAPI<->dexter3 boundary via ``UNITS_TO_RAW_SCALE``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from dexter3.mcp_client import (
    McpClientError,
    McpMutationUncertain,
    McpZombieError,  # re-exported for callers that import it alongside this client
)

__all__ = [
    "Dexter3OpenApiClient",
    "Dexter3OpenApiAccountPinError",
    "Dexter3OpenApiNotImplementedError",
    "Dexter3OpenApiTransportError",
    "McpClientError",
    "McpZombieError",
    "McpMutationUncertain",
]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKER_PATH = ROOT / "ops" / "ctrader_execute_once.py"

# The account this migration is pinned to (design doc: demo login 9922808,
# ctidTraderAccountId 46670728). Env override is for a FUTURE account swap
# only — never a way to silently widen which accounts this client can touch.
DEFAULT_ACCOUNT_ID_PIN = 46670728
ACCOUNT_ID_PIN_ENV_VAR = "DEXTER3_OPENAPI_ACCOUNT_ID"

# Persistent-connection daemon (dexter3/openapi_daemon.py, gap #4 in
# docs/DEXTER3_VM_MIGRATION_DESIGN.md). Unset (default) = unchanged
# subprocess-per-call behavior below; set to the daemon's base URL (e.g.
# "http://127.0.0.1:9877") to POST /call instead of spawning
# ops/ctrader_execute_once.py — no TCP+OAuth handshake per call, since the
# daemon already holds one persistent authenticated connection.
DAEMON_URL_ENV_VAR = "DEXTER3_OPENAPI_DAEMON_URL"
# Daemon-mode default timeout: lower than the subprocess default (25s)
# because there is no connection handshake to wait out — only the protobuf
# round-trip over an already-open socket. Raised 5.0 -> 12.0 on 2026-07-11:
# live evidence showed reconcile's broker round-trip tail exceeds 5s under
# load (17 of 112 OM ticks on 2026-07-10 failed "daemon request timeout
# after 5.0s" -> om_lane_read_failed -> the OM held blind that tick). 12s
# covers the observed reconcile tail (typ ~2s, join-path ~8s) while still
# failing well inside the 20s poll cadence. Env-tunable as before.
DAEMON_TIMEOUT_ENV_VAR = "DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC"
DEFAULT_DAEMON_TIMEOUT_SEC = 12.0

# dexter3 "units" (1 unit = 0.01 lot XAU = 1 oz) <-> cTrader OpenAPI raw
# volume (hundredths of a unit) — see module docstring gap #8 for evidence.
UNITS_TO_RAW_SCALE = 100.0

# mcp_client-style period ("m5") -> worker get_trendbars timeframe ("5m").
_PERIOD_TO_TIMEFRAME = {
    "m1": "1m",
    "m5": "5m",
    "m15": "15m",
    "m30": "30m",
    "h1": "1h",
    "h4": "4h",
    "d1": "1d",
}

# Gap #6: pip size per symbol, pending real get_symbol_details support.
_PIP_SIZE_BY_SYMBOL = {
    "XAUUSD": 0.01,
}

# Gap #3 fix (2026-07-11): OpenAPI ProtoOAPosition carries NO netProfit, so the
# basket/OM read pnl=0.0 -> unreliable -> HOLD forever -> losers ran to broker
# SL unmanaged (the Fri -$18 short the owner caught). get_positions() computes
# live netProfit from the daemon spot cache using this USD-per-price-unit-per-
# volume-unit table. XAUUSD is quoted USD/oz and a dexter3 volume unit = 1 oz
# (raw÷100), so a 1.00 USD price move on 1 unit = 1.00 USD P&L → 1.0. This is
# contract-derived, not a guess; symbols absent here get NO computed pnl (basket
# stays unreliable→hold for them, the pre-fix safe behavior) rather than a wrong
# number. Add a symbol only with its confirmed contract point value.
_USD_POINT_VALUE_PER_UNIT = {
    "XAUUSD": 1.0,
}

DEFAULT_TIMEOUT_SEC = 25.0
DEFAULT_HEALTH_TIMEOUT_SEC = 18.0
DEFAULT_QUOTE_DURATION_SEC = 3
DEFAULT_QUOTE_MAX_EVENTS = 5
DEFAULT_RETRY_BACKOFF_SEC = 1.5

# worker JSON "status" values that mean "transport/process failure — the
# broker never definitively answered" (retryable for reads, McpMutationUncertain
# for mutations). Every other status (even ok=False ones like "rejected") means
# the worker DID get a definitive answer back and must never be retried.
# "disconnected" is the daemon-mode-only status (dexter3/openapi_daemon.py's
# handle_call) meaning the daemon itself has no live cTrader connection right
# now — the broker never saw this request either, so it belongs in the same
# retryable bucket as the subprocess-mode transport failures.
_TRANSPORT_FAILURE_STATUSES = frozenset(
    {"worker_missing", "worker_error", "timeout", "worker_failure", "worker_invalid_result", "disconnected"}
)


class Dexter3OpenApiAccountPinError(McpClientError):
    """Raised when the configured/observed account does not match the pin.

    Constraint: refuse ALL operations rather than silently operate against
    an unverified/unexpected ctidTraderAccountId.
    """


class Dexter3OpenApiNotImplementedError(McpClientError, NotImplementedError):
    """An operation the mcp_client surface exposes but no existing OpenAPI
    worker mode can support yet (see module docstring GAPS). Subclasses
    ``McpClientError`` so existing ``except (McpClientError, McpZombieError)``
    call sites still degrade gracefully instead of crashing; subclasses
    ``NotImplementedError`` so it stays identifiable as "not supported" vs
    "supported but failed"."""


class Dexter3OpenApiTransportError(McpClientError):
    """The worker subprocess itself failed (missing/timeout/garbage output)
    before the broker could definitively answer. Mirrors
    ``dexter3.mcp_client.McpTransportError``'s role for this transport."""


def _units_to_raw_volume(units: float) -> int:
    return int(round(float(units) * UNITS_TO_RAW_SCALE))


def _raw_volume_to_units(raw: Any) -> float:
    try:
        return float(raw) / UNITS_TO_RAW_SCALE
    except (TypeError, ValueError):
        return 0.0


def _utc_now_iso_ms() -> str:
    return _ms_to_iso_with_millis(int(time.time() * 1000))


def _ms_to_iso_with_millis(ms: Any) -> str:
    """ISO-8601 UTC with millisecond precision + ``Z`` suffix, e.g.
    ``2026-07-07T19:11:30.337Z``.

    ``dexter3.basket_live._position_open_ts``'s docstring is explicit that
    basket timing broke when this was second-precision only — deliberately
    NOT reusing ``ops/ctrader_execute_once.py::_ms_to_iso`` (which truncates
    to whole seconds) for this reason.
    """
    try:
        ms_int = int(ms)
    except (TypeError, ValueError):
        return ""
    if ms_int <= 0:
        return ""
    dt = datetime.fromtimestamp(ms_int / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms_int % 1000:03d}Z"


class Dexter3OpenApiClient:
    """cTrader OpenAPI client for Dexter3, matching ``Dexter3McpClient``'s
    method surface and return shapes (see module docstring for gaps).
    """

    def __init__(
        self,
        *,
        worker_path: Path | str | None = None,
        account_id: int | None = None,
        python_executable: str | None = None,
        timeout_sec: float | None = None,
        health_timeout_sec: float = DEFAULT_HEALTH_TIMEOUT_SEC,
        retry_backoff_sec: float = DEFAULT_RETRY_BACKOFF_SEC,
    ) -> None:
        self.worker_path = Path(worker_path) if worker_path is not None else DEFAULT_WORKER_PATH
        self.python_executable = python_executable or sys.executable
        # Daemon mode (additive): DEXTER3_OPENAPI_DAEMON_URL set -> _run_worker_once
        # POSTs to the daemon instead of spawning the subprocess (unset =
        # unchanged subprocess behavior below). timeout_sec=None (the new
        # default) resolves to the daemon's lower default when daemon mode is
        # active, else the historical subprocess default — an explicit
        # timeout_sec argument always wins over either default.
        self.daemon_url = str(os.environ.get(DAEMON_URL_ENV_VAR, "") or "").strip()
        if timeout_sec is not None:
            self.timeout_sec = float(timeout_sec)
        elif self.daemon_url:
            env_val = str(os.environ.get(DAEMON_TIMEOUT_ENV_VAR, "") or "").strip()
            self.timeout_sec = float(env_val) if env_val else DEFAULT_DAEMON_TIMEOUT_SEC
        else:
            self.timeout_sec = DEFAULT_TIMEOUT_SEC
        self.health_timeout_sec = float(health_timeout_sec)
        self.retry_backoff_sec = float(retry_backoff_sec)

        if account_id is not None:
            self.account_id = int(account_id)
        else:
            env_val = str(os.environ.get(ACCOUNT_ID_PIN_ENV_VAR, "") or "").strip()
            self.account_id = int(env_val) if env_val else DEFAULT_ACCOUNT_ID_PIN

        self._pin_verified = False

    # -- transport plumbing --------------------------------------------------

    def _run_worker_once(self, mode: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """One call to the proven worker's mode contract — either the
        persistent daemon (POST /call, when ``self.daemon_url`` is set) or a
        fresh subprocess invocation of ``ops/ctrader_execute_once.py``
        (unset, unchanged default). Mirrors
        ``execution/ctrader_executor.py::CTraderExecutor._run_worker``
        exactly for the subprocess path (same worker, same CLI contract) —
        duplicated here (not imported) because that method is bound to the
        live executor's own instance state; this is the same small,
        self-contained subprocess plumbing, not a new protocol
        implementation. Both branches return the identical raw-dict shape
        so ``_invoke``'s retry/mutation-classification logic below never
        needs to know which transport answered it.
        """
        if self.daemon_url:
            return self._run_worker_once_via_daemon(mode, payload, timeout_sec)
        if not self.worker_path.exists():
            return {"ok": False, "status": "worker_missing", "message": f"worker not found: {self.worker_path}"}
        cmd = [self.python_executable, str(self.worker_path), "--mode", str(mode or "health")]
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"))
                tmp_path = fh.name
            cmd.extend(["--payload-file", str(tmp_path)])
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(5, int(timeout_sec or 20)),
                cwd=str(ROOT),
                env=env,
            )
            parsed = self._extract_json_line(proc.stdout)
            if parsed:
                parsed.setdefault("worker_returncode", int(proc.returncode))
                return parsed
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            return {
                "ok": False,
                "status": "worker_error",
                "message": stderr or stdout or f"worker exited code {proc.returncode}",
                "worker_returncode": int(proc.returncode),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "timeout", "message": f"worker timeout after {int(timeout_sec or 0)}s"}
        except Exception as exc:  # noqa: BLE001 - transport failure, classified by caller
            return {"ok": False, "status": "worker_error", "message": str(exc)}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _run_worker_once_via_daemon(self, mode: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Daemon-mode transport: POST /call to ``dexter3/openapi_daemon.py``.

        Returns the SAME raw-dict shape ``_run_worker_once``'s subprocess
        branch returns — ``_invoke``'s classification (transport failure vs.
        tool-level failure) is transport-agnostic by design, so a network
        error/timeout here is mapped to the identical
        ``_TRANSPORT_FAILURE_STATUSES`` vocabulary the subprocess path uses.
        """
        url = f"{self.daemon_url.rstrip('/')}/call"
        eff_timeout = max(1.0, float(timeout_sec or self.timeout_sec))
        try:
            resp = requests.post(
                url,
                json={"mode": str(mode or "health"), "payload": payload},
                timeout=eff_timeout,
            )
        except requests.exceptions.Timeout as exc:
            return {"ok": False, "status": "timeout", "message": f"daemon request timeout after {eff_timeout}s: {exc}"}
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "status": "worker_error", "message": f"daemon request failed: {exc}"}
        if resp.status_code >= 400:
            detail = (resp.text or "")[:300]
            return {"ok": False, "status": "worker_error", "message": f"daemon HTTP {resp.status_code}: {detail}"}
        try:
            parsed = resp.json()
        except ValueError as exc:
            return {"ok": False, "status": "worker_error", "message": f"daemon returned non-JSON body: {exc}"}
        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "status": "worker_error",
                "message": f"daemon returned non-dict JSON: {type(parsed).__name__}",
            }
        return parsed

    @staticmethod
    def _extract_json_line(stdout_text: str) -> dict[str, Any]:
        for line in reversed([str(x).strip() for x in str(stdout_text or "").splitlines()]):
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _invoke(
        self,
        mode: str,
        payload: dict[str, Any],
        *,
        mutating: bool,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Run one worker mode and classify the result exactly like
        ``dexter3.mcp_client.Dexter3McpClient.call`` classifies MCP tool
        calls: a TRANSPORT failure (worker crashed/timed out/produced no
        JSON — the broker never definitively answered) is retried once for
        reads, or raises ``McpMutationUncertain`` immediately for mutations
        (single attempt — never blind-retry a mutation, same rationale as
        the 2026-07-05 double-fill lesson ``mcp_client`` documents). A TOOL
        failure (the worker/broker DID answer — e.g. ``rejected``,
        ``app_auth_failed``, ``symbol_not_found``) is never retried and
        raises a plain ``McpClientError`` since the mutation is known NOT to
        have executed.
        """
        eff_timeout = float(timeout_sec if timeout_sec is not None else self.timeout_sec)
        raw = self._run_worker_once(mode, payload, eff_timeout)
        status = str(raw.get("status") or "")
        if status in _TRANSPORT_FAILURE_STATUSES or not raw:
            if mutating:
                raise McpMutationUncertain(
                    f"transport failed during mutating worker call mode={mode} — MAY have executed; "
                    f"reconcile against broker state, do not retry: {raw.get('message') or raw}"
                )
            time.sleep(self.retry_backoff_sec)
            raw2 = self._run_worker_once(mode, payload, eff_timeout)
            status2 = str(raw2.get("status") or "")
            if status2 in _TRANSPORT_FAILURE_STATUSES or not raw2:
                raise Dexter3OpenApiTransportError(
                    f"worker transport failed twice for mode={mode}: {raw2.get('message') or raw2}"
                )
            raw = raw2
            status = status2
        if not bool(raw.get("ok")):
            raise McpClientError(f"openapi worker mode={mode} failed status={status}: {raw.get('message') or raw}")
        return raw

    # -- account pin ----------------------------------------------------------

    def _ensure_account_pin(self) -> None:
        """Refuse all operations unless ``self.account_id`` is confirmed
        among the access token's accounts (mode="accounts" — read-only,
        lists every ctidTraderAccountId the configured token can see).
        Cached for the lifetime of this client instance once verified.
        """
        if self._pin_verified:
            return
        raw = self._invoke("accounts", {}, mutating=False, timeout_sec=self.health_timeout_sec)
        accounts = raw.get("accounts") or []
        seen_ids = {int(a.get("accountId", 0) or 0) for a in accounts if isinstance(a, dict)}
        if self.account_id not in seen_ids:
            raise Dexter3OpenApiAccountPinError(
                f"account pin failed: configured ctidTraderAccountId={self.account_id} not found "
                f"among token accounts {sorted(seen_ids)} — refusing all operations. "
                f"Set {ACCOUNT_ID_PIN_ENV_VAR} to override the pin if this account change is intentional."
            )
        self._pin_verified = True

    def diagnose_account_pin(self) -> dict[str, Any]:
        """Read-only, NEVER-raising preflight for the account pin.

        Added 2026-07-10 during the P2 VM token/account investigation: the
        VM smoke test (``docs/DEXTER3_VM_MIGRATION_DESIGN.md`` P2 findings)
        surfaced ``{"ok": false, "message": "Invalid access token"}`` from
        the SAME "accounts" call ``_ensure_account_pin`` makes, and there
        was no way to tell, without reading logs, whether that meant (a)
        the shared token/worker is broken outright or (b) the token is
        fine but simply does not include this pin's ``ctidTraderAccountId``.
        This method makes that distinction a structured, scriptable
        artifact instead of a log-reading exercise — run it on the VM
        BEFORE starting a live/shadow loop:

            python -c "from dexter3.openapi_client import Dexter3OpenApiClient as C; \\
                import json; print(json.dumps(C().diagnose_account_pin()))"

        Returns one of three shapes (never raises):
          - ``reason="pin_ok"`` / ``"pin_ok_cached"``: pin confirmed; on a
            fresh check this also marks the instance's pin verified so a
            subsequent real call does not repeat the network round-trip.
          - ``reason="account_not_in_token_list"``: the worker answered
            (broker reachable, token accepted) but ``account_id`` isn't
            among the accounts the token can see — a genuine account/token
            mismatch, not a transport problem.
          - ``reason="worker_call_failed"``: the worker/broker never gave a
            usable answer (missing worker, timeout, OR a broker-level
            rejection such as "Invalid access token" — ``error_type`` tells
            you which: ``Dexter3OpenApiTransportError`` for the former,
            plain ``McpClientError`` for the latter). This is the case that
            was previously indistinguishable from (b) without reading logs.
        """
        if self._pin_verified:
            return {
                "ok": True,
                "reason": "pin_ok_cached",
                "configured_account_id": self.account_id,
                "seen_account_ids": None,
                "error_type": None,
                "message": "pin already verified earlier in this instance's lifetime",
            }
        try:
            raw = self._invoke("accounts", {}, mutating=False, timeout_sec=self.health_timeout_sec)
        except McpClientError as exc:
            return {
                "ok": False,
                "reason": "worker_call_failed",
                "configured_account_id": self.account_id,
                "seen_account_ids": None,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        accounts = raw.get("accounts") or []
        seen_ids = sorted({int(a.get("accountId", 0) or 0) for a in accounts if isinstance(a, dict)})
        if self.account_id not in seen_ids:
            return {
                "ok": False,
                "reason": "account_not_in_token_list",
                "configured_account_id": self.account_id,
                "seen_account_ids": seen_ids,
                "error_type": None,
                "message": (
                    f"configured ctidTraderAccountId={self.account_id} not found among "
                    f"token accounts {seen_ids}"
                ),
            }
        self._pin_verified = True
        return {
            "ok": True,
            "reason": "pin_ok",
            "configured_account_id": self.account_id,
            "seen_account_ids": seen_ids,
            "error_type": None,
            "message": f"account {self.account_id} confirmed among token accounts {seen_ids}",
        }

    def _payload(self, **kwargs: Any) -> dict[str, Any]:
        self._ensure_account_pin()
        out: dict[str, Any] = {"account_id": self.account_id}
        out.update(kwargs)
        return out

    # -- Phase 1 read-only surface (mirrors Dexter3McpClient) -----------------

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict[str, Any]]:
        timeframe = _PERIOD_TO_TIMEFRAME.get(str(period or "").strip().lower())
        if timeframe is None:
            raise McpClientError(
                f"unsupported trendbar period {period!r}; expected one of {sorted(_PERIOD_TO_TIMEFRAME)}"
            )
        # Same over-fetch-then-trim posture as Dexter3McpClient.get_trendbars
        # (session-gap robustness) — duplicated here because it's pure
        # windowing math, not protocol code.
        window_bars = max(int(count), 1) * 2 + 10
        minutes = _PERIOD_MINUTES.get(str(period).strip().lower(), 5) * window_bars
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - minutes * 60 * 1000
        payload = self._payload(
            symbol=str(symbol or "").strip().upper(),
            timeframe=timeframe,
            from_ms=from_ms,
            to_ms=now_ms,
            count=min(window_bars, 14000),
        )
        raw = self._invoke("get_trendbars", payload, mutating=False, timeout_sec=self.timeout_sec)
        bars_raw = raw.get("bars") or []
        bars = [
            {
                "open": float(b.get("open", 0.0) or 0.0),
                "high": float(b.get("high", 0.0) or 0.0),
                "low": float(b.get("low", 0.0) or 0.0),
                "close": float(b.get("close", 0.0) or 0.0),
                "ts": str(b.get("ts_utc") or ""),
                # tick volume — the daemon has ALWAYS returned it
                # (ProtoOATrendbar.volume) but this normalization silently
                # dropped it, blocking any volume-profile work (found
                # 2026-07-11 probing the VP entry-producer premise).
                "volume": float(b.get("volume", 0.0) or 0.0),
            }
            for b in bars_raw
            if isinstance(b, dict)
        ]
        bars.sort(key=lambda b: b["ts"])
        return bars[-count:] if count and len(bars) > count else bars

    def get_spot_price(self, symbol: str) -> dict[str, Any]:
        """Live bid/ask.

        Daemon mode (``DEXTER3_OPENAPI_DAEMON_URL`` set): served by the
        daemon-only ``spot_quote`` mode from its standing spot-subscription
        cache with a staleness bound (see ``dexter3/openapi_daemon.py``'s
        ``DEFAULT_SPOT_MAX_AGE_SEC`` rationale). A ``spot_stale`` answer is
        a definitive tool-level failure, so ``_invoke`` raises
        ``McpClientError`` — the entry pipeline's HARD VETO stays
        fail-closed instead of pricing off an old quote (the exact failure
        the VM shadow hit 2026-07-10 03:21Z: spread_abs=0.0 from an empty
        short capture window).

        Subprocess mode (unset, historical default): a short
        ``capture_market`` subscribe window, unchanged. Both paths return
        the same shape and raise when no usable quote exists."""
        sym = str(symbol or "").strip().upper()
        if self.daemon_url:
            raw = self._invoke(
                "spot_quote",
                self._payload(symbol=sym),
                mutating=False,
                timeout_sec=self.timeout_sec,
            )
            spots = raw.get("spots") or []
            if not spots:
                raise McpClientError(f"no live quote for {sym} via daemon spot_quote (empty spots)")
            latest = spots[-1]
            return {
                "bid": float(latest.get("bid", 0.0) or 0.0),
                "ask": float(latest.get("ask", 0.0) or 0.0),
                "symbol": sym,
                "quote_source": "openapi_daemon_spot_cache",
                "event_utc": str(latest.get("event_utc") or ""),
                "quote_age_sec": raw.get("quote_age_sec"),
            }
        payload = self._payload(
            symbols=[sym],
            include_depth=False,
            duration_sec=DEFAULT_QUOTE_DURATION_SEC,
            max_events=DEFAULT_QUOTE_MAX_EVENTS,
        )
        raw = self._invoke(
            "capture_market",
            payload,
            mutating=False,
            timeout_sec=max(self.timeout_sec, DEFAULT_QUOTE_DURATION_SEC + 10),
        )
        spots = raw.get("spots") or []
        if not spots:
            raise McpClientError(f"no live quote for {sym} via capture_market (0 spot events in capture window)")
        latest = spots[-1]
        bid = float(latest.get("bid", 0.0) or 0.0)
        ask = float(latest.get("ask", 0.0) or 0.0)
        return {
            "bid": bid,
            "ask": ask,
            "symbol": sym,
            "quote_source": "openapi_capture_market",
            "event_utc": str(latest.get("event_utc") or ""),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        # The OM only needs the live open book.  Do not make its every-tick
        # read wait on the historical deal list: that broker request has a
        # separate, materially slower tail and was the actual source of the
        # daemon's former 5s read timeouts.
        raw = self._reconcile(include_deals=False)
        positions = raw.get("positions") or []
        normed = [self._normalize_position_for_dexter3(p) for p in positions if isinstance(p, dict)]
        self._enrich_positions_with_live_pnl(normed)
        return normed

    def _enrich_positions_with_live_pnl(self, positions: list[dict[str, Any]]) -> None:
        """Attach a live-computed ``netProfit`` per position (gap #3 fix).

        OpenAPI positions have no PnL field, which left the basket/OM blind
        (pnl=0.0 → unreliable → HOLD forever → losers ran to broker SL). We
        compute netProfit from the daemon's live spot: SHORT exits at ask,
        LONG at bid; pnl = price_diff × volume_units × usd_point_value + swap +
        commission. Fetches spot ONCE per symbol. NEVER raises: if spot is
        unavailable (market closed / stale quote / unknown symbol point value),
        the position keeps NO netProfit — basket then reports ``unreliable`` and
        HOLDs, which is correct when there is genuinely no live price to manage
        against (e.g. weekend). Only a fresh quote unblocks active management."""
        if not positions:
            return
        spot_cache: dict[str, dict[str, float] | None] = {}
        for pos in positions:
            symbol = str(pos.get("symbol") or "").strip().upper()
            point_value = _USD_POINT_VALUE_PER_UNIT.get(symbol)
            if not point_value:
                continue  # unknown contract — do not fabricate a PnL
            if symbol not in spot_cache:
                try:
                    spot_cache[symbol] = self.get_spot_price(symbol)
                except Exception:  # noqa: BLE001 - enrichment is best-effort; ANY
                    # spot failure (market closed, stale quote, transport) must
                    # leave the position blind → basket unreliable → HOLD, never
                    # break get_positions itself.
                    spot_cache[symbol] = None
            spot = spot_cache.get(symbol)
            if not spot:
                continue
            side = str(pos.get("tradeSide") or "").upper()
            entry = float(pos.get("entryPrice", 0.0) or 0.0)
            vol = float(pos.get("volume", 0.0) or 0.0)
            if entry <= 0.0 or vol <= 0.0:
                continue
            bid = float(spot.get("bid", 0.0) or 0.0)
            ask = float(spot.get("ask", 0.0) or 0.0)
            if bid <= 0.0 or ask <= 0.0:
                continue
            if side == "BUY":
                price_diff = bid - entry            # long closes at bid
            elif side == "SELL":
                price_diff = entry - ask            # short closes at ask
            else:
                continue
            gross = price_diff * vol * float(point_value)
            net = gross + float(pos.get("swap", 0.0) or 0.0) + float(pos.get("commission", 0.0) or 0.0)
            pos["netProfit"] = round(net, 4)
            pos["grossProfit"] = round(gross, 4)
            pos["pnl_source"] = "computed_from_live_spot"

    def get_balance(self) -> dict[str, Any]:
        raw = self._invoke(
            "health", self._payload(), mutating=False, timeout_sec=self.health_timeout_sec
        )
        money_digits = int(raw.get("money_digits", 2) or 2)
        scale = float(10 ** money_digits) if money_digits > 0 else 1.0
        raw_balance = raw.get("balance", 0.0)
        balance_usd = float(raw_balance or 0.0) / scale if scale > 0 else float(raw_balance or 0.0)
        return {
            "balance": balance_usd,
            "equity": None,  # gap #2 — not exposed by any existing worker mode
            "margin": None,  # gap #2
            "traderId": int(raw.get("account_id", self.account_id) or self.account_id),
            "accountType": None,  # gap #2
            "moneyDigits": money_digits,
            "leverageInCents": int(raw.get("leverage_in_cents", 0) or 0),
            "environment": str(raw.get("environment") or ""),
        }

    def get_symbol_details(self, symbol: str) -> dict[str, Any]:
        """Symbol trading spec via the daemon's ``symbol_details`` mode (closes
        migration gap #2). Only available in daemon mode — the one-shot
        subprocess worker has no symbol-meta mode, so without a daemon this
        still raises NotImplemented (execute_entry then fail-closes as before).

        Converts RAW cTrader ProtoOASymbol fields to the dexter3 dict shape the
        executor reads (minVolume/volumeStep/maxVolume in dexter3 units,
        pipSize, lotSize) using the SAME volume helper as positions/deals so
        the raw↔units boundary stays consistent. pipSize is DERIVED live from
        pipPosition (``10**-pipPosition``) — not hardcoded — with the known
        table used only as a sanity cross-check / fallback when the broker
        sends an unusable pipPosition."""
        if not self.daemon_url:
            raise Dexter3OpenApiNotImplementedError(
                f"get_symbol_details({symbol!r}) requires the OpenAPI daemon "
                "(DEXTER3_OPENAPI_DAEMON_URL). The one-shot subprocess worker has no "
                "symbol-meta mode; execute_entry fail-closes without it."
            )
        raw = self._invoke(
            "symbol_details",
            {**self._payload(), "symbol": str(symbol)},
            mutating=False,
            timeout_sec=self.health_timeout_sec,
        )
        pip_position = int(raw.get("pipPosition", 0) or 0)
        pip_size = 10.0 ** (-pip_position) if pip_position > 0 else 0.0
        known = _PIP_SIZE_BY_SYMBOL.get(str(symbol).upper())
        if pip_size <= 0.0:
            # Broker sent an unusable pipPosition — fall back to the known table
            # rather than trade with a zero pip size (which would corrupt SL/TP).
            pip_size = float(known) if known else 0.0
        elif known is not None and abs(pip_size - known) > 1e-9:
            logging.getLogger("dexter3.openapi_client").warning(
                "get_symbol_details(%s): derived pipSize %s disagrees with known table %s "
                "(using LIVE derived value)", symbol, pip_size, known
            )
        return {
            "symbolName": str(raw.get("symbolName") or symbol),
            "minVolume": _raw_volume_to_units(raw.get("minVolume_raw", 0)),
            "volumeStep": _raw_volume_to_units(raw.get("stepVolume_raw", 0)),
            "maxVolume": _raw_volume_to_units(raw.get("maxVolume_raw", 0)),
            # lotSize = raw-volume units per one dexter3 sizing unit, i.e. the
            # conversion scale itself (informational — executor's sizing math
            # uses minVolume/volumeStep, not lotSize).
            "lotSize": float(UNITS_TO_RAW_SCALE),
            "pipSize": pip_size,
            "digits": int(raw.get("digits", 0) or 0),
        }

    def get_pending_orders(self) -> list[dict[str, Any]]:
        raw = self._reconcile(include_deals=False)
        orders = raw.get("orders") or []
        return [self._normalize_order_for_dexter3(o) for o in orders if isinstance(o, dict)]

    def get_deals(self, count: int = 200) -> list[dict[str, Any]]:
        raw = self._reconcile(max_rows=max(1, int(count)), with_deal_labels=True)
        deals = raw.get("deals") or []
        return [self._normalize_deal_for_dexter3(d) for d in deals if isinstance(d, dict)][: max(1, int(count))]

    # -- internal: reconcile (positions + orders + deals in one worker call) -

    def _reconcile(
        self,
        *,
        lookback_hours: int = 72,
        max_rows: int = 200,
        with_deal_labels: bool = False,
        include_deals: bool = True,
    ) -> dict[str, Any]:
        # with_deal_labels gates the deal->order label join (an extra broker
        # round-trip): only get_deals (governor realized-PnL, called rarely +
        # cached) needs it. get_positions runs the lane-verification EVERY bar
        # and must stay fast — paying the join there caused reconcile to exceed
        # the daemon-mode 5s client timeout -> live_skipped_lane_unverified
        # (observed at cutover 2026-07-10 10:36Z).
        payload = self._payload(lookback_hours=int(lookback_hours), max_rows=int(max_rows))
        # This flag is additive: the legacy subprocess worker ignores it,
        # while the daemon can skip ProtoOADealListReq for OM/entry reads.
        # Keep historical deals on by default for compatibility with callers
        # which consume the raw reconciliation result.
        payload["include_deals"] = bool(include_deals)
        if with_deal_labels:
            payload["include_deal_labels"] = True
            # The labeled path runs TWO extra heavy broker round-trips
            # (ProtoOADealListReq + ProtoOAOrderListReq join) on top of the
            # reconcile — measured ~8s even on an empty account (2026-07-10),
            # more with real deals, which legitimately exceeds the 5s daemon
            # client timeout get_positions needs. get_deals is rare + cached
            # (governor realized-PnL), so it gets a longer dedicated timeout;
            # get_positions (unlabeled, every-bar) keeps self.timeout_sec.
            try:
                deals_timeout = float(os.environ.get("DEXTER3_OPENAPI_DEALS_TIMEOUT_SEC", "20") or 20.0)
            except ValueError:
                deals_timeout = 20.0
            return self._invoke(
                "reconcile", payload, mutating=False,
                timeout_sec=max(float(self.timeout_sec), deals_timeout),
            )
        return self._invoke("reconcile", payload, mutating=False, timeout_sec=self.timeout_sec)

    # -- normalization: worker's already-normalized reconcile shapes ---------
    # (ops/ctrader_execute_once.py::_normalize_position / _normalize_deal
    # already converted raw protobuf -> snake_case dicts; this step re-maps
    # THOSE into the camelCase Dexter3McpClient/Local-MCP shape dexter3 code
    # actually reads — see dexter3/executor.py's position_*_of() helpers and
    # dexter3/basket_live.py's _position_*() helpers for the confirmed keys.)

    @staticmethod
    def _normalize_position_for_dexter3(p: dict[str, Any]) -> dict[str, Any]:
        direction = str(p.get("direction") or "").strip().lower()
        trade_side = "BUY" if direction == "long" else ("SELL" if direction == "short" else "")
        volume_units = _raw_volume_to_units(p.get("volume", 0))
        open_ms = p.get("open_timestamp_ms", 0)
        symbol = str(p.get("symbol") or "").strip().upper()
        return {
            "positionId": int(p.get("position_id", 0) or 0),
            "id": int(p.get("position_id", 0) or 0),
            "symbolName": symbol,
            "symbol": symbol,
            "tradeSide": trade_side,
            "side": trade_side,
            "volume": volume_units,
            "volumeInUnits": volume_units,
            "entryPrice": float(p.get("entry_price", 0.0) or 0.0),
            "price": float(p.get("entry_price", 0.0) or 0.0),
            "stopLoss": float(p.get("stop_loss", 0.0) or 0.0),
            "takeProfit": float(p.get("take_profit", 0.0) or 0.0),
            "label": str(p.get("label") or ""),
            "comment": str(p.get("comment") or ""),
            "openTime": _ms_to_iso_with_millis(open_ms),
            "openTimestamp": int(open_ms or 0),
            "status": str(p.get("status") or ""),
            "swap": float(p.get("swap", 0.0) or 0.0),
            "commission": float(p.get("commission", 0.0) or 0.0),
            "usedMargin": float(p.get("used_margin", 0.0) or 0.0),
            # PnL is attached by _enrich_positions_with_live_pnl after a
            # fresh daemon spot read; absent a fresh quote it stays missing
            # and basket_live deliberately holds fail-closed.
        }

    @staticmethod
    def _normalize_deal_for_dexter3(d: dict[str, Any]) -> dict[str, Any]:
        direction = str(d.get("direction") or "").strip().lower()
        trade_side = "BUY" if direction == "long" else ("SELL" if direction == "short" else "")
        exec_ms = d.get("execution_timestamp_ms", 0)
        symbol = str(d.get("symbol") or "").strip().upper()
        return {
            "dealId": int(d.get("deal_id", 0) or 0),
            "orderId": int(d.get("order_id", 0) or 0),
            "positionId": int(d.get("position_id", 0) or 0),
            "symbolName": symbol,
            "symbol": symbol,
            "tradeSide": trade_side,
            "volume": _raw_volume_to_units(d.get("volume", 0)),
            "executionPrice": float(d.get("execution_price", 0.0) or 0.0),
            "time": _ms_to_iso_with_millis(exec_ms),
            "executionTimestamp": int(exec_ms or 0),
            "dealStatus": str(d.get("deal_status") or ""),
            "netProfit": float(d.get("pnl_usd", 0.0) or 0.0),
            "grossProfit": float(d.get("gross_profit_usd", 0.0) or 0.0),
            "swap": float(d.get("swap_usd", 0.0) or 0.0),
            "commission": float(d.get("commission_usd", 0.0) or 0.0),
            # Gap #4 CLOSED (daemon mode): the daemon's _mode_reconcile joins
            # each deal to its order's label via ProtoOAOrderListReq. Subprocess
            # mode still has no join -> stays "" (governor degrades as before).
            "label": str(d.get("label", "") or ""),
            "comment": str(d.get("comment", "") or ""),
        }

    @staticmethod
    def _normalize_order_for_dexter3(o: dict[str, Any]) -> dict[str, Any]:
        # Best-effort (gap #5): reconcile's raw "order" list is a plain
        # MessageToDict dump of ProtoOAOrder, keyed the same way
        # ProtoOAPosition is (a nested "tradeData" carrying
        # symbolId/volume/tradeSide/label/openTimestamp).
        trade = dict(o.get("tradeData") or {})
        side_token = str(trade.get("tradeSide") or "").strip().upper()
        return {
            "orderId": int(o.get("orderId", 0) or 0),
            "positionId": int(o.get("positionId", 0) or 0),
            "tradeSide": side_token,
            "volume": _raw_volume_to_units(trade.get("volume", 0)),
            "limitPrice": float(o.get("limitPrice", 0.0) or 0.0),
            "stopPrice": float(o.get("stopPrice", 0.0) or 0.0),
            "stopLoss": float(o.get("stopLoss", 0.0) or 0.0),
            "takeProfit": float(o.get("takeProfit", 0.0) or 0.0),
            "label": str(trade.get("label") or ""),
            "comment": str(trade.get("comment") or ""),
            "orderStatus": str(o.get("orderStatus") or ""),
        }

    # -- Phase 2 mutating surface (dexter3/executor.py ONLY) ------------------

    def place_market_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        stop_loss_pips: int,
        take_profit_pips: int,
        label: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """Mirrors ``Dexter3McpClient.place_market_order``'s signature
        exactly (pip-DISTANCE SL/TP, units volume). The worker's order path
        needs ABSOLUTE prices to compute the broker's relative SL/TP offset,
        so this method: (1) reads a fresh quote via ``get_spot_price``,
        (2) resolves pip size for ``symbol`` (gap #6), (3) converts pips ->
        absolute stop/take prices around the quote, (4) sends volume as a
        ``fixed_volume`` (raw units) so the worker does not re-derive size
        from risk_usd — dexter3 already computed the exact size it wants.
        """
        sym = str(symbol or "").strip().upper()
        pip_size = _PIP_SIZE_BY_SYMBOL.get(sym)
        if pip_size is None:
            raise McpClientError(
                f"place_market_order: no known pip size for symbol={sym!r} "
                f"(only {sorted(_PIP_SIZE_BY_SYMBOL)} are configured — see gap #6); "
                "refusing to guess rather than risk a wrong SL/TP distance."
            )
        side_norm = str(side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            raise McpClientError(f"place_market_order: invalid side {side!r}, expected 'buy'/'sell'")
        direction = "long" if side_norm == "buy" else "short"

        quote = self.get_spot_price(sym)
        entry_estimate = float(quote["ask"]) if side_norm == "buy" else float(quote["bid"])
        sl_distance = abs(int(stop_loss_pips)) * pip_size
        tp_distance = abs(int(take_profit_pips)) * pip_size
        if side_norm == "buy":
            stop_loss_price = entry_estimate - sl_distance
            take_profit_price = entry_estimate + tp_distance
        else:
            stop_loss_price = entry_estimate + sl_distance
            take_profit_price = entry_estimate - tp_distance

        payload = self._payload(
            symbol=sym,
            direction=direction,
            order_type="market",
            entry=entry_estimate,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            fixed_volume=_units_to_raw_volume(volume),
            label=str(label or "")[:64],
            comment=str(comment or "")[:128],
        )
        raw = self._invoke("execute", payload, mutating=True, timeout_sec=self.timeout_sec)
        return {
            "ok": bool(raw.get("ok")),
            "status": str(raw.get("status") or ""),
            "orderId": raw.get("order_id"),
            "positionId": raw.get("position_id"),
            "dealId": raw.get("deal_id"),
            "volume": _raw_volume_to_units(
                (raw.get("execution_meta") or {}).get("volume_meta", {}).get("rounded_units")
                or _units_to_raw_volume(volume)
            ),
            "message": str(raw.get("message") or ""),
        }

    def amend_position(
        self,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Mirrors ``Dexter3McpClient.amend_position``: ABSOLUTE prices,
        only non-None legs are sent (the worker only applies a leg when its
        payload value is > 0, which is naturally satisfied by omitting a
        None leg as 0.0)."""
        payload = self._payload(
            position_id=int(position_id),
            stop_loss=float(stop_loss) if stop_loss is not None else 0.0,
            take_profit=float(take_profit) if take_profit is not None else 0.0,
            trailing_stop_loss=False,
        )
        raw = self._invoke("amend_position_sltp", payload, mutating=True, timeout_sec=self.timeout_sec)
        return {
            "ok": bool(raw.get("ok")),
            "status": str(raw.get("status") or ""),
            "positionId": raw.get("position_id"),
            "message": str(raw.get("message") or ""),
        }

    def close_position(self, position_id: int) -> dict[str, Any]:
        """Mirrors ``Dexter3McpClient.close_position`` (no volume param —
        full close). cTrader rejects ``closeVolume=0`` (the SAME lesson
        ``execution/ctrader_executor.py::close_position`` documents), so
        this resolves the position's ACTUAL raw volume via a fresh reconcile
        before sending the close request."""
        pid = int(position_id)
        raw_positions = (self._reconcile(include_deals=False).get("positions")) or []
        match = next((p for p in raw_positions if int(p.get("position_id", 0) or 0) == pid), None)
        if match is None:
            raise McpClientError(f"close_position: position_id={pid} not found in current reconcile (already closed?)")
        raw_volume = int(match.get("volume", 0) or 0)
        payload = self._payload(position_id=pid, volume=raw_volume)
        raw = self._invoke("close", payload, mutating=True, timeout_sec=self.timeout_sec)
        return {
            "ok": bool(raw.get("ok")),
            "status": str(raw.get("status") or ""),
            "positionId": raw.get("position_id"),
            "orderId": raw.get("order_id"),
            "dealId": raw.get("deal_id"),
            "message": str(raw.get("message") or ""),
        }

    # -- generic dispatch (parity with Dexter3McpClient.call) ----------------

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Best-effort dispatch to the named methods above for the small set
        of dexter3 call sites that invoke ``.call(...)`` directly (currently
        only ``dexter3.shadow_runner._maybe_alert_account_guard``'s
        ``show_notification``, which has no OpenAPI equivalent and is always
        wrapped in a catch-all by its caller). Raises ``McpClientError`` on
        any unrecognized tool name, matching ``Dexter3McpClient.call``'s
        "raises on unknown" contract.
        """
        a = dict(args or {})
        dispatch = {
            "get_trendbars": lambda: self.get_trendbars(a.get("symbolName", ""), a.get("timeframe", "m5"), a.get("limit", 60)),
            "get_spot_prices": lambda: self.get_spot_price(a.get("symbolName", "")),
            "get_positions": self.get_positions,
            "get_balance": self.get_balance,
            "get_symbol_details": lambda: self.get_symbol_details(a.get("symbolName", "")),
            "get_pending_orders": self.get_pending_orders,
            "get_deals": lambda: self.get_deals(a.get("count", 200)),
            "place_market_order": lambda: self.place_market_order(
                a.get("symbolName", ""),
                a.get("side", ""),
                a.get("volume", 0.0),
                a.get("stopLossPips", 0),
                a.get("takeProfitPips", 0),
                a.get("label", ""),
                a.get("comment", ""),
            ),
            "amend_position": lambda: self.amend_position(
                a.get("positionId", 0), a.get("stopLoss"), a.get("takeProfit")
            ),
            "close_position": lambda: self.close_position(a.get("positionId", 0)),
        }
        fn = dispatch.get(name)
        if fn is None:
            raise McpClientError(f"unknown OpenAPI client tool: {name!r}")
        return fn()

    def close_session(self) -> None:
        """No-op: this transport has no persistent session to tear down
        (every call is its own subprocess). Kept for interface parity with
        ``Dexter3McpClient.close_session``."""
        return None


_PERIOD_MINUTES = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "h1": 60, "h4": 240, "d1": 1440}

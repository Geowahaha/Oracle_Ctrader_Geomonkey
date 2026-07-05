"""Thin HTTP client for the local cTrader MCP (Dexter3).

Mirrors the proven request/response envelope used by
``scripts/ctrader_mcp_client.py`` (Streamable HTTP transport: JSON-RPC 2.0
over POST, session id from the ``Mcp-Session-Id`` response header, body may
arrive as raw JSON or as a single SSE ``data:`` frame). This module does NOT
invent a new protocol — it copies that parsing exactly.

Phase 1 read-only surface:

    get_trendbars(symbol, period, count)
    get_spot_price(symbol)
    get_positions()
    get_balance()

Phase 2 adds mutating methods (``dexter3/executor.py`` is the ONLY dexter3
module allowed to call them — see docs/DEXTER3_M5_HUNTER_BLUEPRINT.md
"Non-negotiables"). The envelopes below are copied field-for-field from
``scripts/btc_scalp_monitor.py`` (the production-proven order path against
this same local MCP), NOT reinvented:

    place_market_order(symbol, side, volume, stop_loss_pips, take_profit_pips, label, comment)
    amend_position(position_id, stop_loss, take_profit)
    close_position(position_id)

Order-placement tools take SL/TP as PIP DISTANCE (``stopLossPips``/
``takeProfitPips``); ``amend_position`` takes ABSOLUTE prices (``stopLoss``/
``takeProfit``) — this asymmetry is intentional broker/MCP behavior, not a
bug (see the ctrader-mcp-servers skill, "Stop loss and take profit: pip
distance vs absolute price").

Zombie detection: an HTTP 404 from the MCP endpoint (including a 404 raised
during ``initialize``) means the local MCP server is a "zombie" — it still
accepts TCP but the app-level MCP session is dead. That case raises
``McpZombieError`` pointing at the recovery runbook instead of retrying
forever. See docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md and
scripts/ctrader_mcp_watchdog.py --restart.

This client never busy-loops: at most one retry with a short backoff per
call, then it raises.
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

DEFAULT_MCP_URL = "http://127.0.0.1:9876/mcp/"
DEFAULT_TIMEOUT_SEC = 15.0
DEFAULT_RETRY_BACKOFF_SEC = 1.5

ZOMBIE_RECOVERY_HINT = (
    "cTrader MCP appears to be a zombie (HTTP 404 from the MCP endpoint). "
    "Follow docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md — run "
    "`python scripts/ctrader_mcp_watchdog.py --restart` — then retry. "
    "Do not busy-loop against a zombie MCP."
)


class McpZombieError(RuntimeError):
    """Raised when the local MCP endpoint returns HTTP 404 (zombie server)."""

    def __init__(self, detail: str = "") -> None:
        message = ZOMBIE_RECOVERY_HINT if not detail else f"{ZOMBIE_RECOVERY_HINT} detail={detail}"
        super().__init__(message)
        self.detail = detail


class McpClientError(RuntimeError):
    """Raised for any other MCP transport/protocol failure (not a zombie)."""


def parse_mcp_body(raw: str) -> dict[str, Any]:
    """Parse an MCP HTTP response body.

    Mirrors ``scripts/ctrader_mcp_client.py::parse_mcp_body`` — the body is
    either raw JSON, or a single Server-Sent-Events frame with a ``data:``
    line carrying the JSON payload.
    """
    text = raw.strip() if raw else ""
    if not text:
        raise McpClientError("empty MCP response body")
    if "event:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        raise McpClientError(f"no SSE data frame in MCP response: {text[:200]}")
    return json.loads(text)


class Dexter3McpClient:
    """Read-only MCP client for Dexter3 Phase 1.

    One retry with backoff per ``call()``; explicit ``McpZombieError`` on
    HTTP 404 so callers (the shadow loop) can log loudly, sleep, and
    continue instead of hammering a dead server.
    """

    def __init__(
        self,
        url: str = DEFAULT_MCP_URL,
        client_name: str = "dexter3-m5-hunter",
        client_version: str = "0.1.0-shadow",
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        retry_backoff_sec: float = DEFAULT_RETRY_BACKOFF_SEC,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url
        self.client_name = client_name
        self.client_version = client_version
        self.timeout_sec = float(timeout_sec)
        self.retry_backoff_sec = float(retry_backoff_sec)
        self._session = session or requests.Session()
        self.sid: str | None = None

    # -- low-level transport -------------------------------------------------

    def _post(
        self,
        payload: dict[str, Any],
        *,
        include_session: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_session and self.sid:
            headers["Mcp-Session-Id"] = self.sid
        try:
            resp = self._session.post(
                self.url,
                data=json.dumps(payload),
                headers=headers,
                timeout=self.timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            raise McpClientError(f"MCP request failed: {exc}") from exc

        if resp.status_code == 404:
            raise McpZombieError(f"HTTP 404 from {self.url}")
        if resp.status_code >= 400:
            detail = (resp.text or "")[:300]
            raise McpClientError(f"MCP HTTP {resp.status_code}: {detail or resp.reason}")

        if not self.sid:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid

        raw = resp.text or ""
        if not raw.strip():
            return {}
        return parse_mcp_body(raw)

    def _initialize(self) -> None:
        body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
            },
            include_session=False,
        )
        if body.get("error"):
            raise McpClientError(f"MCP initialize error: {body['error']}")
        if not self.sid:
            raise McpClientError("MCP initialize missing Mcp-Session-Id header")
        try:
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                include_session=True,
            )
        except McpClientError:
            # cTrader may return an empty 200/202 body for this notification.
            pass

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool with one retry + short backoff. Raises on failure.

        Never busy-loops: a single retry, then the exception propagates
        (``McpZombieError`` for a dead server, ``McpClientError`` otherwise).
        """
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return self._call_once(name, args)
            except McpZombieError:
                raise  # zombie is not retryable here — caller must recover
            except McpClientError as exc:
                last_exc = exc
                self.sid = None  # force a fresh session on retry
                if attempt == 0:
                    time.sleep(self.retry_backoff_sec)
        assert last_exc is not None
        raise last_exc

    def _call_once(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if not self.sid:
            self._initialize()
        body = self._post(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": args or {}},
            }
        )
        if body.get("error"):
            raise McpClientError(f"MCP tool error {name}: {body['error']}")
        result = body.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or [{}]
            text = content[0].get("text", "unknown MCP tool error")
            raise McpClientError(f"MCP tool {name} returned error: {text}")
        content = result.get("content") or []
        if not content:
            raise McpClientError(f"MCP tool {name} returned no content")
        text = content[0].get("text")
        if text is None:
            raise McpClientError(f"MCP tool {name} content missing text field")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpClientError(f"MCP tool {name} returned non-JSON text: {text[:200]}") from exc

    # -- Phase 1 read-only surface --------------------------------------------

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict[str, Any]]:
        """Fetch up to ``count`` trendbars for ``symbol`` at ``period`` (e.g. m5/m15/h1).

        Returns bars sorted oldest-to-newest, each normalized to
        open/high/low/close/ts (float OHLC, ISO-8601 ``Z`` timestamp string).

        The ``from``/``to`` window is padded with a small safety margin
        (a few extra bar-periods) beyond the theoretical ``count`` span:
        "now" rarely lands exactly on a bar-close boundary, so an
        unpadded window can return one bar short of what was asked for.
        The result is still trimmed to at most ``count`` bars (the most
        recent ones) before returning.
        """
        now = _utc_now()
        safety_margin_bars = max(3, int(count * 0.1))
        minutes = _period_minutes(period) * (max(int(count), 1) + safety_margin_bars)
        data = self.call(
            "get_trendbars",
            {
                "symbolName": symbol,
                "timeframe": period,
                "from": _iso_z(now - _timedelta_minutes(minutes)),
                "to": _iso_z(now),
                "limit": max(int(count), 1) + safety_margin_bars,
            },
        )
        bars_raw = data.get("bars", []) if isinstance(data, dict) else []
        bars = [_normalize_bar(b) for b in bars_raw]
        bars.sort(key=lambda b: b["ts"])
        return bars[-count:] if count and len(bars) > count else bars

    def get_spot_price(self, symbol: str) -> dict[str, Any]:
        """Fetch the current spot snapshot (bid/ask/day high/low) for ``symbol``.

        Tool name is PLURAL on the Local MCP even for one symbol —
        "get_spot_price" (singular) returns "Unknown tool" (live-verified
        2026-07-05; matches scripts/btc_scalp_monitor.py::spot_quote).
        """
        data = self.call("get_spot_prices", {"symbolName": symbol})
        if not isinstance(data, dict):
            raise McpClientError(f"get_spot_prices returned unexpected payload: {data!r}")
        return data

    def get_positions(self) -> list[dict[str, Any]]:
        """Fetch all open positions across symbols (read-only)."""
        data = self.call("get_positions")
        if isinstance(data, dict):
            return list(data.get("positions", []))
        if isinstance(data, list):
            return data
        raise McpClientError(f"get_positions returned unexpected payload: {data!r}")

    def get_balance(self) -> dict[str, Any]:
        """Fetch account balance/equity snapshot (read-only)."""
        data = self.call("get_balance")
        if not isinstance(data, dict):
            raise McpClientError(f"get_balance returned unexpected payload: {data!r}")
        return data

    def get_symbol_details(self, symbol: str) -> dict[str, Any]:
        """Fetch symbol trading spec (minVolume/volumeStep/lotSize/pipSize) — read-only."""
        data = self.call("get_symbol_details", {"symbolName": symbol})
        if not isinstance(data, dict):
            raise McpClientError(f"get_symbol_details returned unexpected payload: {data!r}")
        return data

    def get_pending_orders(self) -> list[dict[str, Any]]:
        """Fetch all pending (unfilled) orders across symbols (read-only)."""
        data = self.call("get_pending_orders")
        if isinstance(data, dict):
            return list(data.get("orders", []))
        if isinstance(data, list):
            return data
        raise McpClientError(f"get_pending_orders returned unexpected payload: {data!r}")

    # -- Phase 2 mutating surface (dexter3/executor.py ONLY) -----------------
    #
    # Envelopes copied field-for-field from scripts/btc_scalp_monitor.py —
    # see module docstring. Do not invent new parameter names here; if the
    # broker rejects a call, fix the envelope to match the proven monitor,
    # not the other way around.

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
        """Place a MARKET order with SL/TP attached (pip distance — see module docstring).

        Mirrors ``scripts/btc_scalp_monitor.py::run_cycle``'s
        ``mcp.tool("place_market_order", {...})`` call exactly (volumeType
        "units", label + comment for peer-label isolation).
        """
        data = self.call(
            "place_market_order",
            {
                "symbolName": symbol,
                "side": side,
                "volume": float(volume),
                "volumeType": "units",
                "stopLossPips": int(stop_loss_pips),
                "takeProfitPips": int(take_profit_pips),
                "label": label,
                "comment": comment[:55],
            },
        )
        return data if isinstance(data, dict) else {"raw": data}

    def amend_position(
        self,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Amend an OPEN position's SL/TP (ABSOLUTE prices — see module docstring).

        Mirrors ``scripts/btc_scalp_monitor.py::repair_missing_sltp`` /
        ``defend_loop_position``'s ``mcp.tool("amend_position", {...})`` call.
        Only non-None values are sent so a partial amend (e.g. SL only) does
        not clobber the other leg — mirrors the monitor's own call sites,
        which always pass both together, but keeps this method safe for
        callers that only need to fix one side.
        """
        payload: dict[str, Any] = {"positionId": int(position_id)}
        if stop_loss is not None:
            payload["stopLoss"] = round(float(stop_loss), 5)
        if take_profit is not None:
            payload["takeProfit"] = round(float(take_profit), 5)
        data = self.call("amend_position", payload)
        return data if isinstance(data, dict) else {"raw": data}

    def close_position(self, position_id: int) -> dict[str, Any]:
        """Close an OPEN position entirely.

        Mirrors ``scripts/xau_scalp_monitor.py``'s repeated
        ``mcp.tool("close_position", {"positionId": pid})`` call sites (no
        volume parameter — full close).
        """
        data = self.call("close_position", {"positionId": int(position_id)})
        return data if isinstance(data, dict) else {"raw": data}


# -- small time/normalization helpers (kept local to avoid new deps) --------


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _timedelta_minutes(minutes: int):
    from datetime import timedelta

    return timedelta(minutes=minutes)


def _iso_z(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _period_minutes(period: str) -> int:
    table = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "h1": 60, "h4": 240, "d1": 1440}
    return table.get(period.lower(), 5)


def _normalize_bar(bar: dict[str, Any]) -> dict[str, Any]:
    ts_raw = bar.get("timestamp") or bar.get("time") or bar.get("ts") or ""
    return {
        "open": float(bar.get("open", 0) or 0),
        "high": float(bar.get("high", 0) or 0),
        "low": float(bar.get("low", 0) or 0),
        "close": float(bar.get("close", 0) or 0),
        "ts": str(ts_raw),
    }

"""Robust cTrader local MCP HTTP client (Streamable HTTP transport).

Fixes recurring cTrader MCP hangs observed when:
- SSE responses are parsed as raw JSON
- notifications/initialized is omitted after initialize
- stale Mcp-Session-Id is reused after server-side session reset (HTTP 404)
- clients hammer reconnect every 30s during outages
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://127.0.0.1:9876/mcp/"
SESSION_MAX_AGE_SEC = 600.0
DEFAULT_TOOL_TIMEOUT_SEC = 30.0
DELETE_SESSION_TIMEOUT_SEC = 5.0
KEEPALIVE_INTERVAL_SEC = 45.0
KEEPALIVE_FAILURE_BACKOFF_SEC = 5.0
MAX_CONSECUTIVE_KEEPALIVE_FAILURES = 3
KEEPALIVE_STOP_JOIN_TIMEOUT_SEC = 5.0


def parse_mcp_body(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        raise RuntimeError("empty MCP response body")
    if "event:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        raise RuntimeError(f"no SSE data frame in MCP response: {text[:200]}")
    return json.loads(text)


class McpKeepaliveThread(threading.Thread):
    """Daemon thread that pings get_server_time() to prevent HTTP session idle timeout."""

    def __init__(self, client: CtraderMcpClient, interval_sec: float = KEEPALIVE_INTERVAL_SEC) -> None:
        super().__init__(daemon=True, name="ctrader-mcp-keepalive")
        self._client = client
        self._interval = interval_sec
        self._stop_event = threading.Event()
        self._consecutive_failures = 0

    def run(self) -> None:
        logger.info("MCP keepalive thread started (interval=%ss)", self._interval)
        while not self._stop_event.wait(self._interval):
            self._ping_once()
        logger.info("MCP keepalive thread stopped %s", self._client.keepalive_stats())

    def _ping_once(self) -> None:
        # Lock scope is intentionally narrow: skip-check and ping only.
        # ensure_connected() / retry sleeps MUST stay outside any with self._lock
        # block here — nested RLock acquire would still hold the outer scope.
        with self._client._lock:
            if time.time() - self._client._last_tool_call_at < self._interval * 0.8:
                self._client.keepalive_skip_count += 1
                return

        try:
            # tool() acquires/releases lock around HTTP — never hold lock across I/O.
            self._client.tool("get_server_time")
            with self._client._lock:
                self._client.keepalive_ping_count += 1
            if self._consecutive_failures > 0:
                logger.info(
                    "Keepalive recovered after %d failure(s)",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
        except Exception as exc:
            self._consecutive_failures += 1
            with self._client._lock:
                self._client.keepalive_failure_count += 1
            logger.warning(
                "Keepalive ping failed (%d/%d): %s",
                self._consecutive_failures,
                MAX_CONSECUTIVE_KEEPALIVE_FAILURES,
                exc,
            )
            if self._consecutive_failures >= MAX_CONSECUTIVE_KEEPALIVE_FAILURES:
                logger.warning("Keepalive: resetting session and forcing reconnect")
                # No lock held — ensure_connected() retry sleeps release the lock fully.
                try:
                    self._client.ensure_connected(retries=3, delay=4.0)
                    self._consecutive_failures = 0
                except Exception as reconnect_exc:
                    logger.error("Keepalive: reconnect also failed: %s", reconnect_exc)
                    time.sleep(KEEPALIVE_FAILURE_BACKOFF_SEC)

    def stop(self) -> None:
        self._stop_event.set()


class CtraderMcpClient:
    def __init__(
        self,
        url: str = DEFAULT_MCP_URL,
        client_name: str = "dexter-mcp-client",
        client_version: str = "1.0",
        tool_timeout_sec: float = DEFAULT_TOOL_TIMEOUT_SEC,
        timeout_sec: float | None = None,
    ) -> None:
        self.url = url
        self.client_name = client_name
        self.client_version = client_version
        resolved_timeout = timeout_sec if timeout_sec is not None else tool_timeout_sec
        if resolved_timeout <= 0:
            raise ValueError("tool_timeout_sec must be positive")
        self.tool_timeout_sec = float(resolved_timeout)
        self.sid: str | None = None
        self._session_started_at: float = 0.0
        self.consecutive_failures = 0
        self._lock = threading.RLock()
        self._keepalive_thread: McpKeepaliveThread | None = None
        self._last_tool_call_at: float = 0.0
        self.keepalive_ping_count = 0
        self.keepalive_skip_count = 0
        self.keepalive_failure_count = 0
        # SESSION HYGIENE (2026-07-09 root-cause fix for the recurring 404
        # zombie): consumers of this client historically leaked their MCP
        # session at process exit; the plugin's session table exhausted and
        # the handler 404'd everything until a full app restart. Deleting our
        # own session at exit keeps the table flat. _delete_session is a
        # no-op when no session exists, so unconditional registration is safe.
        atexit.register(self._delete_session)

    @property
    def timeout_sec(self) -> float:
        """Backward-compatible alias for tool_timeout_sec."""
        return self.tool_timeout_sec

    def _request(
        self,
        payload: dict[str, Any],
        *,
        include_session: bool = True,
        method: str = "POST",
        timeout_sec: float | None = None,
    ) -> tuple[dict[str, Any], int]:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_session and self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        request_timeout = self.tool_timeout_sec if timeout_sec is None else timeout_sec
        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                if not self.sid:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.sid = sid
                        self._session_started_at = time.time()
                raw = resp.read()
                if not raw.strip():
                    return {}, status
                body = parse_mcp_body(raw)
                return body, status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = raw.decode("utf-8", errors="replace")[:300] if raw else ""
            raise RuntimeError(f"MCP HTTP {exc.code}: {detail or exc.reason}") from exc

    def _initialize(self) -> None:
        body, _ = self._request(
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
            raise RuntimeError(f"MCP initialize error: {body['error']}")
        if not self.sid:
            raise RuntimeError("MCP initialize missing Mcp-Session-Id header")
        try:
            self._request(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                include_session=True,
            )
        except RuntimeError:
            # cTrader may return an empty 200/202 body for initialized notification.
            pass
        self._session_started_at = time.time()

    def _delete_session(self) -> None:
        if not self.sid:
            return
        sid = self.sid
        self.sid = None
        self._session_started_at = 0.0
        try:
            req = urllib.request.Request(
                self.url,
                headers={"Mcp-Session-Id": sid},
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=DELETE_SESSION_TIMEOUT_SEC):
                pass
        except Exception:
            pass

    def reset_session(self) -> None:
        with self._lock:
            self._reset_session_unlocked()

    def _reset_session_unlocked(self) -> None:
        self._delete_session()

    def session_age_sec(self) -> float:
        if not self._session_started_at:
            return 0.0
        return max(0.0, time.time() - self._session_started_at)

    def ensure_connected(self, retries: int = 3, delay: float = 4.0) -> None:
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            with self._lock:
                try:
                    self._ensure_connected_unlocked()
                    return
                except Exception as exc:
                    last_err = exc
            if attempt < retries:
                time.sleep(delay * attempt)
        with self._lock:
            self.consecutive_failures += 1
        raise RuntimeError(f"MCP unavailable after retries: {last_err}") from last_err

    def _ensure_connected_unlocked(self) -> None:
        if self.sid and self.session_age_sec() > SESSION_MAX_AGE_SEC:
            self._reset_session_unlocked()
        if self.sid:
            try:
                self._tool_unlocked("get_server_time")
                self.consecutive_failures = 0
                return
            except Exception:
                self._reset_session_unlocked()
        self._reset_session_unlocked()
        self._initialize()
        self._tool_unlocked("get_server_time")
        self.consecutive_failures = 0

    def tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        with self._lock:
            return self._tool_unlocked(name, args)

    def _tool_unlocked(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if not self.sid:
            self._initialize()
        self._last_tool_call_at = time.time()
        body, _ = self._request(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": args or {}},
            }
        )
        if body.get("error"):
            raise RuntimeError(f"MCP tool error {name}: {body['error']}")
        result = body.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        text = result["content"][0]["text"]
        return json.loads(text)

    def start_keepalive(self, interval_sec: float = KEEPALIVE_INTERVAL_SEC) -> None:
        with self._lock:
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return
            self._keepalive_thread = McpKeepaliveThread(self, interval_sec)
            self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        with self._lock:
            thread = self._keepalive_thread
            self._keepalive_thread = None
        if thread is not None:
            thread.stop()
            thread.join(timeout=KEEPALIVE_STOP_JOIN_TIMEOUT_SEC)
            if thread.is_alive():
                logger.warning(
                    "MCP keepalive thread still alive after join(%ss); "
                    "likely blocked on in-flight HTTP (tool_timeout_sec=%ss)",
                    KEEPALIVE_STOP_JOIN_TIMEOUT_SEC,
                    self.tool_timeout_sec,
                )
            else:
                logger.info("MCP keepalive stopped %s", self.keepalive_stats())

    def keepalive_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "keepalive_ping_count": self.keepalive_ping_count,
                "keepalive_skip_count": self.keepalive_skip_count,
                "keepalive_failure_count": self.keepalive_failure_count,
                "tool_timeout_sec": self.tool_timeout_sec,
                "keepalive_interval_sec": KEEPALIVE_INTERVAL_SEC,
            }

    def ping_health(self) -> dict[str, Any]:
        """Lightweight probe for watchdog scripts."""
        started = time.time()
        self.ensure_connected(retries=2, delay=2.0)
        ts = self.tool("get_server_time")
        return {
            "ok": True,
            "latency_ms": int((time.time() - started) * 1000),
            "session_id": (self.sid or "")[:8],
            "server_time": ts,
            **self.keepalive_stats(),
        }


def is_session_stale_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "404",
            "not found",
            "session",
            "unavailable",
            "empty mcp",
            "no sse data",
        )
    )
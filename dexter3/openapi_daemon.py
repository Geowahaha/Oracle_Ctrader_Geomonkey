"""
dexter3/openapi_daemon.py

Persistent-connection cTrader OpenAPI daemon for Dexter3 — closes gap #4 in
docs/DEXTER3_VM_MIGRATION_DESIGN.md ("PM risk (Fable)"): ``dexter3/openapi_client.py``'s
``_invoke`` spawns ``ops/ctrader_execute_once.py`` as a FRESH SUBPROCESS per
call — a full cTrader OpenAPI TCP+OAuth handshake every read (~18s measured
live on the VM) — unusable at the lanes' 8-20s scan cadence, and the same
connection-churn antipattern the MCP session-zombie incident already taught
this repo one layer up (see ``docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md``).

Run standalone:

    python -m dexter3.openapi_daemon

Serves ONE persistent Twisted-reactor cTrader OpenAPI connection to as many
local callers as need it, via a minimal JSON HTTP API on
``127.0.0.1:<DEXTER3_OPENAPI_DAEMON_PORT, default 9877>``:

    POST /call   {"mode": <same modes ops/ctrader_execute_once.py accepts:
                  health|accounts|reconcile|capture_market|get_trendbars|
                  execute|close|cancel_order|amend_order|
                  amend_position_sltp — plus the daemon-only "spot_quote",
                  which serves real-time bid/ask from the standing
                  spot-subscription cache below>, "payload": {...}}
        -> the SAME JSON envelope that worker prints for that mode (see
           ``ops/ctrader_execute_once.py`` and ``dexter3/openapi_client.py``'s
           module docstring for the confirmed field shapes this mirrors).
        Domain-level failures (broker rejection, unknown mode, no live
        connection) are always HTTP 200 with ``{"ok": false, "status": ...}``
        in the body — mirroring the one-shot worker's own convention of
        always exiting 0 and printing exactly one JSON line regardless of
        outcome. HTTP 4xx/5xx is reserved for genuine HTTP-protocol
        violations (bad path, bad method, unparseable body, an
        unhandled dispatcher exception).
    GET  /health -> connection state snapshot, see ``ConnectionState.health_payload``.

``dexter3/openapi_client.py`` posts to this daemon instead of spawning the
subprocess when ``DEXTER3_OPENAPI_DAEMON_URL`` is set (unset = unchanged
subprocess behavior; this is additive, see that module's ``_run_worker_once``).

DESIGN CONSTRAINTS (PM-decided, docs/DEXTER3_VM_MIGRATION_DESIGN.md gap #4):

  - Crib, don't import: this module re-derives the connection/auth/reconnect
    approach from ``execution/ctrader_stream.py`` (the proven persistent
    live-trading stream service, live since 2026-07-01) and the per-mode
    protobuf request/response shapes from ``ops/ctrader_execute_once.py``
    (the proven one-shot worker) — NEITHER file is modified, and neither is
    imported for its live/executable logic. The small set of pure
    normalization helpers below (position/deal/order shaping, volume
    quantization, symbol resolution) are duplicated rather than imported —
    this mirrors the precedent ``dexter3/openapi_client.py`` itself already
    set for the identical reason (see that module's ``_run_worker_once``
    docstring): ``ops/ctrader_execute_once.py`` is a script whose import-time
    behavior is ``raise SystemExit(0)`` if twisted/protobuf/ctrader_open_api
    are missing (fine for a disposable subprocess, fatal for a long-lived
    daemon module that must stay importable — e.g. for the HTTP-layer tests
    in an environment without those deps installed).

  - Token handling is READ-ONLY. This module calls ONLY
    ``token_manager.get_access_token()`` — never ``try_refresh()`` and never
    writes ``ctrader_token_state.json``. Refresh ownership belongs solely to
    ``scripts/ctrader_token_keepalive.py`` under the single-owner
    architecture shipped in commit ``da341f4`` ("cTrader refresh tokens are
    single-use/rotating — every successful refresh invalidates the previous
    pair"; a second refresher racing the owner is exactly the bug that
    commit fixed). On an invalid/expired token this daemon can only surface
    the failure via ``/health.last_error`` and keep retrying the CONNECTION
    itself with backoff — it never mints or persists a token.

  - Concurrency model: the HTTP listener runs on stdlib
    ``http.server.ThreadingHTTPServer`` (same pattern already used by
    ``notifier/billing_webhook.py`` in this repo) purely to accept/parse
    requests — deliberately NOT ``twisted.web``, so the HTTP routing/
    validation layer stays fully unit-testable with a plain Python
    dispatcher stub and zero Twisted reactor involvement (test requirement
    (a)). Every actual cTrader protobuf call is executed on the ONE Twisted
    reactor thread via ``twisted.internet.threads.blockingCallFromThread``:
    an HTTP handler thread blocks (only itself) waiting for its own
    Deferred to resolve on the reactor thread, so N concurrent HTTP callers
    become N blocked threads plus N in-flight Deferreds cooperatively
    interleaved on the single reactor — and the OpenAPI ``Client`` itself
    already demultiplexes those concurrent in-flight requests over the ONE
    TCP connection via a per-call ``clientMsgId`` (confirmed by reading
    ``ctrader_open_api.client.Client.send``: it keys
    ``self._responseDeferreds`` by ``str(id(deferred))`` per call and
    resolves each independently as its matching response arrives), so no
    additional locking is needed anywhere in this module — nothing ever
    touches ``self.client`` or the daemon's mutable state from more than one
    thread at a time.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from config import config  # noqa: E402
from api.ctrader_token_manager import token_manager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("dexter3_openapi_daemon")


def _fresh_access_token() -> str:
    """READ-ONLY access token that survives keepalive rotation.

    The single-owner keepalive rotates the token every ~30 min and each
    rotation INVALIDATES the previous access token broker-side. A process-
    lifetime cache therefore goes stale at the first rotation after daemon
    start — live incident 2026-07-10 03:52Z: mode=accounts (new auth on a
    temp client) failed "Invalid access token" while the already-authed demo
    connection kept working. Fix: adopt the newer on-disk state (persisted by
    the owner) before every NEW authentication. Never refreshes, never
    writes — single-owner discipline preserved.
    """
    try:
        disk = token_manager._load_state()
        if token_manager._disk_state_is_newer(disk):
            token_manager._adopt_disk_state(disk)
            logger.info("adopted newer on-disk token state before auth (keepalive rotation)")
    except Exception as exc:  # noqa: BLE001 - fall back to cached token
        logger.warning("disk token adopt failed (using cached): %s", exc)
    return token_manager.get_access_token()

# ---------------------------------------------------------------------------
# env-tunable constants. Every value is either a proven figure cribbed from
# an existing LIVE file (cited inline) or documented with its own rationale
# — none of these are arbitrary.
# ---------------------------------------------------------------------------

# Default daemon port: 9877, chosen adjacent to (but distinct from) the
# existing local cTrader MCP's 9876 (dexter3/mcp_client.py::DEFAULT_MCP_URL)
# so both transports can run side by side during the local_mcp/openapi
# migration without a port clash. Override via env for VM deployment.
DEFAULT_PORT = 9877
PORT_ENV_VAR = "DEXTER3_OPENAPI_DAEMON_PORT"

# Reconnect backoff: identical base/max to execution/ctrader_stream.py's
# proven constants (_RECONNECT_BASE_SEC / _RECONNECT_MAX_SEC) — that service
# has run this exact doubling schedule live since 2026-07-01 without a
# flagged reconnect-storm incident; not re-derived from scratch here.
RECONNECT_BASE_SEC = 5.0
RECONNECT_MAX_SEC = 300.0

# Per-request protobuf response timeouts: same figures
# ops/ctrader_execute_once.py uses per mode (10s for light reads — account/
# trader/reconcile/symbol lookups — 15s for order-affecting calls that may
# involve slower broker-side matching) so a daemon-mode caller sees the same
# worst-case per-call latency contract as the subprocess transport it
# replaces, even though the connection itself no longer needs re-handshaking.
REQUEST_TIMEOUT_LIGHT_SEC = 10.0
REQUEST_TIMEOUT_HEAVY_SEC = 15.0

# cTrader live spot/depth/trendbar events arrive in 1e-5 price units for
# these CFD/crypto symbols, NOT the symbol's own display "digits" field —
# using symbol digits under-scales by ~1000x. Confirmed empirically against
# this repo's own live journal (see ops/ctrader_execute_once.py's identical
# constant + comment, duplicated here per this module's docstring).
_MARKET_DATA_PRICE_SCALE = 100000.0

# ── spot_quote mode (live-cache real-time quotes) ──────────────────────────
# Motivation (coordinator directive 2026-07-10, VM shadow evidence 03:21:32Z):
# the worker's short-window capture_market returned spread_abs=0.0 on the VM
# and the entry pipeline correctly HARD-VETOed. The daemon instead maintains
# a STANDING ProtoOASubscribeSpotsReq per requested symbol and answers spot
# requests from the live tick cache — with a staleness bound so it never
# serves a materially old price as "live".

# Max age for a cache-served quote. 5.0s because: XAUUSD ticks multiple times
# per second in every active session, so a feed silent for >5s means the
# subscription is broken, the market is closed (weekend/holiday), or a real
# liquidity vacuum — in ALL three cases the entry pipeline SHOULD refuse to
# price a scalp off the last-seen quote (the same fail-closed posture as the
# HARD VETO that motivated this mode: no trade beats a trade priced on stale
# spread). 5s also sits below the lanes' 8-20s scan cadence, so one stale
# rejection self-heals by the next cycle instead of aliasing into permanent
# staleness on a healthy feed. Env-tunable for the operator; never silently
# widened in code.
DEFAULT_SPOT_MAX_AGE_SEC = 5.0
SPOT_MAX_AGE_ENV_VAR = "DEXTER3_SPOT_QUOTE_MAX_AGE_SEC"

# How long one spot_quote request waits for the FIRST tick after a fresh
# subscription (cold cache, or the first call after a reconnect re-subscribe)
# before reporting spot_stale. 2.5s: comfortably longer than one tick
# interval on an active feed, while leaving headroom inside the daemon-mode
# client's 12s default HTTP timeout (openapi_client.DEFAULT_DAEMON_TIMEOUT_SEC)
# for account-auth + HTTP overhead. Payload-overridable ("wait_sec") rather
# than env-tunable — it is a per-call trade-off, not a deployment property.
DEFAULT_SPOT_FIRST_TICK_WAIT_SEC = 2.5


def resolve_spot_max_age_sec() -> float:
    """Env override for the staleness bound, else the documented default."""
    raw = str(os.environ.get(SPOT_MAX_AGE_ENV_VAR, "") or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_SPOT_MAX_AGE_SEC
    except ValueError:
        return DEFAULT_SPOT_MAX_AGE_SEC
    return val if val > 0 else DEFAULT_SPOT_MAX_AGE_SEC


def resolve_port(port: int | None) -> int:
    """Pure: explicit argument wins, else env var, else DEFAULT_PORT.
    Split out from ``build_server``/``OpenApiDaemon.__init__`` so the
    resolution rule itself is unit-testable without binding a socket."""
    if port is not None:
        return int(port)
    return int(os.environ.get(PORT_ENV_VAR, DEFAULT_PORT) or DEFAULT_PORT)


# Entry-vs-target field-confusion sanity guard (2026-07-15, P0 live-PnL
# forensics: the OM's live PnL was found flip-flopping between an
# entry-based number and a number matching (take_profit - spot) exactly to
# the cent, on positions with a fresh spot — i.e. NOT a staleness bug.
# ``entry_price`` here is ``ProtoOAPosition.price`` (confirmed against the
# installed protobuf descriptor: fields are positionId/tradeData/
# positionStatus/swap/price/stopLoss/takeProfit/... — ``price`` is the
# position's own open/entry price, there is no separate "current price"
# field on this message). A real broker fill coinciding to the sanity
# tolerance with its OWN stop_loss/take_profit is practically impossible in
# live trading (both are always placed at a deliberate distance from
# entry) — so if it ever happens, a field-mapping regression (entry_price
# silently reading take_profit/stop_loss instead of ``price``) is the far
# more likely explanation, and every downstream OM decision (ladder/take/
# smart-exit) would then run on a netProfit silently wrong by exactly
# |entry-take_profit|. This must never be silent again.
DEFAULT_ENTRY_TARGET_SANITY_PRICE = 0.01
ENTRY_TARGET_SANITY_ENV_VAR = "DEXTER3_ENTRY_TARGET_SANITY_PRICE"


def resolve_entry_target_sanity_price() -> float:
    """Env override for the entry-vs-target coincidence tolerance (price
    units, e.g. USD/oz for XAUUSD), else the documented default."""
    raw = str(os.environ.get(ENTRY_TARGET_SANITY_ENV_VAR, "") or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_ENTRY_TARGET_SANITY_PRICE
    except ValueError:
        return DEFAULT_ENTRY_TARGET_SANITY_PRICE
    return val if val > 0 else DEFAULT_ENTRY_TARGET_SANITY_PRICE


def _entry_matches_target(entry_price: float, target_price: float, tol: float) -> bool:
    """Pure: True when ``entry_price``/``target_price`` are both real
    (>0) and coincide within ``tol``. Split out from the logging wrapper so
    the coincidence rule itself is unit-testable without a logger."""
    return entry_price > 0 and target_price > 0 and abs(entry_price - target_price) < tol


def _warn_if_entry_matches_targets(
    position_id: int, entry_price: float, stop_loss: float, take_profit: float
) -> None:
    """Log (never raise) when a normalized position's own entry_price
    coincides with its stop_loss or take_profit — see the sanity-guard
    rationale above ``DEFAULT_ENTRY_TARGET_SANITY_PRICE``. A broken guard
    must never break reconcile/normalization itself."""
    try:
        tol = resolve_entry_target_sanity_price()
        if _entry_matches_target(entry_price, take_profit, tol):
            logger.warning(
                "position %s: entry_price=%.5f suspiciously coincides with take_profit=%.5f "
                "(tol=%.5f) — possible entry/take-profit field-mapping bug; any pnl computed "
                "from entry_price here would be indistinguishable from a take-profit-based "
                "miscalculation",
                position_id, entry_price, take_profit, tol,
            )
        if _entry_matches_target(entry_price, stop_loss, tol):
            logger.warning(
                "position %s: entry_price=%.5f suspiciously coincides with stop_loss=%.5f "
                "(tol=%.5f) — possible entry/stop-loss field-mapping bug",
                position_id, entry_price, stop_loss, tol,
            )
    except Exception:  # noqa: BLE001 - a sanity guard must never break normalization
        logger.exception("entry/target sanity guard failed for position %s", position_id)


# ---------------------------------------------------------------------------
# Pure helpers with no I/O and no protobuf dependency — always importable,
# always unit-testable, never touch a wall clock or the network themselves.
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _ms_to_iso(value: Any) -> str:
    ms = _safe_int(value, 0)
    if ms <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _normalize_symbol_key(symbol: str) -> str:
    import re

    return re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())


def _scrub_tokens(data: dict) -> dict:
    out = dict(data or {})
    for key in ("accessToken", "refreshToken", "access_token", "refresh_token"):
        if key in out:
            out[key] = "<redacted>"
    return out


def _symbol_candidates(payload: dict) -> list[str]:
    vals: list[str] = []
    for raw in (
        payload.get("symbol"),
        payload.get("market_symbol"),
        (payload.get("raw_scores") or {}).get("market_symbol") if isinstance(payload.get("raw_scores"), dict) else None,
    ):
        token = str(raw or "").strip().upper()
        if token and token not in vals:
            vals.append(token)
    return vals


def _account_id_from_payload(payload: dict) -> int:
    """Resolve ctidTraderAccountId from the request payload — mirrors
    ops/ctrader_execute_once.py::_account_id_from_payload's priority chain
    (explicit payload account_id first; dexter3 always supplies this — see
    Dexter3OpenApiClient._payload — so the config-fallback branches below
    only matter for parity/manual testing, never the hot trading path)."""
    explicit = _safe_int(payload.get("account_id"), 0)
    if explicit > 0:
        return explicit
    explicit_login = str(payload.get("account_login") or payload.get("account_number") or "").strip()
    finder = getattr(config, "find_ctrader_account", None)
    use_demo = bool(getattr(config, "CTRADER_USE_DEMO", False))
    if explicit_login and callable(finder):
        row = finder(explicit_login, use_demo=use_demo)
        if isinstance(row, dict):
            account_id = _safe_int(row.get("accountId"), 0)
            if account_id > 0:
                return account_id
    # Dexter3-scoped pin: when the caller supplied no EXPLICIT account identity,
    # default to the account this daemon is pinned to (the lane's trading
    # account). This deliberately beats the generic config CTRADER_ACCOUNT_*
    # fallbacks below — those point at a different demo (9900897/46552794) on
    # this box, which was silently winning for account_id-less calls and made
    # the governor's get_deals reconcile hit an EMPTY account. Explicit payload
    # account_id / account_login (the hot trading path) still win above.
    # (2026-07-10)
    env_pin = _safe_int(os.environ.get("DEXTER3_OPENAPI_ACCOUNT_ID", ""), 0)
    if env_pin > 0:
        return env_pin
    raw_login = str(getattr(config, "CTRADER_ACCOUNT_LOGIN", "") or "").strip()
    if raw_login and callable(finder):
        row = finder(raw_login, use_demo=use_demo)
        if isinstance(row, dict):
            account_id = _safe_int(row.get("accountId"), 0)
            if account_id > 0:
                return account_id
    raw = str(getattr(config, "CTRADER_ACCOUNT_ID", "") or "").strip()
    if raw:
        if callable(finder):
            row = finder(raw)
            if isinstance(row, dict):
                account_id = _safe_int(row.get("accountId"), 0)
                if account_id > 0:
                    return account_id
        return _safe_int(raw, 0)
    if callable(finder):
        row = finder("", use_demo=use_demo)
        if isinstance(row, dict):
            account_id = _safe_int(row.get("accountId"), 0)
            if account_id > 0:
                return account_id
    return 0


def _is_fd_exhaustion_error(err: str) -> bool:
    """Pure: does a connection-failure message indicate the process has run
    out of file descriptors (EMFILE/ENFILE)? Matched on the strings Twisted
    actually produced during the 2026-08-08 incident ("Couldn't bind: 24:
    Too many open files.") plus the ENFILE sibling. When True, the daemon
    hard-exits so systemd restarts it with a clean fd table — the wedged
    alternative disabled every lane's exit manager for ~2h40m."""
    text = str(err or "").lower()
    return "too many open files" in text or "file table overflow" in text


def _enum_name(enum_cls: Any, value: int) -> str:
    try:
        return str(enum_cls.Name(int(value)))
    except Exception:
        return str(value)


@dataclass
class ReconnectBackoff:
    """Pure, deterministic capped-exponential backoff — no wall clock, no
    I/O, fully unit-testable. Mirrors execution/ctrader_stream.py's
    ``_schedule_reconnect`` doubling schedule (base -> base*2 -> ... -> cap).
    """

    base_sec: float = RECONNECT_BASE_SEC
    max_sec: float = RECONNECT_MAX_SEC
    _current_sec: float = field(init=False, repr=False, default=0.0)

    def __post_init__(self) -> None:
        self._current_sec = float(self.base_sec)

    def next_delay(self) -> float:
        """Return the delay to wait NOW, then double (capped) for next time."""
        delay = min(self._current_sec, self.max_sec)
        self._current_sec = min(self._current_sec * 2.0, self.max_sec)
        return delay

    def reset(self) -> None:
        self._current_sec = float(self.base_sec)


@dataclass
class ConnectionState:
    """Mutable connection status, read by GET /health and consulted by
    ``OpenApiDaemon.handle_call`` to short-circuit requests while
    disconnected. Every mutation happens on the Twisted reactor thread only
    (see module docstring's concurrency-model note) — plain attribute
    reads/writes from the HTTP handler threads are safe under the GIL, and
    nothing here performs a compound read-modify-write from a non-reactor
    thread."""

    connected: bool = False
    app_authed: bool = False
    environment: str = ""
    last_error: str = ""
    reconnect_count: int = 0
    started_at: float = 0.0
    authed_account_ids: set[int] = field(default_factory=set)

    def health_payload(self, *, now: float) -> dict[str, Any]:
        """Pure snapshot for GET /health. ``now`` is ALWAYS caller-supplied
        (never ``time.time()`` read inside this method) so tests can assert
        an exact ``uptime_sec`` without racing the real clock."""
        uptime = max(0.0, float(now) - self.started_at) if self.started_at > 0 else 0.0
        return {
            "connected": bool(self.connected),
            "app_authed": bool(self.app_authed),
            "environment": str(self.environment or ""),
            "account_ids": sorted(self.authed_account_ids),
            "uptime_sec": round(uptime, 1),
            "reconnect_count": int(self.reconnect_count),
            "last_error": str(self.last_error or ""),
        }


class CallRequestError(ValueError):
    """Raised by ``parse_call_body`` for a malformed POST /call body. Carries
    the HTTP status the handler should respond with so the handler doesn't
    need its own copy of that decision."""

    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = int(http_status)


def parse_call_body(raw_body: bytes) -> tuple[str, dict[str, Any]]:
    """Parse+validate a POST /call body. Pure — no I/O, fully unit-testable.

    Returns ``(mode, payload)``. Raises ``CallRequestError`` (never a bare
    exception) for: empty body, invalid JSON, non-dict JSON, missing/blank
    ``mode``, or a non-dict ``payload``. A missing ``payload`` key defaults
    to ``{}`` (dexter3's own payloads always include one, but "mode" alone
    with no payload — e.g. ``{"mode": "accounts"}`` — is still valid).
    """
    text = (raw_body or b"").decode("utf-8", errors="replace").strip()
    if not text:
        raise CallRequestError("empty request body")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CallRequestError(f"invalid JSON body: {exc}") from exc
    if not isinstance(obj, dict):
        raise CallRequestError(f"request body must be a JSON object, got {type(obj).__name__}")
    mode = str(obj.get("mode") or "").strip()
    if not mode:
        raise CallRequestError("missing required field: mode")
    payload = obj.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CallRequestError(f"'payload' must be a JSON object, got {type(payload).__name__}")
    return mode, payload


# ---------------------------------------------------------------------------
# HTTP layer — stdlib only, no Twisted. See module docstring: this keeps
# routing/validation independently unit-testable with a plain dispatcher
# stub (test requirement (a)).
# ---------------------------------------------------------------------------


class _CallHandler(BaseHTTPRequestHandler):
    """Set per-subclass (via ``build_server``) with bound ``dispatch_call``/
    ``health_snapshot`` callables — a fresh subclass is minted per
    ``build_server`` call (rather than mutating shared class attributes on
    this base class) so multiple daemons/test servers in the same process
    never clobber each other's dispatcher."""

    dispatch_call: Callable[[str, dict], dict]
    health_snapshot: Callable[[], dict]

    # Enables keep-alive (we always send Content-Length) — meaningful for
    # the real daemon under repeated lane polling; harmless for tests, which
    # close their own connections explicitly either way.
    protocol_version = "HTTP/1.1"
    server_version = "Dexter3OpenApiDaemon/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.info("%s - %s", self.address_string(), format % args)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # caller disconnected before the response was sent — not our failure

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        if self.path != "/health":
            self._write_json(404, {"ok": False, "status": "not_found", "message": f"no GET route for {self.path}"})
            return
        try:
            snapshot = self.health_snapshot()
        except Exception as exc:  # noqa: BLE001 - never let /health crash the daemon
            self._write_json(500, {"ok": False, "status": "internal_error", "message": str(exc)})
            return
        self._write_json(200, snapshot)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming convention
        if self.path != "/call":
            self._write_json(404, {"ok": False, "status": "not_found", "message": f"no POST route for {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length > 0 else b""
        try:
            mode, payload = parse_call_body(raw_body)
        except CallRequestError as exc:
            self._write_json(exc.http_status, {"ok": False, "status": "bad_request", "message": str(exc)})
            return
        try:
            result = self.dispatch_call(mode, payload)
        except Exception as exc:  # noqa: BLE001 - never crash the daemon on a bad call
            logger.exception("dispatch_call raised for mode=%s", mode)
            self._write_json(500, {"ok": False, "status": "internal_error", "message": str(exc)})
            return
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "status": "internal_error",
                "message": f"dispatcher returned {type(result).__name__}, expected a dict",
            }
        self._write_json(200, result)


def build_server(
    *,
    dispatch_call: Callable[[str, dict], dict],
    health_snapshot: Callable[[], dict],
    host: str = "127.0.0.1",
    port: int | None = None,
) -> ThreadingHTTPServer:
    """Construct (but do not start serving on) the HTTP listener.

    Split out from ``OpenApiDaemon`` so tests can bind a fake
    dispatcher/health pair to a REAL ephemeral local socket (``port=0``)
    without touching Twisted, the OpenAPI connection, or the network at all.
    """
    resolved_port = resolve_port(port)
    handler_cls = type(
        "_CallHandlerBound",
        (_CallHandler,),
        {"dispatch_call": staticmethod(dispatch_call), "health_snapshot": staticmethod(health_snapshot)},
    )
    return ThreadingHTTPServer((host, resolved_port), handler_cls)


# ---------------------------------------------------------------------------
# Twisted / cTrader OpenAPI layer — guarded import so the module (and every
# piece above) stays importable/testable even where these deps are absent.
# ---------------------------------------------------------------------------

try:
    from google.protobuf.json_format import MessageToDict
    from twisted.application.internet import ClientService as _TwistedClientService
    from twisted.internet import defer, reactor, threads
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages import OpenApiMessages_pb2 as pb
    from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model

    _HAS_DEPS = True
    _IMPORT_ERROR: Exception | None = None
except Exception as _import_exc:  # pragma: no cover - exercised only when deps are absent
    _HAS_DEPS = False
    _IMPORT_ERROR = _import_exc
    MessageToDict = None  # type: ignore[assignment]
    _TwistedClientService = None  # type: ignore[assignment]


if _HAS_DEPS:

    class _IsolatedTcpProtocol(TcpProtocol):
        """``TcpProtocol`` with per-INSTANCE send state.

        The vendored ``ctrader_open_api.tcpProtocol.TcpProtocol`` declares
        ``_send_queue`` (a ``deque``), ``_send_task`` and
        ``_lastSendMessageTime`` as CLASS attributes — every protocol
        instance in the process shares ONE send queue, and each connection's
        1s ``_sendStrings`` loop pops from that SHARED deque. With two live
        connections in this daemon (the persistent connection plus
        ``_mode_accounts``'s temp live-host connection — the one mode that
        must open its own connection, see its docstring), whichever loop
        fires first transmits the OTHER connection's protobuf requests over
        the wrong TCP connection; the response then arrives on the wrong
        connection, whose ``Client`` doesn't own that ``clientMsgId``
        deferred, so it is silently dropped and the real requester times
        out.

        Confirmed live 2026-07-10 ~03:52Z on the VM: every ``mode=accounts``
        call failed with ``twisted.internet.defer.TimeoutError(10,
        'Deferred')`` (traceback landing at ``_mode_accounts``'s
        ``temp_client.send(ProtoOAApplicationAuthReq...)``) while the
        persistent connection stayed perfectly healthy — deterministic,
        not intermittent, because the temp protocol's LoopingCall starts at
        its own connect time and is therefore almost always phase-BEHIND
        the long-running persistent connection's loop, which steals the
        temp client's auth request from the shared queue essentially every
        time. The one-shot worker never hits this (one connection per
        process); this daemon is the first place two ``TcpProtocol``
        instances coexist in one process by design.

        Assigning these three names in ``__init__`` shadows the class
        attributes with per-instance state — full isolation, zero changes
        to the vendored package (per this module's "crib, don't import" /
        never-modify-shared-files constraint). Twisted instantiates
        protocol classes with no arguments (``Factory.buildProtocol`` calls
        ``self.protocol()``), so a no-arg ``__init__`` is safe."""

        def __init__(self) -> None:
            from collections import deque

            self._send_queue = deque()
            self._send_task = None
            self._lastSendMessageTime = None

else:  # pragma: no cover - only when twisted/ctrader deps are absent
    _IsolatedTcpProtocol = None  # type: ignore[assignment]


def _force_stop_client(client: Any) -> None:
    """Actually halt an OpenAPI ``Client``'s underlying reconnect machine and
    close any connection it still holds — bypassing
    ``ctrader_open_api.Client.stopService()``'s broken guard.

    Root cause (confirmed live 2026-07-10, see docs handoff): that method
    reads ``if self.running and self.isConnected: ClientService.stopService(self)``
    — i.e. it only forwards to the REAL stop (``ClientService.stopService``,
    which calls ``self._machine.stop()``: twisted's own "stop attempting to
    reconnect and close any existing connections") when the client is
    CURRENTLY connected. But ``Client._disconnected()`` sets
    ``self.isConnected = False`` *before* invoking the disconnected callback,
    and a connect attempt that never succeeds never sets it True at all — so
    every call site in this module tears down a ``Client`` from inside a
    disconnect/connect-error callback, where the guard is ALWAYS false. The
    real stop is silently skipped EVERY time, leaking the old
    ``ClientService``'s internal retry loop forever on Twisted's own default
    backoff policy — fully decoupled from this daemon's reconnect
    bookkeeping, and still wired to this daemon's ``_on_disconnected``/
    ``_messageReceivedCallback``, so a zombie's own later disconnect
    re-triggers ``_schedule_reconnect`` a second (third, fourth...) time and
    compounds. Live evidence: ``ss -tnp`` showed 13 concurrent ESTABLISHED
    sockets to demo.ctraderapi.com:5035 from execution/ctrader_stream.py's
    single PID (a service designed to hold exactly ONE persistent
    connection, same vendored-library defect, just masked by rarer
    reconnects) and this daemon's log showing reconnect events fractions of
    a second apart that only multiple concurrent zombie Clients explain.
    Calling ``ClientService.stopService`` directly bypasses the buggy
    subclass override and performs the real teardown."""
    if client is None or _TwistedClientService is None:
        return
    try:
        _TwistedClientService.stopService(client)
    except Exception:
        pass


def _proto_to_dict(message: Any) -> dict:
    if MessageToDict is None or message is None:
        return {}
    try:
        return MessageToDict(message, preserving_proto_field_name=True)
    except Exception:
        return {}


def _resolve_host() -> tuple[str, int, str]:
    """Same resolution ops/ctrader_execute_once.py::_resolve_host uses
    (duplicated per this module's docstring, not imported)."""
    override = str(getattr(config, "CTRADER_OPENAPI_PROTOBUF_HOST", "") or "").strip()
    try:
        port = int(getattr(config, "CTRADER_OPENAPI_PROTOBUF_PORT", EndPoints.PROTOBUF_PORT) or EndPoints.PROTOBUF_PORT)
    except Exception:
        port = int(EndPoints.PROTOBUF_PORT)
    port = max(1, min(port, 65535))
    use_demo = bool(getattr(config, "CTRADER_USE_DEMO", False))
    env = "demo" if use_demo else "live"
    if override:
        return override, port, env
    if use_demo:
        return EndPoints.PROTOBUF_DEMO_HOST, int(EndPoints.PROTOBUF_PORT), "demo"
    return EndPoints.PROTOBUF_LIVE_HOST, int(EndPoints.PROTOBUF_PORT), "live"


def _normalize_accounts_payload(message: Any) -> list[dict]:
    rows = list(getattr(message, "ctidTraderAccount", []) or [])
    out: list[dict] = []
    for row in rows:
        item = _proto_to_dict(row)
        normalized = {
            "accountId": _safe_int(item.get("ctidTraderAccountId"), 0),
            "accountNumber": _safe_int(item.get("traderLogin"), 0),
            "traderLogin": _safe_int(item.get("traderLogin"), 0),
            "live": bool(item.get("isLive", False)),
            "isLive": bool(item.get("isLive", False)),
            "lastClosingDealTimestamp": str(item.get("lastClosingDealTimestamp", "") or ""),
            "lastBalanceUpdateTimestamp": str(item.get("lastBalanceUpdateTimestamp", "") or ""),
        }
        if normalized["accountId"] > 0:
            out.append(normalized)
    return out


def _normalize_capture_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _resolve_symbol(light_symbols: Any, payload: dict) -> tuple[object | None, str]:
    candidates = _symbol_candidates(payload)
    normalized = {_normalize_symbol_key(x): x for x in candidates if x}
    for sym in list(light_symbols or []):
        name = str(getattr(sym, "symbolName", "") or "").strip().upper()
        key = _normalize_symbol_key(name)
        if key in normalized:
            return sym, "exact_name"
    for sym in list(light_symbols or []):
        name = str(getattr(sym, "symbolName", "") or "").strip().upper()
        desc = str(getattr(sym, "description", "") or "").strip().upper()
        key = _normalize_symbol_key(name)
        if any(tok in key for tok in normalized.keys()):
            return sym, "contains_name"
        if any(tok in _normalize_symbol_key(desc) for tok in normalized.keys()):
            return sym, "contains_desc"
    return None, "not_found"


def _build_symbol_map(rows: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    for sym in list(rows or []):
        try:
            sid = _safe_int(getattr(sym, "symbolId", 0), 0)
        except Exception:
            sid = 0
        name = str(getattr(sym, "symbolName", "") or "").strip().upper()
        if sid > 0 and name:
            out[sid] = name
    return out


def _normalize_position(position: Any, symbol_map: dict[int, str]) -> dict:
    raw = _proto_to_dict(position)
    trade = dict(raw.get("tradeData") or {})
    symbol_id = _safe_int(trade.get("symbolId"), _safe_int(raw.get("symbolId"), 0))
    side_token = str(trade.get("tradeSide") or raw.get("tradeSide") or "").strip().upper()
    position_id = _safe_int(raw.get("positionId"), 0)
    # ``price`` is ProtoOAPosition's own open/entry price (confirmed against
    # the installed protobuf descriptor — see the sanity-guard rationale on
    # ``DEFAULT_ENTRY_TARGET_SANITY_PRICE`` above); it must never be read
    # from ``stopLoss``/``takeProfit``. Guarded immediately below.
    entry_price = _safe_float(raw.get("price"), 0.0)
    stop_loss = _safe_float(raw.get("stopLoss"), 0.0)
    take_profit = _safe_float(raw.get("takeProfit"), 0.0)
    _warn_if_entry_matches_targets(position_id, entry_price, stop_loss, take_profit)
    # 2026-07-15 P0 fix: swap/commission/usedMargin are raw broker integers
    # scaled by moneyDigits (same convention ``_normalize_deal`` below already
    # divides by, at ~L838-844) — this site read them UNSCALED for as long as
    # it existed, so on XAU (moneyDigits=2) an open position's commission/swap
    # were reported ~100x too large. Proven live on open short 653082985:
    # raw commission=-12 -> unscaled path fed -12.0 into netProfit instead of
    # -0.12, poisoning the OM's live PnL (netProfit=-17.54 vs true gross
    # -5.54, delta ~= the unscaled commission itself). ``money_digits`` was
    # already captured below but never applied to these three fields; fixed
    # by applying the identical scale ``_normalize_deal`` uses. Deals/realized
    # PnL paths were never affected (they always divided) — only open-position
    # netProfit (basket_live/OM/shadow_runner/executor consumers) was poisoned.
    money_digits = _safe_int(raw.get("moneyDigits"), 2)
    money_digits = max(0, min(8, money_digits))  # sane clamp; malformed proto value must never explode/invert the scale
    scale = float(10 ** money_digits) if money_digits > 0 else 1.0
    return {
        "position_id": position_id,
        "symbol_id": symbol_id,
        "symbol": str(symbol_map.get(symbol_id, "") or "").strip().upper(),
        "direction": "long" if side_token == "BUY" else ("short" if side_token == "SELL" else ""),
        "volume": _safe_int(trade.get("volume"), 0),
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "label": str(trade.get("label", "") or raw.get("label", "") or ""),
        "comment": str(trade.get("comment", "") or raw.get("comment", "") or ""),
        "open_timestamp_ms": _safe_int(trade.get("openTimestamp"), 0),
        "open_utc": _ms_to_iso(trade.get("openTimestamp")),
        "updated_timestamp_ms": _safe_int(raw.get("utcLastUpdateTimestamp"), 0),
        "updated_utc": _ms_to_iso(raw.get("utcLastUpdateTimestamp")),
        "status": str(raw.get("positionStatus", "") or ""),
        "swap": _safe_float(raw.get("swap"), 0.0) / scale,
        "commission": _safe_float(raw.get("commission"), 0.0) / scale,
        "used_margin": _safe_float(raw.get("usedMargin"), 0.0) / scale,
        "money_digits": money_digits,
        # NOTE: ``mirroringCommission`` (if present) lives only inside
        # ``raw['raw']`` today — it is not surfaced as a normalized field by
        # this function, so it is intentionally left unscaled/untouched here;
        # scale it the same way if it is ever promoted to a normalized field.
        "raw": raw,
    }


def _normalize_deal(deal: Any, symbol_map: dict[int, str]) -> dict:
    raw = _proto_to_dict(deal)
    symbol_id = _safe_int(raw.get("symbolId"), 0)
    close_detail = dict(raw.get("closePositionDetail") or {})
    digits = max(0, _safe_int(close_detail.get("moneyDigits"), _safe_int(raw.get("moneyDigits"), 2)))
    scale = float(10 ** digits) if digits > 0 else 1.0
    gross_profit = _safe_float(close_detail.get("grossProfit"), 0.0) / scale
    swap = _safe_float(close_detail.get("swap"), 0.0) / scale
    commission = _safe_float(close_detail.get("commission"), 0.0) / scale
    pnl_fee = _safe_float(close_detail.get("pnlConversionFee"), 0.0) / scale
    net_pnl = gross_profit + swap + commission + pnl_fee
    side_token = str(raw.get("tradeSide") or "").strip().upper()
    return {
        "deal_id": _safe_int(raw.get("dealId"), 0),
        "order_id": _safe_int(raw.get("orderId"), 0),
        "position_id": _safe_int(raw.get("positionId"), 0),
        "order_id": _safe_int(raw.get("orderId"), 0),
        # Filled by _mode_reconcile's order-list join (ProtoOADeal has no label
        # field — the label lives on the order that produced the deal). Closes
        # migration gap #1: the governor's per-label realized-PnL tracking.
        "label": "",
        "comment": "",
        "symbol_id": symbol_id,
        "symbol": str(symbol_map.get(symbol_id, "") or "").strip().upper(),
        "direction": "long" if side_token == "BUY" else ("short" if side_token == "SELL" else ""),
        "volume": _safe_int(raw.get("volume"), 0),
        "filled_volume": _safe_int(raw.get("filledVolume"), 0),
        "execution_price": _safe_float(raw.get("executionPrice"), 0.0),
        "execution_timestamp_ms": _safe_int(raw.get("executionTimestamp"), 0),
        "execution_utc": _ms_to_iso(raw.get("executionTimestamp")),
        "create_timestamp_ms": _safe_int(raw.get("createTimestamp"), 0),
        "deal_status": str(raw.get("dealStatus", "") or ""),
        "gross_profit_usd": gross_profit,
        "swap_usd": swap,
        "commission_usd": commission,
        "pnl_conversion_fee_usd": pnl_fee,
        "pnl_usd": net_pnl,
        "has_close_detail": bool(close_detail),
        "entry_price": _safe_float(close_detail.get("entryPrice"), 0.0),
        "closed_volume": _safe_int(close_detail.get("closedVolume"), 0),
        "balance_after_usd": _safe_float(close_detail.get("balance"), 0.0) / scale if close_detail else 0.0,
        "raw": raw,
    }


def _quantize_volume(meta: Any, payload: dict) -> tuple[int, dict]:
    fixed_volume = _safe_int(payload.get("fixed_volume"), 0)
    min_volume = max(1, _safe_int(getattr(meta, "minVolume", 1), 1))
    step_volume = max(1, _safe_int(getattr(meta, "stepVolume", 1), 1))
    max_volume = max(min_volume, _safe_int(getattr(meta, "maxVolume", min_volume), min_volume))
    risk_usd = max(0.0, _safe_float(payload.get("risk_usd"), 0.0))
    entry = _safe_float(payload.get("entry"), 0.0)
    stop_loss = _safe_float(payload.get("stop_loss"), 0.0)
    risk_price = abs(entry - stop_loss)
    raw_volume = 0.0
    if fixed_volume > 0:
        vol = fixed_volume
        reason = "fixed_volume"
    elif risk_usd > 0 and risk_price > 0:
        raw_volume = risk_usd / risk_price
        vol = int(raw_volume)
        reason = "approx_quote_risk"
    else:
        vol = min_volume
        reason = "min_volume_fallback"
    if vol < min_volume:
        vol = min_volume
    if step_volume > 1:
        vol = max(min_volume, int(vol // step_volume) * step_volume)
    vol = min(max_volume, max(min_volume, int(vol)))
    return int(vol), {
        "reason": reason,
        "risk_usd": round(risk_usd, 4),
        "risk_price": round(risk_price, 8),
        "min_volume": int(min_volume),
        "step_volume": int(step_volume),
        "max_volume": int(max_volume),
        "raw_volume": round(raw_volume, 6),
    }


def _relative_distance(price_a: float, price_b: float) -> int:
    distance = abs(_safe_float(price_a, 0.0) - _safe_float(price_b, 0.0))
    if distance <= 0:
        return 0
    return max(0, int(round(distance * 100000)))


def _price_digits(symbol_meta: Any, trader: Any) -> int:
    for cand in (
        getattr(symbol_meta, "digits", None),
        getattr(symbol_meta, "moneyDigits", None),
        getattr(trader, "moneyDigits", None),
        2,
    ):
        try:
            return max(0, int(cand))
        except Exception:
            continue
    return 2


def _quantize_price(value: float, digits: int) -> float:
    return round(_safe_float(value, 0.0), max(0, int(digits or 0)))


class _ModeError(Exception):
    """Internal-only: raised by ``_ensure_account_auth`` so ``_dispatch``
    can format every failure the same way, in one place."""

    def __init__(self, status: str, message: str = "") -> None:
        super().__init__(message or status)
        self.status = status
        self.message = message or status


class OpenApiDaemon:
    """Owns the ONE persistent cTrader OpenAPI connection and serves it over
    HTTP. See the module docstring for the full concurrency-model rationale.
    """

    def __init__(self, *, port: int | None = None) -> None:
        if not _HAS_DEPS:
            raise RuntimeError(
                "dexter3.openapi_daemon requires twisted + ctrader_open_api + protobuf, "
                f"which failed to import: {_IMPORT_ERROR!r}"
            )
        self.state = ConnectionState()
        self.backoff = ReconnectBackoff(base_sec=RECONNECT_BASE_SEC, max_sec=RECONNECT_MAX_SEC)
        self.client: Any = None
        self._symbols_cache: dict[int, tuple[list, dict[int, str]]] = {}
        self._running = False
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._port = resolve_port(port)
        # spot_quote live cache: (account_id, symbol_id) -> {bid, ask,
        # bid_mono, ask_mono, ts_ms, symbol}. bid/ask sides are cached
        # SEPARATELY with their own monotonic stamps because a
        # ProtoOASpotEvent may carry only one side — a quote is only
        # servable when BOTH sides exist and the OLDER side is within the
        # staleness bound (a fresh bid against a stale ask is a fake spread).
        self._spot_cache: dict[tuple[int, int], dict[str, Any]] = {}
        # Deferreds parked by spot_quote requests waiting for the first
        # complete (bid+ask) tick after a cold/reconnected subscription.
        self._spot_waiters: dict[tuple[int, int], list] = {}
        # Standing subscriptions active on the CURRENT connection —
        # account_id -> symbol_ids. Cleared on disconnect; the next
        # spot_quote lazily re-subscribes (self-healing within one lane
        # cycle), so no proactive re-subscribe pass is needed on reconnect.
        self._active_spot_subs: dict[int, set[int]] = {}
        # capture_market registers its transient event handler here instead
        # of calling client.setMessageReceivedCallback directly — the client
        # supports only ONE message callback, and the daemon's permanent
        # _on_push_message (which feeds the spot cache) must never be
        # displaced by a capture window.
        self._capture_listeners: list[Callable[[Any, Any], None]] = []
        # Single-flight reconnect guard (2026-08-08 incident): with no guard,
        # _on_connect_error AND _on_disconnected each scheduled their OWN
        # reconnect chain, so one flaky connection multiplied into thousands
        # of parallel chains (42k attempts logged), each leaking a socket
        # until "Too many open files" wedged the whole daemon for ~2h40m
        # while every lane's exit manager silently failed. True = a
        # reactor.callLater(_connect) is already queued; set/cleared on the
        # reactor thread only.
        self._reconnect_pending = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the HTTP listener (background thread) and the OpenAPI
        connection (reactor thread), then run the reactor. Blocks forever —
        call this only from ``__main__``."""
        self._running = True
        self.state.started_at = time.time()
        self._start_http_server()
        reactor.callWhenRunning(self._connect)
        reactor.addSystemEventTrigger("before", "shutdown", self._on_shutdown)
        logger.info("dexter3 OpenAPI daemon starting - HTTP on 127.0.0.1:%d", self._port)
        reactor.run()

    def _start_http_server(self) -> None:
        self._http_server = build_server(
            dispatch_call=self.handle_call,
            health_snapshot=self.health_snapshot,
            port=self._port,
        )
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="dexter3-openapi-daemon-http",
            daemon=True,
        )
        self._http_thread.start()
        logger.info("HTTP listener thread started on 127.0.0.1:%d", self._port)

    def _on_shutdown(self) -> None:
        self._running = False
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
            except Exception:
                pass
        _force_stop_client(self.client)

    # -- connection lifecycle (cribbed from execution/ctrader_stream.py) --

    def _connect(self) -> None:
        self._reconnect_pending = False   # the queued reconnect is now running
        if not self._running:
            return
        host, port, environment = _resolve_host()
        logger.info("Connecting to cTrader %s (%s:%d)...", environment, host, port)
        self.state.environment = environment
        self.client = Client(host, port, _IsolatedTcpProtocol)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.startService()
        d = self.client.whenConnected(failAfterFailures=1)
        d.addCallback(self._on_connected)
        d.addErrback(self._on_connect_error)

    @defer.inlineCallbacks
    def _on_connected(self, _protocol):
        client_id = str(getattr(config, "CTRADER_OPENAPI_CLIENT_ID", "") or "").strip()
        client_secret = str(getattr(config, "CTRADER_OPENAPI_CLIENT_SECRET", "") or "").strip()
        if not client_id or not client_secret:
            self.state.last_error = "credentials missing (CTRADER_OPENAPI_CLIENT_ID/CLIENT_SECRET)"
            self._schedule_reconnect()
            return
        try:
            app_msg = yield self.client.send(
                pb.ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
            )
            app_payload = Protobuf.extract(app_msg)
            if isinstance(app_payload, pb.ProtoOAErrorRes):
                err = str(getattr(app_payload, "description", "") or getattr(app_payload, "errorCode", ""))
                self.state.last_error = f"app auth failed: {err}"
                self._schedule_reconnect()
                return
        except Exception as exc:  # noqa: BLE001 - never crash the daemon on an auth hiccup
            self.state.last_error = f"app auth error: {exc}"
            self._schedule_reconnect()
            return
        # Success — reset backoff only now (not on bare TCP connect), so a
        # connection that connects but fails auth repeatedly still backs off.
        self.backoff.reset()
        self.state.last_error = ""
        self.state.connected = True
        self.state.app_authed = True
        self.state.authed_account_ids.clear()
        self._symbols_cache.clear()
        # Permanent push-message callback: feeds the spot_quote cache and
        # forwards raw messages to any transient capture_market listeners.
        self.client.setMessageReceivedCallback(self._on_push_message)
        logger.info("App-authed against cTrader %s", self.state.environment)

    def _on_connect_error(self, failure) -> None:
        err = failure.getErrorMessage() if failure else "unknown"
        logger.warning("Connection failed: %s", err)
        self.state.last_error = f"connection failed: {err}"
        if _is_fd_exhaustion_error(err):
            # File descriptors are gone — no in-process recovery is possible
            # (every further connect/accept fails, the HTTP listener starves,
            # and the 2026-08-08 incident showed the process just wedges).
            # Hard-exit so systemd Restart=always revives us with a clean fd
            # table within seconds instead of hours.
            logger.critical("fd exhaustion detected (%s) — hard-exiting for systemd restart", err)
            os._exit(70)
            return  # unreachable in production; keeps mocked-_exit tests honest
        self._schedule_reconnect()

    def _on_disconnected(self, _client, reason) -> None:
        logger.warning("Disconnected: %s", reason)
        self.state.connected = False
        self.state.app_authed = False
        self.state.authed_account_ids.clear()
        self._symbols_cache.clear()
        # Standing spot subscriptions die with the connection: clear the
        # active set so the next spot_quote re-subscribes on the NEW
        # connection (lazy re-establishment — see __init__ comment), and
        # unblock any parked first-tick waiters immediately so their HTTP
        # threads return "spot_stale" now instead of burning their timeout.
        self._active_spot_subs.clear()
        for key in list(self._spot_waiters.keys()):
            for waiter in self._spot_waiters.pop(key, []):
                if not waiter.called:
                    waiter.callback(False)
        self.state.last_error = f"disconnected: {reason}"
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if not self._running:
            return
        self.state.connected = False
        self.state.app_authed = False
        _force_stop_client(self.client)
        self.client = None
        if self._reconnect_pending:
            # Single-flight (2026-08-08 incident): a reconnect is already
            # queued — piling on another chain is how one flaky connection
            # became 42k parallel attempts and an fd-exhaustion wedge.
            return
        self._reconnect_pending = True
        self.state.reconnect_count += 1
        delay = self.backoff.next_delay()
        logger.info("Reconnecting in %.0fs (attempt %d)...", delay, self.state.reconnect_count)
        reactor.callLater(delay, self._connect)

    # -- health -------------------------------------------------------------

    def health_snapshot(self) -> dict[str, Any]:
        return self.state.health_payload(now=time.time())

    # -- HTTP -> reactor bridge ----------------------------------------------

    def handle_call(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Called from an HTTP handler THREAD (see module docstring). Blocks
        only the calling thread, bridging into the reactor thread where the
        real protobuf call happens."""
        if not self.state.connected:
            return {
                "ok": False,
                "status": "disconnected",
                "message": "daemon has no live cTrader OpenAPI connection right now",
            }
        start = time.time()
        try:
            return threads.blockingCallFromThread(reactor, self._dispatch, str(mode or ""), dict(payload or {}))
        except Exception as exc:  # noqa: BLE001 - surface as a clean JSON error, never crash
            return {"ok": False, "status": "worker_error", "message": str(exc)}
        finally:
            self._log_slow_call(mode, time.time() - start)

    @staticmethod
    def _log_slow_call(mode: str, elapsed_sec: float) -> None:
        """H3 (2026-07-15 cross-lane entanglement audit) — OBSERVABILITY
        ONLY: logs one line when a dispatched call's total wall time
        (including the HTTP-thread-to-reactor blocking wait) exceeds env
        ``DEXTER3_DAEMON_SLOW_CALL_LOG_SEC`` (default 5.0s; <=0 disables).
        Does not change dispatch behavior, timeouts, or retries in any way —
        this is a log line, nothing else."""
        try:
            raw_threshold = os.environ.get("DEXTER3_DAEMON_SLOW_CALL_LOG_SEC", "")
            threshold = float(raw_threshold) if raw_threshold.strip() else 5.0
        except ValueError:
            threshold = 5.0
        if threshold <= 0:
            return
        if elapsed_sec > threshold:
            logger.warning(
                "slow daemon call mode=%s elapsed=%.2fs (threshold=%.2fs)", mode, elapsed_sec, threshold
            )

    # -- per-mode dispatch ----------------------------------------------------

    @defer.inlineCallbacks
    def _ensure_account_auth(self, account_id: int):
        if account_id in self.state.authed_account_ids:
            return
        access_token = _fresh_access_token()  # READ-ONLY + disk-adopt (rotation-safe)
        acc_msg = yield self.client.send(
            pb.ProtoOAAccountAuthReq(ctidTraderAccountId=int(account_id), accessToken=str(access_token or "")),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
        )
        acc_payload = Protobuf.extract(acc_msg)
        if isinstance(acc_payload, pb.ProtoOAErrorRes):
            err = str(getattr(acc_payload, "description", "") or getattr(acc_payload, "errorCode", ""))
            # "Trading account is already authorized in this channel" is a
            # BENIGN idempotent condition, not a failure: a reconnect cleared
            # our local authed_account_ids set (see _on_app_authed) while the
            # broker still holds the account authed on this channel. Adopt it
            # and proceed instead of raising _ModeError — the old behavior
            # blocked reconcile/trade during the reconnect window and left a
            # scary last_error even though the account was usable. (2026-07-10)
            if "already authorized" in err.lower():
                self.state.authed_account_ids.add(int(account_id))
                self.state.last_error = ""
                return
            self.state.last_error = f"account auth failed for {account_id}: {err}"
            raise _ModeError("account_auth_failed", err)
        self.state.authed_account_ids.add(int(account_id))

    @defer.inlineCallbacks
    def _get_symbols(self, account_id: int):
        cached = self._symbols_cache.get(int(account_id))
        if cached is not None:
            defer.returnValue(cached)
            return
        symbols_msg = yield self.client.send(
            pb.ProtoOASymbolsListReq(ctidTraderAccountId=int(account_id), includeArchivedSymbols=False),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
        )
        symbols_payload = Protobuf.extract(symbols_msg)
        light_symbols = list(getattr(symbols_payload, "symbol", []) or [])
        symbol_map = _build_symbol_map(light_symbols)
        result = (light_symbols, symbol_map)
        self._symbols_cache[int(account_id)] = result
        defer.returnValue(result)

    # -- spot_quote live cache (standing subscription) ------------------------

    def _on_push_message(self, client, message) -> None:
        """THE single permanent message callback on the persistent client
        (reactor thread). Feeds the spot cache from every ProtoOASpotEvent —
        including those triggered by a capture_market window — then forwards
        the raw message to any transient capture listeners."""
        try:
            payload_evt = Protobuf.extract(message)
        except Exception:
            payload_evt = None
        if payload_evt is not None and isinstance(payload_evt, pb.ProtoOASpotEvent):
            try:
                self._update_spot_cache(payload_evt)
            except Exception:  # noqa: BLE001 - cache upkeep must never kill the callback
                logger.exception("spot cache update failed")
        for listener in list(self._capture_listeners):
            try:
                listener(client, message)
            except Exception:  # noqa: BLE001
                logger.exception("capture listener failed")

    def _update_spot_cache(self, evt: Any) -> None:
        account_id = _safe_int(getattr(evt, "ctidTraderAccountId", 0), 0)
        sid = _safe_int(getattr(evt, "symbolId", 0), 0)
        if account_id <= 0 or sid <= 0:
            return
        bid_raw = _safe_float(getattr(evt, "bid", 0.0), 0.0)
        ask_raw = _safe_float(getattr(evt, "ask", 0.0), 0.0)
        if bid_raw <= 0 and ask_raw <= 0:
            return
        key = (account_id, sid)
        entry = self._spot_cache.setdefault(
            key, {"bid": 0.0, "ask": 0.0, "bid_mono": 0.0, "ask_mono": 0.0, "ts_ms": 0}
        )
        mono = time.monotonic()
        if bid_raw > 0:
            entry["bid"] = bid_raw / _MARKET_DATA_PRICE_SCALE
            entry["bid_mono"] = mono
        if ask_raw > 0:
            entry["ask"] = ask_raw / _MARKET_DATA_PRICE_SCALE
            entry["ask_mono"] = mono
        entry["ts_ms"] = _safe_int(getattr(evt, "timestamp", 0), 0) or int(time.time() * 1000)
        if entry["bid"] > 0 and entry["ask"] > 0:
            for waiter in self._spot_waiters.pop(key, []):
                if not waiter.called:
                    waiter.callback(True)

    def _fresh_quote(
        self, account_id: int, symbol_id: int, max_age_sec: float
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Return (quote, age_sec) when a COMPLETE (bid+ask) quote no older
        than ``max_age_sec`` is cached, else (None, age_of_what_exists|None).
        Age is measured from the OLDER of the two sides — a fresh bid paired
        with a stale ask is a fabricated spread, not a quote."""
        entry = self._spot_cache.get((int(account_id), int(symbol_id)))
        if not entry or entry.get("bid", 0.0) <= 0 or entry.get("ask", 0.0) <= 0:
            return None, None
        oldest_side_mono = min(float(entry.get("bid_mono", 0.0)), float(entry.get("ask_mono", 0.0)))
        if oldest_side_mono <= 0:
            return None, None
        age = time.monotonic() - oldest_side_mono
        if age > float(max_age_sec):
            return None, age
        return dict(entry), age

    @defer.inlineCallbacks
    def _ensure_spot_subscription(self, account_id: int, symbol_id: int):
        """Send ProtoOASubscribeSpotsReq once per (connection, account,
        symbol). Reactor thread only. The subscription response itself is
        not awaited (protocol.send instant) — the same fire-and-forget
        pattern the proven worker uses; the first arriving SpotEvent is the
        real confirmation, and spot_quote's first-tick wait covers the gap."""
        subs = self._active_spot_subs.setdefault(int(account_id), set())
        if int(symbol_id) in subs:
            return
        protocol = yield self.client.whenConnected(failAfterFailures=1)
        protocol.send(
            pb.ProtoOASubscribeSpotsReq(
                ctidTraderAccountId=int(account_id),
                symbolId=[int(symbol_id)],
                subscribeToSpotTimestamp=True,
            ),
            instant=True,
        )
        subs.add(int(symbol_id))
        logger.info("Standing spot subscription: account=%d symbol_id=%d", account_id, symbol_id)

    @defer.inlineCallbacks
    def _mode_spot_quote(self, account_id: int, payload: dict[str, Any]):
        symbol = str(payload.get("symbol", "") or "").strip().upper()
        if not symbol:
            defer.returnValue({"ok": False, "status": "symbol_missing", "message": "symbol missing"})
            return
        light_symbols, _symbol_map = yield self._get_symbols(account_id)
        sym_obj, _match = _resolve_symbol(light_symbols, {"symbol": symbol, "market_symbol": symbol})
        if sym_obj is None:
            defer.returnValue({"ok": False, "status": "symbol_not_found", "message": f"symbol not found: {symbol}"})
            return
        symbol_id = _safe_int(getattr(sym_obj, "symbolId", 0), 0)
        symbol_name = str(getattr(sym_obj, "symbolName", "") or "").strip().upper()
        yield self._ensure_spot_subscription(account_id, symbol_id)

        max_age = resolve_spot_max_age_sec()
        try:
            wait_sec = float(payload.get("wait_sec", DEFAULT_SPOT_FIRST_TICK_WAIT_SEC))
        except (TypeError, ValueError):
            wait_sec = DEFAULT_SPOT_FIRST_TICK_WAIT_SEC

        quote, age = self._fresh_quote(account_id, symbol_id, max_age)
        if quote is None and wait_sec > 0:
            key = (int(account_id), int(symbol_id))
            waiter = defer.Deferred()
            self._spot_waiters.setdefault(key, []).append(waiter)

            def _timeout() -> None:
                waiters = self._spot_waiters.get(key, [])
                if waiter in waiters:
                    waiters.remove(waiter)
                if not waiter.called:
                    waiter.callback(False)

            timeout_call = reactor.callLater(wait_sec, _timeout)
            try:
                yield waiter
            finally:
                if timeout_call.active():
                    timeout_call.cancel()
            quote, age = self._fresh_quote(account_id, symbol_id, max_age)

        if quote is None:
            defer.returnValue({
                "ok": False,
                "status": "spot_stale",
                "message": (
                    f"no fresh {symbol_name} quote within max_age={max_age}s"
                    + (f" (newest cached quote is {age:.1f}s old)" if age is not None else " (no complete bid+ask cached yet)")
                ),
                "symbol": symbol_name,
                "symbol_id": int(symbol_id),
                "account_id": int(account_id),
                "max_age_sec": float(max_age),
                "age_sec": round(age, 3) if age is not None else None,
            })
            return

        bid = float(quote["bid"])
        ask = float(quote["ask"])
        mid = (bid + ask) / 2.0
        spread = ask - bid
        ts_ms = _safe_int(quote.get("ts_ms"), 0)
        defer.returnValue({
            "ok": True,
            "status": "spot_quote",
            "message": f"live-cache quote for {symbol_name} (age {age:.3f}s)",
            "symbol": symbol_name,
            "symbol_id": int(symbol_id),
            "account_id": int(account_id),
            "environment": self.state.environment,
            "quote_age_sec": round(float(age or 0.0), 3),
            # Alias of quote_age_sec (2026-07-15 OM-blindspot fix #1): the
            # incident's forensics (docs/AGENT_SYNC_BOARD.md 2026-07-15
            # ~07:30Z "OM ROUND-TRIP INCIDENT") named the field the CLIENT's
            # own independent staleness gate should read `spot_age_sec` —
            # this daemon already computes the correct "age since the last
            # REAL tick" value (``_fresh_quote`` measures from
            # bid_mono/ask_mono, stamped only by ``_update_spot_cache`` on a
            # genuine ``ProtoOASpotEvent``, never on read/reconnect), so this
            # is a pure rename-for-the-consumer, not a new computation.
            "spot_age_sec": round(float(age or 0.0), 3),
            "max_age_sec": float(max_age),
            "source": "live_cache",
            # "spots" list mirrors capture_market's per-event shape so the
            # client parses both modes with identical code.
            "spots": [{
                "account_id": int(account_id),
                "symbol_id": int(symbol_id),
                "symbol": symbol_name,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "spread_pct": ((spread / mid) * 100.0) if mid > 0 and spread >= 0 else 0.0,
                "event_utc": _ms_to_iso(ts_ms),
                "event_ts": round(ts_ms / 1000.0, 3) if ts_ms > 0 else 0.0,
            }],
            "token_refresh": {},
        })

    @defer.inlineCallbacks
    def _dispatch(self, mode: str, payload: dict[str, Any]):
        try:
            if mode == "accounts":
                result = yield self._mode_accounts()
            else:
                account_id = _account_id_from_payload(payload)
                if account_id <= 0:
                    defer.returnValue({"ok": False, "status": "account_missing", "message": "ctidTraderAccountId missing"})
                    return
                yield self._ensure_account_auth(account_id)
                if mode == "health":
                    result = yield self._mode_health(account_id)
                elif mode == "reconcile":
                    result = yield self._mode_reconcile(account_id, payload)
                elif mode == "spot_quote":
                    result = yield self._mode_spot_quote(account_id, payload)
                elif mode == "get_trendbars":
                    result = yield self._mode_get_trendbars(account_id, payload)
                elif mode == "symbol_details":
                    result = yield self._mode_symbol_details(account_id, payload)
                elif mode == "capture_market":
                    result = yield self._mode_capture_market(account_id, payload)
                elif mode == "execute":
                    result = yield self._mode_execute(account_id, payload)
                elif mode == "close":
                    result = yield self._mode_close(account_id, payload)
                elif mode == "cancel_order":
                    result = yield self._mode_cancel_order(account_id, payload)
                elif mode == "amend_order":
                    result = yield self._mode_amend_order(account_id, payload)
                elif mode == "amend_position_sltp":
                    result = yield self._mode_amend_position_sltp(account_id, payload)
                else:
                    defer.returnValue({"ok": False, "status": "unknown_mode", "message": f"unsupported mode: {mode!r}"})
                    return
            if isinstance(result, dict) and isinstance(result.get("execution_meta"), dict):
                result = dict(result)
                result["execution_meta"] = _scrub_tokens(result["execution_meta"])
            defer.returnValue(result)
        except _ModeError as exc:
            defer.returnValue({"ok": False, "status": exc.status, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - never crash the reactor thread on one bad call
            logger.exception("mode=%s dispatch failed", mode)
            defer.returnValue({"ok": False, "status": "worker_error", "message": str(exc)})

    @defer.inlineCallbacks
    def _mode_accounts(self):
        """``ProtoOAGetAccountListByAccessTokenReq`` must go over the LIVE
        host even for a demo-scoped token — a confirmed protocol quirk (see
        docs/DEXTER3_VM_MIGRATION_DESIGN.md's 2026-07-10 "Q1" finding, and
        ``ops/ctrader_execute_once.py:390-392``'s identical hardcode). The
        daemon's persistent connection may be pointed at the demo host, so
        this is the ONE mode that always opens its own short-lived
        connection to the live host rather than reusing ``self.client`` —
        that is cTrader's own design, not a limitation introduced here. It
        is also the one mode dexter3 calls rarely (account-pin verification,
        cached after the first success — see
        ``Dexter3OpenApiClient._ensure_account_pin``), so this extra connect
        does not reintroduce the per-call churn this daemon exists to
        eliminate on the hot read path."""
        client_id = str(getattr(config, "CTRADER_OPENAPI_CLIENT_ID", "") or "").strip()
        client_secret = str(getattr(config, "CTRADER_OPENAPI_CLIENT_SECRET", "") or "").strip()
        access_token = _fresh_access_token()  # READ-ONLY + disk-adopt (rotation-safe)
        if not client_id or not client_secret:
            defer.returnValue({"ok": False, "status": "credentials_missing", "message": "client id/secret missing"})
            return
        temp_client = Client(EndPoints.PROTOBUF_LIVE_HOST, int(EndPoints.PROTOBUF_PORT), _IsolatedTcpProtocol)
        temp_client.startService()
        try:
            yield temp_client.whenConnected(failAfterFailures=1)
            app_msg = yield temp_client.send(
                pb.ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
            )
            app_payload = Protobuf.extract(app_msg)
            if isinstance(app_payload, pb.ProtoOAErrorRes):
                err = str(getattr(app_payload, "description", "") or getattr(app_payload, "errorCode", ""))
                defer.returnValue({"ok": False, "status": "app_auth_failed", "message": err})
                return
            acct_msg = yield temp_client.send(
                pb.ProtoOAGetAccountListByAccessTokenReq(accessToken=str(access_token or "")),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
            )
            acct_payload = Protobuf.extract(acct_msg)
            if isinstance(acct_payload, pb.ProtoOAErrorRes):
                err = str(getattr(acct_payload, "description", "") or getattr(acct_payload, "errorCode", ""))
                defer.returnValue({"ok": False, "status": "accounts_failed", "message": err, "token_refresh": {}})
                return
            accounts = _normalize_accounts_payload(acct_payload)
            defer.returnValue({
                "ok": True,
                "status": "accounts_loaded",
                "message": f"loaded {len(accounts)} accounts",
                "environment": "live",
                "accounts": accounts,
                "token_refresh": {},
            })
        finally:
            _force_stop_client(temp_client)

    @defer.inlineCallbacks
    def _mode_health(self, account_id: int):
        trader_msg = yield self.client.send(
            pb.ProtoOATraderReq(ctidTraderAccountId=int(account_id)), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC)
        )
        trader_payload = Protobuf.extract(trader_msg)
        reconcile_msg = yield self.client.send(
            pb.ProtoOAReconcileReq(ctidTraderAccountId=int(account_id)), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC)
        )
        reconcile_payload = Protobuf.extract(reconcile_msg)
        trader = getattr(trader_payload, "trader", None)
        defer.returnValue({
            "ok": True,
            "status": "connected",
            "message": "ctrader health ok",
            "account_id": int(account_id),
            "environment": self.state.environment,
            "balance": _safe_float(getattr(trader, "balance", 0.0), 0.0),
            "money_digits": _safe_int(getattr(trader, "moneyDigits", 2), 2),
            "leverage_in_cents": _safe_int(getattr(trader, "leverageInCents", 0), 0),
            "positions": len(list(getattr(reconcile_payload, "position", []) or [])),
            "orders": len(list(getattr(reconcile_payload, "order", []) or [])),
            "token_refresh": {},
        })

    @defer.inlineCallbacks
    def _mode_reconcile(self, account_id: int, payload: dict[str, Any]):
        _light_symbols, symbol_map = yield self._get_symbols(account_id)
        reconcile_msg = yield self.client.send(
            pb.ProtoOAReconcileReq(ctidTraderAccountId=int(account_id)), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC)
        )
        reconcile_payload = Protobuf.extract(reconcile_msg)
        lookback_h = max(1, _safe_int(payload.get("lookback_hours"), 72))
        now_ms = int(time.time() * 1000)
        from_ts = _safe_int(payload.get("from_timestamp"), now_ms - (lookback_h * 3600 * 1000))
        to_ts = _safe_int(payload.get("to_timestamp"), now_ms)
        max_rows = max(10, min(_safe_int(payload.get("max_rows"), 200), 1000))
        positions = [_normalize_position(x, symbol_map) for x in list(getattr(reconcile_payload, "position", []) or [])]
        orders = [_proto_to_dict(x) for x in list(getattr(reconcile_payload, "order", []) or [])]
        # Position-management is the daemon's hottest path.  It has no use
        # for historical deals, yet previously always incurred this extra
        # broker round trip (up to REQUEST_TIMEOUT_HEAVY_SEC), starving OM
        # reads.  Deal consumers opt in explicitly; absent the new flag we
        # preserve the old response shape for compatibility.
        include_deals = bool(payload.get("include_deals", True))
        deals: list[dict[str, Any]] = []
        if include_deals:
            deal_msg = yield self.client.send(
                pb.ProtoOADealListReq(
                    ctidTraderAccountId=int(account_id),
                    fromTimestamp=int(from_ts),
                    toTimestamp=int(to_ts),
                    maxRows=int(max_rows),
                ),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC),
            )
            deal_payload = Protobuf.extract(deal_msg)
            deals = [_normalize_deal(x, symbol_map) for x in list(getattr(deal_payload, "deal", []) or [])]
        # -- deal->order label join (closes migration gap #1) -----------------
        # ProtoOADeal carries no label; the label lives on the order that
        # produced it. Fetch the historical order list for the SAME window and
        # stamp each deal with its order's label so the governor's per-label
        # realized-PnL tracking works on the OpenAPI transport. Best-effort:
        # a failed/empty order-list leaves labels blank (governor degrades to
        # the pre-join behavior — no worse than before this join existed).
        # Only run the extra order-list round-trip when the caller asked for
        # labels (get_deals); get_positions skips it to stay under the 5s
        # daemon client timeout on its every-bar lane-verification path.
        if bool(payload.get("include_deal_labels")):
          try:
            order_msg = yield self.client.send(
                pb.ProtoOAOrderListReq(
                    ctidTraderAccountId=int(account_id),
                    fromTimestamp=int(from_ts),
                    toTimestamp=int(to_ts),
                ),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC),
            )
            order_payload = Protobuf.extract(order_msg)
            label_by_order_id: dict[int, tuple[str, str]] = {}
            for o in list(getattr(order_payload, "order", []) or []):
                od = _proto_to_dict(o)
                oid = _safe_int(od.get("orderId"), 0)
                if oid <= 0:
                    continue
                trade = dict(od.get("tradeData") or {})
                label_by_order_id[oid] = (
                    str(trade.get("label", "") or "").strip(),
                    str(trade.get("comment", "") or "").strip(),
                )
            for d in deals:
                lbl, cmt = label_by_order_id.get(int(d.get("order_id", 0) or 0), ("", ""))
                if lbl:
                    d["label"] = lbl
                if cmt:
                    d["comment"] = cmt
          except Exception as exc:  # noqa: BLE001 - label join is best-effort
            logger.warning("deal->order label join failed (deals unlabeled this cycle): %s", exc)
        defer.returnValue({
            "ok": True,
            "status": "reconciled",
            "message": f"positions={len(positions)} deals={len(deals)}",
            "account_id": int(account_id),
            "environment": self.state.environment,
            "positions": positions,
            "orders": orders,
            "deals": deals,
            "token_refresh": {},
        })

    @defer.inlineCallbacks
    def _mode_symbol_details(self, account_id: int, payload: dict[str, Any]):
        """Full trading spec for a symbol (closes migration gap #2 — execute_entry
        needs minVolume/volumeStep/lotSize/pipSize to size orders).

        Returns RAW cTrader ProtoOASymbol fields; the client
        (Dexter3OpenApiClient.get_symbol_details) does the unit conversion with
        its own existing helpers so the raw-vs-dexter3-units boundary lives in
        ONE place. Volumes here are cTrader raw (centi-units); the client
        divides by UNITS_TO_RAW_SCALE. pipPosition/digits are the price
        precision the client turns into pipSize = 10**(-pipPosition)."""
        sd_symbol = str(payload.get("symbol", "XAUUSD") or "XAUUSD")
        light_symbols, _symbol_map = yield self._get_symbols(account_id)
        sd_obj, _match = _resolve_symbol(light_symbols, {"symbol": sd_symbol, "market_symbol": sd_symbol})
        if sd_obj is None:
            defer.returnValue({"ok": False, "status": "symbol_not_found", "message": f"symbol not found: {sd_symbol}"})
            return
        sd_symbol_id = _safe_int(getattr(sd_obj, "symbolId", 0), 0)
        sd_symbol_name = str(getattr(sd_obj, "symbolName", "") or "").strip()
        meta_msg = yield self.client.send(
            pb.ProtoOASymbolByIdReq(ctidTraderAccountId=int(account_id), symbolId=[int(sd_symbol_id)]),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
        )
        meta_payload = Protobuf.extract(meta_msg)
        meta_list = list(getattr(meta_payload, "symbol", []) or [])
        if not meta_list:
            defer.returnValue({"ok": False, "status": "symbol_details_empty", "message": f"no ProtoOASymbol for {sd_symbol_name}"})
            return
        meta = meta_list[0]
        # Market-state telemetry (2026-07-25): surface the broker's OWN
        # open/closed signals that ProtoOASymbol already carries but which
        # this mode previously dropped. ALL defensive (getattr + try/except ->
        # None) — this runs on the live daemon and must never raise on a proto
        # field/enum-accessor shape it doesn't recognise; consumers fail-open.
        trading_mode_raw = _safe_int(getattr(meta, "tradingMode", -1), -1)
        trading_enabled: bool | None = None
        try:
            # Named enum lookup — never a hardcoded int (the lib is the source
            # of truth for what ENABLED maps to).
            enabled_val = model.ProtoOATradingMode.Value("ENABLED")
            if trading_mode_raw >= 0:
                trading_enabled = trading_mode_raw == int(enabled_val)
        except Exception:
            trading_enabled = None
        schedule_out: list[dict[str, int]] = []
        try:
            for iv in list(getattr(meta, "schedule", []) or []):
                schedule_out.append({
                    "start": _safe_int(getattr(iv, "startSecond", 0), 0),
                    "end": _safe_int(getattr(iv, "endSecond", 0), 0),
                })
        except Exception:
            schedule_out = []
        defer.returnValue({
            "ok": True,
            "status": "symbol_details_loaded",
            "symbolId": sd_symbol_id,
            "symbolName": sd_symbol_name,
            "digits": _safe_int(getattr(meta, "digits", 0), 0),
            "pipPosition": _safe_int(getattr(meta, "pipPosition", 0), 0),
            "minVolume_raw": _safe_int(getattr(meta, "minVolume", 0), 0),
            "stepVolume_raw": _safe_int(getattr(meta, "stepVolume", 0), 0),
            "maxVolume_raw": _safe_int(getattr(meta, "maxVolume", 0), 0),
            "tradingMode": trading_mode_raw if trading_mode_raw >= 0 else None,
            "trading_enabled": trading_enabled,
            "schedule": schedule_out,
        })

    @defer.inlineCallbacks
    def _mode_get_trendbars(self, account_id: int, payload: dict[str, Any]):
        tf_to_period = {
            "1m": 1, "2m": 2, "3m": 3, "4m": 4, "5m": 5,
            "10m": 6, "15m": 7, "30m": 8,
            "1h": 9, "4h": 10, "12h": 11,
            "1d": 12, "1w": 13, "1mn": 14,
        }
        tb_symbol = str(payload.get("symbol", "XAUUSD") or "XAUUSD")
        tb_tf = str(payload.get("timeframe", "5m") or "5m").lower()
        tb_period = tf_to_period.get(tb_tf)
        if tb_period is None:
            defer.returnValue({
                "ok": False,
                "status": "invalid_timeframe",
                "message": f"unsupported timeframe: {tb_tf}, valid: {list(tf_to_period.keys())}",
            })
            return
        from_ms = _safe_int(payload.get("from_ms"), 0)
        to_ms = _safe_int(payload.get("to_ms"), int(time.time() * 1000))
        tb_count = max(1, min(_safe_int(payload.get("count"), 5000), 14000))
        light_symbols, _symbol_map = yield self._get_symbols(account_id)
        tb_symbol_obj, _match = _resolve_symbol(light_symbols, {"symbol": tb_symbol, "market_symbol": tb_symbol})
        if tb_symbol_obj is None:
            defer.returnValue({"ok": False, "status": "symbol_not_found", "message": f"symbol not found: {tb_symbol}"})
            return
        tb_symbol_id = _safe_int(getattr(tb_symbol_obj, "symbolId", 0), 0)
        tb_symbol_name = str(getattr(tb_symbol_obj, "symbolName", "") or "").strip()
        try:
            tb_meta_msg = yield self.client.send(
                pb.ProtoOASymbolByIdReq(ctidTraderAccountId=int(account_id), symbolId=[int(tb_symbol_id)]),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
            )
            tb_meta_payload = Protobuf.extract(tb_meta_msg)
            tb_meta_list = list(getattr(tb_meta_payload, "symbol", []) or [])
            tb_meta = tb_meta_list[0] if tb_meta_list else None
            tb_digits = _safe_int(getattr(tb_meta, "digits", 5), 5) if tb_meta else 5
        except Exception:
            tb_digits = 5
        tb_scale = float(_MARKET_DATA_PRICE_SCALE)
        try:
            tb_msg = yield self.client.send(
                pb.ProtoOAGetTrendbarsReq(
                    ctidTraderAccountId=int(account_id),
                    symbolId=int(tb_symbol_id),
                    period=int(tb_period),
                    fromTimestamp=int(from_ms),
                    toTimestamp=int(to_ms),
                    count=int(tb_count),
                ),
                responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC),
            )
        except Exception as exc:
            defer.returnValue({
                "ok": False,
                "status": "trendbar_request_failed",
                "message": str(exc),
                "symbol": tb_symbol_name,
                "timeframe": tb_tf,
            })
            return
        tb_payload = Protobuf.extract(tb_msg)
        if isinstance(tb_payload, pb.ProtoOAErrorRes):
            defer.returnValue({
                "ok": False,
                "status": "trendbar_error",
                "message": str(getattr(tb_payload, "description", "") or ""),
                "error_code": str(getattr(tb_payload, "errorCode", "") or ""),
                "symbol": tb_symbol_name,
                "timeframe": tb_tf,
            })
            return
        raw_bars = list(getattr(tb_payload, "trendbar", []) or [])
        bars = []
        for bar in raw_bars:
            low_raw = _safe_int(getattr(bar, "low", 0), 0)
            delta_open = _safe_int(getattr(bar, "deltaOpen", 0), 0)
            delta_close = _safe_int(getattr(bar, "deltaClose", 0), 0)
            delta_high = _safe_int(getattr(bar, "deltaHigh", 0), 0)
            vol = _safe_int(getattr(bar, "volume", 0), 0)
            ts_min = _safe_int(getattr(bar, "utcTimestampInMinutes", 0), 0)
            low = low_raw / tb_scale
            bars.append({
                "ts_ms": ts_min * 60 * 1000,
                "ts_utc": _ms_to_iso(ts_min * 60 * 1000),
                "open": (low_raw + delta_open) / tb_scale,
                "high": (low_raw + delta_high) / tb_scale,
                "low": low,
                "close": (low_raw + delta_close) / tb_scale,
                "volume": vol,
            })
        defer.returnValue({
            "ok": True,
            "status": "trendbars_loaded",
            "symbol": tb_symbol_name,
            "symbol_id": int(tb_symbol_id),
            "timeframe": tb_tf,
            "digits": int(tb_digits),
            "bar_count": len(bars),
            "bars": bars,
            "has_more": bool(getattr(tb_payload, "hasMore", False)),
            "token_refresh": {},
        })

    @defer.inlineCallbacks
    def _mode_capture_market(self, account_id: int, payload: dict[str, Any]):
        light_symbols, _symbol_map = yield self._get_symbols(account_id)
        capture_symbols = [
            _normalize_capture_symbol(sym) for sym in list(payload.get("symbols") or []) if _normalize_capture_symbol(sym)
        ]
        if not capture_symbols:
            defer.returnValue({"ok": False, "status": "capture_symbols_missing", "message": "symbols missing", "account_id": int(account_id)})
            return
        include_depth = bool(payload.get("include_depth", True))
        duration_sec = max(3, _safe_int(payload.get("duration_sec"), 12))
        max_events = max(50, _safe_int(payload.get("max_events"), 600))
        max_depth_levels = max(1, _safe_int(payload.get("max_depth_levels"), 5))

        symbol_ids: list[int] = []
        symbol_name_by_id: dict[int, str] = {}
        resolved_symbols: list[dict] = []
        seen_symbol_ids: set[int] = set()
        for token in capture_symbols:
            light_symbol, match_reason = _resolve_symbol(light_symbols, {"symbol": token, "market_symbol": token})
            if light_symbol is None:
                continue
            symbol_id = _safe_int(getattr(light_symbol, "symbolId", 0), 0)
            symbol_name = str(getattr(light_symbol, "symbolName", "") or "").strip().upper()
            if symbol_id <= 0 or not symbol_name or symbol_id in seen_symbol_ids:
                continue
            seen_symbol_ids.add(symbol_id)
            symbol_ids.append(symbol_id)
            symbol_name_by_id[symbol_id] = symbol_name
            resolved_symbols.append({"symbol": symbol_name, "symbol_id": symbol_id, "match": match_reason})
        if not symbol_ids:
            defer.returnValue({"ok": False, "status": "capture_symbols_not_found", "message": f"no symbols resolved from {capture_symbols}", "account_id": int(account_id)})
            return

        scale_by_symbol_id = {int(sid): float(_MARKET_DATA_PRICE_SCALE) for sid in symbol_ids}
        spot_events: list[dict] = []
        depth_events: list[dict] = []
        total_events = 0
        used_tick_fallback = False
        capture_wait = defer.Deferred()
        capture_finish_scheduled = False

        def _maybe_finish_capture() -> None:
            nonlocal capture_finish_scheduled
            if (total_events >= max_events) and (not capture_wait.called) and (not capture_finish_scheduled):
                capture_finish_scheduled = True
                reactor.callLater(0, lambda: (not capture_wait.called) and capture_wait.callback(True))

        def _append_spot_event(evt) -> None:
            nonlocal total_events
            if total_events >= max_events:
                return
            sid = _safe_int(getattr(evt, "symbolId", 0), 0)
            if sid <= 0:
                return
            scale = float(scale_by_symbol_id.get(int(sid), 1.0) or 1.0)
            bid_raw = _safe_float(getattr(evt, "bid", 0.0), 0.0)
            ask_raw = _safe_float(getattr(evt, "ask", 0.0), 0.0)
            bid = (bid_raw / scale) if bid_raw > 0 else 0.0
            ask = (ask_raw / scale) if ask_raw > 0 else 0.0
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask or 0.0)
            spread = (ask - bid) if bid > 0 and ask > 0 else 0.0
            ts_ms = _safe_int(getattr(evt, "timestamp", 0), 0) or int(time.time() * 1000)
            spot_events.append({
                "account_id": int(account_id),
                "symbol_id": int(sid),
                "symbol": symbol_name_by_id.get(int(sid), ""),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "spread_pct": ((spread / mid) * 100.0) if mid > 0 and spread >= 0 else 0.0,
                "event_utc": _ms_to_iso(ts_ms),
                "event_ts": round(ts_ms / 1000.0, 3),
            })
            total_events += 1
            _maybe_finish_capture()

        def _append_depth_event(evt) -> None:
            nonlocal total_events
            sid = _safe_int(getattr(evt, "symbolId", 0), 0)
            if sid <= 0:
                return
            scale = float(scale_by_symbol_id.get(int(sid), 1.0) or 1.0)
            event_ts_ms = int(time.time() * 1000)
            level_counts = {"bid": 0, "ask": 0}
            for quote in list(getattr(evt, "newQuotes", []) or []):
                if total_events >= max_events:
                    break
                size = _safe_float(getattr(quote, "size", 0.0), 0.0)
                bid_raw = _safe_float(getattr(quote, "bid", 0.0), 0.0)
                ask_raw = _safe_float(getattr(quote, "ask", 0.0), 0.0)
                side = ""
                price = 0.0
                if bid_raw > 0:
                    side = "bid"
                    price = bid_raw / scale
                elif ask_raw > 0:
                    side = "ask"
                    price = ask_raw / scale
                if not side or price <= 0:
                    continue
                level_index = int(level_counts.get(side, 0))
                if level_index >= max_depth_levels:
                    continue
                level_counts[side] = level_index + 1
                depth_events.append({
                    "account_id": int(account_id),
                    "symbol_id": int(sid),
                    "symbol": symbol_name_by_id.get(int(sid), ""),
                    "quote_id": _safe_int(getattr(quote, "id", 0), 0) or None,
                    "side": side,
                    "price": price,
                    "size": size,
                    "level_index": level_index,
                    "event_utc": _ms_to_iso(event_ts_ms),
                    "event_ts": round(event_ts_ms / 1000.0, 3),
                })
                total_events += 1
            _maybe_finish_capture()

        def _on_market_message(_client, message) -> None:
            try:
                payload_evt = Protobuf.extract(message)
            except Exception:
                return
            if isinstance(payload_evt, pb.ProtoOASpotEvent):
                _append_spot_event(payload_evt)
            elif isinstance(payload_evt, pb.ProtoOADepthEvent):
                _append_depth_event(payload_evt)

        # Transient listener (NOT setMessageReceivedCallback — that slot is
        # permanently owned by _on_push_message, which forwards to us here).
        # Appended only AFTER the protocol is in hand so a whenConnected
        # failure cannot leak a dangling listener.
        protocol = yield self.client.whenConnected(failAfterFailures=1)
        self._capture_listeners.append(_on_market_message)
        try:
            try:
                protocol.send(
                    pb.ProtoOASubscribeSpotsReq(ctidTraderAccountId=int(account_id), symbolId=[int(v) for v in symbol_ids], subscribeToSpotTimestamp=True),
                    instant=True,
                )
            except Exception:
                pass
            if include_depth:
                try:
                    protocol.send(
                        pb.ProtoOASubscribeDepthQuotesReq(ctidTraderAccountId=int(account_id), symbolId=[int(v) for v in symbol_ids]),
                        instant=True,
                    )
                except Exception:
                    include_depth = False

            reactor.callLater(duration_sec, lambda: (not capture_wait.called) and capture_wait.callback(True))
            try:
                yield capture_wait
            except Exception:
                pass
        finally:
            if _on_market_message in self._capture_listeners:
                self._capture_listeners.remove(_on_market_message)

        # Unsubscribe ONLY the symbols that do not also hold a standing
        # spot_quote subscription for this account — a capture window must
        # never tear down the live cache feed underneath it.
        standing = self._active_spot_subs.get(int(account_id), set())
        transient_ids = [int(v) for v in symbol_ids if int(v) not in standing]
        if transient_ids:
            try:
                protocol.send(pb.ProtoOAUnsubscribeSpotsReq(ctidTraderAccountId=int(account_id), symbolId=transient_ids), instant=True)
            except Exception:
                pass
        if include_depth:
            try:
                protocol.send(pb.ProtoOAUnsubscribeDepthQuotesReq(ctidTraderAccountId=int(account_id), symbolId=[int(v) for v in symbol_ids]), instant=True)
            except Exception:
                pass

        if not spot_events:
            used_tick_fallback = True
            now_ms = int(time.time() * 1000)
            from_ms = max(0, now_ms - (duration_sec * 1000))
            for sid in list(symbol_ids or []):
                if total_events >= max_events:
                    break
                scale = float(scale_by_symbol_id.get(int(sid), 1.0) or 1.0)
                tick_series: dict[int, dict[str, float]] = {}
                for quote_type in (model.ProtoOAQuoteType.BID, model.ProtoOAQuoteType.ASK):
                    try:
                        tick_msg = yield self.client.send(
                            pb.ProtoOAGetTickDataReq(
                                ctidTraderAccountId=int(account_id),
                                symbolId=int(sid),
                                type=int(quote_type),
                                fromTimestamp=int(from_ms),
                                toTimestamp=int(now_ms),
                            ),
                            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC / 2),
                        )
                        tick_payload = Protobuf.extract(tick_msg)
                    except Exception:
                        continue
                    if isinstance(tick_payload, pb.ProtoOAErrorRes):
                        continue
                    side = "bid" if int(quote_type) == int(model.ProtoOAQuoteType.BID) else "ask"
                    for tick_row in list(getattr(tick_payload, "tickData", []) or []):
                        ts_ms = _safe_int(getattr(tick_row, "timestamp", 0), 0)
                        raw_tick = _safe_float(getattr(tick_row, "tick", 0.0), 0.0)
                        if ts_ms <= 0 or raw_tick <= 0:
                            continue
                        item = tick_series.setdefault(ts_ms, {})
                        item[side] = raw_tick / scale
                for ts_ms in sorted(tick_series.keys()):
                    item = tick_series.get(ts_ms) or {}
                    bid = _safe_float(item.get("bid"), 0.0)
                    ask = _safe_float(item.get("ask"), 0.0)
                    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask or 0.0)
                    spread = (ask - bid) if bid > 0 and ask > 0 else 0.0
                    spot_events.append({
                        "account_id": int(account_id),
                        "symbol_id": int(sid),
                        "symbol": symbol_name_by_id.get(sid, ""),
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "spread": spread,
                        "spread_pct": ((spread / mid) * 100.0) if mid > 0 and spread >= 0 else 0.0,
                        "event_utc": _ms_to_iso(ts_ms),
                        "event_ts": round(ts_ms / 1000.0, 3),
                    })
                    total_events += 1
                    if total_events >= max_events:
                        break

        run_id = datetime.now(timezone.utc).strftime("ctcap_%Y%m%d_%H%M%S")
        defer.returnValue({
            "ok": True,
            "status": "captured_live" if depth_events else ("captured_live_spot_only" if spot_events else "captured_empty"),
            "message": f"captured spots={len(spot_events)} depth={len(depth_events)} mode={'tick_fallback' if used_tick_fallback else 'live_subscribe'}",
            "run_id": run_id,
            "account_id": int(account_id),
            "environment": self.state.environment,
            "duration_sec": int(duration_sec),
            "include_depth": bool(include_depth),
            "symbols": resolved_symbols,
            "captured_at": _ms_to_iso(int(time.time() * 1000)),
            "spots": spot_events[:max_events],
            "depth": depth_events[:max_events],
            "token_refresh": {},
        })

    @defer.inlineCallbacks
    def _mode_execute(self, account_id: int, payload: dict[str, Any]):
        light_symbols, _symbol_map = yield self._get_symbols(account_id)
        light_symbol, match_reason = _resolve_symbol(light_symbols, payload)
        if light_symbol is None:
            defer.returnValue({
                "ok": False,
                "status": "symbol_not_found",
                "message": f"symbol not found for {payload.get('symbol')}",
                "signal_symbol": str(payload.get("symbol", "") or ""),
                "account_id": int(account_id),
                "environment": self.state.environment,
                "symbol_match": match_reason,
            })
            return
        symbol_id = _safe_int(getattr(light_symbol, "symbolId", 0), 0)
        symbol_name = str(getattr(light_symbol, "symbolName", "") or "").strip()
        symbol_by_id_msg = yield self.client.send(
            pb.ProtoOASymbolByIdReq(ctidTraderAccountId=int(account_id), symbolId=[int(symbol_id)]),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC),
        )
        symbol_by_id_payload = Protobuf.extract(symbol_by_id_msg)
        full_symbols = list(getattr(symbol_by_id_payload, "symbol", []) or [])
        symbol_meta = full_symbols[0] if full_symbols else None
        if symbol_meta is None:
            defer.returnValue({
                "ok": False,
                "status": "symbol_meta_missing",
                "message": f"symbol metadata missing for {symbol_name}",
                "signal_symbol": str(payload.get("symbol", "") or ""),
                "broker_symbol": symbol_name,
                "account_id": int(account_id),
                "environment": self.state.environment,
            })
            return
        trader_msg = yield self.client.send(
            pb.ProtoOATraderReq(ctidTraderAccountId=int(account_id)), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_LIGHT_SEC)
        )
        trader_payload = Protobuf.extract(trader_msg)
        trader = getattr(trader_payload, "trader", None)

        volume, volume_meta = _quantize_volume(symbol_meta, payload)
        direction = str(payload.get("direction", "") or "").strip().lower()
        order_type = str(payload.get("order_type", "market") or "market").strip().lower()
        side = model.ProtoOATradeSide.BUY if direction == "long" else model.ProtoOATradeSide.SELL
        if order_type == "limit":
            order_type_proto = model.ProtoOAOrderType.LIMIT
        elif order_type == "stop":
            order_type_proto = model.ProtoOAOrderType.STOP
        else:
            order_type_proto = model.ProtoOAOrderType.MARKET
        _lbl = str(payload.get("label", "") or "").strip()[:64]
        _cid_in = str(payload.get("client_order_id", "") or "").strip()[:64]
        _client_oid = (_cid_in or _lbl or f"w_{uuid.uuid4().hex[:20]}")[:64]
        if not _lbl:
            _lbl = _client_oid[:64]
        req_kwargs = {
            "ctidTraderAccountId": int(account_id),
            "symbolId": int(symbol_id),
            "orderType": order_type_proto,
            "tradeSide": side,
            "volume": int(volume),
            "comment": str(payload.get("comment", "") or "")[:128],
            "label": _lbl,
            "clientOrderId": _client_oid,
            "timeInForce": model.ProtoOATimeInForce.GOOD_TILL_CANCEL,
        }
        price_digits = _price_digits(symbol_meta, trader)
        entry_price = _quantize_price(payload.get("entry"), price_digits)
        stop_loss_price = _quantize_price(payload.get("stop_loss"), price_digits)
        take_profit_price = _quantize_price(payload.get("take_profit"), price_digits)
        if order_type == "limit":
            req_kwargs["limitPrice"] = entry_price
            if stop_loss_price > 0:
                req_kwargs["stopLoss"] = stop_loss_price
            if take_profit_price > 0:
                req_kwargs["takeProfit"] = take_profit_price
        elif order_type == "stop":
            req_kwargs["stopPrice"] = entry_price
            if stop_loss_price > 0:
                req_kwargs["stopLoss"] = stop_loss_price
            if take_profit_price > 0:
                req_kwargs["takeProfit"] = take_profit_price
        else:
            rel_stop = _relative_distance(entry_price, stop_loss_price)
            rel_take = _relative_distance(entry_price, take_profit_price)
            if rel_stop > 0:
                req_kwargs["relativeStopLoss"] = rel_stop
            if rel_take > 0:
                req_kwargs["relativeTakeProfit"] = rel_take
        exec_msg = yield self.client.send(pb.ProtoOANewOrderReq(**req_kwargs), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC))
        exec_payload = Protobuf.extract(exec_msg)
        if isinstance(exec_payload, (pb.ProtoOAOrderErrorEvent, pb.ProtoOAErrorRes)):
            defer.returnValue({
                "ok": False,
                "status": "rejected",
                "message": str(getattr(exec_payload, "description", "") or getattr(exec_payload, "errorCode", "order rejected")),
                "signal_symbol": str(payload.get("symbol", "") or ""),
                "broker_symbol": symbol_name,
                "account_id": int(account_id),
                "volume": float(volume),
                "execution_meta": {
                    "error_code": str(getattr(exec_payload, "errorCode", "") or ""),
                    "symbol_match": match_reason,
                    "price_digits": int(price_digits),
                    "volume_meta": volume_meta,
                },
            })
            return
        execution_type = _enum_name(model.ProtoOAExecutionType, getattr(exec_payload, "executionType", 0))
        order = getattr(exec_payload, "order", None)
        position = getattr(exec_payload, "position", None)
        deal = getattr(exec_payload, "deal", None)
        ok = execution_type in {"ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_PARTIAL_FILL"}
        status = "filled" if execution_type in {"ORDER_FILLED", "ORDER_PARTIAL_FILL"} else ("accepted" if ok else "rejected")
        defer.returnValue({
            "ok": bool(ok),
            "status": status,
            "message": f"ctrader {execution_type.lower()}",
            "signal_symbol": str(payload.get("symbol", "") or ""),
            "broker_symbol": symbol_name,
            "account_id": int(account_id),
            "order_id": _safe_int(getattr(order, "orderId", 0), 0) or None,
            "position_id": _safe_int(getattr(position, "positionId", 0), 0) or None,
            "deal_id": _safe_int(getattr(deal, "dealId", 0), 0) or None,
            "volume": float(volume),
            "execution_meta": {
                "environment": self.state.environment,
                "execution_type": execution_type,
                "symbol_id": int(symbol_id),
                "symbol_match": match_reason,
                "symbol_name": symbol_name,
                "price_digits": int(price_digits),
                "symbol_meta": _proto_to_dict(symbol_meta),
                "volume_meta": volume_meta,
                "raw_execution": _proto_to_dict(exec_payload),
            },
        })

    @defer.inlineCallbacks
    def _mode_close(self, account_id: int, payload: dict[str, Any]):
        position_id = _safe_int(payload.get("position_id"), 0)
        volume = _safe_int(payload.get("volume"), 0)
        if position_id <= 0:
            defer.returnValue({"ok": False, "status": "position_missing", "message": "position_id missing", "account_id": int(account_id), "environment": self.state.environment})
            return
        close_msg = yield self.client.send(
            pb.ProtoOAClosePositionReq(ctidTraderAccountId=int(account_id), positionId=int(position_id), volume=int(volume)),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC),
        )
        close_payload = Protobuf.extract(close_msg)
        if isinstance(close_payload, (pb.ProtoOAOrderErrorEvent, pb.ProtoOAErrorRes)):
            defer.returnValue({
                "ok": False,
                "status": "close_rejected",
                "message": str(getattr(close_payload, "description", "") or getattr(close_payload, "errorCode", "close rejected")),
                "account_id": int(account_id),
                "position_id": int(position_id),
                "environment": self.state.environment,
                "raw": _proto_to_dict(close_payload),
            })
            return
        execution_type = _enum_name(model.ProtoOAExecutionType, getattr(close_payload, "executionType", 0))
        order = getattr(close_payload, "order", None)
        position = getattr(close_payload, "position", None)
        deal = getattr(close_payload, "deal", None)
        defer.returnValue({
            "ok": execution_type in {"ORDER_FILLED", "ORDER_PARTIAL_FILL", "ORDER_ACCEPTED"},
            "status": "closed" if execution_type in {"ORDER_FILLED", "ORDER_PARTIAL_FILL"} else "close_submitted",
            "message": f"ctrader {execution_type.lower()}",
            "account_id": int(account_id),
            "position_id": _safe_int(getattr(position, "positionId", 0), position_id) or int(position_id),
            "order_id": _safe_int(getattr(order, "orderId", 0), 0) or None,
            "deal_id": _safe_int(getattr(deal, "dealId", 0), 0) or None,
            "environment": self.state.environment,
            "execution_meta": {"execution_type": execution_type, "raw_execution": _proto_to_dict(close_payload)},
        })

    @defer.inlineCallbacks
    def _mode_cancel_order(self, account_id: int, payload: dict[str, Any]):
        order_id = _safe_int(payload.get("order_id"), 0)
        if order_id <= 0:
            defer.returnValue({"ok": False, "status": "order_missing", "message": "order_id missing", "account_id": int(account_id), "environment": self.state.environment})
            return
        cancel_msg = yield self.client.send(
            pb.ProtoOACancelOrderReq(ctidTraderAccountId=int(account_id), orderId=int(order_id)),
            responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC),
        )
        cancel_payload = Protobuf.extract(cancel_msg)
        if isinstance(cancel_payload, (pb.ProtoOAOrderErrorEvent, pb.ProtoOAErrorRes)):
            defer.returnValue({
                "ok": False,
                "status": "cancel_rejected",
                "message": str(getattr(cancel_payload, "description", "") or getattr(cancel_payload, "errorCode", "cancel rejected")),
                "account_id": int(account_id),
                "order_id": int(order_id),
                "environment": self.state.environment,
                "raw": _proto_to_dict(cancel_payload),
            })
            return
        execution_type = _enum_name(model.ProtoOAExecutionType, getattr(cancel_payload, "executionType", 0))
        order = getattr(cancel_payload, "order", None)
        defer.returnValue({
            "ok": True,
            "status": "canceled",
            "message": (f"ctrader {execution_type.lower()}" if execution_type not in {"", "0"} else "ctrader canceled pending order"),
            "account_id": int(account_id),
            "order_id": _safe_int(getattr(order, "orderId", 0), order_id) or int(order_id),
            "environment": self.state.environment,
            "execution_meta": {"execution_type": execution_type, "raw_execution": _proto_to_dict(cancel_payload)},
        })

    @defer.inlineCallbacks
    def _mode_amend_order(self, account_id: int, payload: dict[str, Any]):
        order_id = _safe_int(payload.get("order_id"), 0)
        limit_price = _safe_float(payload.get("limit_price"), 0.0)
        stop_price = _safe_float(payload.get("stop_price"), 0.0)
        stop_loss = _safe_float(payload.get("stop_loss"), 0.0)
        take_profit = _safe_float(payload.get("take_profit"), 0.0)
        volume = _safe_int(payload.get("volume"), 0)
        trailing_stop_loss = bool(payload.get("trailing_stop_loss", False))
        if order_id <= 0:
            defer.returnValue({"ok": False, "status": "order_missing", "message": "order_id missing", "account_id": int(account_id), "environment": self.state.environment})
            return
        amend_kwargs = {
            "ctidTraderAccountId": int(account_id),
            "orderId": int(order_id),
            "trailingStopLoss": bool(trailing_stop_loss),
        }
        if volume > 0:
            amend_kwargs["volume"] = int(volume)
        if limit_price > 0:
            amend_kwargs["limitPrice"] = float(limit_price)
        if stop_price > 0:
            amend_kwargs["stopPrice"] = float(stop_price)
        if stop_loss > 0:
            amend_kwargs["stopLoss"] = float(stop_loss)
        if take_profit > 0:
            amend_kwargs["takeProfit"] = float(take_profit)
        amend_msg = yield self.client.send(pb.ProtoOAAmendOrderReq(**amend_kwargs), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC))
        amend_payload = Protobuf.extract(amend_msg)
        if isinstance(amend_payload, (pb.ProtoOAOrderErrorEvent, pb.ProtoOAErrorRes)):
            defer.returnValue({
                "ok": False,
                "status": "amend_order_rejected",
                "message": str(getattr(amend_payload, "description", "") or getattr(amend_payload, "errorCode", "amend order rejected")),
                "account_id": int(account_id),
                "order_id": int(order_id),
                "environment": self.state.environment,
                "raw": _proto_to_dict(amend_payload),
            })
            return
        execution_type = _enum_name(model.ProtoOAExecutionType, getattr(amend_payload, "executionType", 0))
        order = getattr(amend_payload, "order", None)
        defer.returnValue({
            "ok": True,
            "status": "amended_order",
            "message": (f"ctrader amended order {execution_type.lower()}" if execution_type not in {"", "0"} else "ctrader amended order"),
            "account_id": int(account_id),
            "order_id": _safe_int(getattr(order, "orderId", 0), order_id) or int(order_id),
            "environment": self.state.environment,
            "execution_meta": {"execution_type": execution_type, "raw_execution": _proto_to_dict(amend_payload)},
        })

    @defer.inlineCallbacks
    def _mode_amend_position_sltp(self, account_id: int, payload: dict[str, Any]):
        position_id = _safe_int(payload.get("position_id"), 0)
        stop_loss = _safe_float(payload.get("stop_loss"), 0.0)
        take_profit = _safe_float(payload.get("take_profit"), 0.0)
        trailing_stop_loss = bool(payload.get("trailing_stop_loss", False))
        if position_id <= 0:
            defer.returnValue({"ok": False, "status": "position_missing", "message": "position_id missing", "account_id": int(account_id), "environment": self.state.environment})
            return
        amend_kwargs = {
            "ctidTraderAccountId": int(account_id),
            "positionId": int(position_id),
            "trailingStopLoss": bool(trailing_stop_loss),
        }
        if stop_loss > 0:
            amend_kwargs["stopLoss"] = float(stop_loss)
        if take_profit > 0:
            amend_kwargs["takeProfit"] = float(take_profit)
        amend_msg = yield self.client.send(pb.ProtoOAAmendPositionSLTPReq(**amend_kwargs), responseTimeoutInSeconds=int(REQUEST_TIMEOUT_HEAVY_SEC))
        amend_payload = Protobuf.extract(amend_msg)
        if isinstance(amend_payload, (pb.ProtoOAOrderErrorEvent, pb.ProtoOAErrorRes)):
            defer.returnValue({
                "ok": False,
                "status": "amend_rejected",
                "message": str(getattr(amend_payload, "description", "") or getattr(amend_payload, "errorCode", "amend rejected")),
                "account_id": int(account_id),
                "position_id": int(position_id),
                "environment": self.state.environment,
                "raw": _proto_to_dict(amend_payload),
            })
            return
        execution_type = _enum_name(model.ProtoOAExecutionType, getattr(amend_payload, "executionType", 0))
        position = getattr(amend_payload, "position", None)
        defer.returnValue({
            "ok": True,
            "status": "amended",
            "message": (f"ctrader amended {execution_type.lower()}" if execution_type not in {"", "0"} else "ctrader amended position sl/tp"),
            "account_id": int(account_id),
            "position_id": _safe_int(getattr(position, "positionId", 0), position_id) or int(position_id),
            "environment": self.state.environment,
            "execution_meta": {"execution_type": execution_type, "raw_execution": _proto_to_dict(amend_payload)},
        })


def main() -> int:
    if not _HAS_DEPS:
        print(json.dumps({"ok": False, "status": "import_error", "message": str(_IMPORT_ERROR)}, ensure_ascii=True))
        return 1
    daemon = OpenApiDaemon()
    daemon.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

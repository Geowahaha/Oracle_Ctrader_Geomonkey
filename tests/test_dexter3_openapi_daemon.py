"""dexter3/openapi_daemon.py — HTTP layer, request validation, reconnect
state machine, and dispatch semantics. NO live network, NO Twisted reactor:

- HTTP-layer tests run ``build_server`` on an ephemeral loopback port with a
  plain-Python fake dispatcher (the daemon's HTTP listener is stdlib
  ``ThreadingHTTPServer`` by design so exactly this is possible — see the
  module docstring's concurrency-model note).
- Reconnect/backoff tests exercise ``ReconnectBackoff`` — a pure dataclass
  with no clock and no I/O.
- Dispatch tests exercise ``OpenApiDaemon._dispatch``'s mode routing/error
  envelopes by driving the ``@defer.inlineCallbacks`` coroutine with a fake
  client whose ``send()`` returns already-fired Deferreds — Twisted's
  ``defer`` module alone (no reactor started, nothing scheduled).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from dexter3.openapi_daemon import (
    DEFAULT_PORT,
    PORT_ENV_VAR,
    CallRequestError,
    ConnectionState,
    ReconnectBackoff,
    _account_id_from_payload,
    build_server,
    parse_call_body,
    resolve_port,
)


# ---------------------------------------------------------------------------
# parse_call_body — pure request validation
# ---------------------------------------------------------------------------


def test_parse_call_body_valid():
    mode, payload = parse_call_body(b'{"mode": "health", "payload": {"account_id": 46670728}}')
    assert mode == "health"
    assert payload == {"account_id": 46670728}


def test_parse_call_body_payload_defaults_to_empty_dict():
    mode, payload = parse_call_body(b'{"mode": "accounts"}')
    assert mode == "accounts"
    assert payload == {}


@pytest.mark.parametrize(
    "raw, fragment",
    [
        (b"", "empty"),
        (b"not json {", "invalid JSON"),
        (b'"just a string"', "JSON object"),
        (b"[1, 2, 3]", "JSON object"),
        (b'{"payload": {}}', "mode"),
        (b'{"mode": ""}', "mode"),
        (b'{"mode": "   "}', "mode"),
        (b'{"mode": "health", "payload": [1]}', "payload"),
        (b'{"mode": "health", "payload": "x"}', "payload"),
    ],
)
def test_parse_call_body_rejections(raw: bytes, fragment: str):
    with pytest.raises(CallRequestError) as excinfo:
        parse_call_body(raw)
    assert fragment.lower() in str(excinfo.value).lower()
    assert excinfo.value.http_status == 400


# ---------------------------------------------------------------------------
# resolve_port
# ---------------------------------------------------------------------------


def test_resolve_port_default(monkeypatch):
    monkeypatch.delenv(PORT_ENV_VAR, raising=False)
    assert resolve_port(None) == DEFAULT_PORT == 9877


def test_resolve_port_env_override(monkeypatch):
    monkeypatch.setenv(PORT_ENV_VAR, "12345")
    assert resolve_port(None) == 12345


def test_resolve_port_explicit_argument_wins(monkeypatch):
    monkeypatch.setenv(PORT_ENV_VAR, "12345")
    assert resolve_port(777) == 777


# ---------------------------------------------------------------------------
# ReconnectBackoff — pure state machine (base -> doubling -> cap -> reset)
# ---------------------------------------------------------------------------


def test_backoff_doubles_and_caps():
    b = ReconnectBackoff(base_sec=5.0, max_sec=300.0)
    delays = [b.next_delay() for _ in range(9)]
    assert delays == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]


def test_backoff_reset_returns_to_base():
    b = ReconnectBackoff(base_sec=5.0, max_sec=300.0)
    for _ in range(6):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 5.0
    assert b.next_delay() == 10.0


def test_backoff_base_already_above_cap_clamps():
    b = ReconnectBackoff(base_sec=500.0, max_sec=300.0)
    assert b.next_delay() == 300.0
    assert b.next_delay() == 300.0


# ---------------------------------------------------------------------------
# ConnectionState.health_payload — pure snapshot
# ---------------------------------------------------------------------------


def test_health_payload_disconnected_initial_state():
    s = ConnectionState()
    snap = s.health_payload(now=100.0)
    assert snap == {
        "connected": False,
        "app_authed": False,
        "environment": "",
        "account_ids": [],
        "uptime_sec": 0.0,
        "reconnect_count": 0,
        "last_error": "",
    }


def test_health_payload_connected_with_uptime_and_accounts():
    s = ConnectionState()
    s.connected = True
    s.app_authed = True
    s.environment = "demo"
    s.started_at = 1000.0
    s.reconnect_count = 3
    s.last_error = ""
    s.authed_account_ids.update({46670728, 111})
    snap = s.health_payload(now=1012.5)
    assert snap["connected"] is True
    assert snap["environment"] == "demo"
    assert snap["uptime_sec"] == pytest.approx(12.5)
    assert snap["account_ids"] == [111, 46670728]
    assert snap["reconnect_count"] == 3


def test_health_payload_surfaces_last_error():
    s = ConnectionState()
    s.last_error = "account auth failed for 46670728: CH_ACCESS_TOKEN_INVALID"
    snap = s.health_payload(now=0.0)
    assert "CH_ACCESS_TOKEN_INVALID" in snap["last_error"]


# ---------------------------------------------------------------------------
# HTTP layer — real loopback socket, fake dispatcher, no Twisted
# ---------------------------------------------------------------------------


class _HttpFixture:
    """Real ThreadingHTTPServer on an ephemeral port with a scriptable
    dispatcher; collects every (mode, payload) it receives."""

    def __init__(self, dispatch=None, health=None) -> None:
        self.received: list[tuple[str, dict]] = []

        def _default_dispatch(mode: str, payload: dict) -> dict:
            self.received.append((mode, dict(payload)))
            return {"ok": True, "status": "echo", "mode": mode, "payload": payload}

        self._server = build_server(
            dispatch_call=dispatch or _default_dispatch,
            health_snapshot=health or (lambda: {"connected": True, "last_error": ""}),
            port=0,
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, path: str, body: bytes) -> tuple[int, dict]:
        req = urllib.request.Request(self.url(path), data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def http_fixture():
    fx = _HttpFixture()
    yield fx
    fx.close()


def test_http_health_route(http_fixture: _HttpFixture):
    status, body = http_fixture.get("/health")
    assert status == 200
    assert body == {"connected": True, "last_error": ""}


def test_http_call_routes_mode_and_payload_to_dispatcher(http_fixture: _HttpFixture):
    status, body = http_fixture.post("/call", b'{"mode": "reconcile", "payload": {"account_id": 46670728, "max_rows": 50}}')
    assert status == 200
    assert body["ok"] is True
    assert http_fixture.received == [("reconcile", {"account_id": 46670728, "max_rows": 50})]


def test_http_call_bad_json_is_400_with_json_error_shape(http_fixture: _HttpFixture):
    status, body = http_fixture.post("/call", b"this is not json")
    assert status == 400
    assert body["ok"] is False
    assert body["status"] == "bad_request"
    assert http_fixture.received == []  # dispatcher never reached


def test_http_call_missing_mode_is_400(http_fixture: _HttpFixture):
    status, body = http_fixture.post("/call", b'{"payload": {}}')
    assert status == 400
    assert body["status"] == "bad_request"
    assert "mode" in body["message"]


def test_http_unknown_get_route_is_404_json(http_fixture: _HttpFixture):
    status, body = http_fixture.get("/nope")
    assert status == 404
    assert body["status"] == "not_found"


def test_http_unknown_post_route_is_404_json(http_fixture: _HttpFixture):
    status, body = http_fixture.post("/nope", b'{"mode": "health"}')
    assert status == 404
    assert body["status"] == "not_found"


def test_http_dispatcher_exception_is_500_json_not_a_crash():
    def _boom(mode: str, payload: dict) -> dict:
        raise RuntimeError("dispatcher exploded")

    fx = _HttpFixture(dispatch=_boom)
    try:
        status, body = fx.post("/call", b'{"mode": "health"}')
        assert status == 500
        assert body["ok"] is False
        assert body["status"] == "internal_error"
        assert "exploded" in body["message"]
        # and the server is still alive for the next request
        status2, _ = fx.get("/health")
        assert status2 == 200
    finally:
        fx.close()


def test_http_dispatcher_non_dict_result_is_wrapped_as_error():
    def _bad(mode: str, payload: dict):
        return ["not", "a", "dict"]

    fx = _HttpFixture(dispatch=_bad)
    try:
        status, body = fx.post("/call", b'{"mode": "health"}')
        assert status == 200  # domain-level error convention: HTTP 200 + ok:false
        assert body["ok"] is False
        assert body["status"] == "internal_error"
    finally:
        fx.close()


def test_http_two_servers_in_one_process_do_not_share_dispatchers():
    """build_server mints a fresh handler subclass per call — two servers in
    the same process must route to their OWN dispatchers (regression guard
    for the class-attribute-clobbering failure mode the factory avoids)."""
    fx_a = _HttpFixture(dispatch=lambda m, p: {"ok": True, "who": "a"})
    fx_b = _HttpFixture(dispatch=lambda m, p: {"ok": True, "who": "b"})
    try:
        _, body_a = fx_a.post("/call", b'{"mode": "health"}')
        _, body_b = fx_b.post("/call", b'{"mode": "health"}')
        assert body_a["who"] == "a"
        assert body_b["who"] == "b"
    finally:
        fx_a.close()
        fx_b.close()


def test_http_concurrent_calls_are_served_in_parallel():
    """ThreadingHTTPServer must not serialize slow calls behind each other:
    two concurrent 0.3s dispatches should complete in well under 0.6s total.
    This is the property the lanes depend on (one slow capture_market must
    not block a peer lane's health read at the HTTP layer)."""
    import time as _time

    barrier = threading.Barrier(2, timeout=5)

    def _slow(mode: str, payload: dict) -> dict:
        barrier.wait()  # proves both requests are in-flight simultaneously
        _time.sleep(0.3)
        return {"ok": True, "status": "slow_done"}

    fx = _HttpFixture(dispatch=_slow)
    try:
        results: list[dict] = []
        t0 = _time.monotonic()

        def _worker():
            _, body = fx.post("/call", b'{"mode": "health"}')
            results.append(body)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = _time.monotonic() - t0
        assert len(results) == 2
        assert all(r["ok"] for r in results)
        assert elapsed < 1.5  # generous bound; serialized would be >= 0.6 + barrier timeout
    finally:
        fx.close()


# ---------------------------------------------------------------------------
# _account_id_from_payload — explicit payload id always wins
# ---------------------------------------------------------------------------


def test_account_id_from_payload_explicit_wins():
    assert _account_id_from_payload({"account_id": 46670728}) == 46670728


def test_account_id_from_payload_bad_value_falls_back_to_config_chain(monkeypatch):
    """A non-numeric payload account_id must not crash and must fall through
    to the config chain — with every config fallback blanked, the result is
    exactly 0 (the 'account_missing' trigger in _dispatch)."""
    import dexter3.openapi_daemon as daemon_mod

    monkeypatch.delenv("DEXTER3_OPENAPI_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(daemon_mod.config, "find_ctrader_account", None, raising=False)
    monkeypatch.setattr(daemon_mod.config, "CTRADER_ACCOUNT_LOGIN", "", raising=False)
    monkeypatch.setattr(daemon_mod.config, "CTRADER_ACCOUNT_ID", "", raising=False)
    assert _account_id_from_payload({"account_id": "not-a-number"}) == 0
    assert _account_id_from_payload({}) == 0


def test_account_id_from_payload_env_pin_beats_generic_config_login(monkeypatch):
    """With no EXPLICIT account identity in the payload, the dexter3 pin
    (DEXTER3_OPENAPI_ACCOUNT_ID) must beat a SET generic config
    CTRADER_ACCOUNT_LOGIN that points at a different demo. This is the live VM
    bug: CTRADER_ACCOUNT_LOGIN=9900897 resolved 46552794 and won for
    account_id-less calls, so the governor's get_deals reconcile hit an EMPTY
    account (46552794) instead of the lane account (46670728)."""
    import dexter3.openapi_daemon as daemon_mod

    monkeypatch.setenv("DEXTER3_OPENAPI_ACCOUNT_ID", "46670728")
    # a finder + config login that WOULD otherwise resolve the wrong demo
    monkeypatch.setattr(
        daemon_mod.config, "find_ctrader_account",
        lambda ident="", **_k: {"accountId": 46552794}, raising=False,
    )
    monkeypatch.setattr(daemon_mod.config, "CTRADER_ACCOUNT_LOGIN", "9900897", raising=False)
    monkeypatch.setattr(daemon_mod.config, "CTRADER_ACCOUNT_ID", "", raising=False)
    monkeypatch.setattr(daemon_mod.config, "CTRADER_USE_DEMO", True, raising=False)
    assert _account_id_from_payload({}) == 46670728


def test_account_id_from_payload_explicit_beats_env_pin(monkeypatch):
    """The hot trading path always supplies account_id; it must win over the
    env pin so the pin never silently redirects an explicit request."""
    monkeypatch.setenv("DEXTER3_OPENAPI_ACCOUNT_ID", "46670728")
    assert _account_id_from_payload({"account_id": 99999999}) == 99999999


# ---------------------------------------------------------------------------
# OpenApiDaemon dispatch semantics — Twisted defer only, no reactor
# ---------------------------------------------------------------------------

twisted_defer = pytest.importorskip("twisted.internet.defer")

from dexter3.openapi_daemon import OpenApiDaemon, _HAS_DEPS  # noqa: E402

pytestmark_daemon = pytest.mark.skipif(not _HAS_DEPS, reason="twisted/ctrader_open_api not importable")


def _sync_result(d):
    """Extract the result of an already-fired Deferred (fails the test if it
    hasn't fired — nothing in these tests may depend on a running reactor)."""
    out: dict[str, Any] = {}
    d.addCallback(lambda r: out.update({"result": r}))
    d.addErrback(lambda f: out.update({"failure": f}))
    assert "result" in out or "failure" in out, "Deferred did not fire synchronously"
    if "failure" in out:
        out["failure"].raiseException()
    return out["result"]


def _make_daemon() -> OpenApiDaemon:
    d = OpenApiDaemon(port=0)
    d.state.connected = True
    d.state.app_authed = True
    d.state.environment = "demo"
    return d


@pytestmark_daemon
def test_dispatch_unknown_mode_returns_clean_error():
    d = _make_daemon()
    d.state.authed_account_ids.add(46670728)
    result = _sync_result(d._dispatch("frobnicate", {"account_id": 46670728}))
    assert result == {"ok": False, "status": "unknown_mode", "message": "unsupported mode: 'frobnicate'"}


@pytestmark_daemon
def test_dispatch_missing_account_id_returns_account_missing(monkeypatch):
    # Blank out every config fallback so the payload is the only source.
    import dexter3.openapi_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_account_id_from_payload", lambda payload: 0)
    d = _make_daemon()
    result = _sync_result(d._dispatch("health", {}))
    assert result["ok"] is False
    assert result["status"] == "account_missing"


@pytestmark_daemon
def test_dispatch_account_auth_failure_maps_to_account_auth_failed(monkeypatch):
    """A broker ProtoOAErrorRes during account auth must come back as the
    same status ops/ctrader_execute_once.py uses ('account_auth_failed'),
    and must be recorded in /health's last_error."""
    from ctrader_open_api.messages import OpenApiMessages_pb2 as pb
    from ctrader_open_api import Protobuf

    d = _make_daemon()
    err = pb.ProtoOAErrorRes()
    err.errorCode = "CH_ACCESS_TOKEN_INVALID"
    err.description = "Invalid access token"

    class _FakeClient:
        def send(self, message, responseTimeoutInSeconds=None, **_kw):  # noqa: N803 - lib kwarg name
            return twisted_defer.succeed(err)

    monkeypatch.setattr(Protobuf, "extract", staticmethod(lambda m: m))
    d.client = _FakeClient()
    result = _sync_result(d._dispatch("health", {"account_id": 46670728}))
    assert result["ok"] is False
    assert result["status"] == "account_auth_failed"
    assert "Invalid access token" in result["message"]
    assert "Invalid access token" in d.state.last_error
    assert 46670728 not in d.state.authed_account_ids


@pytestmark_daemon
def test_ensure_account_auth_already_authorized_is_benign_success(monkeypatch):
    """'Trading account is already authorized in this channel' is NOT a
    failure: a reconnect cleared our local authed set while the broker still
    holds the account authed on this channel. Adopt it + clear last_error
    instead of raising _ModeError (which blocked reconcile/trade during the
    reconnect window)."""
    import dexter3.openapi_daemon as daemon_mod
    from ctrader_open_api.messages import OpenApiMessages_pb2 as pb
    from ctrader_open_api import Protobuf

    d = _make_daemon()
    d.state.last_error = "account auth failed for 46670728: already authorized"
    err = pb.ProtoOAErrorRes()
    err.errorCode = "ALREADY_AUTHORIZED"
    err.description = "Trading account is already authorized in this channel."

    class _FakeClient:
        def send(self, message, responseTimeoutInSeconds=None, **_kw):  # noqa: N803
            return twisted_defer.succeed(err)

    monkeypatch.setattr(Protobuf, "extract", staticmethod(lambda m: m))
    monkeypatch.setattr(daemon_mod, "_fresh_access_token", lambda: "tok", raising=False)
    d.client = _FakeClient()
    # must NOT raise, and must not depend on a running reactor
    _sync_result(d._ensure_account_auth(46670728))
    assert 46670728 in d.state.authed_account_ids
    assert d.state.last_error == ""


@pytestmark_daemon
def test_dispatch_account_auth_cached_after_success(monkeypatch):
    """Once an account is authed on this connection, subsequent calls skip
    ProtoOAAccountAuthReq (cTrader account-auth is per-connection, so
    re-authing every call would be pointless round-trips)."""
    from ctrader_open_api import Protobuf

    d = _make_daemon()
    sent: list[str] = []

    class _Trader:
        balance = 100000
        moneyDigits = 2
        leverageInCents = 10000

    class _TraderRes:
        trader = _Trader()

    class _ReconcileRes:
        position: list = []
        order: list = []

    class _FakeClient:
        def send(self, message, responseTimeoutInSeconds=None, **_kw):  # noqa: N803
            name = type(message).__name__
            sent.append(name)
            if "TraderReq" in name:
                return twisted_defer.succeed(_TraderRes())
            if "ReconcileReq" in name:
                return twisted_defer.succeed(_ReconcileRes())
            if "AccountAuthReq" in name:
                return twisted_defer.succeed(object())  # non-error -> auth ok
            raise AssertionError(f"unexpected message {name}")

    monkeypatch.setattr(Protobuf, "extract", staticmethod(lambda m: m))
    d.client = _FakeClient()

    first = _sync_result(d._dispatch("health", {"account_id": 46670728}))
    assert first["ok"] is True
    assert first["balance"] == 100000
    assert 46670728 in d.state.authed_account_ids
    auth_count_first = sum(1 for n in sent if "AccountAuthReq" in n)
    assert auth_count_first == 1

    second = _sync_result(d._dispatch("health", {"account_id": 46670728}))
    assert second["ok"] is True
    auth_count_total = sum(1 for n in sent if "AccountAuthReq" in n)
    assert auth_count_total == 1  # NOT re-authed


@pytestmark_daemon
def test_dispatch_unexpected_exception_becomes_worker_error_envelope(monkeypatch):
    """Any unhandled exception inside a mode handler must surface as the
    worker's own 'worker_error' status envelope — never a raw traceback to
    the HTTP caller, never a crashed reactor thread."""
    d = _make_daemon()

    class _ExplodingClient:
        def send(self, *_a, **_kw):
            raise RuntimeError("kaboom mid-protocol")

    d.client = _ExplodingClient()
    result = _sync_result(d._dispatch("health", {"account_id": 46670728}))
    assert result["ok"] is False
    assert result["status"] == "worker_error"
    assert "kaboom" in result["message"]


@pytestmark_daemon
def test_handle_call_while_disconnected_returns_disconnected_json():
    """Requests during a disconnect window get the clean JSON error the task
    spec requires — {"ok": false, "status": "disconnected"} — without ever
    touching the (absent) connection or the reactor."""
    d = OpenApiDaemon(port=0)
    d.state.connected = False
    result = d.handle_call("reconcile", {"account_id": 46670728})
    assert result["ok"] is False
    assert result["status"] == "disconnected"


@pytestmark_daemon
def test_daemon_never_calls_token_refresh(monkeypatch):
    """READ-ONLY token consumption (single-owner architecture, commit
    da341f4): the daemon must never invoke try_refresh/ensure_fresh_access_token
    or write token state — only get_access_token. Guard the whole dispatch
    path for the auth-bearing mode."""
    from ctrader_open_api import Protobuf

    import dexter3.openapi_daemon as daemon_mod

    forbidden_calls: list[str] = []
    monkeypatch.setattr(
        daemon_mod.token_manager, "try_refresh", lambda *a, **k: forbidden_calls.append("try_refresh") or ""
    )
    monkeypatch.setattr(
        daemon_mod.token_manager,
        "ensure_fresh_access_token",
        lambda *a, **k: forbidden_calls.append("ensure_fresh_access_token") or "",
    )
    monkeypatch.setattr(
        daemon_mod.token_manager, "on_token_failed", lambda *a, **k: forbidden_calls.append("on_token_failed")
    )
    monkeypatch.setattr(
        daemon_mod.token_manager, "on_token_refreshed", lambda *a, **k: forbidden_calls.append("on_token_refreshed")
    )
    monkeypatch.setattr(daemon_mod.token_manager, "get_access_token", lambda: "tok-read-only")

    d = _make_daemon()

    class _FakeClient:
        def send(self, message, responseTimeoutInSeconds=None, **_kw):  # noqa: N803
            name = type(message).__name__
            if "AccountAuthReq" in name:
                assert message.accessToken == "tok-read-only"
                return twisted_defer.succeed(object())
            # any further protocol step is irrelevant to this test
            raise RuntimeError("stop here")

    monkeypatch.setattr(Protobuf, "extract", staticmethod(lambda m: m))
    d.client = _FakeClient()
    result = _sync_result(d._dispatch("health", {"account_id": 46670728}))
    assert result["status"] == "worker_error"  # the deliberate stop above
    assert forbidden_calls == []  # THE assertion: no refresh path was touched


@pytestmark_daemon
def test_reconnect_scheduling_uses_backoff_and_flags_state(monkeypatch):
    """_schedule_reconnect: state goes disconnected, reconnect_count
    increments, and reactor.callLater is asked for exactly the backoff's
    next delay (reactor is monkeypatched — never actually running)."""
    import dexter3.openapi_daemon as daemon_mod

    scheduled: list[tuple[float, Any]] = []
    monkeypatch.setattr(
        daemon_mod.reactor, "callLater", lambda delay, fn, *a: scheduled.append((delay, fn)), raising=False
    )
    d = _make_daemon()
    d._running = True
    d.client = None

    d._schedule_reconnect()
    d._schedule_reconnect()
    d._schedule_reconnect()

    assert d.state.connected is False
    assert d.state.app_authed is False
    assert d.state.reconnect_count == 3
    assert [delay for delay, _fn in scheduled] == [5.0, 10.0, 20.0]
    assert all(fn == d._connect for _delay, fn in scheduled)


@pytestmark_daemon
def test_reconnect_not_scheduled_after_shutdown(monkeypatch):
    import dexter3.openapi_daemon as daemon_mod

    scheduled: list[float] = []
    monkeypatch.setattr(daemon_mod.reactor, "callLater", lambda delay, fn, *a: scheduled.append(delay), raising=False)
    d = _make_daemon()
    d._running = False  # shutdown in progress
    d._schedule_reconnect()
    assert scheduled == []


@pytestmark_daemon
def test_disconnect_callback_clears_auth_and_symbol_caches(monkeypatch):
    """On a dropped connection the per-connection caches MUST be flushed:
    account auth is per-connection in cTrader's protocol, so replaying a
    cached auth on a NEW connection would silently operate unauthenticated."""
    import dexter3.openapi_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.reactor, "callLater", lambda *a, **k: None, raising=False)
    d = _make_daemon()
    d._running = True
    d.state.authed_account_ids.add(46670728)
    d._symbols_cache[46670728] = ([], {})

    d._on_disconnected(None, "connection lost")

    assert d.state.connected is False
    assert d.state.authed_account_ids == set()
    assert d._symbols_cache == {}
    assert "connection lost" in d.state.last_error


# ---------------------------------------------------------------------------
# spot_quote — standing-subscription live cache (coordinator directive
# 2026-07-10: VM shadow HARD-VETOed on spread_abs=0.0 because the short
# capture window returned no ticks; the daemon must serve real-time quotes
# from a live cache with a staleness bound instead)
# ---------------------------------------------------------------------------

ACCOUNT = 46670728
XAU_ID = 41


class _LightSymbol:
    symbolName = "XAUUSD"
    symbolId = XAU_ID
    description = "Gold vs US Dollar"


class _FakeSpotEvent:
    """Duck-typed ProtoOASpotEvent — _update_spot_cache reads via getattr."""

    def __init__(self, bid: float = 0.0, ask: float = 0.0, account_id: int = ACCOUNT, symbol_id: int = XAU_ID, ts: int = 1783623090337):
        self.ctidTraderAccountId = account_id
        self.symbolId = symbol_id
        self.bid = bid  # raw 1e-5 units, e.g. 412583000.0 -> 4125.83
        self.ask = ask
        self.timestamp = ts


class _FakeProtocol:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message, instant=False, **_kw):
        self.sent.append(type(message).__name__)


class _SpotFakeClient:
    """Fake persistent client for spot_quote dispatch tests: answers
    SymbolsListReq/AccountAuthReq, exposes a protocol that records
    subscription sends."""

    def __init__(self) -> None:
        self.protocol = _FakeProtocol()
        self.sent: list[str] = []

    def send(self, message, responseTimeoutInSeconds=None, **_kw):  # noqa: N803
        name = type(message).__name__
        self.sent.append(name)
        if "AccountAuthReq" in name:
            return twisted_defer.succeed(object())
        if "SymbolsListReq" in name:
            class _SymbolsRes:
                symbol = [_LightSymbol()]

            return twisted_defer.succeed(_SymbolsRes())
        raise AssertionError(f"unexpected message {name}")

    def whenConnected(self, failAfterFailures=None):  # noqa: N803
        return twisted_defer.succeed(self.protocol)


def _spot_daemon(monkeypatch) -> tuple[Any, _SpotFakeClient]:
    from ctrader_open_api import Protobuf

    monkeypatch.setattr(Protobuf, "extract", staticmethod(lambda m: m))
    d = _make_daemon()
    fake = _SpotFakeClient()
    d.client = fake
    return d, fake


@pytestmark_daemon
def test_spot_quote_fresh_quote_served_from_cache(monkeypatch):
    d, fake = _spot_daemon(monkeypatch)
    # A live tick arrives via the permanent push path (raw 1e-5 price units).
    d._update_spot_cache(_FakeSpotEvent(bid=412583000.0, ask=412603000.0))
    result = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert result["ok"] is True
    assert result["status"] == "spot_quote"
    assert result["source"] == "live_cache"
    [spot] = result["spots"]
    assert spot["bid"] == pytest.approx(4125.83)
    assert spot["ask"] == pytest.approx(4126.03)
    assert spot["spread"] == pytest.approx(0.20)
    assert result["quote_age_sec"] < 1.0
    # a standing subscription was established for the symbol
    assert "ProtoOASubscribeSpotsReq" in fake.protocol.sent
    assert XAU_ID in d._active_spot_subs[ACCOUNT]


@pytestmark_daemon
def test_spot_quote_stale_quote_rejected(monkeypatch):
    """A cached quote older than the staleness bound must be REFUSED
    (status=spot_stale, ok=false) — never served as live. This is the
    fail-closed contract: the entry pipeline HARD-VETOes on it, exactly as
    it should."""
    import time as _time

    d, _fake = _spot_daemon(monkeypatch)
    stale_mono = _time.monotonic() - 10.0  # bound is 5.0s (DEFAULT_SPOT_MAX_AGE_SEC)
    d._spot_cache[(ACCOUNT, XAU_ID)] = {
        "bid": 4125.8, "ask": 4126.0, "bid_mono": stale_mono, "ask_mono": stale_mono, "ts_ms": 0,
    }
    result = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert result["ok"] is False
    assert result["status"] == "spot_stale"
    assert result["age_sec"] >= 9.0
    assert result["max_age_sec"] == pytest.approx(5.0)
    assert "spots" not in result  # a stale price must never ride along


@pytestmark_daemon
def test_spot_quote_one_stale_side_is_not_a_quote(monkeypatch):
    """Fresh bid + stale ask = fabricated spread. The age of a quote is the
    age of its OLDER side."""
    import time as _time

    d, _fake = _spot_daemon(monkeypatch)
    d._spot_cache[(ACCOUNT, XAU_ID)] = {
        "bid": 4125.8, "ask": 4126.0,
        "bid_mono": _time.monotonic(),          # fresh
        "ask_mono": _time.monotonic() - 60.0,   # a minute old
        "ts_ms": 0,
    }
    result = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert result["ok"] is False
    assert result["status"] == "spot_stale"


@pytestmark_daemon
def test_spot_quote_subscription_reestablished_after_reconnect(monkeypatch):
    """Disconnect kills the standing subscription; the NEXT spot_quote on
    the new connection must re-send ProtoOASubscribeSpotsReq (lazy
    re-establishment — self-heals within one lane cycle)."""
    import dexter3.openapi_daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.reactor, "callLater", lambda *a, **k: None, raising=False)
    d, fake_a = _spot_daemon(monkeypatch)

    d._update_spot_cache(_FakeSpotEvent(bid=412583000.0, ask=412603000.0))
    first = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert first["ok"] is True
    assert fake_a.protocol.sent.count("ProtoOASubscribeSpotsReq") == 1

    # second call on the SAME connection: no duplicate subscribe
    second = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert second["ok"] is True
    assert fake_a.protocol.sent.count("ProtoOASubscribeSpotsReq") == 1

    # connection drops
    d._running = True
    d._on_disconnected(None, "tcp reset")
    assert d._active_spot_subs == {}

    # new connection comes up (fresh fake client), auth caches were cleared
    fake_b = _SpotFakeClient()
    d.client = fake_b
    d.state.connected = True
    d.state.app_authed = True
    d._update_spot_cache(_FakeSpotEvent(bid=412590000.0, ask=412610000.0))
    third = _sync_result(d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 0}))
    assert third["ok"] is True
    assert fake_b.protocol.sent.count("ProtoOASubscribeSpotsReq") == 1  # re-established
    assert any("AccountAuthReq" in n for n in fake_b.sent)  # re-authed on the new connection too


@pytestmark_daemon
def test_spot_quote_waiter_fires_on_first_complete_tick(monkeypatch):
    """Cold cache: the request parks a waiter; the first COMPLETE (bid+ask)
    tick fires it and the request returns that quote. Driven synchronously —
    reactor.callLater is stubbed to a no-op timeout handle."""
    import dexter3.openapi_daemon as daemon_mod

    class _FakeDelayedCall:
        def active(self) -> bool:
            return False

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(daemon_mod.reactor, "callLater", lambda *_a, **_k: _FakeDelayedCall(), raising=False)
    d, _fake = _spot_daemon(monkeypatch)

    results: list[dict] = []
    dd = d._dispatch("spot_quote", {"account_id": ACCOUNT, "symbol": "XAUUSD", "wait_sec": 2.5})
    dd.addCallback(results.append)
    assert results == []  # parked, waiting for the first tick
    assert len(d._spot_waiters[(ACCOUNT, XAU_ID)]) == 1

    # bid-only event: still not a complete quote — must NOT fire the waiter
    d._update_spot_cache(_FakeSpotEvent(bid=412583000.0, ask=0.0))
    assert results == []

    # ask arrives: complete quote -> waiter fires -> request completes
    d._update_spot_cache(_FakeSpotEvent(bid=0.0, ask=412603000.0))
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["spots"][0]["bid"] == pytest.approx(4125.83)
    assert results[0]["spots"][0]["ask"] == pytest.approx(4126.03)
    assert d._spot_waiters.get((ACCOUNT, XAU_ID), []) == []  # waiter cleaned up


@pytestmark_daemon
def test_spot_max_age_env_override(monkeypatch):
    from dexter3.openapi_daemon import DEFAULT_SPOT_MAX_AGE_SEC, SPOT_MAX_AGE_ENV_VAR, resolve_spot_max_age_sec

    monkeypatch.delenv(SPOT_MAX_AGE_ENV_VAR, raising=False)
    assert resolve_spot_max_age_sec() == DEFAULT_SPOT_MAX_AGE_SEC == 5.0
    monkeypatch.setenv(SPOT_MAX_AGE_ENV_VAR, "2.0")
    assert resolve_spot_max_age_sec() == 2.0
    monkeypatch.setenv(SPOT_MAX_AGE_ENV_VAR, "0")  # nonsense -> default, never "no bound"
    assert resolve_spot_max_age_sec() == DEFAULT_SPOT_MAX_AGE_SEC
    monkeypatch.setenv(SPOT_MAX_AGE_ENV_VAR, "banana")
    assert resolve_spot_max_age_sec() == DEFAULT_SPOT_MAX_AGE_SEC

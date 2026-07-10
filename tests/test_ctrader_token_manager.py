"""
tests/test_ctrader_token_manager.py

Covers the 2026-07-10 token-architecture fix in api/ctrader_token_manager.py:
  - single-owner refresh + read-only consumers (DEXTER3_TOKEN_SINGLE_OWNER)
  - the stale-write guard that refuses to let an older token_state clobber
    a newer one (cross-process safety against Spotware's single-use/rotating
    refresh_token)
  - on_token_failed() never overwriting a valid token with empty/stale data

No live network calls: ctrader_open_api.Auth is always faked via
sys.modules injection, so these tests work whether or not the real
ctrader-open-api package is installed in the current interpreter.
"""
import json
import sys
import types

import pytest

from api.ctrader_token_manager import CTraderTokenManager


def _make_manager(tmp_path, **overrides):
    """Build a fully-isolated CTraderTokenManager instance: its own state
    file under tmp_path, pre-populated in-memory fields, no dependency on
    the real config/.env.local or the process-wide singleton."""
    mgr = CTraderTokenManager()
    state_path = tmp_path / "ctrader_token_state.json"
    mgr._state_path = lambda: state_path  # instance override
    mgr._initialized = True
    mgr._client_id = overrides.get("client_id", "test_client_id")
    mgr._client_secret = overrides.get("client_secret", "test_client_secret")
    mgr._redirect_uri = overrides.get("redirect_uri", "http://localhost")
    mgr._access_token = overrides.get("access_token", "OLD_ACCESS")
    mgr._refresh_token = overrides.get("refresh_token", "OLD_REFRESH")
    mgr._last_refresh_utc = overrides.get("last_refresh_utc", "")
    mgr._refresh_count = overrides.get("refresh_count", 1)
    mgr._consecutive_failures = overrides.get("consecutive_failures", 0)
    mgr._state_saved_utc = overrides.get("state_saved_utc", "")
    return mgr, state_path


def _inject_fake_ctrader_open_api(monkeypatch, auth_cls):
    fake_mod = types.ModuleType("ctrader_open_api")
    fake_mod.Auth = auth_cls
    monkeypatch.setitem(sys.modules, "ctrader_open_api", fake_mod)


def _write_state(path, **fields):
    base = {
        "access_token": "",
        "refresh_token": "",
        "last_refresh_utc": "",
        "refresh_count": 0,
        "consecutive_failures": 0,
        "saved_utc": "",
    }
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")
    return base


class _ExplodingAuth:
    """Auth stand-in that fails the test if it is ever constructed — proves
    no network call was attempted."""

    def __init__(self, *a, **kw):
        raise AssertionError("Auth() must not be constructed in this scenario")


# ── (a) read-only consumer never writes / never refreshes ──────────────────

def test_read_only_consumer_never_writes_or_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("DEXTER3_TOKEN_SINGLE_OWNER", "1")
    monkeypatch.delenv("CTRADER_TOKEN_IS_OWNER", raising=False)
    _inject_fake_ctrader_open_api(monkeypatch, _ExplodingAuth)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="CURRENT_A",
        refresh_token="CURRENT_R",
        refresh_count=3,
        state_saved_utc="2026-07-10 10:00:00",
    )
    original_state = _write_state(
        state_path,
        access_token="CURRENT_A",
        refresh_token="CURRENT_R",
        refresh_count=3,
        saved_utc="2026-07-10 10:00:00",
    )

    # try_refresh(): not the owner, disk not newer -> no Auth call, no write.
    result = mgr.try_refresh()
    assert result == ""

    before_mtime = state_path.stat().st_mtime_ns
    before_content = json.loads(state_path.read_text(encoding="utf-8"))
    assert before_content == original_state

    # on_token_failed(): read-only consumer must never write shared state.
    mgr.on_token_failed("Access denied. Make sure the credentials are valid.")

    after_mtime = state_path.stat().st_mtime_ns
    after_content = json.loads(state_path.read_text(encoding="utf-8"))
    assert after_content == original_state, "read-only consumer clobbered/altered shared state"
    assert after_mtime == before_mtime, "read-only consumer wrote to disk at all"


def test_read_only_consumer_adopts_newer_token_without_writing(tmp_path, monkeypatch):
    """A read-only consumer MAY pick up a fresher token the owner already
    installed — but must still never write to disk itself."""
    monkeypatch.setenv("DEXTER3_TOKEN_SINGLE_OWNER", "1")
    monkeypatch.delenv("CTRADER_TOKEN_IS_OWNER", raising=False)
    _inject_fake_ctrader_open_api(monkeypatch, _ExplodingAuth)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=5,
        state_saved_utc="2026-07-10 09:00:00",
    )
    newer_state = _write_state(
        state_path,
        access_token="NEW_A",
        refresh_token="NEW_R",
        refresh_count=1,
        saved_utc="2026-07-10 09:05:00",
    )

    result = mgr.try_refresh()

    assert result == "NEW_A"
    assert mgr._access_token == "NEW_A"
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == newer_state  # unchanged — read-only consumer never writes


# ── (b) on_token_failed preserves a valid (newer) token ─────────────────────

def test_on_token_failed_preserves_newer_token_installed_by_another_process(tmp_path, monkeypatch):
    monkeypatch.delenv("DEXTER3_TOKEN_SINGLE_OWNER", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_IS_OWNER", raising=False)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=5,
        state_saved_utc="2026-07-10 09:00:00",
    )
    # Simulate another process (or a manual re-auth) having installed a
    # fresh token AFTER this process last synced. Note refresh_count is
    # LOWER (1 < 5) — a manual re-auth resets it — which is exactly why the
    # guard must use wall-clock saved_utc, not the counter, as primary key.
    _write_state(
        state_path,
        access_token="NEW_A",
        refresh_token="NEW_R",
        refresh_count=1,
        saved_utc="2026-07-10 09:05:00",
    )

    mgr.on_token_failed("Access denied. Make sure the credentials are valid.")

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["access_token"] == "NEW_A"
    assert on_disk["refresh_token"] == "NEW_R"
    assert mgr._access_token == "NEW_A"  # self-healed in memory


def test_save_state_never_overwrites_valid_token_with_empty(tmp_path, monkeypatch):
    """Direct test of the _save_state() belt-and-braces guard: an empty
    in-memory token must never blank out a good on-disk one."""
    mgr, state_path = _make_manager(
        tmp_path,
        access_token="",
        refresh_token="",
        refresh_count=4,
        state_saved_utc="2026-07-10 09:00:00",
    )
    _write_state(
        state_path,
        access_token="REAL_A",
        refresh_token="REAL_R",
        refresh_count=4,
        saved_utc="2026-07-10 09:00:00",
    )

    mgr._save_state()

    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["access_token"] == "REAL_A"
    assert on_disk["refresh_token"] == "REAL_R"


# ── (c) stale-write guard rejects older-over-newer ──────────────────────────

def test_try_refresh_stale_write_guard_adopts_newer_disk_state_without_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("DEXTER3_TOKEN_SINGLE_OWNER", raising=False)
    _inject_fake_ctrader_open_api(monkeypatch, _ExplodingAuth)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=5,
        state_saved_utc="2026-07-10 09:00:00",
    )
    newer_state = _write_state(
        state_path,
        access_token="NEW_A",
        refresh_token="NEW_R",
        refresh_count=1,
        saved_utc="2026-07-10 09:05:00",
    )

    result = mgr.try_refresh()

    assert result == "NEW_A"
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == newer_state, "an older in-memory token must never overwrite a newer on-disk one"


def test_disk_state_is_newer_ignores_lower_refresh_count_after_manual_reauth(tmp_path):
    mgr, state_path = _make_manager(
        tmp_path,
        refresh_count=20,
        state_saved_utc="2026-07-01 00:00:00",
    )
    fresher_but_reset = {
        "access_token": "FRESH_A",
        "refresh_token": "FRESH_R",
        "refresh_count": 0,
        "saved_utc": "2026-07-10 00:00:00",
    }
    assert mgr._disk_state_is_newer(fresher_but_reset) is True

    older_state = {
        "access_token": "STALE_A",
        "refresh_count": 99,
        "saved_utc": "2026-06-01 00:00:00",
    }
    assert mgr._disk_state_is_newer(older_state) is False


# ── (d) single-owner refresh persists the new refresh_token ─────────────────

def test_single_owner_refresh_persists_new_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DEXTER3_TOKEN_SINGLE_OWNER", "1")
    monkeypatch.setenv("CTRADER_TOKEN_IS_OWNER", "1")

    calls = []

    class FakeAuth:
        def __init__(self, client_id, client_secret, redirect_uri):
            calls.append(("init", client_id, client_secret, redirect_uri))

        def refreshToken(self, refresh_token):
            calls.append(("refreshToken", refresh_token))
            return {"accessToken": "NEW_A", "refreshToken": "NEW_R"}

    _inject_fake_ctrader_open_api(monkeypatch, FakeAuth)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=2,
        state_saved_utc="2026-07-10 08:00:00",
    )
    _write_state(
        state_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=2,
        saved_utc="2026-07-10 08:00:00",
    )

    result = mgr.try_refresh()

    assert result == "NEW_A"
    assert ("refreshToken", "OLD_R") in calls, "the owner must actually call Spotware's refresh endpoint"
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["access_token"] == "NEW_A"
    assert on_disk["refresh_token"] == "NEW_R", "the new single-use refresh_token must be persisted immediately"
    assert on_disk["refresh_count"] == 3


def test_default_mode_unchanged_every_consumer_still_self_refreshes(tmp_path, monkeypatch):
    """Regression guard: with both flags off (today's default), a consumer
    still performs its own refresh on failure exactly as before this fix —
    'safe for the live main system' / 'default = current behavior'."""
    monkeypatch.delenv("DEXTER3_TOKEN_SINGLE_OWNER", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_IS_OWNER", raising=False)

    calls = []

    class FakeAuth:
        def __init__(self, *a, **kw):
            pass

        def refreshToken(self, refresh_token):
            calls.append(refresh_token)
            return {"accessToken": "NEW_A", "refreshToken": "NEW_R"}

    _inject_fake_ctrader_open_api(monkeypatch, FakeAuth)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="OLD_A",
        refresh_token="OLD_R",
        refresh_count=0,
        state_saved_utc="",
    )

    result = mgr.try_refresh()

    assert result == "NEW_A"
    assert calls == ["OLD_R"]
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["access_token"] == "NEW_A"
    assert on_disk["refresh_token"] == "NEW_R"


def test_default_mode_failure_still_persists_failure_count(tmp_path, monkeypatch):
    """With single-owner mode off, on_token_failed keeps incrementing and
    saving consecutive_failures as before (no disk state exists yet -> not
    'newer', so the legacy write path runs unchanged)."""
    monkeypatch.delenv("DEXTER3_TOKEN_SINGLE_OWNER", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_IS_OWNER", raising=False)

    mgr, state_path = _make_manager(
        tmp_path,
        access_token="A",
        refresh_token="R",
        refresh_count=1,
        consecutive_failures=0,
        state_saved_utc="",
    )

    mgr.on_token_failed("boom")

    assert mgr._consecutive_failures == 1
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["consecutive_failures"] == 1
    assert on_disk["access_token"] == "A"
    assert on_disk["refresh_token"] == "R"

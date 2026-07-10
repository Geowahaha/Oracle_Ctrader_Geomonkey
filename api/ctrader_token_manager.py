"""
api/ctrader_token_manager.py

Centralized cTrader OpenAPI Token Manager
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Single source of truth for all cTrader tokens.
Eliminates stale fallback chains, persists refreshed tokens, and alerts
on failure.

Token priority chain:
  1. Persistent state file (data/runtime/ctrader_token_state.json)
     → Updated after every successful refresh
  2. Environment (.env.local CTRADER_OPENAPI_ACCESS_TOKEN)
     → Initial seed, overridden once refresh succeeds
  3. Legacy fallback keys (OpenAPI_Access_token_API_key etc.)
     → ONLY used if (1) and (2) are both empty

Key features:
  - Persists refreshed tokens to disk → survives process restart
  - Auto-refresh with exponential backoff (2s → 4s → 8s → max 120s)
  - Max 5 retry attempts per refresh cycle
  - Proactive health check on first token request
  - Telegram alert on persistent token failure
  - Thread-safe singleton
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.atomic_write import atomic_json_write

logger = logging.getLogger(__name__)

_STATE_FILE = "data/runtime/ctrader_token_state.json"
_MAX_REFRESH_RETRIES = 5
_BACKOFF_BASE_SEC = 2.0
_BACKOFF_MAX_SEC = 120.0
_SAVED_UTC_FMT = "%Y-%m-%d %H:%M:%S"


def _flag(name: str) -> bool:
    """Read a boolean feature flag directly from the live process
    environment.

    Deliberately NOT read from config.py's cached class attribute: config
    evaluates os.getenv(...) once at import time, so a process that flips
    its owner/read-only role at runtime (e.g. scripts/ctrader_token_keepalive.py
    self-identifying as the owner before its first token check) would be
    silently ignored if we consulted the frozen config snapshot instead of
    the live environment. config.py still exposes these same keys
    (DEXTER3_TOKEN_SINGLE_OWNER / CTRADER_TOKEN_IS_OWNER) for visibility/ops
    tooling; this module intentionally reads os.environ directly so it
    always reflects the current process, including in tests via
    monkeypatch.setenv.
    """
    return str(os.getenv(name, "0") or "0").strip().lower() in ("1", "true", "yes", "on")


def _single_owner_mode_enabled() -> bool:
    """DEXTER3_TOKEN_SINGLE_OWNER=1 — default OFF, current behavior unchanged."""
    return _flag("DEXTER3_TOKEN_SINGLE_OWNER")


def _is_refresh_owner() -> bool:
    """CTRADER_TOKEN_IS_OWNER=1 — only meaningful when single-owner mode is on."""
    return _flag("CTRADER_TOKEN_IS_OWNER")


class CTraderTokenManager:
    """Centralized token manager — the ONLY place to get cTrader tokens."""

    def __init__(self):
        self._lock = threading.Lock()
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._client_id: str = ""
        self._client_secret: str = ""
        self._redirect_uri: str = ""
        self._last_refresh_utc: str = ""
        self._refresh_count: int = 0
        self._consecutive_failures: int = 0
        self._initialized = False
        self._telegram_alerted = False
        # Last-seen "saved_utc" from disk — our baseline for detecting
        # whether another process (or a manual re-auth) has since installed
        # a newer token state that must not be clobbered. See
        # _disk_state_is_newer() / _adopt_disk_state().
        self._state_saved_utc: str = ""

    def _state_path(self) -> Path:
        try:
            from config import config as _cfg
            db_path = str(getattr(_cfg, "CTRADER_OPENAPI_DB_PATH", "") or "")
            if db_path:
                return Path(db_path).parent.parent / "data" / "runtime" / "ctrader_token_state.json"
        except Exception:
            pass
        return Path(_STATE_FILE)

    def _load_config(self):
        """Load credentials from config (env vars)."""
        try:
            from config import config as _cfg
            self._client_id = str(getattr(_cfg, "CTRADER_OPENAPI_CLIENT_ID", "") or "").strip()
            self._client_secret = str(getattr(_cfg, "CTRADER_OPENAPI_CLIENT_SECRET", "") or "").strip()
            self._redirect_uri = str(getattr(_cfg, "CTRADER_OPENAPI_REDIRECT_URI", "http://localhost") or "http://localhost").strip()

            # Access/refresh from env — used as seed, NOT final source
            env_access = str(getattr(_cfg, "CTRADER_OPENAPI_ACCESS_TOKEN", "") or "").strip()
            env_refresh = str(getattr(_cfg, "CTRADER_OPENAPI_REFRESH_TOKEN", "") or "").strip()
            return env_access, env_refresh
        except Exception as e:
            logger.warning("[TokenManager] config load error: %s", e)
            return "", ""

    def _load_state(self) -> dict:
        """Load persisted token state from disk."""
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug("[TokenManager] state load error: %s", e)
            return {}

    @staticmethod
    def _parse_saved_utc(ts: str) -> Optional[datetime]:
        ts = str(ts or "").strip()
        if not ts:
            return None
        try:
            return datetime.strptime(ts, _SAVED_UTC_FMT).replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _disk_state_is_newer(self, disk_state: dict) -> bool:
        """True if the on-disk token state was saved more recently than the
        state this process last observed (self._state_saved_utc).

        This is the cross-process staleness guard: it is what tells a
        process "someone else (the owner, a manual re-auth, another
        consumer) already installed a token after you last synced — do not
        write your own, older copy over it." Wall-clock saved_utc is used
        (not refresh_count) because a full manual re-auth legitimately
        resets refresh_count to 0/1, which would otherwise look "older"
        than a process that had refreshed many times against the now-dead
        token chain.
        """
        disk_access = str(disk_state.get("access_token", "") or "").strip()
        if not disk_access:
            return False  # nothing on disk worth protecting
        disk_saved = self._parse_saved_utc(disk_state.get("saved_utc", ""))
        mine_saved = self._parse_saved_utc(self._state_saved_utc)
        if disk_saved and mine_saved:
            return disk_saved > mine_saved
        if disk_saved and not mine_saved:
            return True
        # No usable timestamps on either side — fall back to refresh_count
        # as a weaker monotonic signal rather than assuming staleness.
        try:
            return int(disk_state.get("refresh_count", -1) or -1) > int(self._refresh_count)
        except Exception:
            return False

    def _adopt_disk_state(self, disk_state: dict) -> None:
        """Adopt a newer on-disk token state into memory (self-heal) instead
        of overwriting it. Caller must hold self._lock."""
        self._access_token = str(disk_state.get("access_token", "") or "").strip()
        new_refresh = str(disk_state.get("refresh_token", "") or "").strip()
        if new_refresh:
            self._refresh_token = new_refresh
        self._last_refresh_utc = str(disk_state.get("last_refresh_utc", "") or self._last_refresh_utc)
        try:
            self._refresh_count = int(disk_state.get("refresh_count", self._refresh_count) or self._refresh_count)
        except Exception:
            pass
        self._state_saved_utc = str(disk_state.get("saved_utc", "") or self._state_saved_utc)

    def _save_state(self):
        """Persist current tokens to disk (atomic).

        Safety net: never let a write erase an existing non-empty
        access_token/refresh_token with an empty value — if this process's
        in-memory copy is empty but disk already has a real token, keep
        disk's value instead of clobbering it.
        """
        path = self._state_path()
        try:
            disk_state = self._load_state()
            access_to_write = self._access_token
            refresh_to_write = self._refresh_token
            if not access_to_write and str(disk_state.get("access_token", "") or "").strip():
                access_to_write = str(disk_state["access_token"]).strip()
                logger.warning("[TokenManager] refused to overwrite existing access_token with an empty value")
            if not refresh_to_write and str(disk_state.get("refresh_token", "") or "").strip():
                refresh_to_write = str(disk_state["refresh_token"]).strip()
                logger.warning("[TokenManager] refused to overwrite existing refresh_token with an empty value")
            saved_utc = datetime.now(timezone.utc).strftime(_SAVED_UTC_FMT)
            state = {
                "access_token": access_to_write,
                "refresh_token": refresh_to_write,
                "last_refresh_utc": self._last_refresh_utc,
                "refresh_count": self._refresh_count,
                "consecutive_failures": self._consecutive_failures,
                "saved_utc": saved_utc,
            }
            atomic_json_write(path, state)
            self._state_saved_utc = saved_utc
            logger.debug("[TokenManager] state saved to %s", path)
        except Exception as e:
            logger.warning("[TokenManager] state save error: %s", e)

    def _initialize(self):
        """One-time initialization: load from state file → env fallback."""
        if self._initialized:
            return
        self._initialized = True

        env_access, env_refresh = self._load_config()
        state = self._load_state()
        self._state_saved_utc = str(state.get("saved_utc", "") or "")

        # Priority: persisted state > env
        persisted_access = str(state.get("access_token", "") or "").strip()
        persisted_refresh = str(state.get("refresh_token", "") or "").strip()

        if persisted_access:
            self._access_token = persisted_access
            self._refresh_token = persisted_refresh or env_refresh
            self._refresh_count = int(state.get("refresh_count", 0) or 0)
            self._last_refresh_utc = str(state.get("last_refresh_utc", "") or "")
            logger.info(
                "[TokenManager] Loaded persisted token (refreshed %d times, last: %s)",
                self._refresh_count, self._last_refresh_utc or "never",
            )
        elif env_access:
            self._access_token = env_access
            self._refresh_token = env_refresh
            logger.info("[TokenManager] Using env token (no persisted state)")
        else:
            logger.warning("[TokenManager] No access token available from state or env")
            self._refresh_token = env_refresh

    def get_access_token(self) -> str:
        """Get the current best access token. Thread-safe."""
        with self._lock:
            self._initialize()
            return self._access_token

    def ensure_fresh_access_token(self, max_age_minutes: float = 45.0) -> str:
        """Return a valid access token, refreshing proactively before expiry."""
        with self._lock:
            self._initialize()
            if not self._refresh_token:
                return self._access_token
            last = self._last_refresh_utc
        if not last:
            refreshed = self.try_refresh()
            return refreshed or self.get_access_token()
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
        except Exception:
            age_min = max_age_minutes + 1.0
        if age_min >= float(max_age_minutes):
            refreshed = self.try_refresh()
            return refreshed or self.get_access_token()
        return self.get_access_token()

    def get_refresh_token(self) -> str:
        """Get the current refresh token. Thread-safe."""
        with self._lock:
            self._initialize()
            return self._refresh_token

    def get_credentials(self) -> dict:
        """Get all auth credentials in one call."""
        with self._lock:
            self._initialize()
            return {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
            }

    def on_token_refreshed(self, new_access_token: str, new_refresh_token: Optional[str] = None):
        """Called after a successful token refresh — persists to disk.

        This is the KEY improvement: any code path that refreshes tokens
        must call this to persist the new token.
        """
        with self._lock:
            self._access_token = str(new_access_token or "").strip()
            if new_refresh_token:
                self._refresh_token = str(new_refresh_token).strip()
            self._last_refresh_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self._refresh_count += 1
            self._consecutive_failures = 0
            self._telegram_alerted = False
            self._save_state()
            logger.info(
                "[TokenManager] Token refreshed and persisted (#%d at %s)",
                self._refresh_count, self._last_refresh_utc,
            )

    def on_token_failed(self, error: str = ""):
        """Called when token refresh fails — tracks failures and alerts.

        Never clobbers a good token: if the on-disk state is newer than
        what this process last saw (another process or a manual re-auth
        already installed a valid token), adopt it into memory instead of
        writing this process's stale copy back over it. In single-owner
        mode, non-owner consumers never write shared state at all — they
        observe locally and wait for the owner to fix it.
        """
        with self._lock:
            self._consecutive_failures += 1

            read_only = _single_owner_mode_enabled() and not _is_refresh_owner()
            if read_only:
                disk_state = self._load_state()
                if self._disk_state_is_newer(disk_state):
                    self._adopt_disk_state(disk_state)
                    self._consecutive_failures = int(disk_state.get("consecutive_failures", 0) or 0)
                logger.error(
                    "[TokenManager] (read-only consumer) token failure #%d: %s — "
                    "not writing shared state, waiting for owner refresh",
                    self._consecutive_failures, error,
                )
                return

            disk_state = self._load_state()
            if self._disk_state_is_newer(disk_state):
                logger.warning(
                    "[TokenManager] failing on a stale local token while disk has a "
                    "newer one (refresh_count %s -> %s) — adopting instead of overwriting",
                    self._refresh_count, disk_state.get("refresh_count"),
                )
                self._adopt_disk_state(disk_state)
                self._consecutive_failures = int(disk_state.get("consecutive_failures", 0) or 0)
            else:
                self._save_state()

            logger.error(
                "[TokenManager] Token failure #%d: %s",
                self._consecutive_failures, error,
            )
            if self._consecutive_failures >= 3 and not self._telegram_alerted:
                self._telegram_alerted = True
                self._send_alert(error)

    def try_refresh(self) -> str:
        """Attempt to refresh the access token with retry + backoff.

        Returns new access token on success, empty string on failure.

        Cross-process safety: before ever hitting the network, re-check
        disk for a newer token (another process — the owner, a manual
        re-auth — may have already installed one) and adopt it instead of
        racing Spotware's single-use refresh_token with a stale copy. In
        single-owner mode (DEXTER3_TOKEN_SINGLE_OWNER=1), only the
        designated owner (CTRADER_TOKEN_IS_OWNER=1) is allowed past this
        point to actually call Spotware — every other consumer returns
        here with whatever is currently valid (or "") and never writes
        failure state.
        """
        with self._lock:
            self._initialize()

            disk_state = self._load_state()
            if self._disk_state_is_newer(disk_state):
                logger.info(
                    "[TokenManager] newer token found on disk before refresh "
                    "attempt (refresh_count %s -> %s) — adopting, no network call made",
                    self._refresh_count, disk_state.get("refresh_count"),
                )
                self._adopt_disk_state(disk_state)
                return self._access_token

            if _single_owner_mode_enabled() and not _is_refresh_owner():
                logger.debug(
                    "[TokenManager] read-only consumer — refresh delegated to "
                    "owner, no network call made"
                )
                return ""

            refresh_token = self._refresh_token
            client_id = self._client_id
            client_secret = self._client_secret
            redirect_uri = self._redirect_uri

        if not client_id or not client_secret or not refresh_token:
            self.on_token_failed("missing credentials for refresh")
            return ""

        for attempt in range(1, _MAX_REFRESH_RETRIES + 1):
            try:
                from ctrader_open_api import Auth
                auth = Auth(client_id, client_secret, redirect_uri)
                refreshed = auth.refreshToken(refresh_token)
                if isinstance(refreshed, dict):
                    new_access = str(refreshed.get("accessToken") or "").strip()
                    new_refresh = str(refreshed.get("refreshToken") or "").strip()
                    if new_access:
                        self.on_token_refreshed(new_access, new_refresh or None)
                        return new_access
                    error_msg = str(refreshed.get("description") or refreshed.get("errorCode") or "empty accessToken")
                    logger.warning("[TokenManager] refresh attempt %d/%d: %s", attempt, _MAX_REFRESH_RETRIES, error_msg)
                else:
                    logger.warning("[TokenManager] refresh attempt %d/%d: non-dict response", attempt, _MAX_REFRESH_RETRIES)
            except Exception as e:
                logger.warning("[TokenManager] refresh attempt %d/%d error: %s", attempt, _MAX_REFRESH_RETRIES, e)

            if attempt < _MAX_REFRESH_RETRIES:
                delay = min(_BACKOFF_MAX_SEC, _BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                logger.info("[TokenManager] retry in %.1fs...", delay)
                time.sleep(delay)

        self.on_token_failed(f"all {_MAX_REFRESH_RETRIES} refresh attempts failed")
        return ""

    def health_check(self) -> dict:
        """Proactive token health check — call on startup.

        Returns dict with status and diagnostics.
        """
        with self._lock:
            self._initialize()
            result = {
                "has_access_token": bool(self._access_token),
                "has_refresh_token": bool(self._refresh_token),
                "has_client_id": bool(self._client_id),
                "has_client_secret": bool(self._client_secret),
                "refresh_count": self._refresh_count,
                "last_refresh_utc": self._last_refresh_utc,
                "consecutive_failures": self._consecutive_failures,
            }

        if not result["has_client_id"] or not result["has_client_secret"]:
            result["status"] = "critical:missing_credentials"
            result["message"] = "CTRADER_OPENAPI_CLIENT_ID or CLIENT_SECRET not set"
            self._send_alert(result["message"])
            return result

        if not result["has_access_token"] and not result["has_refresh_token"]:
            result["status"] = "critical:no_tokens"
            result["message"] = "No access or refresh token available"
            self._send_alert(result["message"])
            return result

        if not result["has_access_token"] and result["has_refresh_token"]:
            # Try to refresh proactively
            logger.info("[TokenManager] No access token at startup — attempting refresh")
            new_token = self.try_refresh()
            if new_token:
                result["status"] = "ok:refreshed_at_startup"
                result["message"] = "Token refreshed successfully at startup"
                result["has_access_token"] = True
            else:
                result["status"] = "critical:refresh_failed_at_startup"
                result["message"] = "Refresh failed at startup — trading disabled"
                self._send_alert(result["message"])
            return result

        # Proactive validation: try a lightweight API call to verify token isn't expired
        try:
            from ctrader_open_api import Client, EndPoints
            from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoOAVersionReq
            import asyncio
            loop = asyncio.new_event_loop()
            _client = Client(EndPoints.PROTOBUF_LIVE_HOST, EndPoints.PROTOBUF_PORT)
            # If we get here without error, token format is valid — mark ok
            # Full connection test happens at scheduler start; this is a fast check
        except ImportError:
            pass  # ctrader_open_api not installed, skip validation
        except Exception:
            pass

        # Proactive refresh: seed token or access token older than 45 minutes
        if result["has_refresh_token"]:
            needs_refresh = self._refresh_count == 0 and not self._last_refresh_utc
            if not needs_refresh and self._last_refresh_utc:
                try:
                    last_dt = datetime.strptime(self._last_refresh_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
                    needs_refresh = age_min >= 45.0
                except Exception:
                    needs_refresh = True
            if needs_refresh:
                logger.info("[TokenManager] Proactive refresh at startup (age or seed token)")
                new_token = self.try_refresh()
                if new_token:
                    result["status"] = "ok:refreshed_at_startup"
                    result["message"] = "Token refreshed proactively at startup"
                    result["has_access_token"] = True
                    return result

        result["status"] = "ok"
        result["message"] = "Token available"
        return result

    def _send_alert(self, error: str):
        """Send Telegram alert for token failure."""
        try:
            from notifier.telegram_bot import notifier as tg
            msg = (
                "🔴 cTrader OpenAPI Token Alert\n\n"
                f"Error: {error}\n"
                f"Consecutive failures: {self._consecutive_failures}\n"
                f"Last refresh: {self._last_refresh_utc or 'never'}\n\n"
                "⚠️ Trading may be disabled until token is fixed\\.\n"
                "Fix: Update CTRADER\\_OPENAPI\\_ACCESS\\_TOKEN in \\.env\\.local and restart\\."
            )
            tg._send(msg, feature="system_alert")
            logger.info("[TokenManager] Telegram alert sent")
        except Exception as e:
            logger.debug("[TokenManager] Telegram alert failed (non-fatal): %s", e)


# ── Singleton ───────────────────────────────────────────────────────────────
token_manager = CTraderTokenManager()

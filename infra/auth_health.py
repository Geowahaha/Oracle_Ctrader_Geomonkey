"""
infra/auth_health.py — Token/auth health monitoring for cTrader OpenAPI.

Provides startup and periodic health checks for the token lifecycle.
Designed to be called from scheduler.py at startup and periodically.

No trading logic modified. Additive observability only.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_FILE = Path("data/runtime/ctrader_token_state.json")

# cTrader OpenAPI tokens typically expire after ~24 hours.
# Alert when fewer than this many minutes remain.
DEFAULT_EXPIRY_WARNING_MINUTES = 30

# Alert after this many consecutive failures
DEFAULT_FAILURE_ALERT_THRESHOLD = 2


def _load_token_state() -> dict:
    """Load token state from disk. Returns empty dict if not found."""
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_utc(ts_str: str) -> Optional[datetime]:
    """Parse a UTC timestamp string to datetime."""
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def check_token_health(
    warning_minutes: int = DEFAULT_EXPIRY_WARNING_MINUTES,
    failure_threshold: int = DEFAULT_FAILURE_ALERT_THRESHOLD,
) -> dict:
    """Run a comprehensive token health check.

    Returns a dict with:
        - status: "healthy" | "warning" | "critical" | "unknown"
        - details: human-readable description
        - fields: raw data for further processing

    This function does NOT attempt to refresh tokens.
    It only observes and reports.
    """
    state = _load_token_state()

    if not state:
        logger.warning("[auth_health] Token state file not found at %s", _STATE_FILE)
        return {
            "status": "unknown",
            "details": "Token state file not found — no persisted tokens",
            "fields": {"state_file_exists": False},
        }

    result = {
        "status": "healthy",
        "details": "",
        "fields": {
            "state_file_exists": True,
            "last_refresh_utc": state.get("last_refresh_utc", ""),
            "refresh_count": state.get("refresh_count", 0),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "saved_utc": state.get("saved_utc", ""),
            "has_access_token": bool(state.get("access_token")),
            "has_refresh_token": bool(state.get("refresh_token")),
        },
    }

    # Check 1: Tokens present
    if not state.get("access_token"):
        result["status"] = "critical"
        result["details"] = "No access_token in state file"
        logger.error("[auth_health] CRITICAL: No access_token in state file")
        return result

    if not state.get("refresh_token"):
        result["status"] = "warning"
        result["details"] = "No refresh_token — cannot recover if access_token expires"
        logger.warning("[auth_health] WARNING: No refresh_token in state file")

    # Check 2: Consecutive failures
    failures = state.get("consecutive_failures", 0)
    if failures >= failure_threshold:
        result["status"] = "critical"
        result["details"] = f"{failures} consecutive token refresh failures"
        logger.error("[auth_health] CRITICAL: %d consecutive refresh failures", failures)
        return result
    elif failures > 0:
        result["status"] = "warning"
        result["details"] = f"{failures} recent refresh failures (threshold: {failure_threshold})"
        logger.warning("[auth_health] WARNING: %d consecutive refresh failures", failures)

    # Check 3: Staleness — last refresh too long ago
    last_refresh = _parse_utc(state.get("last_refresh_utc", ""))
    if last_refresh:
        age = datetime.now(timezone.utc) - last_refresh
        age_hours = age.total_seconds() / 3600

        if age_hours > 48:
            result["status"] = "critical" if result["status"] == "healthy" else result["status"]
            result["details"] += f" Token last refreshed {age_hours:.0f}h ago (>48h). "
            logger.error("[auth_health] Token stale: %.0f hours since last refresh", age_hours)
        elif age_hours > 24:
            if result["status"] == "healthy":
                result["status"] = "warning"
            result["details"] += f" Token last refreshed {age_hours:.0f}h ago (>24h). "
            logger.warning("[auth_health] Token aging: %.0f hours since last refresh", age_hours)

        result["fields"]["hours_since_refresh"] = round(age_hours, 1)
    else:
        result["fields"]["hours_since_refresh"] = None

    # Check 4: State file save freshness
    saved = _parse_utc(state.get("saved_utc", ""))
    if saved:
        save_age = datetime.now(timezone.utc) - saved
        result["fields"]["hours_since_save"] = round(save_age.total_seconds() / 3600, 1)
    else:
        result["fields"]["hours_since_save"] = None

    if not result["details"]:
        result["details"] = "Token state looks healthy"

    logger.info(
        "[auth_health] Status: %s — %s (refresh_count=%d, failures=%d)",
        result["status"], result["details"].strip(),
        result["fields"]["refresh_count"], result["fields"]["consecutive_failures"],
    )

    return result


def log_token_health_summary() -> None:
    """Log a structured summary of token health. Call at scheduler startup."""
    health = check_token_health()

    log_fn = {
        "healthy": logger.info,
        "warning": logger.warning,
        "critical": logger.error,
        "unknown": logger.warning,
    }.get(health["status"], logger.info)

    log_fn(
        "[auth_health] STARTUP CHECK — status=%s | %s | "
        "last_refresh=%s | refresh_count=%d | consecutive_failures=%d",
        health["status"],
        health["details"],
        health["fields"].get("last_refresh_utc", "unknown"),
        health["fields"].get("refresh_count", 0),
        health["fields"].get("consecutive_failures", 0),
    )


def get_token_health_for_report() -> dict:
    """Get token health data for inclusion in scheduled reports.

    Returns a dict suitable for JSON serialization.
    """
    health = check_token_health()
    return {
        "auth_health_status": health["status"],
        "auth_health_details": health["details"],
        **health["fields"],
    }

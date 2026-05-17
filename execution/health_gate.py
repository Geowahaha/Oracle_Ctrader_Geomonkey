"""
execution/health_gate.py — Graded degrade gate for new-entry execution.

Non-blocking, read-only consumer of the persisted health state that Hermes'
infra/db_health.py + infra/auth_health.py produce. Returns a graded
{allow, warn, block} decision the executor uses to short-circuit new
entries when persistent state signals a critical infra problem.

Design invariants:
- Never queries the live DB — only reads data/runtime state files.
- Fails OPEN on missing state (startup race, fresh checkouts, etc.).
- Stale state (> max_age_min) degrades to 'unknown' — treated as allow.
- Only gates new entries. Position management / close paths never call this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthGateDecision:
    status: str  # "allow" | "warn" | "block"
    reason: str
    db_status: str
    auth_status: str
    db_age_min: Optional[float]
    auth_age_min: Optional[float]


def _parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _age_minutes(ts_iso: str, now: datetime) -> Optional[float]:
    dt = _parse_utc(ts_iso)
    if dt is None:
        return None
    try:
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return None


def _load_db_component(now: datetime, max_age_min: float) -> tuple[str, Optional[float]]:
    try:
        from infra.db_health import get_health_state
    except Exception:
        return ("unknown", None)
    try:
        state = get_health_state() or {}
    except Exception:
        return ("unknown", None)
    if not isinstance(state, dict) or not state:
        return ("unknown", None)
    age = _age_minutes(state.get("timestamp_utc", ""), now)
    if age is not None and age > max_age_min:
        return ("stale", age)
    raw_status = str(state.get("overall_status") or "").strip().lower()
    if raw_status in ("healthy", "warning", "critical"):
        return (raw_status, age)
    return ("unknown", age)


def _load_auth_component(now: datetime, max_age_min: float) -> tuple[str, Optional[float]]:
    try:
        from infra.auth_health import check_token_health, _STATE_FILE, _load_token_state
    except Exception:
        return ("unknown", None)
    try:
        raw_state = _load_token_state() or {}
    except Exception:
        raw_state = {}
    age = _age_minutes(str(raw_state.get("saved_utc") or ""), now)
    if age is not None and age > max_age_min:
        return ("stale", age)
    try:
        health = check_token_health() or {}
    except Exception:
        return ("unknown", age)
    raw_status = str(health.get("status") or "").strip().lower()
    if raw_status in ("healthy", "warning", "critical", "unknown"):
        return (raw_status, age)
    return ("unknown", age)


def health_gate_decision(
    *,
    enabled: bool,
    block_on_critical: bool,
    max_state_age_min: float = 120.0,
    now: Optional[datetime] = None,
) -> HealthGateDecision:
    """Compute the current health-gate decision for a new entry.

    Args:
        enabled: Master switch. If False, always returns 'allow' with no
                 I/O (zero overhead in the disabled path).
        block_on_critical: When True, critical components produce 'block'.
                           When False, critical only produces 'warn'.
        max_state_age_min: Persisted health states older than this are
                           treated as 'stale' and do NOT influence the
                           decision (fail-open).
        now: Injectable for testing.
    """
    if not enabled:
        return HealthGateDecision(
            status="allow",
            reason="health_gate_disabled",
            db_status="unknown",
            auth_status="unknown",
            db_age_min=None,
            auth_age_min=None,
        )

    now = now or datetime.now(timezone.utc)
    db_status, db_age = _load_db_component(now, max_state_age_min)
    auth_status, auth_age = _load_auth_component(now, max_state_age_min)

    critical_components = []
    if db_status == "critical":
        critical_components.append("db")
    if auth_status == "critical":
        critical_components.append("auth")

    warning_components = []
    if db_status == "warning":
        warning_components.append("db")
    if auth_status == "warning":
        warning_components.append("auth")

    if critical_components:
        reason = "health_critical:" + ",".join(critical_components)
        if block_on_critical:
            return HealthGateDecision(
                status="block",
                reason=reason,
                db_status=db_status,
                auth_status=auth_status,
                db_age_min=db_age,
                auth_age_min=auth_age,
            )
        return HealthGateDecision(
            status="warn",
            reason=reason,
            db_status=db_status,
            auth_status=auth_status,
            db_age_min=db_age,
            auth_age_min=auth_age,
        )

    if warning_components:
        return HealthGateDecision(
            status="warn",
            reason="health_warning:" + ",".join(warning_components),
            db_status=db_status,
            auth_status=auth_status,
            db_age_min=db_age,
            auth_age_min=auth_age,
        )

    return HealthGateDecision(
        status="allow",
        reason="healthy",
        db_status=db_status,
        auth_status=auth_status,
        db_age_min=db_age,
        auth_age_min=auth_age,
    )

"""Process-singleton accessor for AdversarialAwareness.

The scheduler tick records close events; the scalp scanner / executor
consults ``is_blocked()`` before dispatching. Both need the SAME instance
so cool-downs registered on one side are visible on the other. This
module owns that singleton and wires it from `config` at first call.

Two public entry points:

  ``get_default_awareness()`` — returns the singleton (creates on demand).
  ``reset_default_awareness_for_tests()`` — clears the singleton so the
                                            next call rebuilds from config.

The accessor is import-safe and never raises during normal startup. If
``analysis.adversarial_awareness.awareness`` is unavailable (e.g. an
older deploy), callers get back ``None`` and treat it as "no block".
"""
from __future__ import annotations

import logging
import threading
from typing import Optional


logger = logging.getLogger(__name__)


_lock = threading.Lock()
_singleton: Optional[object] = None  # AdversarialAwareness | None


def get_default_awareness():
    """Return the process-shared AdversarialAwareness instance."""
    global _singleton
    with _lock:
        if _singleton is not None:
            return _singleton
        try:
            from analysis.adversarial_awareness.awareness import (
                AdversarialAwareness, AdversarialAwarenessConfig,
            )
        except Exception as exc:  # pragma: no cover — defensive import
            logger.debug("adversarial_awareness_import_failed: %s", exc)
            return None
        try:
            from config import config as _cfg
            cfg = AdversarialAwarenessConfig(
                enabled=bool(getattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", False)),
                revenge_loss_count=int(getattr(_cfg, "AA_REVENGE_LOSS_COUNT", 3)),
                revenge_window_minutes=float(getattr(_cfg, "AA_REVENGE_WINDOW_MIN", 60.0)),
                revenge_cooldown_minutes=float(getattr(_cfg, "AA_REVENGE_COOLDOWN_MIN", 30.0)),
                hunt_cluster_count=int(getattr(_cfg, "AA_HUNT_CLUSTER_COUNT", 3)),
                hunt_cluster_window_minutes=float(getattr(_cfg, "AA_HUNT_CLUSTER_WINDOW_MIN", 90.0)),
                hunt_cooldown_minutes=float(getattr(_cfg, "AA_HUNT_COOLDOWN_MIN", 20.0)),
                panic_cluster_count=int(getattr(_cfg, "AA_PANIC_CLUSTER_COUNT", 4)),
                panic_cluster_window_minutes=float(getattr(_cfg, "AA_PANIC_CLUSTER_WINDOW_MIN", 60.0)),
                panic_cooldown_minutes=float(getattr(_cfg, "AA_PANIC_COOLDOWN_MIN", 15.0)),
                drawdown_window_count=int(getattr(_cfg, "AA_DRAWDOWN_WINDOW_COUNT", 8)),
                drawdown_threshold_usd=float(getattr(_cfg, "AA_DRAWDOWN_THRESHOLD_USD", -200.0)),
                drawdown_cooldown_minutes=float(getattr(_cfg, "AA_DRAWDOWN_COOLDOWN_MIN", 60.0)),
            )
        except Exception:
            cfg = AdversarialAwarenessConfig(enabled=False)
        _singleton = AdversarialAwareness(config=cfg)
        return _singleton


def reset_default_awareness_for_tests() -> None:
    """Drop the singleton so the next ``get_default_awareness()`` call
    rebuilds it with the (possibly monkey-patched) config."""
    global _singleton
    with _lock:
        _singleton = None


def is_blocked_safe(*, direction: str, source: str) -> tuple[bool, str]:
    """Caller-friendly wrapper: never raises, returns (False, 'no_awareness')
    when the singleton is unavailable or disabled.
    """
    inst = get_default_awareness()
    if inst is None:
        return False, "no_awareness"
    try:
        blocked, why = inst.is_blocked(direction=direction, source=source)
        return bool(blocked), str(why)
    except Exception as exc:
        logger.debug("adversarial_awareness_is_blocked_error: %s", exc)
        return False, f"error:{exc}"


__all__ = [
    "get_default_awareness",
    "reset_default_awareness_for_tests",
    "is_blocked_safe",
]

"""Tests for the singleton + safe wrapper that bridge AdversarialAwareness
into the scheduler tick and the scalp scanner dispatch path.

These tests don't import the full scheduler / scanner (they pull heavy
deps). Instead we verify the public bridge functions used by both:
  - ``get_default_awareness()`` returns a usable instance
  - ``is_blocked_safe(direction, source)`` is a never-raise wrapper that
    delegates to the singleton when present
  - ``reset_default_awareness_for_tests()`` lets a test re-build with
    monkey-patched config
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.adversarial_awareness import (
    AdversarialAwareness,
    AdversarialAwarenessConfig,
    CloseEvent,
    get_default_awareness,
    is_blocked_safe,
    reset_default_awareness_for_tests,
)


def _close(pid, pnl=-30.0, dir_="short", realised_r=-0.5, mae_r=0.6, mfe_r=0.0,
           source="scalp_xauusd", at: datetime | None = None) -> CloseEvent:
    return CloseEvent(
        position_id=pid, direction=dir_, source=source,
        pnl_usd=pnl, realised_r=realised_r, mfe_r=mfe_r, mae_r=mae_r,
        closed_utc=at or datetime.now(timezone.utc),
    )


def test_singleton_returns_same_instance_each_call(monkeypatch):
    reset_default_awareness_for_tests()
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", True)
    a = get_default_awareness()
    b = get_default_awareness()
    assert a is b
    assert isinstance(a, AdversarialAwareness)


def test_singleton_picks_up_config_on_first_build(monkeypatch):
    reset_default_awareness_for_tests()
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", True)
    monkeypatch.setattr(_cfg, "AA_REVENGE_LOSS_COUNT", 99)  # absurdly high
    a = get_default_awareness()
    assert a.config.enabled is True
    assert a.config.revenge_loss_count == 99


def test_reset_drops_singleton_so_config_rebuilds(monkeypatch):
    reset_default_awareness_for_tests()
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", True)
    monkeypatch.setattr(_cfg, "AA_REVENGE_LOSS_COUNT", 5)
    a = get_default_awareness()
    assert a.config.revenge_loss_count == 5
    monkeypatch.setattr(_cfg, "AA_REVENGE_LOSS_COUNT", 9)
    # Without reset → still old value cached.
    b = get_default_awareness()
    assert b.config.revenge_loss_count == 5
    # With reset → new build, new value.
    reset_default_awareness_for_tests()
    c = get_default_awareness()
    assert c.config.revenge_loss_count == 9


def test_is_blocked_safe_returns_false_when_disabled(monkeypatch):
    reset_default_awareness_for_tests()
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", False)
    blocked, why = is_blocked_safe(direction="short", source="scalp_xauusd")
    # The disabled instance returns ([], not blocked).
    assert blocked is False


def test_is_blocked_safe_reports_revenge_block(monkeypatch):
    reset_default_awareness_for_tests()
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "ADVERSARIAL_AWARENESS_ENABLED", True)
    monkeypatch.setattr(_cfg, "AA_REVENGE_LOSS_COUNT", 3)
    monkeypatch.setattr(_cfg, "AA_REVENGE_WINDOW_MIN", 60.0)
    monkeypatch.setattr(_cfg, "AA_REVENGE_COOLDOWN_MIN", 30.0)
    monkeypatch.setattr(_cfg, "AA_HUNT_CLUSTER_COUNT", 99)
    monkeypatch.setattr(_cfg, "AA_PANIC_CLUSTER_COUNT", 99)
    monkeypatch.setattr(_cfg, "AA_DRAWDOWN_WINDOW_COUNT", 99)
    a = get_default_awareness()
    # Feed 3 consecutive short losses
    now = datetime.now(timezone.utc)
    for i in range(3):
        a.record_close(_close(pid=8000 + i, dir_="short",
                              at=now - timedelta(minutes=10 - i * 3)))
    blocked, why = is_blocked_safe(direction="short", source="scalp_xauusd")
    assert blocked is True
    assert "direction:short" in why or "revenge" in why
    # Long is not blocked.
    blocked_long, _ = is_blocked_safe(direction="long", source="scalp_xauusd")
    assert blocked_long is False


def test_is_blocked_safe_swallows_errors(monkeypatch):
    """If the singleton raises for any reason, the wrapper returns False."""
    reset_default_awareness_for_tests()
    from analysis.adversarial_awareness import instance as inst_mod

    class _Boom:
        def is_blocked(self, **_kw):
            raise RuntimeError("boom")

    # Force the singleton path to return a broken instance.
    monkeypatch.setattr(inst_mod, "_singleton", _Boom())
    blocked, why = is_blocked_safe(direction="short", source="x")
    assert blocked is False
    assert "error" in why


def test_is_blocked_safe_no_awareness_fallback(monkeypatch):
    """When the underlying module fails to import, wrapper returns
    (False, 'no_awareness')."""
    reset_default_awareness_for_tests()
    from analysis.adversarial_awareness import instance as inst_mod

    # Patch get_default_awareness to return None, simulating import failure.
    monkeypatch.setattr(inst_mod, "get_default_awareness", lambda: None)
    blocked, why = inst_mod.is_blocked_safe(direction="short", source="x")
    assert blocked is False
    assert why == "no_awareness"

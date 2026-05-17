"""Integration test for scheduler._resolve_knob().

Verifies that the scheduler reads whitelisted mutable knobs through the
Self-Mutation resolver, picks up canary/main overrides when present, and
falls back to the config default otherwise. We don't import the full
scheduler module (it pulls in heavy market deps); instead we exercise the
helper directly because it's the contract the read-sites depend on.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from learning.self_mutation.overrides import OverrideEntry, OverrideStore
from learning.self_mutation.resolver import KnobResolver, reset_default_resolver_for_tests


# ---------------------------------------------------------------------------
# We need scheduler.py to wire its module-level resolver against a temp
# overrides file. Easiest path: monkey-patch `learning.self_mutation.resolver.
# get_default_resolver` to return a resolver pointed at the tmp dir, then
# import scheduler in a clean process state.
# ---------------------------------------------------------------------------


def _make_resolver(overrides_path: Path) -> KnobResolver:
    store = OverrideStore(overrides_path)

    # Minimal config stand-in with the knobs we'll test against.
    class _FakeConfig:
        XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER = 0.35
        XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE = 7
        XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS = 0.70
        XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO = 0.08
        RISK_PER_TRADE = 0.01

    return KnobResolver(overrides=store, config_obj=_FakeConfig())


def test_resolve_knob_returns_config_default_when_no_override(tmp_path: Path):
    resolver = _make_resolver(tmp_path / "o.json")
    # Use the resolver directly to assert resolution math; this is the same
    # call that scheduler._resolve_knob makes internally.
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER") == 0.35
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE") == 7
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS") == 0.70
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO") == 0.08


def test_resolve_knob_picks_up_main_override(tmp_path: Path):
    resolver = _make_resolver(tmp_path / "o.json")
    # Inject a "main" override; resolver must surface it.
    resolver._overrides.set_main(OverrideEntry(
        knob="XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER",
        value=0.50,
        applied_utc="2026-05-18T01:00:00Z",
        mutation_id="m_test_main",
    ))
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER") == 0.50


def test_resolve_knob_canary_beats_main(tmp_path: Path):
    resolver = _make_resolver(tmp_path / "o.json")
    resolver._overrides.set_main(OverrideEntry(
        knob="XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE",
        value=8.0,
        applied_utc="2026-05-18T01:00:00Z",
        mutation_id="m_main",
    ))
    resolver._overrides.set_canary(OverrideEntry(
        knob="XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE",
        value=9.0,
        applied_utc="2026-05-18T01:05:00Z",
        mutation_id="m_canary",
        expires_utc="2099-01-01T00:00:00Z",
    ))
    assert resolver.get("XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE") == 9.0


def test_resolve_knob_rejects_non_whitelisted_knob(tmp_path: Path):
    resolver = _make_resolver(tmp_path / "o.json")
    with pytest.raises(KeyError):
        resolver.get("NOT_A_WHITELISTED_KNOB")


def test_scheduler_helper_fallback_safe_when_resolver_disabled(monkeypatch, tmp_path: Path):
    """If learning.self_mutation is unavailable, the helper still works
    by falling back to getattr(config, knob, default)."""
    # Simulate the absence of the self_mutation package by setting the
    # module-level singletons to disabled state before importing the helper.
    import scheduler as scheduler_module
    importlib.reload(scheduler_module)

    # Force the disabled path.
    monkeypatch.setattr(scheduler_module, "_get_knob_resolver", None, raising=True)
    monkeypatch.setattr(scheduler_module, "_MUTABLE_KNOBS", {}, raising=True)

    value = scheduler_module._resolve_knob(
        "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER", 0.35
    )
    # Should equal the config default (which is 0.35 in fresh state).
    assert isinstance(value, float)
    assert value == 0.35


def test_scheduler_helper_returns_override_when_resolver_active(monkeypatch, tmp_path: Path):
    """When the resolver is wired and an override exists, the scheduler
    helper returns the override value (this is the live integration path).
    """
    overrides_path = tmp_path / "o.json"
    custom_resolver = _make_resolver(overrides_path)
    custom_resolver._overrides.set_main(OverrideEntry(
        knob="XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER",
        value=0.55,
        applied_utc="2026-05-18T02:00:00Z",
        mutation_id="m_integration",
    ))

    import scheduler as scheduler_module
    importlib.reload(scheduler_module)
    # Replace the lazy resolver getter with one that returns our custom resolver.
    monkeypatch.setattr(scheduler_module, "_get_knob_resolver", lambda: custom_resolver, raising=True)
    # Ensure the knob name is recognised by the whitelist check.
    from learning.self_mutation.sampler import KNOB_BY_NAME
    monkeypatch.setattr(scheduler_module, "_MUTABLE_KNOBS", KNOB_BY_NAME, raising=True)

    value = scheduler_module._resolve_knob(
        "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER", 0.35
    )
    assert value == 0.55

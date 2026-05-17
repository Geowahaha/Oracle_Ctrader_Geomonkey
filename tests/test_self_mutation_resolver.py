"""Tests for KnobResolver."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from learning.self_mutation.overrides import OverrideEntry, OverrideStore
from learning.self_mutation.resolver import KnobResolver


@dataclass
class _FakeConfig:
    XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER: float = 0.35
    RISK_PER_TRADE: float = 0.01


def test_resolver_returns_config_default_when_no_override(tmp_path: Path):
    store = OverrideStore(tmp_path / "o.json")
    resolver = KnobResolver(overrides=store, config_obj=_FakeConfig())
    assert resolver.get("RISK_PER_TRADE") == 0.01
    assert resolver.describe("RISK_PER_TRADE")["source"] == "baseline"


def test_resolver_main_override_beats_default(tmp_path: Path):
    store = OverrideStore(tmp_path / "o.json")
    store.set_main(OverrideEntry(
        knob="RISK_PER_TRADE", value=0.008,
        applied_utc="2026-05-17T01:00:00Z", mutation_id="m1",
    ))
    resolver = KnobResolver(overrides=store, config_obj=_FakeConfig())
    assert resolver.get("RISK_PER_TRADE") == 0.008
    desc = resolver.describe("RISK_PER_TRADE")
    assert desc["source"] == "main"
    assert desc["mutation_id"] == "m1"


def test_resolver_canary_beats_main(tmp_path: Path):
    store = OverrideStore(tmp_path / "o.json")
    store.set_main(OverrideEntry("RISK_PER_TRADE", 0.008, "2026-05-17T01:00:00Z", "m1"))
    store.set_canary(OverrideEntry(
        "RISK_PER_TRADE", 0.012, "2026-05-17T01:05:00Z", "m2",
        expires_utc="2099-01-01T00:00:00Z",
    ))
    resolver = KnobResolver(overrides=store, config_obj=_FakeConfig())
    assert resolver.get("RISK_PER_TRADE") == 0.012
    assert resolver.describe("RISK_PER_TRADE")["source"] == "canary"


def test_resolver_unknown_knob_raises(tmp_path: Path):
    store = OverrideStore(tmp_path / "o.json")
    resolver = KnobResolver(overrides=store, config_obj=_FakeConfig())
    with pytest.raises(KeyError):
        resolver.get("NOT_A_KNOB")
    with pytest.raises(KeyError):
        resolver.baseline("NOT_A_KNOB")


def test_resolver_snapshot_covers_every_whitelisted_knob(tmp_path: Path):
    store = OverrideStore(tmp_path / "o.json")
    resolver = KnobResolver(overrides=store, config_obj=_FakeConfig())
    snap = resolver.snapshot()
    # Every whitelisted knob is described, even those without a config attribute
    # (they fall back to spec.min_value).
    from learning.self_mutation.sampler import MUTABLE_KNOBS
    for spec in MUTABLE_KNOBS:
        assert spec.name in snap
        assert "value" in snap[spec.name]

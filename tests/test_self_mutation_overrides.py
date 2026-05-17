"""Tests for the Self-Mutation runtime overrides store."""
from __future__ import annotations

import json
from pathlib import Path

from learning.self_mutation.overrides import OverrideEntry, OverrideStore


def test_overrides_round_trip_atomic(tmp_path: Path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    entry = OverrideEntry(
        knob="RISK_PER_TRADE",
        value=0.012,
        applied_utc="2026-05-17T01:00:00Z",
        mutation_id="mut_x",
    )
    store.set_main(entry)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["main"]["RISK_PER_TRADE"]["value"] == 0.012
    # Read back.
    view = store.read()
    assert view.main["RISK_PER_TRADE"].value == 0.012
    assert "RISK_PER_TRADE" not in view.canary


def test_overrides_canary_hidden_when_expired(tmp_path: Path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_canary(OverrideEntry(
        knob="RISK_PER_TRADE",
        value=0.015,
        applied_utc="2026-05-17T00:00:00Z",
        mutation_id="mut_y",
        expires_utc="2026-05-17T01:00:00Z",
    ))
    # Before expiry.
    view_before = store.read(now_utc="2026-05-17T00:30:00Z")
    assert "RISK_PER_TRADE" in view_before.canary
    # After expiry the read view hides it (file still has it; reader is advisory).
    view_after = store.read(now_utc="2026-05-17T02:00:00Z")
    assert "RISK_PER_TRADE" not in view_after.canary


def test_overrides_value_for_canary_beats_main(tmp_path: Path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_main(OverrideEntry(
        knob="RISK_PER_TRADE",
        value=0.010,
        applied_utc="2026-05-17T01:00:00Z",
        mutation_id="mut_main",
    ))
    store.set_canary(OverrideEntry(
        knob="RISK_PER_TRADE",
        value=0.015,
        applied_utc="2026-05-17T01:05:00Z",
        mutation_id="mut_canary",
        expires_utc="2099-01-01T00:00:00Z",
    ))
    assert store.value_for("RISK_PER_TRADE", default=0.008) == 0.015
    store.remove_canary("RISK_PER_TRADE")
    assert store.value_for("RISK_PER_TRADE", default=0.008) == 0.010
    store.remove_main("RISK_PER_TRADE")
    assert store.value_for("RISK_PER_TRADE", default=0.008) == 0.008


def test_overrides_kill_all_clears_everything(tmp_path: Path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_main(OverrideEntry("A", 1.0, "2026-05-17T01:00:00Z", "m1"))
    store.set_canary(OverrideEntry("B", 2.0, "2026-05-17T01:00:00Z", "m2", expires_utc="2099-01-01T00:00:00Z"))
    empty = store.kill_all()
    assert empty.main == {}
    assert empty.canary == {}
    assert store.value_for("A", default=99.0) == 99.0


def test_overrides_promote_to_main_clears_canary(tmp_path: Path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_canary(OverrideEntry("K", 0.5, "2026-05-17T01:00:00Z", "m1", expires_utc="2099-01-01T00:00:00Z"))
    assert store.value_for("K", default=0.0) == 0.5
    store.set_main(OverrideEntry("K", 0.5, "2026-05-17T02:00:00Z", "m1"))
    view = store.read()
    assert "K" in view.main
    assert "K" not in view.canary


def test_overrides_corrupt_file_resets_cleanly(tmp_path: Path):
    path = tmp_path / "overrides.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = OverrideStore(path)
    view = store.read()
    assert view.main == {}
    assert view.canary == {}

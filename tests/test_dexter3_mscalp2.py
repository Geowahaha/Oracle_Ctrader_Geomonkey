"""Pins the MSCALP2 dollar-take twin (owner order 2026-08-06): identical
producer/entries/exits to mscalp plus exactly one addition — the fast-tick
dollar-take. Label isolation, off-by-default flags, take decision."""
from __future__ import annotations

from dexter3 import mscalp as ms


def test_mode_off_by_default_and_independent_of_mscalp(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert ms.mscalp2_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "mscalp2")
    assert ms.mscalp2_mode_enabled() is True
    # the twin mode must NOT arm the original lane's flag and vice versa
    assert ms.mscalp_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "mscalp")
    assert ms.mscalp2_mode_enabled() is False
    assert ms.mscalp_mode_enabled() is True


def test_take_usd_env_gated_off_by_default(monkeypatch):
    monkeypatch.delenv(ms.ENV_MSCALP2_TAKE_USD, raising=False)
    assert ms.mscalp2_take_usd() == 0.0
    monkeypatch.setenv(ms.ENV_MSCALP2_TAKE_USD, "5")
    assert ms.mscalp2_take_usd() == 5.0


def test_take_decision_thresholds():
    assert ms.mscalp2_take_decision(4.99, 5.0) is False
    assert ms.mscalp2_take_decision(5.0, 5.0) is True
    assert ms.mscalp2_take_decision(101.4, 5.0) is True
    assert ms.mscalp2_take_decision(-3.0, 5.0) is False
    # disabled threshold never takes, whatever the PnL
    assert ms.mscalp2_take_decision(999.0, 0.0) is False
    assert ms.mscalp2_take_decision(999.0, -1.0) is False


def test_label_isolation_and_runner_resolvers(monkeypatch):
    from dexter3.executor import label_matches_family

    assert ms.MSCALP2_LABEL == "dexter3:mscalp2:canary"
    assert label_matches_family(ms.MSCALP2_LABEL, "dexter3:mscalp2") is True
    # the twin's label must never match the original's family or vice versa
    assert label_matches_family(ms.MSCALP2_LABEL, "dexter3:mscalp") is False
    assert label_matches_family(ms.MSCALP_LABEL, "dexter3:mscalp2") is False

    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_MODE", "mscalp2")
    assert sr._active_order_label() == "dexter3:mscalp2:canary"
    assert sr._active_label_family() == "dexter3:mscalp2"
    assert sr._active_state_file().name == "dexter3_mscalp2_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_mscalp2_shadow.log"
    assert sr._mscalp2_producer_enabled() is True
    assert sr._mscalp_producer_enabled() is False
    assert sr._alt_producer_enabled() is True


def test_lane_tally_family_split():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:mscalp2:canary") == "mscalp2"
    assert _lane_family("dexter3:mscalp:canary") == "mscalp"

"""V1.6 entry-quality pro-pack + smart cool-down A+ bypass."""
from __future__ import annotations

from types import SimpleNamespace

from dexter3.opening_manager import OMConfig, OpeningManager, _update_peak_r
from dexter3.v16_entry_quality import (
    V16EntryQualityConfig,
    classify_a_plus,
    evaluate_v16_entry_gate,
    is_noise_exit,
    record_noise_close,
)


def _decision(**kwargs):
    base = {
        "symbol": "XAUUSD",
        "side": "buy",
        "setup": "hunt_swing_structure",
        "leader_score": 0.10,
        "features": {
            "anti_chase": {"is_chase": False},
            "pullback_gate": {"is_pullback": False},
        },
    }
    base.update(kwargs)
    if "features" in kwargs and kwargs["features"] is not None:
        base["features"] = kwargs["features"]
    return SimpleNamespace(**base)


def test_a_plus_elite_score_bypasses_cooldown():
    cfg = V16EntryQualityConfig(cooldown_sec=600)
    state = {
        "v16_entry_cooldown": {
            "symbol": "XAUUSD",
            "side": "buy",
            "reason": "stall_take",
            "peak_r": 0.1,
            "live_r": -0.02,
            "ts": "2026-07-09T07:00:00Z",
        }
    }
    d = _decision(
        leader_score=0.32,
        setup="hunt_h1_context",
        features={"anti_chase": {"is_chase": False}, "pullback_gate": {"is_pullback": True}},
    )
    out = evaluate_v16_entry_gate(
        decision=d, state=state, mcp_consec_errors=0, now_iso="2026-07-09T07:05:00Z", cfg=cfg
    )
    assert out["allow"] is True
    assert out["a_plus"] is True
    assert out["cooldown_bypassed"] is True
    assert out["reason"] == "pass_a_plus_cooldown_bypass"


def test_noise_cooldown_blocks_low_quality_same_side():
    cfg = V16EntryQualityConfig(cooldown_sec=600, min_leader_score=0.10)
    state = {
        "v16_entry_cooldown": {
            "symbol": "XAUUSD",
            "side": "buy",
            "reason": "stall_take",
            "peak_r": 0.1,
            "live_r": -0.02,
            "ts": "2026-07-09T07:00:00Z",
        }
    }
    d = _decision(leader_score=0.15, side="buy")
    out = evaluate_v16_entry_gate(
        decision=d, state=state, mcp_consec_errors=0, now_iso="2026-07-09T07:05:00Z", cfg=cfg
    )
    assert out["allow"] is False
    assert out["reason"] == "same_side_noise_cooldown"
    assert out["cooldown_bypassed"] is False


def test_cooldown_disabled_allows_low_quality_same_side():
    cfg = V16EntryQualityConfig(cooldown_enabled=False, cooldown_sec=600, min_leader_score=0.10)
    state = {
        "v16_entry_cooldown": {
            "symbol": "XAUUSD",
            "side": "buy",
            "reason": "stall_take",
            "peak_r": 0.1,
            "live_r": -0.02,
            "ts": "2026-07-09T07:00:00Z",
        }
    }
    d = _decision(leader_score=0.15, side="buy")
    out = evaluate_v16_entry_gate(
        decision=d, state=state, mcp_consec_errors=0, now_iso="2026-07-09T07:05:00Z", cfg=cfg
    )
    assert out["allow"] is True
    assert out["reason"] == "pass"


def test_cooldown_does_not_block_opposite_side():
    cfg = V16EntryQualityConfig(cooldown_sec=600, min_leader_score=0.10)
    state = {
        "v16_entry_cooldown": {
            "symbol": "XAUUSD",
            "side": "buy",
            "reason": "stall_take",
            "peak_r": 0.1,
            "live_r": -0.02,
            "ts": "2026-07-09T07:00:00Z",
        }
    }
    d = _decision(leader_score=0.20, side="sell")
    out = evaluate_v16_entry_gate(
        decision=d, state=state, mcp_consec_errors=0, now_iso="2026-07-09T07:05:00Z", cfg=cfg
    )
    assert out["allow"] is True


def test_winner_pullback_is_a_plus():
    ok, reason = classify_a_plus(
        leader_score=0.23,
        setup="hunt_swing_structure",
        is_chase=False,
        is_pullback=True,
        cfg=V16EntryQualityConfig(),
    )
    assert ok is True
    assert reason == "winner_pullback"


def test_min_leader_score_blocks():
    d = _decision(leader_score=0.05)
    out = evaluate_v16_entry_gate(decision=d, state={}, mcp_consec_errors=0, cfg=V16EntryQualityConfig())
    assert out["allow"] is False
    assert out["reason"] == "min_leader_score"


def test_chase_hard_block_and_exceptional_pullback_pass_when_legacy_bucket_bypass_enabled():
    cfg = V16EntryQualityConfig(
        min_leader_score=0.10,
        block_chase_bypass_on_aligned_trending=False,
    )
    blocked = _decision(
        leader_score=0.20,
        features={"anti_chase": {"is_chase": True}, "pullback_gate": {"is_pullback": False}},
    )
    out_b = evaluate_v16_entry_gate(decision=blocked, state={}, mcp_consec_errors=0, cfg=cfg)
    assert out_b["allow"] is False
    assert out_b["reason"] == "chase_hard_block"

    allowed = _decision(
        leader_score=0.40,
        features={"anti_chase": {"is_chase": True}, "pullback_gate": {"is_pullback": True}},
    )
    out_a = evaluate_v16_entry_gate(decision=allowed, state={}, mcp_consec_errors=0, cfg=cfg)
    assert out_a["allow"] is True


def test_v17_winner_pullback_bypasses_chase_hard_block():
    """Live V1.6 mistake: A+ winner_pullback was classified then blocked as chase."""
    cfg = V16EntryQualityConfig(
        min_leader_score=0.18,
        block_chase_bypass_on_aligned_trending=False,
    )
    d = _decision(
        leader_score=0.237,
        setup="hunt_swing_structure",
        features={"anti_chase": {"is_chase": True}, "pullback_gate": {"is_pullback": True}},
    )
    out = evaluate_v16_entry_gate(decision=d, state={}, mcp_consec_errors=0, cfg=cfg)
    assert out["allow"] is True
    assert out["reason"] == "pass_a_plus_chase_bypass"
    assert out["a_plus"] is True
    assert out["a_plus_reason"] == "winner_pullback"
    assert out["features"]["chase_bypass"] == "winner_pullback"


def test_v17_mission_control_blocks_a_plus_aligned_trending_chase():
    """Mission-control patch: do not reopen the proven-loss aligned/trending bucket."""
    cfg = V16EntryQualityConfig(
        min_leader_score=0.18,
        block_chase_bypass_on_aligned_trending=True,
    )
    d = _decision(
        leader_score=0.237,
        setup="hunt_swing_structure",
        features={
            "anti_chase": {"is_chase": True, "align": "aligned", "regime": "trending"},
            "pullback_gate": {"is_pullback": True},
        },
    )
    out = evaluate_v16_entry_gate(decision=d, state={}, mcp_consec_errors=0, cfg=cfg)
    assert out["allow"] is False
    assert out["reason"] == "chase_hard_block"
    assert out["a_plus"] is True
    assert out["a_plus_reason"] == "winner_pullback"
    assert out["features"]["chase_bypass_blocked_by_bucket"] == "aligned_trending"


def test_weak_setup_hard_skip():
    d = _decision(leader_score=0.19, setup="hunt_sweep_reclaim")
    out = evaluate_v16_entry_gate(decision=d, state={}, mcp_consec_errors=0, cfg=V16EntryQualityConfig())
    assert out["allow"] is False
    assert out["reason"] == "weak_setup_hard_skip"


def test_mcp_unhealthy_blocks_even_a_plus():
    d = _decision(
        leader_score=0.50,
        features={"anti_chase": {"is_chase": False}, "pullback_gate": {"is_pullback": True}},
    )
    out = evaluate_v16_entry_gate(
        decision=d, state={}, mcp_consec_errors=3, cfg=V16EntryQualityConfig(mcp_max_consec_errors=2)
    )
    assert out["allow"] is False
    assert out["reason"] == "mcp_unhealthy"
    assert out["a_plus"] is True  # still classified, but infra wins


def test_noise_exit_and_record():
    assert is_noise_exit(reason="stall_take", live_r=-0.02, peak_r=0.1) is True
    assert is_noise_exit(reason="ladder_floor", live_r=0.50, peak_r=0.80) is False
    state: dict = {}
    stamp = record_noise_close(
        state,
        symbol="XAUUSD",
        side="buy",
        reason="stall_take",
        peak_r=0.1,
        live_r=-0.01,
        now_iso="2026-07-09T07:00:00Z",
    )
    assert stamp is not None
    assert state["v16_entry_cooldown"]["side"] == "buy"


def test_stall_min_peak_blocks_tiny_noise():
    """Pro-pack: peak 0.05 must NOT stall-take (noise floor)."""
    from tests.test_dexter3_opening_manager import _lane_at_r

    om = OpeningManager(
        config=OMConfig(
            take_r=50.0,
            spike_take_r=100.0,
            stall_max_peak_r=0.35,
            stall_ticks=2,
            stall_decay_frac=0.9,
            stall_min_peak_r=0.12,
            stall_min_hold_ticks=0,
        )
    )
    state: dict = {"base_risk_usd": 1.0}
    for r in [0.05, 0.04, 0.03, 0.02]:
        lane = _lane_at_r(r, side="buy")
        action = om.evaluate("XAUUSD", lane, None, [], [], [], state)
        state["basket_runtime"] = action.get("basket_runtime")
        assert action.get("reason") != "stall_take"


def test_update_peak_tracks_ticks_open():
    rt = _update_peak_r(None, "t1", 0.1)
    assert rt["ticks_open"] == 0
    rt = _update_peak_r(rt, "t1", 0.1)
    assert rt["ticks_open"] == 1
    rt = _update_peak_r(rt, "t1", 0.1)
    assert rt["ticks_open"] == 2

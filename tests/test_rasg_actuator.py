from analysis.route_classifier import classify_xau_route


def test_rasg_active_forces_single_leg_safer_route_not_normal_add():
    decision = classify_xau_route({
        "symbol": "XAUUSD",
        "direction": "short",
        "source": "xauusd_scheduled",
        "confidence": 82.0,
        "flow_confirmed": True,
        "impulse_state": "impulse_run",
        "price_near_liquidity_target": False,
        "equal_lows_distance_atr": 1.2,
        "extended_move_atr": 0.4,
        "rasg_active": True,
        "rasg_reason": "rolling_side_bleed",
    })

    assert decision.max_legs == 1
    assert decision.leg_count == 1
    assert decision.route in {"retest_entry", "wait_retest_plan"}
    assert decision.invalidation_policy in {"tight", "retest_only"}
    assert any("rasg" in reason.lower() for reason in decision.reasons)

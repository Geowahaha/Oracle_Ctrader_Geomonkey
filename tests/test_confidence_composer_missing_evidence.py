from analysis.confidence_composer import compose_confidence


def test_missing_impulse_and_weak_delta_activate_missing_evidence_penalty():
    result = compose_confidence(
        base_confidence=72.0,
        components={"htf_bias": 2.0},
        features={
            "flow_confirmed": False,
            "delta_proxy": 0.0,
            "bar_volume_proxy": 0.0,
            "impulse_state": "idle",
            "sharpness_has_data": False,
            "price_near_liquidity_target": False,
        },
    )

    assert result.missing_evidence_penalty >= 6.0
    assert result.passed_execution_gate is False
    assert any("missing" in reason for reason in result.reasons)

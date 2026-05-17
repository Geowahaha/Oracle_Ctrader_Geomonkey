from analysis.confidence_composer import compose_confidence


def test_correlated_narrative_stack_with_missing_flow_does_not_pass_execution_gate():
    result = compose_confidence(
        base_confidence=76.0,
        components={
            "htf_bearish": 3.0,
            "smc_bearish": 3.0,
            "ob_fvg_bearish": 2.5,
            "session_overlap": 2.0,
            "historical_family_edge": 2.5,
        },
        features={
            "direction": "short",
            "flow_confirmed": False,
            "impulse_state": "idle",
            "price_near_liquidity_target": True,
            "correlated_component_groups": [
                ["htf_bearish", "smc_bearish", "ob_fvg_bearish", "session_overlap", "historical_family_edge"]
            ],
        },
    )

    assert result.correlation_penalty > 0
    assert result.missing_evidence_penalty > 0
    assert result.proximity_penalty > 0
    assert result.final_confidence < 80.0
    assert result.passed_execution_gate is False

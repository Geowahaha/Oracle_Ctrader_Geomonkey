from scripts.dexter3_edge_discovery import _executed_risk_usd


def test_execution_economics_replays_floor_step_and_abs_cap() -> None:
    # 1oz of a $12 stop is rejected by the live abs-cap guard, regardless of
    # the smaller designed risk that a sizing-policy replay might assign.
    assert _executed_risk_usd(12.0, 4.0, min_volume_abs_risk_cap_usd=9.0) is None

    # A $4 designed risk at a $3 stop is clamped to 1oz, so live risk is $3.
    assert _executed_risk_usd(3.0, 4.0) == 3.0

    # Executor floors (never rounds up) to its volume step: $10/$3 -> 3oz.
    assert _executed_risk_usd(3.0, 10.0) == 9.0

from execution.risk_basket import BasketRegistry


def test_same_signal_run_main_open_denies_duplicate_canary_full_risk():
    registry = BasketRegistry(default_budget_usd=100.0)
    first = registry.can_admit(
        signal_run_id="202605151347-837",
        requested_risk_usd=80.0,
        leg_role="main",
    )
    assert first.admitted is True
    registry.record_open_leg("202605151347-837", risk_usd=80.0, leg_role="main")

    duplicate = registry.can_admit(
        signal_run_id="202605151347-837",
        requested_risk_usd=80.0,
        leg_role="canary",
    )

    assert duplicate.admitted is False
    assert duplicate.open_risk_usd == 80.0
    assert "duplicate" in duplicate.reason or "budget" in duplicate.reason

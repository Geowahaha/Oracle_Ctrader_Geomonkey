from analysis.fibo_confluence_reclaim import evaluate_fibo_confluence_reclaim


def test_long_reclaim_cluster_near_dema_matches_user_chart_shape():
    decision = evaluate_fibo_confluence_reclaim(
        direction="long",
        current_price=4691.60,
        atr=4.0,
        fib_level_prices=[4688.8, 4690.2, 4692.0, 4695.2, 4705.0],
        dema=4692.57,
        dema_previous=4688.40,
        recent_bars=[
            {"open": 4684.0, "high": 4689.0, "low": 4675.0, "close": 4688.2},
            {"open": 4688.2, "high": 4696.0, "low": 4684.0, "close": 4694.8},
            {"open": 4694.8, "high": 4705.0, "low": 4688.4, "close": 4691.6},
        ],
    )

    assert decision.setup == "fibo_reclaim_long"
    assert decision.cluster_count >= 3
    assert decision.dema_aligned is True
    assert decision.reclaim_confirmed is True
    assert decision.score >= 70
    assert "fib_cluster" in decision.reasons
    assert "dema_reclaim" in decision.reasons


def test_failed_long_reclaim_when_price_rejects_cluster_below_dema():
    decision = evaluate_fibo_confluence_reclaim(
        direction="long",
        current_price=4686.20,
        atr=4.0,
        fib_level_prices=[4688.8, 4690.2, 4692.0, 4695.2],
        dema=4692.57,
        dema_previous=4694.10,
        recent_bars=[
            {"open": 4694.8, "high": 4696.0, "low": 4688.4, "close": 4689.0},
            {"open": 4689.0, "high": 4691.0, "low": 4684.0, "close": 4686.2},
        ],
    )

    assert decision.setup == "failed_reclaim"
    assert decision.reclaim_confirmed is False
    assert decision.dema_aligned is False
    assert decision.score < 50
    assert "below_dema" in decision.risks


def test_short_reclaim_cluster_uses_mirrored_dema_logic():
    decision = evaluate_fibo_confluence_reclaim(
        direction="short",
        current_price=4712.0,
        atr=3.0,
        fib_level_prices=[4711.0, 4712.5, 4714.0, 4720.0],
        dema=4713.2,
        dema_previous=4717.5,
        recent_bars=[
            {"open": 4719.0, "high": 4725.0, "low": 4715.0, "close": 4714.0},
            {"open": 4714.0, "high": 4717.0, "low": 4710.5, "close": 4712.0},
        ],
    )

    assert decision.setup == "fibo_reclaim_short"
    assert decision.dema_aligned is True
    assert decision.reclaim_confirmed is True
    assert decision.score >= 70

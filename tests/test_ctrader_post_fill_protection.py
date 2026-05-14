from execution.ctrader_executor import CTraderExecutor


def test_missing_buy_sl_after_deep_limit_fill_preserves_planned_risk_distance():
    target = CTraderExecutor._post_fill_missing_sl_target(
        direction="long",
        planned_entry=4706.24,
        planned_stop_loss=4703.94,
        live_entry=4689.04,
        candidate_stop_loss=4703.94,
        missing_stop_loss=True,
    )

    assert round(target, 2) == 4686.74
    assert CTraderExecutor._stop_valid_for_position("long", 4689.04, target)


def test_missing_sell_sl_after_deep_limit_fill_preserves_planned_risk_distance():
    target = CTraderExecutor._post_fill_missing_sl_target(
        direction="short",
        planned_entry=4700.00,
        planned_stop_loss=4702.50,
        live_entry=4711.20,
        candidate_stop_loss=4702.50,
        missing_stop_loss=True,
    )

    assert round(target, 2) == 4713.70
    assert CTraderExecutor._stop_valid_for_position("short", 4711.20, target)


def test_valid_missing_sl_candidate_is_kept_unchanged():
    target = CTraderExecutor._post_fill_missing_sl_target(
        direction="long",
        planned_entry=4706.24,
        planned_stop_loss=4703.94,
        live_entry=4706.00,
        candidate_stop_loss=4703.94,
        missing_stop_loss=True,
    )

    assert target == 4703.94


def test_non_missing_sl_candidate_is_kept_unchanged():
    target = CTraderExecutor._post_fill_missing_sl_target(
        direction="long",
        planned_entry=4706.24,
        planned_stop_loss=4703.94,
        live_entry=4689.04,
        candidate_stop_loss=0.0,
        missing_stop_loss=False,
    )

    assert target == 0.0

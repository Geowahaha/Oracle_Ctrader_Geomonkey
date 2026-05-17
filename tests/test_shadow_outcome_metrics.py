from analysis.shadow_outcome_metrics import compute_shadow_path_metrics


def test_shadow_metrics_records_mae_and_mfe_before_tp_hit_for_long():
    bars = [
        (100.4, 99.6),
        (101.2, 100.1),
    ]
    metrics = compute_shadow_path_metrics("long", entry=100.0, stop_loss=99.0, take_profit_1=101.0, bars=bars)

    assert metrics["outcome"] == "tp_hit"
    assert metrics["pnl_rr"] == 1.0
    assert metrics["mae_rr"] == 0.4
    assert metrics["mfe_rr"] == 1.2


def test_shadow_metrics_records_mae_and_mfe_before_sl_hit_for_short():
    bars = [
        (101.6, 99.4),
    ]
    metrics = compute_shadow_path_metrics("short", entry=100.0, stop_loss=101.0, take_profit_1=99.0, bars=bars)

    assert metrics["outcome"] == "sl_hit"
    assert metrics["pnl_rr"] == -1.0
    assert metrics["mae_rr"] == 1.6
    assert metrics["mfe_rr"] == 0.6


def test_shadow_metrics_expires_with_path_excursions_when_neither_tp_nor_sl_hit():
    bars = [
        (100.3, 99.8),
        (100.4, 99.7),
    ]
    metrics = compute_shadow_path_metrics("long", entry=100.0, stop_loss=99.0, take_profit_1=101.0, bars=bars)

    assert metrics["outcome"] == "expired"
    assert metrics["pnl_rr"] is None
    assert metrics["mae_rr"] == 0.3
    assert metrics["mfe_rr"] == 0.4

from ops.fibo_mtf_acceptance_analytics import summarize_rows


def _row(tf, zone, rr, mismatch=False):
    raw = {"tf_label": tf, "ratio_zone": zone, "signal_direction": "long"}
    if mismatch:
        raw["deal_direction"] = "short"
    else:
        raw["deal_direction"] = "long"
    return {"raw": raw, "shadow_pnl_rr": rr, "direction": "long"}


def test_acceptance_analytics_groups_by_tf_and_zone_with_trimmed_metrics():
    rows = [
        _row("M5", "near_0.618", 1.0),
        _row("M5", "near_0.618", 1.2),
        _row("M5", "near_0.618", -0.4),
        _row("M5", "near_0.618", 9.0),
    ]
    report = summarize_rows(rows, min_trades=2, min_pf=1.1, max_mismatch=0.10)
    m5 = report["by_tf"]["M5"]
    assert m5["resolved"] == 4
    assert m5["profit_factor"] == 28.0
    assert m5["expectancy_R_trim_top2"] == 0.3
    assert report["by_ratio_zone"]["near_0.618"]["resolved"] == 4
    assert report["by_tf_ratio_zone"]["M5|near_0.618"]["eligible_for_one_tf_demo_review"] is True


def test_acceptance_analytics_blocks_direction_mismatch():
    rows = [_row("M1", "near_0.50", 0.2), _row("M1", "near_0.50", 0.3, mismatch=True), _row("M1", "near_0.50", -0.1)]
    report = summarize_rows(rows, min_trades=2, min_pf=1.0, max_mismatch=0.10)
    m1 = report["by_tf"]["M1"]
    assert m1["direction_mismatch_rate"] > 0.10
    assert m1["eligible_for_one_tf_demo_review"] is False
    assert any("direction_mismatch" in b for b in m1["promotion_blockers"])

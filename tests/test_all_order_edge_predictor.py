from ops.all_order_edge_predictor import (
    build_edge_report,
    mine_predictive_edges,
    select_live_uses,
)


def _row(pid, symbol, family, direction, pnl, month, session="asia", rr="sub_1r_target"):
    return {
        "position_id": pid,
        "symbol": symbol,
        "family": family,
        "direction": direction,
        "pnl_usd": pnl,
        "month": month,
        "session": session,
        "risk_geometry": rr,
        "weekday": "wed",
        "hour_bucket": "h04",
    }


def test_mine_predictive_edges_requires_forward_month_support():
    rows = []
    pid = 1
    for month in ["2026-03", "2026-04", "2026-05"]:
        for _ in range(15):
            rows.append(_row(pid, "XAUUSD", "xauusd_scheduled", "long", 2.0, month))
            pid += 1
    rows += [_row(pid + i, "XAUUSD", "scalp_xauusd", "short", -2.0, "2026-05") for i in range(45)]

    edges = mine_predictive_edges(rows, min_samples=40, min_pf=1.4, min_wr=0.55)

    assert edges
    best = edges[0]
    assert best["bucket_key"] == ["XAUUSD", "xauusd_scheduled", "long", "sub_1r_target"]
    assert best["train"]["samples"] == 30
    assert best["forward"]["samples"] == 15
    assert best["forward"]["net_usd"] == 30.0
    assert best["edge_type"] == "max_wr_and_profit"


def test_select_live_uses_outputs_canary_actions_not_dispatch():
    edge = {
        "cluster_id": "predictive_edge|symbol_family_dir_rr|XAUUSD|xauusd_scheduled|long|sub_1r_target",
        "bucket_level": "symbol_family_dir_rr",
        "bucket_key": ["XAUUSD", "xauusd_scheduled", "long", "sub_1r_target"],
        "samples": 45,
        "winrate": 0.71,
        "profit_factor": 2.0,
        "net_usd": 90.0,
        "forward": {"samples": 15, "winrate": 0.73, "profit_factor": 2.1, "net_usd": 30.0},
        "positive_months": 3,
        "months": 3,
        "edge_type": "max_wr_and_profit",
    }

    uses = select_live_uses([edge])

    assert uses[0]["execution_enabled"] is False
    assert uses[0]["recommended_action"] in {
        "canary_allow_or_boost_existing_lane",
        "canary_pm_bias_or_micro_risk_only",
    }
    assert uses[0]["max_risk_usd"] <= 0.5


def test_build_edge_report_contains_predictive_contract():
    rows = [_row(i, "ETHUSD", "scalp_ethusd", "long", 1.0, "2026-05") for i in range(1, 42)]

    report = build_edge_report(rows, min_samples=40, min_pf=1.0, min_wr=0.5)

    assert report["summary"]["positions"] == 41
    assert report["prediction_contract"]["data_source"] == "ctrader_positions_joined_deals"
    assert report["prediction_contract"]["execution_enabled"] is False

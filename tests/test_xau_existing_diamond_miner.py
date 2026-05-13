from ops.xau_existing_diamond_miner import (
    build_report,
    mine_existing_diamonds,
    select_shadow_promotions,
)


def _pos(pid, family, direction, pnl, opened, rr_bucket="over_5r"):
    return {
        "position_id": pid,
        "family": family,
        "direction": direction,
        "pnl_usd": pnl,
        "first_seen_utc": opened,
        "session": "london",
        "risk_geometry": rr_bucket,
        "label": family,
        "comment": family,
    }


def test_mine_existing_diamonds_promotes_profitable_high_sample_bucket():
    rows = []
    for i in range(4):
        rows.append(_pos(i, "fibo_xauusd", "long", 10, f"2026-0{1 + i}-12T08:00:00Z"))
    rows.append(_pos(10, "fibo_xauusd", "long", -3, "2026-05-12T08:00:00Z"))

    diamonds = mine_existing_diamonds(rows, min_samples=5, min_pf=2.0, min_net=20.0)

    assert diamonds[0]["cluster_id"] == "existing_diamond|family_dir_rr|fibo_xauusd|long|over_5r"
    assert diamonds[0]["samples"] == 5
    assert diamonds[0]["profit_factor"] > 10
    assert diamonds[0]["shadow_promote_candidate"] is True


def test_select_shadow_promotions_keeps_only_month_coherent_scheduled_sub1r_long():
    diamonds = [
        {
            "cluster_id": "existing_diamond|family_dir_rr|xauusd_scheduled|long|sub_1r_target",
            "bucket_level": "family_dir_rr",
            "bucket_key": ["xauusd_scheduled", "long", "sub_1r_target"],
            "samples": 45,
            "profit_factor": 2.01,
            "net_usd": 191.0,
            "winrate": 0.71,
            "positive_months": 3,
            "months": 3,
        },
        {
            "cluster_id": "existing_diamond|family_rr|fibo_xauusd|tiny_stop",
            "bucket_level": "family_rr",
            "bucket_key": ["fibo_xauusd", "tiny_stop"],
            "samples": 113,
            "profit_factor": 9.0,
            "net_usd": 637.0,
            "winrate": 0.35,
            "positive_months": 2,
            "months": 2,
        },
    ]

    promotions = select_shadow_promotions(diamonds)

    assert [p["cluster_id"] for p in promotions] == [
        "existing_diamond|family_dir_rr|xauusd_scheduled|long|sub_1r_target"
    ]
    assert promotions[0]["promotion_phase"] == "shadow_only_forward_walk"

    rows = []
    for i in range(5):
        rows.append(_pos(i, "scalp_xauusd", "short", 4, f"2026-05-{10+i:02d}T08:00:00Z", "sub_1r"))
    rows.append(_pos(7, "scalp_xauusd", "short", -2, "2026-05-20T08:00:00Z", "sub_1r"))

    report = build_report(rows, min_samples=6, min_pf=2.0, min_net=10.0)

    assert report["summary"]["diamonds_found"] >= 1
    assert report["promotion_scaffold"]["execution_enabled"] is False
    assert report["promotion_scaffold"]["candidate_family"] == "xau_existing_diamond_shadow"

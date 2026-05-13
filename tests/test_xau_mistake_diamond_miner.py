from ops.xau_mistake_diamond_miner import build_report, mine_stophunt_inversions


def _pos(pid, family, direction, pnl, opened, closed=None):
    return {
        "position_id": pid,
        "family": family,
        "direction": direction,
        "pnl_usd": pnl,
        "first_seen_utc": opened,
        "close_utc": closed or opened,
        "last_seen_utc": closed or opened,
        "label": f"dexter:XAUUSD:{family}:1",
        "comment": family,
        "entry_price": 3300.0,
        "stop_loss": 3290.0 if direction == "long" else 3310.0,
        "take_profit": 3330.0 if direction == "long" else 3270.0,
    }


def test_mine_stophunt_inversions_finds_opposite_followup_cluster():
    rows = [
        _pos(1, "fibo_xauusd", "short", -8, "2026-05-12T08:00:00Z", "2026-05-12T08:10:00Z"),
        _pos(2, "xauusd_scheduled", "long", 12, "2026-05-12T08:18:00Z", "2026-05-12T08:40:00Z"),
        _pos(3, "fibo_xauusd", "short", -5, "2026-05-13T08:00:00Z", "2026-05-13T08:10:00Z"),
        _pos(4, "scalp_xauusd", "long", 6, "2026-05-13T08:20:00Z", "2026-05-13T08:28:00Z"),
    ]

    clusters = mine_stophunt_inversions(rows, max_minutes=30, min_samples=2)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_id"] == "stophunt_inversion|fibo_xauusd|london|short|30m"
    assert cluster["samples"] == 2
    assert cluster["inversion_net_usd"] == 18.0
    assert cluster["theoretical_direction"] == "long"
    assert cluster["phase_a_shadow_candidate"] is True


def test_build_report_surfaces_diamonds_and_guards_tiny_samples():
    rows = [
        _pos(1, "fibo_xauusd", "short", -8, "2026-05-12T08:00:00Z", "2026-05-12T08:10:00Z"),
        _pos(2, "xauusd_scheduled", "long", 12, "2026-05-12T08:18:00Z", "2026-05-12T08:40:00Z"),
        _pos(3, "fibo_xauusd", "short", -5, "2026-05-13T08:00:00Z", "2026-05-13T08:10:00Z"),
        _pos(4, "scalp_xauusd", "long", -2, "2026-05-13T08:20:00Z", "2026-05-13T08:28:00Z"),
        _pos(5, "fibo_xauusd", "long", -5, "2026-05-13T15:00:00Z", "2026-05-13T15:10:00Z"),
        _pos(6, "scalp_xauusd", "short", 40, "2026-05-13T15:20:00Z", "2026-05-13T15:28:00Z"),
    ]

    report = build_report(rows, min_samples=1)

    assert report["summary"]["positions"] == 6
    assert report["diamonds"][0]["inversion_net_usd"] == 40.0
    assert report["strategy_scaffold"]["family"] == "xau_stophunt_inversion_shadow"
    assert report["strategy_scaffold"]["live_enabled"] is False

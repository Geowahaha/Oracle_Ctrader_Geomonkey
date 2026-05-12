from datetime import datetime, timezone

from ops.fibo_mtf_planner_shadow_report import summarize_rows


def _row(route="probe", tf="M5", zone="near_0.618", rr=None, anchor=True, mae=1.0, day=1, hour=14):
    raw = {
        "fibo_mtf_route": route,
        "tf_label": tf,
        "ratio_zone": zone,
    }
    if anchor:
        raw["execution_swing_low"] = 3300.0
    if mae is not None:
        raw["shadow_mae_rr"] = mae
    return {
        "block_reason": f"fibo_mtf_planner:{route}",
        "route": route,
        "tf": tf,
        "ratio_zone": zone,
        "raw": raw,
        "shadow_pnl_rr": rr,
        "signal_dt": datetime(2026, 5, day, hour, 0, tzinfo=timezone.utc),
        "session_key": f"2026-05-{day:02d}:ny",
        "has_real_anchor": anchor,
        "mae_r": mae,
    }


def test_planner_shadow_report_groups_by_route_tf_zone():
    rows = [
        _row("probe", "M5", "near_0.618", rr=0.8),
        _row("probe", "M5", "near_0.618", rr=-0.2),
        _row("observe_only", "H4", "other", rr=None, anchor=False, mae=None),
    ]
    report = summarize_rows(rows)
    assert report["summary"]["decisions"] == 3
    assert report["by_route"]["probe"]["resolved"] == 2
    assert report["by_route_tf"]["probe|M5"]["decisions"] == 2
    assert report["by_route_ratio_zone"]["observe_only|other"]["real_anchor_rate"] == 0.0
    assert report["micro_live_probe_gate"]["eligible_for_opus_micro_live_review"] is False


def test_probe_gate_can_pass_when_opus_requirements_are_met():
    rows = []
    # 30 decisions, 30 resolved outcomes, 14 calendar days, mixed sessions, >50% WR, >0.4R expectancy,
    # all anchored, MAE under the Opus cap.
    for i in range(30):
        day = (i % 14) + 1
        hour = 3 if i % 2 else 14
        session = "asia" if hour == 3 else "ny"
        row = _row("probe", "M1", "near_0.50", rr=(1.0 if i < 20 else -0.1), anchor=True, mae=2.0, day=day, hour=hour)
        row["session_key"] = f"2026-05-{day:02d}:{session}"
        rows.append(row)
    report = summarize_rows(rows)
    gate = report["micro_live_probe_gate"]
    assert report["by_route"]["probe"]["decisions"] == 30
    assert report["by_route"]["probe"]["calendar_days"] == 14
    assert report["by_route"]["probe"]["real_anchor_rate"] == 1.0
    assert gate["eligible_for_opus_micro_live_review"] is True
    assert gate["blockers"] == []


def test_probe_gate_blocks_missing_mae_and_anchor_rate():
    rows = [_row("probe", rr=1.0, anchor=False, mae=None, day=(i % 14) + 1) for i in range(30)]
    report = summarize_rows(rows)
    blockers = report["micro_live_probe_gate"]["blockers"]
    assert "mae_R_missing" in blockers
    assert any("real_anchor_rate" in blocker for blocker in blockers)

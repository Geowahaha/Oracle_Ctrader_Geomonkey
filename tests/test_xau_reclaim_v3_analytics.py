from ops.xau_reclaim_v3_analytics import summarize_rows


def test_summarize_rows_counts_inactive_metadata_without_live_effect():
    rows = [
        {
            "source": "xauusd_scheduled",
            "status": "filtered",
            "xau_reclaim_v3": {
                "active": False,
                "shadow_only": False,
                "phase": "none",
                "score": 0.0,
                "reason": "no_reclaim_features",
                "planned_rr": 5.0,
                "confidence_bonus": 0.0,
                "risk_mult": 1.0,
            },
        }
    ]
    report = summarize_rows(rows)
    assert report["rows"] == 1
    assert report["active_rows"] == 0
    assert report["possible_live_effect_rows"] == 0
    assert report["by_reason"][0] == {"key": "no_reclaim_features", "n": 1}


def test_summarize_rows_flags_active_live_effects():
    rows = [
        {
            "source": "scalp_xauusd",
            "status": "sent",
            "xau_reclaim_v3": {
                "active": True,
                "shadow_only": False,
                "phase": "staircase",
                "score": 78.0,
                "reason": "structure_break_reclaim+flow",
                "bypass_conf_below": True,
                "winner_partial_override": False,
                "planned_rr": 3.2,
                "confidence_bonus": 2.5,
                "risk_mult": 1.25,
            },
        }
    ]
    report = summarize_rows(rows)
    assert report["rows"] == 1
    assert report["active_rows"] == 1
    assert report["possible_live_effect_rows"] == 1
    assert report["score_max"] == 78.0
    assert report["risk_mult_max"] == 1.25
    assert report["by_bypass_conf_below"][0] == {"key": "True", "n": 1}

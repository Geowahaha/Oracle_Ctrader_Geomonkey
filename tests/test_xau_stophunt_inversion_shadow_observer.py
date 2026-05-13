from datetime import datetime, timezone

from ops.xau_stophunt_inversion_shadow_observer import build_shadow_observations


def test_build_shadow_observations_emits_safe_candidate_for_matching_recent_loss():
    loss = {
        "position_id": 42,
        "family": "fibo_xauusd",
        "session": "asia",
        "direction": "short",
        "pnl_usd": -7.5,
        "close_utc": "2026-05-13T02:10:00Z",
    }
    diamonds = [
        {
            "cluster_id": "stophunt_inversion|fibo_xauusd|asia|short|30m",
            "bucket_level": "family_session_dir",
            "bucket_key": ["fibo_xauusd", "asia", "short"],
            "theoretical_direction": "long",
            "window_minutes": 30,
            "samples": 48,
            "profit_factor": 1.84,
            "inversion_net_usd": 100.93,
            "phase_a_shadow_candidate": True,
        }
    ]

    observations = build_shadow_observations([loss], diamonds, now=datetime(2026, 5, 13, 2, 20, tzinfo=timezone.utc))

    assert len(observations) == 1
    obs = observations[0]
    assert obs["family"] == "xau_stophunt_inversion_shadow"
    assert obs["direction"] == "long"
    assert obs["execution_enabled"] is False
    assert obs["matched_position_id"] == 42
    assert obs["matched_cluster_id"] == diamonds[0]["cluster_id"]


def test_build_shadow_observations_skips_expired_window():
    loss = {
        "position_id": 42,
        "family": "fibo_xauusd",
        "session": "asia",
        "direction": "short",
        "pnl_usd": -7.5,
        "close_utc": "2026-05-13T02:10:00Z",
    }
    diamonds = [
        {
            "cluster_id": "stophunt_inversion|fibo_xauusd|asia|short|15m",
            "bucket_level": "family_session_dir",
            "bucket_key": ["fibo_xauusd", "asia", "short"],
            "theoretical_direction": "long",
            "window_minutes": 15,
            "samples": 48,
            "profit_factor": 1.84,
            "phase_a_shadow_candidate": True,
        }
    ]

    observations = build_shadow_observations([loss], diamonds, now=datetime(2026, 5, 13, 2, 40, tzinfo=timezone.utc))

    assert observations == []

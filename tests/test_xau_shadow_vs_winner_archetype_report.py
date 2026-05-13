from datetime import datetime, timezone

from ops.xau_shadow_vs_winner_archetype_report import build_comparison, classify_shadow_candidate


def _shadow(
    route="observe_only",
    rr=None,
    tf="M5",
    session="asia",
    entry=3300.0,
    stop=3290.0,
    tp=3330.0,
    anchor=True,
):
    raw = {
        "fibo_mtf_route": route,
        "tf_label": tf,
        "execution_anchor_source": "recent_M5_structure" if anchor else "unavailable",
        "execution_anchor_is_live_plan": False,
    }
    return {
        "route": route,
        "raw": raw,
        "tf": tf,
        "entry": entry,
        "stop_loss": stop,
        "take_profit_1": tp,
        "shadow_pnl_rr": rr,
        "signal_dt": datetime(2026, 5, 12, 3 if session == "asia" else 14, tzinfo=timezone.utc),
        "session_key": f"2026-05-12:{session}",
        "has_real_anchor": anchor,
        "mae_r": 0.8,
    }


def _hist(family="xauusd_scheduled", pnl=5.0, session="asia", entry=3300.0, stop=3290.0, tp=3330.0):
    return {
        "position_id": 1,
        "symbol": "XAUUSD",
        "broker_symbol": "XAUUSD",
        "direction": "long",
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": tp,
        "volume": 1.0,
        "first_seen_utc": "2026-05-12T02:00:00Z" if session == "asia" else "2026-05-12T14:00:00Z",
        "last_seen_utc": "2026-05-12T03:00:00Z",
        "close_utc": "2026-05-12T03:00:00Z",
        "label": f"dexter:XAUUSD:{family}:1",
        "comment": f"dexter|{family}|XAUUSD",
        "pnl_usd": pnl,
        "deal_count": 2,
    }


def test_classify_shadow_candidate_maps_session_anchor_and_risk_geometry():
    item = classify_shadow_candidate(_shadow(session="asia", anchor=True))

    assert item["session"] == "asia"
    assert item["risk_geometry"] == "healthy_2r_to_5r"
    assert item["anchor_usable"] is True
    assert item["live_plan"] is False


def test_build_comparison_scores_shadow_against_winner_and_loser_archetypes():
    shadow_rows = [
        _shadow(session="asia", rr=0.5, anchor=True),
        _shadow(session="ny", rr=-1.0, anchor=False, entry=3300, stop=3290, tp=3370),
    ]
    historical_rows = [
        *[_hist("xauusd_scheduled", pnl=4, session="asia") for _ in range(40)],
        *[_hist("fibo_xauusd", pnl=-3, session="ny", tp=3370) for _ in range(40)],
    ]

    report = build_comparison(shadow_rows, historical_rows)

    assert report["summary"]["shadow_decisions"] == 2
    assert report["summary"]["anchor_usable_rate"] == 0.5
    assert report["by_match_bucket"]["resembles_winner_archetype"]["decisions"] == 1
    assert report["by_match_bucket"]["resembles_loser_archetype"]["decisions"] == 1
    assert report["verdict"]["action"] == "fix_before_counting_days"
    assert "anchor_coverage<0.50" in report["verdict"]["blockers"]

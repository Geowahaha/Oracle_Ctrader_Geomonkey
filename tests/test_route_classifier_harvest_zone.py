import json
from pathlib import Path

from analysis.route_classifier import classify_xau_route

FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "handoff" / "XAU_LOSS_CLUSTER_RAW_SCORES_20260515.json"


def _case(name):
    return json.loads(FIXTURE.read_text())["cases"][name]


def test_row7_row8_loss_cluster_routes_to_harvest_or_wait_not_fresh_entry():
    for name in ("loss_row8_main", "loss_row7_canary"):
        case = _case(name)
        decision = classify_xau_route({
            "symbol": "XAUUSD",
            "direction": "short",
            "source": case["fixture_features"]["source"],
            "confidence": case["fixture_features"]["confidence"],
            "entry_type": case["fixture_features"]["entry_type"],
            "raw_scores": case["raw_scores"],
            "price_near_liquidity_target": True,
            "equal_lows_distance_atr": 0.18,
            "extended_move_atr": 1.45,
            "flow_confirmed": False,
            "impulse_state": "missing_candles",
            "rasg_active": True,
            "existing_position": False,
        })

        assert decision.route in {"harvest_zone_pm_only", "wait_retest_plan"}
        assert decision.action != "allow_entry"
        assert decision.max_legs <= 1
        assert "harvest" in " ".join(decision.reasons) or "wait" in " ".join(decision.reasons)

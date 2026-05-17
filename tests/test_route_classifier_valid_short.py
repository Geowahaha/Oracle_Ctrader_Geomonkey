import json
from pathlib import Path

from analysis.route_classifier import classify_xau_route

FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "handoff" / "XAU_LOSS_CLUSTER_RAW_SCORES_20260515.json"


def _case(name):
    return json.loads(FIXTURE.read_text())["cases"][name]


def test_profitable_short_winner_analogs_are_preserved_not_blanket_blocked():
    for name in ("win_row2_winner", "win_row22_winner", "win_row24_winner", "win_row26_winner"):
        case = _case(name)
        decision = classify_xau_route({
            "symbol": "XAUUSD",
            "direction": "short",
            "source": case["fixture_features"]["source"],
            "confidence": case["fixture_features"]["confidence"],
            "entry_type": case["fixture_features"]["entry_type"],
            "raw_scores": case["raw_scores"],
            "price_near_liquidity_target": False,
            "equal_lows_distance_atr": 1.4,
            "extended_move_atr": 0.35,
            "flow_confirmed": True,
            "impulse_state": "impulse_run",
            "retest_rejection": True,
            "rasg_active": False,
        })

        assert decision.route in {"retest_entry", "breakdown_continuation"}
        assert decision.action == "allow_entry"
        assert decision.max_legs >= 1

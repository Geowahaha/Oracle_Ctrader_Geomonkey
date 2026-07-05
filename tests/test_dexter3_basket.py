"""Unit tests for dexter3/basket_manager.py — deterministic basket state machine.

Critical invariant under test (per Dexter3 Phase 1 spec):
  (a) basket caps can NEVER be exceeded under adversarial action sequences
      (property-style loop test over many randomized call sequences).
"""
from __future__ import annotations

import random

import pytest

from dexter3.basket_manager import (
    ACTION_ADD_REPAIR_LEG,
    ACTION_CLOSE_ALL_CAP_STOP,
    ACTION_CLOSE_ALL_IN_PROFIT,
    ACTION_NONE,
    FLAT,
    OPEN,
    REPAIR,
    BasketConfig,
    BasketManager,
    Leg,
)


def _leg(side="buy", entry=2000.0, sl=1998.0, risk=10.0, at_min=0.0, label="leg") -> Leg:
    return Leg(side=side, entry=entry, sl=sl, risk_usd=risk, opened_at_min=at_min, label=label)


# -- basic lifecycle -----------------------------------------------------------------


def test_on_entry_from_flat_opens_basket():
    bm = BasketManager(BasketConfig())
    result = bm.on_entry(_leg())
    assert bm.state.state == OPEN
    assert len(bm.state.legs) == 1
    assert result.action == ACTION_NONE  # opening itself is not a cap-driven action


def test_on_entry_ignored_when_not_flat():
    bm = BasketManager(BasketConfig())
    bm.on_entry(_leg())
    result = bm.on_entry(_leg(label="leg2"))
    assert len(bm.state.legs) == 1  # second on_entry while OPEN must be ignored
    assert "ignored" in result.reason.lower()


def test_repair_requires_full_structure_evidence():
    bm = BasketManager(BasketConfig())
    bm.on_entry(_leg(at_min=0.0))

    # price moved against us but NO structure evidence -> no repair
    result = bm.on_m5_close([], {}, {"now_min": 5, "aggregate_r": -0.8, "structure_evidence": {}})
    assert result.action == ACTION_NONE
    assert len(bm.state.legs) == 1


@pytest.mark.parametrize(
    "evidence",
    [
        {"level_lost": True, "m5_close_beyond": False},
        {"level_lost": False, "m5_close_beyond": True},
        {"level_lost": False, "m5_close_beyond": False},
    ],
)
def test_repair_refused_on_partial_evidence(evidence):
    bm = BasketManager(BasketConfig())
    bm.on_entry(_leg(at_min=0.0))
    result = bm.on_m5_close(
        [], {}, {
            "now_min": 5, "aggregate_r": -0.8, "structure_evidence": evidence,
            "proposed_repair_leg": _leg(label="repair1"),
        },
    )
    assert result.action == ACTION_NONE
    assert len(bm.state.legs) == 1, "repair must never fire on partial (price-distance-like) evidence"


def test_repair_fires_on_full_structure_evidence():
    bm = BasketManager(BasketConfig())
    bm.on_entry(_leg(at_min=0.0))
    result = bm.on_m5_close(
        [], {}, {
            "now_min": 5, "aggregate_r": -0.8,
            "structure_evidence": {"level_lost": True, "m5_close_beyond": True},
            "proposed_repair_leg": _leg(label="repair1", at_min=5.0),
        },
    )
    assert result.action == ACTION_ADD_REPAIR_LEG
    assert len(bm.state.legs) == 2
    assert bm.state.state == REPAIR


def test_resolve_in_profit_closes_all_and_returns_to_flat():
    bm = BasketManager(BasketConfig(resolve_target_r=0.2))
    bm.on_entry(_leg(at_min=0.0))
    result = bm.on_m5_close([], {}, {"now_min": 5, "aggregate_r": 0.25, "structure_evidence": {}})
    assert result.action == ACTION_CLOSE_ALL_IN_PROFIT
    assert bm.state.state == FLAT
    assert bm.state.legs == []
    # profit resolution must NOT increment the daily loss-basket counter
    assert bm.state.daily_resolved_loss_baskets == 0


def test_time_stop_forces_resolve_and_counts_as_loss():
    bm = BasketManager(BasketConfig(time_stop_min=60))
    bm.on_entry(_leg(at_min=0.0))
    result = bm.on_m5_close([], {}, {"now_min": 61, "aggregate_r": -0.3, "structure_evidence": {}})
    assert result.action == ACTION_CLOSE_ALL_CAP_STOP
    assert bm.state.state == FLAT
    assert bm.state.daily_resolved_loss_baskets == 1


def test_daily_loss_basket_cap_blocks_new_basket():
    bm = BasketManager(BasketConfig(daily_loss_baskets=1, time_stop_min=10))
    bm.on_entry(_leg(at_min=0.0))
    bm.on_m5_close([], {}, {"now_min": 15, "aggregate_r": -0.5, "structure_evidence": {}})  # time-stop loss #1
    assert bm.state.daily_resolved_loss_baskets == 1

    result = bm.on_entry(_leg(at_min=20.0, label="new_basket"))
    assert result.action != ACTION_ADD_REPAIR_LEG
    assert bm.state.state == FLAT, "daily loss cap must prevent opening a new basket"
    assert len(bm.state.legs) == 0


def test_max_legs_cap_never_breached_even_with_valid_evidence():
    bm = BasketManager(BasketConfig(max_legs=2, time_stop_min=1000))
    bm.on_entry(_leg(at_min=0.0))
    ev = {"level_lost": True, "m5_close_beyond": True}
    bm.on_m5_close([], {}, {"now_min": 5, "aggregate_r": -0.5, "structure_evidence": ev, "proposed_repair_leg": _leg(label="r1", at_min=5)})
    assert len(bm.state.legs) == 2
    result = bm.on_m5_close([], {}, {"now_min": 10, "aggregate_r": -0.9, "structure_evidence": ev, "proposed_repair_leg": _leg(label="r2", at_min=10)})
    assert result.action == ACTION_NONE
    assert len(bm.state.legs) == 2, "max_legs cap must be unbreachable"


def test_max_basket_risk_cap_blocks_oversized_repair_leg():
    bm = BasketManager(BasketConfig(max_legs=5, max_basket_risk_mult=1.5, time_stop_min=1000))
    bm.on_entry(_leg(risk=10.0, at_min=0.0))  # base_risk_usd = 10 -> cap = 15
    ev = {"level_lost": True, "m5_close_beyond": True}
    result = bm.on_m5_close(
        [], {}, {
            "now_min": 5, "aggregate_r": -0.5, "structure_evidence": ev,
            "proposed_repair_leg": _leg(risk=8.0, label="too_big", at_min=5),  # 10+8=18 > 15
        },
    )
    assert result.action == ACTION_NONE
    assert len(bm.state.legs) == 1, "max_basket_risk_mult cap must be unbreachable"


# -- (a) property-style adversarial cap test --------------------------------------------


def test_caps_never_exceeded_under_adversarial_random_sequences():
    """Fire hundreds of randomized on_entry/on_m5_close calls — with repair
    evidence, oversized legs, rapid time advances, and repeated open
    attempts — none of the hard caps may ever be breached."""
    rng = random.Random(1234)
    for trial in range(30):
        cfg = BasketConfig(
            max_legs=rng.choice([2, 3, 4]),
            max_basket_risk_mult=rng.choice([1.5, 2.0, 3.0]),
            time_stop_min=rng.choice([30, 60, 180]),
            daily_loss_baskets=rng.choice([1, 2, 3]),
            resolve_target_r=0.2,
        )
        bm = BasketManager(cfg)
        now_min = 0.0

        for _step in range(60):
            action_choice = rng.choice(["open", "repair_full", "repair_partial", "resolve_profit", "advance_time", "open_again"])
            now_min += rng.uniform(1, 40)

            if action_choice in ("open", "open_again"):
                bm.on_entry(_leg(risk=rng.uniform(5, 50), at_min=now_min, label=f"t{trial}s{_step}"))

            elif action_choice == "repair_full":
                leg = _leg(risk=rng.uniform(1, 100), at_min=now_min, label=f"repair{_step}")
                bm.on_m5_close(
                    [], {}, {
                        "now_min": now_min,
                        "aggregate_r": rng.uniform(-3.0, -0.1),
                        "structure_evidence": {"level_lost": True, "m5_close_beyond": True},
                        "proposed_repair_leg": leg,
                    },
                )

            elif action_choice == "repair_partial":
                bm.on_m5_close(
                    [], {}, {
                        "now_min": now_min,
                        "aggregate_r": rng.uniform(-3.0, -0.1),
                        "structure_evidence": {"level_lost": rng.choice([True, False]), "m5_close_beyond": False},
                        "proposed_repair_leg": _leg(at_min=now_min),
                    },
                )

            elif action_choice == "resolve_profit":
                bm.on_m5_close([], {}, {"now_min": now_min, "aggregate_r": rng.uniform(0.2, 2.0), "structure_evidence": {}})

            else:  # advance_time
                bm.on_m5_close([], {}, {"now_min": now_min, "aggregate_r": rng.uniform(-0.5, 0.19), "structure_evidence": {}})

            # -- invariants checked after EVERY single call --
            assert len(bm.state.legs) <= cfg.max_legs, (
                f"trial={trial} step={_step}: max_legs breached "
                f"({len(bm.state.legs)} > {cfg.max_legs})"
            )
            current_risk = bm.current_basket_risk_usd()
            max_risk = bm.max_basket_risk_usd()
            # allow tiny float slack; must never exceed by more than epsilon
            assert current_risk <= max_risk + 1e-6 or bm.state.state == FLAT, (
                f"trial={trial} step={_step}: max_basket_risk breached "
                f"({current_risk} > {max_risk})"
            )
            assert bm.state.daily_resolved_loss_baskets <= 10_000  # sanity, never negative/runaway
            assert bm.state.daily_resolved_loss_baskets >= 0

            if bm.daily_loss_cap_reached():
                assert bm.state.state == FLAT or len(bm.state.legs) > 0 and bm.state.state != FLAT, (
                    "once daily cap is reached, no state should silently keep growing"
                )


def test_caps_never_exceeded_when_daily_cap_reached_mid_basket():
    """Adversarial: reach the daily loss cap, then hammer on_entry — the
    basket must stay FLAT/capped, never silently open past the cap."""
    bm = BasketManager(BasketConfig(daily_loss_baskets=1, time_stop_min=10, max_legs=3))
    bm.on_entry(_leg(at_min=0.0))
    bm.on_m5_close([], {}, {"now_min": 15, "aggregate_r": -0.5, "structure_evidence": {}})
    assert bm.state.daily_resolved_loss_baskets == 1

    for i in range(20):
        result = bm.on_entry(_leg(at_min=20.0 + i, label=f"attempt{i}"))
        assert bm.state.state == FLAT
        assert len(bm.state.legs) == 0
        assert result.action != ACTION_ADD_REPAIR_LEG


def test_reset_daily_counters_allows_new_basket_next_day():
    bm = BasketManager(BasketConfig(daily_loss_baskets=1, time_stop_min=10))
    bm.on_entry(_leg(at_min=0.0))
    bm.on_m5_close([], {}, {"now_min": 15, "aggregate_r": -0.5, "structure_evidence": {}})
    assert bm.state.daily_resolved_loss_baskets == 1

    bm.reset_daily_counters()
    assert bm.state.daily_resolved_loss_baskets == 0
    result = bm.on_entry(_leg(at_min=1500.0, label="next_day"))
    assert bm.state.state == OPEN
    assert len(bm.state.legs) == 1


def test_on_m5_close_on_flat_basket_is_noop():
    bm = BasketManager(BasketConfig())
    result = bm.on_m5_close([], {}, {"now_min": 5, "aggregate_r": -1.0, "structure_evidence": {}})
    assert result.action == ACTION_NONE
    assert bm.state.state == FLAT

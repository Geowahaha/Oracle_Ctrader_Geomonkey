from types import SimpleNamespace


def _reclaim_raw(**overrides):
    raw = {
        "prior_impulse_direction": "short",
        "structure_break_direction": "long",
        "retest_rejection_direction": "long",
        "base_bars": 5,
        "base_compression_ratio": 0.62,
        "dema_reclaim": True,
        "dema_hold": True,
        "delta_proxy": 0.16,
        "depth_imbalance": 0.06,
        "mid_drift_pct": 0.006,
        "tick_up_ratio": 0.64,
        "bar_volume_proxy": 0.55,
        "rejection_ratio": 0.18,
    }
    raw.update(overrides)
    return raw


def test_reclaim_score_detects_red_to_blue_base_reclaim():
    from analysis.xau_reclaim_staircase import score_reclaim_features

    score, phase, reasons = score_reclaim_features(_reclaim_raw(), direction="long")

    assert score >= 62
    assert phase in {"base_reclaim", "staircase"}
    assert "structure_break_reclaim" in reasons
    assert "dema_reclaim" in reasons


def test_reclaim_decision_shadow_mode_does_not_bypass_live_conf_gate():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4535.0,
        stop_loss=4525.0,
        take_profit_1=4568.0,
        raw_scores=_reclaim_raw(),
    )

    decision = decision_from_signal(
        signal,
        source="xauusd_scheduled",
        enabled=False,
        shadow_only=True,
        min_score=62,
        min_rr=3.0,
    )

    assert decision.active is True
    assert decision.shadow_only is True
    assert decision.bypass_conf_below is False
    assert decision.planned_rr >= 3.0


def test_reclaim_decision_can_bypass_scheduled_conf_only_when_enabled_live_and_rr_ok():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4535.0,
        stop_loss=4525.0,
        take_profit_1=4568.0,
        raw_scores=_reclaim_raw(),
    )

    decision = decision_from_signal(
        signal,
        source="xauusd_scheduled:canary",
        enabled=True,
        shadow_only=False,
        min_score=62,
        min_rr=3.0,
    )

    assert decision.active is True
    assert decision.bypass_conf_below is True
    assert decision.reason


def test_reclaim_decision_refuses_bypass_when_rr_below_minimum():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4535.0,
        stop_loss=4525.0,
        take_profit_1=4545.0,
        raw_scores=_reclaim_raw(),
    )

    decision = decision_from_signal(
        signal,
        source="xauusd_scheduled",
        enabled=True,
        shadow_only=False,
        min_score=62,
        min_rr=3.0,
    )

    assert decision.active is True
    assert decision.planned_rr == 1.0
    assert decision.bypass_conf_below is False
    assert decision.reason.startswith("rr_below_min")


def test_reclaim_decision_refuses_bypass_when_score_below_minimum():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4535.0,
        stop_loss=4525.0,
        take_profit_1=4568.0,
        raw_scores=_reclaim_raw(
            structure_break_direction="",
            retest_rejection_direction="",
            dema_reclaim=False,
            dema_hold=False,
            base_compression_ratio=0.95,
            base_bars=1,
            delta_proxy=0.0,
            depth_imbalance=0.0,
            mid_drift_pct=0.0,
            tick_up_ratio=0.5,
            bar_volume_proxy=0.0,
            rejection_ratio=0.0,
        ),
    )

    decision = decision_from_signal(signal, source="xauusd_scheduled", enabled=True, shadow_only=False, min_score=62, min_rr=3.0)

    assert decision.active is False
    assert decision.bypass_conf_below is False
    assert decision.score < 62


def test_untrusted_explicit_score_does_not_bypass_microstructure_checks():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4535.0,
        stop_loss=4525.0,
        take_profit_1=4568.0,
        raw_scores=_reclaim_raw(
            xau_reclaim_v3_score=99,
            xau_reclaim_v3_phase="staircase",
            structure_break_direction="",
            retest_rejection_direction="",
            dema_reclaim=False,
            dema_hold=False,
            base_compression_ratio=0.95,
            base_bars=1,
            delta_proxy=0.0,
            depth_imbalance=0.0,
            mid_drift_pct=0.0,
            tick_up_ratio=0.5,
            bar_volume_proxy=0.0,
            rejection_ratio=0.0,
        ),
    )

    decision = decision_from_signal(signal, source="xauusd_scheduled", enabled=True, shadow_only=False, min_score=62, min_rr=3.0)

    assert decision.active is False
    assert decision.score < 62


def test_winner_partial_override_suppressed_in_shadow_mode():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4560.0,
        stop_loss=4550.0,
        take_profit_1=4592.0,
        raw_scores=_reclaim_raw(higher_high_higher_low=True),
    )

    decision = decision_from_signal(
        signal,
        source="scalp_xauusd:winner",
        enabled=True,
        shadow_only=True,
        min_score=62,
        min_rr=3.0,
        winner_override=True,
    )

    assert decision.active is True
    assert decision.shadow_only is True
    assert decision.winner_partial_override is False


def test_winner_partial_override_only_for_staircase_with_flow_and_enabled_live():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4560.0,
        stop_loss=4550.0,
        take_profit_1=4592.0,
        raw_scores=_reclaim_raw(higher_high_higher_low=True, xau_reclaim_v3_phase="staircase", xau_reclaim_v3_score=76),
    )

    decision = decision_from_signal(
        signal,
        source="scalp_xauusd:winner",
        enabled=True,
        shadow_only=False,
        min_score=62,
        min_rr=3.0,
        winner_override=True,
    )

    assert decision.winner_partial_override is True
    assert decision.phase == "staircase"


def test_tf_alignment_multiplier_caps_and_never_penalizes():
    from analysis.xau_reclaim_staircase import tf_alignment_multiplier

    assert tf_alignment_multiplier({}) == 1.0
    mult = tf_alignment_multiplier(
        {
            "m15_trend_agree": True,
            "h1_trend_agree": True,
            "h4_not_opposing": True,
            "dema_hold": True,
        },
        max_mult=1.75,
    )
    assert mult == 1.75


def test_apply_bonus_and_risk_are_idempotent_and_use_family_risk_only():
    from analysis.xau_reclaim_staircase import apply_confidence_bonus, apply_risk_multiplier, decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        confidence=74.0,
        entry=4560.0,
        stop_loss=4550.0,
        take_profit_1=4592.0,
        raw_scores=_reclaim_raw(higher_high_higher_low=True, ctrader_risk_usd_override=2.5),
    )
    decision = decision_from_signal(signal, source="scalp_xauusd:winner", enabled=True, shadow_only=False)

    apply_confidence_bonus(signal, decision, cap=85.0)
    apply_confidence_bonus(signal, decision, cap=85.0)
    apply_risk_multiplier(signal, decision)
    apply_risk_multiplier(signal, decision)

    assert signal.confidence == 76.5
    assert signal.raw_scores["ctrader_risk_usd_override"] <= 2.5 * 1.75
    assert signal.raw_scores["xau_reclaim_v3_conf_before"] == 74.0
    assert signal.raw_scores["xau_reclaim_v3_risk_before_usd"] == 2.5


def test_apply_risk_refuses_synthetic_default_when_family_override_missing():
    from analysis.xau_reclaim_staircase import apply_risk_multiplier, decision_from_signal

    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        entry=4560.0,
        stop_loss=4550.0,
        take_profit_1=4592.0,
        raw_scores=_reclaim_raw(higher_high_higher_low=True),
    )
    decision = decision_from_signal(signal, source="scalp_xauusd:winner", enabled=True, shadow_only=False)

    raw = apply_risk_multiplier(signal, decision, default_risk_usd=10.0)

    assert "ctrader_risk_usd_override" not in raw
    assert raw["xau_reclaim_v3_risk_skipped"] == "missing_family_risk_override"


def test_reclaim_scopes_to_nonfibo_xau_only():
    from analysis.xau_reclaim_staircase import decision_from_signal

    signal = SimpleNamespace(symbol="BTCUSD", direction="long", raw_scores=_reclaim_raw())
    decision = decision_from_signal(signal, source="scalp_btcusd:winner", enabled=True, shadow_only=False)
    fibo_like = decision_from_signal(signal, source="xauusd_scheduled:fibo_repair", enabled=True, shadow_only=False)
    fibo_prefix = decision_from_signal(signal, source="xauusd_fibo", enabled=True, shadow_only=False)
    accidental_prefix = decision_from_signal(signal, source="scalp_xauusd_canary", enabled=True, shadow_only=False)

    assert decision.active is False
    assert decision.reason == "source_not_in_scope"
    assert fibo_like.active is False
    assert fibo_like.reason == "source_not_in_scope"
    assert fibo_prefix.active is False
    assert fibo_prefix.reason == "source_not_in_scope"
    assert accidental_prefix.active is False
    assert accidental_prefix.reason == "source_not_in_scope"

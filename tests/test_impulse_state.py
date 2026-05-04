from analysis.impulse_state import ImpulseStateName, compute_impulse_state


def test_detects_bullish_impulse_start_after_sweep_continuation():
    candles = [
        {"open": 4598.0, "high": 4600.0, "low": 4596.0, "close": 4599.0, "volume": 100},
        {"open": 4599.0, "high": 4601.0, "low": 4597.0, "close": 4600.0, "volume": 95},
        {"open": 4600.0, "high": 4602.0, "low": 4595.0, "close": 4601.0, "volume": 110},
        {"open": 4601.0, "high": 4601.5, "low": 4598.0, "close": 4600.0, "volume": 105},
        {"open": 4600.0, "high": 4602.0, "low": 4597.5, "close": 4601.0, "volume": 115},
        {"open": 4601.0, "high": 4601.8, "low": 4598.5, "close": 4600.5, "volume": 100},
        {"open": 4600.5, "high": 4602.0, "low": 4599.0, "close": 4601.5, "volume": 108},
        # Sweeps below the range low, then closes strongly above the range high.
        {"open": 4601.0, "high": 4610.0, "low": 4594.0, "close": 4608.0, "volume": 260, "delta_proxy": 2.4, "tick_up_ratio": 0.72},
        # Follow-through does not retrace more than 50% of the breakout candle.
        {"open": 4608.0, "high": 4618.0, "low": 4606.0, "close": 4616.0, "volume": 280, "delta_proxy": 2.1, "tick_up_ratio": 0.69},
    ]

    state = compute_impulse_state(
        candles,
        current_direction="long",
        entry_sharpness=68,
        higher_tf_bias="long",
    )

    assert state.name is ImpulseStateName.IMPULSE_START
    assert state.direction == "long"
    assert state.aligned_with("long") is True
    assert state.blocks_direction("short") is True
    assert "sweep_continuation" in state.reasons


def test_detects_bullish_correction_end_as_resume_not_reversal():
    candles = [
        {"open": 4500, "high": 4510, "low": 4498, "close": 4508, "volume": 100},
        {"open": 4508, "high": 4562, "low": 4507, "close": 4560, "volume": 250, "delta_proxy": 2.0, "tick_up_ratio": 0.70},
        {"open": 4560, "high": 4564, "low": 4540, "close": 4545, "volume": 140, "delta_proxy": -0.4, "tick_up_ratio": 0.44},
        {"open": 4545, "high": 4550, "low": 4538, "close": 4542, "volume": 130, "delta_proxy": -0.3, "tick_up_ratio": 0.46},
        {"open": 4542, "high": 4546, "low": 4536, "close": 4538, "volume": 125, "delta_proxy": -0.4, "tick_up_ratio": 0.43},
        {"open": 4538, "high": 4540, "low": 4532, "close": 4535, "volume": 118, "delta_proxy": -0.5, "tick_up_ratio": 0.42},
        {"open": 4535, "high": 4538, "low": 4530, "close": 4532, "volume": 115, "delta_proxy": -0.6, "tick_up_ratio": 0.40},
        # Retraces near 61.8% of impulse and rejects upward.
        {"open": 4532, "high": 4554, "low": 4529, "close": 4551, "volume": 190, "delta_proxy": 1.4, "tick_up_ratio": 0.66},
    ]

    state = compute_impulse_state(
        candles,
        prior_impulse_direction="long",
        prior_impulse_start=4500,
        prior_impulse_end=4560,
        current_direction="long",
        entry_sharpness=64,
        higher_tf_bias="long",
    )

    assert state.name is ImpulseStateName.RESUME
    assert state.direction == "long"
    assert "correction_end" in state.reasons
    assert state.blocks_direction("short") is True


def test_blocks_counter_impulse_entry_without_reversal_confirmation():
    candles = [
        {"open": 4610, "high": 4612, "low": 4606, "close": 4608, "volume": 100},
        {"open": 4608, "high": 4610, "low": 4604, "close": 4606, "volume": 95},
        {"open": 4606, "high": 4608, "low": 4601, "close": 4603, "volume": 105},
        {"open": 4603, "high": 4606, "low": 4598, "close": 4600, "volume": 110},
        {"open": 4600, "high": 4602, "low": 4596, "close": 4598, "volume": 100},
        {"open": 4598, "high": 4600, "low": 4594, "close": 4596, "volume": 98},
        {"open": 4596, "high": 4598, "low": 4592, "close": 4594, "volume": 102},
        {"open": 4594, "high": 4597, "low": 4558, "close": 4562, "volume": 300, "delta_proxy": -2.0, "tick_up_ratio": 0.28},
        # Small bounce is correction only, not confirmed reversal.
        {"open": 4562, "high": 4574, "low": 4560, "close": 4569, "volume": 130, "delta_proxy": 0.5, "tick_up_ratio": 0.55},
    ]

    state = compute_impulse_state(
        candles,
        current_direction="long",
        entry_sharpness=70,
        higher_tf_bias="short",
    )

    assert state.name in {ImpulseStateName.IMPULSE_START, ImpulseStateName.IMPULSE_RUN}
    assert state.direction == "short"
    assert state.aligned_with("long") is False
    assert state.blocks_direction("long") is True


def test_allows_opposite_entry_only_after_reversal_confirmation():
    candles = [
        {"open": 4610, "high": 4612, "low": 4606, "close": 4608, "volume": 100},
        {"open": 4608, "high": 4610, "low": 4604, "close": 4606, "volume": 95},
        {"open": 4606, "high": 4608, "low": 4601, "close": 4603, "volume": 105},
        {"open": 4603, "high": 4606, "low": 4598, "close": 4600, "volume": 110},
        {"open": 4600, "high": 4602, "low": 4596, "close": 4598, "volume": 100},
        {"open": 4598, "high": 4600, "low": 4594, "close": 4596, "volume": 98},
        {"open": 4610, "high": 4612, "low": 4580, "close": 4585, "volume": 260, "delta_proxy": -2.0, "tick_up_ratio": 0.25},
        {"open": 4585, "high": 4590, "low": 4550, "close": 4558, "volume": 280, "delta_proxy": -2.1, "tick_up_ratio": 0.27},
        # Structure break against the prior short impulse.
        {"open": 4558, "high": 4598, "low": 4556, "close": 4594, "volume": 310, "delta_proxy": 2.2, "tick_up_ratio": 0.74},
        # Retest and reject with new-side flow.
        {"open": 4594, "high": 4602, "low": 4584, "close": 4600, "volume": 240, "delta_proxy": 1.8, "tick_up_ratio": 0.70},
    ]

    state = compute_impulse_state(
        candles,
        prior_impulse_direction="short",
        current_direction="long",
        entry_sharpness=72,
        higher_tf_bias="neutral",
        structure_break_direction="long",
        retest_rejection_direction="long",
    )

    assert state.name is ImpulseStateName.REVERSAL
    assert state.direction == "long"
    assert state.blocks_direction("long") is False
    assert "reversal_confirmed" in state.reasons


def test_zero_tick_ratio_is_valid_short_flow_not_defaulted_to_neutral():
    candles = [
        {"open": 4610, "high": 4612, "low": 4608, "close": 4610, "volume": 100},
        {"open": 4610, "high": 4611, "low": 4607, "close": 4609, "volume": 100},
        {"open": 4609, "high": 4610, "low": 4606, "close": 4608, "volume": 100},
        {"open": 4608, "high": 4610, "low": 4605, "close": 4607, "volume": 100},
        {"open": 4607, "high": 4609, "low": 4604, "close": 4606, "volume": 100},
        {"open": 4606, "high": 4608, "low": 4603, "close": 4605, "volume": 100},
        {"open": 4605, "high": 4607, "low": 4602, "close": 4604, "volume": 100},
        # delta is neutral; only tick_up_ratio=0.0 proves one-sided short flow.
        {"open": 4604, "high": 4606, "low": 4588, "close": 4590, "volume": 260, "delta_proxy": 0.0, "tick_up_ratio": 0.0},
    ]

    state = compute_impulse_state(candles, entry_sharpness=65, higher_tf_bias="short")

    assert state.direction == "short"
    assert "flow_expansion" in state.reasons


def test_directional_expansion_without_break_flow_or_sweep_stays_idle():
    candles = [
        {"open": 4590, "high": 4620, "low": 4580, "close": 4600, "volume": 100},
        {"open": 4600, "high": 4620, "low": 4582, "close": 4605, "volume": 100},
        {"open": 4605, "high": 4620, "low": 4584, "close": 4610, "volume": 100},
        {"open": 4610, "high": 4620, "low": 4586, "close": 4615, "volume": 100},
        {"open": 4615, "high": 4620, "low": 4588, "close": 4616, "volume": 100},
        {"open": 4595, "high": 4619, "low": 4590, "close": 4610, "volume": 100, "delta_proxy": 0.2, "tick_up_ratio": 0.55},
        {"open": 4610, "high": 4619, "low": 4595, "close": 4616, "volume": 100, "delta_proxy": 0.2, "tick_up_ratio": 0.55},
        {"open": 4616, "high": 4619, "low": 4600, "close": 4618, "volume": 100, "delta_proxy": 0.2, "tick_up_ratio": 0.55},
    ]

    state = compute_impulse_state(candles, entry_sharpness=80, higher_tf_bias="long")

    assert state.name is ImpulseStateName.IDLE


def test_deep_pullback_is_not_preserved_as_impulse_run():
    candles = [
        {"open": 4610, "high": 4612, "low": 4606, "close": 4608, "volume": 100},
        {"open": 4608, "high": 4610, "low": 4604, "close": 4606, "volume": 95},
        {"open": 4606, "high": 4608, "low": 4601, "close": 4603, "volume": 105},
        {"open": 4603, "high": 4606, "low": 4598, "close": 4600, "volume": 110},
        {"open": 4600, "high": 4602, "low": 4596, "close": 4598, "volume": 100},
        {"open": 4598, "high": 4600, "low": 4594, "close": 4596, "volume": 98},
        {"open": 4596, "high": 4598, "low": 4592, "close": 4594, "volume": 102},
        {"open": 4594, "high": 4597, "low": 4558, "close": 4562, "volume": 300, "delta_proxy": -2.0, "tick_up_ratio": 0.28},
        # Deep pullback beyond 50% of the impulse body; not safe to keep blocking as an impulse run.
        {"open": 4562, "high": 4581, "low": 4560, "close": 4580, "volume": 180, "delta_proxy": 1.2, "tick_up_ratio": 0.63},
    ]

    state = compute_impulse_state(candles, current_direction="long", entry_sharpness=70, higher_tf_bias="short")

    assert state.name is not ImpulseStateName.IMPULSE_RUN

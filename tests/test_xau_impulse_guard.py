from types import SimpleNamespace


class DummyConfig:
    XAU_IMPULSE_GUARD_ENABLED = True
    XAU_IMPULSE_GUARD_MIN_CONFIDENCE = 0.70
    XAU_IMPULSE_GUARD_BLOCK_STATES = "impulse_run"


class DisabledConfig(DummyConfig):
    XAU_IMPULSE_GUARD_ENABLED = False


def _signal(direction="short", shadow=None, symbol="XAUUSD"):
    raw = {}
    if shadow is not None:
        raw["xau_impulse_shadow"] = shadow
    return SimpleNamespace(symbol=symbol, direction=direction, confidence=72.0, raw_scores=raw)


def _shadow(**overrides):
    payload = {
        "enabled": True,
        "status": "ok",
        "state": "impulse_run",
        "direction": "long",
        "confidence": 0.82,
        "block_direction": "short",
        "reasons": ("range_break",),
    }
    payload.update(overrides)
    return payload


def test_guard_blocks_matching_counter_impulse_and_marks_raw_scores():
    from analysis.xau_impulse_guard import evaluate_xau_impulse_guard

    signal = _signal(direction="short", shadow=_shadow())
    decision = evaluate_xau_impulse_guard(signal, config=DummyConfig)

    assert decision.blocked is True
    assert decision.reason.startswith("xau_impulse_guard:")
    assert signal.raw_scores["xau_impulse_guard_blocked"] is True
    assert signal.raw_scores["xau_impulse_guard_reason"] == decision.reason


def test_guard_disabled_is_noop_and_sets_no_block_field():
    from analysis.xau_impulse_guard import evaluate_xau_impulse_guard

    signal = _signal(direction="short", shadow=_shadow())
    decision = evaluate_xau_impulse_guard(signal, config=DisabledConfig)

    assert decision.blocked is False
    assert decision.reason == "disabled"
    assert "xau_impulse_guard_blocked" not in signal.raw_scores


def test_guard_negative_cases_do_not_block():
    from analysis.xau_impulse_guard import evaluate_xau_impulse_guard

    cases = [
        _signal(symbol="BTCUSD", direction="short", shadow=_shadow()),
        _signal(direction="long", shadow=_shadow()),
        _signal(direction="short", shadow=_shadow(status="missing_candles")),
        _signal(direction="short", shadow=_shadow(confidence=0.69)),
        _signal(direction="short", shadow=_shadow(state="impulse_start")),
        _signal(direction="short", shadow=_shadow(block_direction="")),
    ]

    for signal in cases:
        decision = evaluate_xau_impulse_guard(signal, config=DummyConfig)
        assert decision.blocked is False
        assert signal.raw_scores.get("xau_impulse_guard_blocked") is not True


def test_guard_can_reuse_existing_payload_without_reannotation():
    from analysis.xau_impulse_guard import evaluate_xau_impulse_guard

    def fail_annotate(*args, **kwargs):
        raise AssertionError("should not reannotate when payload exists")

    signal = _signal(direction="short", shadow=_shadow())
    decision = evaluate_xau_impulse_guard(signal, config=DummyConfig, annotate_fn=fail_annotate)

    assert decision.blocked is True


def test_guard_reannotates_when_shadow_payload_missing():
    from analysis.xau_impulse_guard import evaluate_xau_impulse_guard

    calls = []

    def annotate(signal, **kwargs):
        calls.append(kwargs)
        payload = _shadow()
        raw = dict(signal.raw_scores or {})
        raw["xau_impulse_shadow"] = payload
        signal.raw_scores = raw
        return payload

    signal = _signal(direction="short", shadow=None)
    decision = evaluate_xau_impulse_guard(
        signal,
        config=DummyConfig,
        annotate_fn=annotate,
        source="xauusd_scheduled",
        stage="candidate",
    )

    assert calls
    assert calls[0]["source"] == "xauusd_scheduled"
    assert decision.blocked is True
    assert signal.raw_scores["xau_impulse_shadow"]["state"] == "impulse_run"

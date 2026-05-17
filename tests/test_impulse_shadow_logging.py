from types import SimpleNamespace


def _bullish_candles():
    return [
        {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.4, "volume": 100},
        {"open": 100.4, "high": 101.2, "low": 100.0, "close": 100.9, "volume": 110},
        {"open": 100.9, "high": 101.8, "low": 100.7, "close": 101.4, "volume": 115},
        {"open": 101.4, "high": 102.1, "low": 101.0, "close": 101.8, "volume": 120},
        {"open": 101.8, "high": 103.0, "low": 101.6, "close": 102.7, "volume": 160},
        {"open": 102.7, "high": 104.2, "low": 102.5, "close": 103.8, "volume": 190},
        {"open": 103.8, "high": 105.6, "low": 103.6, "close": 105.1, "volume": 220},
        {
            "open": 105.1,
            "high": 107.8,
            "low": 104.9,
            "close": 107.4,
            "volume": 260,
            "swept_low": True,
            "tick_up_ratio": 0.75,
        },
    ]


class CapturingLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, message, *args):
        self.infos.append(message % args if args else message)

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


def test_shadow_logging_adds_raw_scores_without_changing_signal_decision_fields():
    from analysis.impulse_shadow_log import annotate_xau_impulse_shadow

    logger = CapturingLogger()
    signal = SimpleNamespace(
        symbol="XAUUSD",
        direction="long",
        confidence=77.0,
        entry=107.4,
        stop_loss=104.5,
        take_profit_2=113.2,
        raw_scores={
            "xau_impulse_candles": _bullish_candles(),
            "entry_sharpness_score": 82,
            "signal_h1_trend": "bullish",
            "strategy_family": "xau_scheduled_trend",
        },
    )

    before_decision_fields = {
        "direction": signal.direction,
        "confidence": signal.confidence,
        "entry": signal.entry,
        "stop_loss": signal.stop_loss,
        "take_profit_2": signal.take_profit_2,
    }

    shadow = annotate_xau_impulse_shadow(
        signal,
        source="xauusd_scheduled",
        stage="candidate",
        logger=logger,
        now_iso="2026-05-04T05:40:00Z",
    )

    after_decision_fields = {
        "direction": signal.direction,
        "confidence": signal.confidence,
        "entry": signal.entry,
        "stop_loss": signal.stop_loss,
        "take_profit_2": signal.take_profit_2,
    }
    assert after_decision_fields == before_decision_fields
    assert shadow["enabled"] is True
    assert shadow["symbol"] == "XAUUSD"
    assert shadow["source"] == "xauusd_scheduled"
    assert shadow["signal_direction"] == "long"
    assert shadow["state"] in {"impulse_start", "impulse_run"}
    assert shadow["direction"] == "long"
    assert shadow["block_direction"] == "short"
    assert shadow["stage"] == "candidate"
    assert signal.raw_scores["xau_impulse_shadow"] == shadow
    assert any("[XAUImpulseShadow]" in line for line in logger.infos)


def test_shadow_logging_is_noop_for_non_xau_signal():
    from analysis.impulse_shadow_log import annotate_xau_impulse_shadow

    signal = SimpleNamespace(symbol="BTCUSD", direction="long", confidence=80.0, raw_scores={})
    shadow = annotate_xau_impulse_shadow(signal, source="scalp_btcusd")

    assert shadow["enabled"] is False
    assert shadow["reason"] == "not_xau"
    assert signal.raw_scores == {}


def test_shadow_logging_records_missing_candles_without_blocking_or_throwing():
    from analysis.impulse_shadow_log import annotate_xau_impulse_shadow

    logger = CapturingLogger()
    signal = SimpleNamespace(symbol="XAUUSD", direction="short", confidence=64.0, raw_scores={})

    shadow = annotate_xau_impulse_shadow(signal, source="xauusd_scheduled", logger=logger)

    assert shadow["enabled"] is True
    assert shadow["status"] == "missing_candles"
    assert shadow["state"] == "idle"
    assert shadow["signal_direction"] == "short"
    assert signal.raw_scores["xau_impulse_shadow"]["status"] == "missing_candles"
    assert logger.warnings == []

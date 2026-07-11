from dexter3.vp_regime import profile_regime


def test_profile_regime_is_causal_and_classifies_directional_volume():
    bars = [{"close": 100 + i, "volume": 10} for i in range(47)] + [{"close": 147, "volume": 25}]
    out = profile_regime(bars)
    assert out["state"] == "directional"
    assert out["volume_ratio"] > 2


def test_profile_regime_returns_unknown_without_context():
    assert profile_regime([{"close": 1, "volume": 1}])["state"] == "unknown"

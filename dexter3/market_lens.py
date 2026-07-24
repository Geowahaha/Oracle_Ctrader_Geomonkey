"""Dexter3 market lens — pure price-action feature functions.

No I/O. Every function takes lists of OHLC bar dicts (``open``/``high``/
``low``/``close``/``ts``) and returns a small dict of ``{"value": ..., ...
evidence fields}``. No classic indicators (no RSI/MACD/Bollinger) — every
feature here is a direct read of swing structure, wick/close geometry, or
true-range distribution, in the spirit of
``scripts/btc_scalp_monitor.py``'s ``market_leader_side``/
``market_leader_indicator`` (liquidity sweep/reclaim, displacement break,
compression release, close-location pressure) and
``scripts/xau_intraday_dragon.py``'s day-range shelf logic.

Bars are assumed sorted oldest-to-newest; the last element is the most
recently CLOSED bar the caller wants evaluated (callers are responsible for
trimming off any still-forming bar before passing it in — see
``shadow_runner.py``'s M5-close detection).
"""
from __future__ import annotations

from typing import Any

Bar = dict[str, Any]

# ---------------------------------------------------------------------------
# small local helpers (deliberately duplicated/pure — no cross-imports of
# live-loop internals, per the additive-only constraint)
# ---------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def bar_range(bar: Bar) -> float:
    return max(0.0, _f(bar.get("high")) - _f(bar.get("low")))


def true_range(bar: Bar, prev_close: float | None) -> float:
    high = _f(bar.get("high"))
    low = _f(bar.get("low"))
    if prev_close is None:
        return max(0.0, high - low)
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def true_ranges(bars: list[Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        out.append(true_range(bar, prev_close))
        prev_close = _f(bar.get("close"))
    return out


def body_ratio(bar: Bar) -> float:
    rng = bar_range(bar)
    if rng <= 0:
        return 0.0
    return abs(_f(bar.get("close")) - _f(bar.get("open"))) / rng


def close_location(bar: Bar) -> float:
    """0 = closed at bar low, 1 = closed at bar high."""
    rng = bar_range(bar)
    if rng <= 0:
        return 0.5
    return _clamp((_f(bar.get("close")) - _f(bar.get("low"))) / rng)


def upper_wick_ratio(bar: Bar) -> float:
    rng = bar_range(bar)
    if rng <= 0:
        return 0.0
    return (_f(bar.get("high")) - max(_f(bar.get("open")), _f(bar.get("close")))) / rng


def lower_wick_ratio(bar: Bar) -> float:
    rng = bar_range(bar)
    if rng <= 0:
        return 0.0
    return (min(_f(bar.get("open")), _f(bar.get("close"))) - _f(bar.get("low"))) / rng


def is_green(bar: Bar) -> bool:
    return _f(bar.get("close")) > _f(bar.get("open"))


def is_red(bar: Bar) -> bool:
    return _f(bar.get("close")) < _f(bar.get("open"))


def _quantile(values: list[float], q: float) -> float:
    """Simple linear-interpolation quantile — no numpy dependency."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


# ---------------------------------------------------------------------------
# swing structure
# ---------------------------------------------------------------------------


def swing_structure(bars: list[Bar], lookback: int = 20, pivot_span: int = 2) -> dict[str, Any]:
    """Classify recent swing structure as HH/HL/LH/LL and surface swing points.

    A swing high at index i requires bar[i].high to be the max over
    [i-pivot_span, i+pivot_span]; symmetric for swing lows. Classification
    compares the two most recent confirmed swing highs and the two most
    recent confirmed swing lows.
    """
    sample = bars[-lookback:] if len(bars) > lookback else list(bars)
    n = len(sample)
    swing_highs: list[dict[str, Any]] = []
    swing_lows: list[dict[str, Any]] = []

    for i in range(pivot_span, n - pivot_span):
        window = sample[i - pivot_span : i + pivot_span + 1]
        hi = _f(sample[i].get("high"))
        lo = _f(sample[i].get("low"))
        if hi == max(_f(b.get("high")) for b in window):
            swing_highs.append({"index": i, "price": hi, "ts": sample[i].get("ts")})
        if lo == min(_f(b.get("low")) for b in window):
            swing_lows.append({"index": i, "price": lo, "ts": sample[i].get("ts")})

    classification = "unknown"
    if len(swing_highs) >= 2:
        prev_h, last_h = swing_highs[-2]["price"], swing_highs[-1]["price"]
        high_trend = "HH" if last_h > prev_h else ("LH" if last_h < prev_h else "EH")
    else:
        high_trend = None
    if len(swing_lows) >= 2:
        prev_l, last_l = swing_lows[-2]["price"], swing_lows[-1]["price"]
        low_trend = "HL" if last_l > prev_l else ("LL" if last_l < prev_l else "EL")
    else:
        low_trend = None

    if high_trend == "HH" and low_trend == "HL":
        classification = "uptrend"
    elif high_trend == "LH" and low_trend == "LL":
        classification = "downtrend"
    elif high_trend in ("HH", "LH") or low_trend in ("HL", "LL"):
        classification = "transition"

    # EXTENSION (2026-07-24): how far price sits IN the trend direction within
    # the recent range -- 0 = deep pullback (structure weakening), 1 = extended
    # (powering through). swing_structure is a MOMENTUM signal: the isolated
    # 3-window backtest showed deep-pullback trend-follows lose consistently
    # (expR -0.05..-0.12) while extended ones win -- the OPPOSITE of a
    # pullback-continuation edge. Consumers gate on it to skip the weakening
    # deep pullbacks.
    r_hi = max(_f(b.get("high")) for b in sample)
    r_lo = min(_f(b.get("low")) for b in sample)
    rng = r_hi - r_lo
    pos = ((_f(sample[-1].get("close")) - r_lo) / rng) if rng > 0 else 0.5
    extension = pos if classification != "downtrend" else (1.0 - pos)

    return {
        "value": classification,
        "high_trend": high_trend,
        "low_trend": low_trend,
        "extension": round(extension, 3),
        "last_swing_high": swing_highs[-1] if swing_highs else None,
        "last_swing_low": swing_lows[-1] if swing_lows else None,
        "swing_highs": swing_highs[-4:],
        "swing_lows": swing_lows[-4:],
    }


# ---------------------------------------------------------------------------
# liquidity sweep + reclaim
# ---------------------------------------------------------------------------


def liquidity_sweep(bars: list[Bar], lookback: int = 10) -> dict[str, Any]:
    """Detect a wick beyond the prior swing extreme that closes back inside.

    Looks at the last CLOSED bar against the high/low of the ``lookback``
    bars preceding it (excludes the bar itself from the reference range).
    """
    if len(bars) < 3:
        return {"value": False, "side": None, "level": None, "evidence": "insufficient_bars"}

    last = bars[-1]
    prior = bars[max(0, len(bars) - 1 - lookback) : len(bars) - 1]
    if not prior:
        return {"value": False, "side": None, "level": None, "evidence": "insufficient_prior_bars"}

    prior_high = max(_f(b.get("high")) for b in prior)
    prior_low = min(_f(b.get("low")) for b in prior)
    close = _f(last.get("close"))
    high = _f(last.get("high"))
    low = _f(last.get("low"))
    open_ = _f(last.get("open"))

    # STRENGTH metrics (2026-07-24): a REAL liquidity grab is a big wick on
    # participation; a minor poke is noise. wick_atr = grab wick / recent ATR;
    # vol_ratio = bar volume / trailing-20 avg. Reported so consumers can gate
    # on grab strength (the fade→follow edge lives on STRONG grabs only).
    trs = true_ranges(prior)
    atr = (sum(trs) / len(trs)) if trs else 0.0
    vols = [_f(b.get("volume")) for b in prior]
    vavg = (sum(vols) / len(vols)) if vols else 0.0
    vol_last = _f(last.get("volume"))
    vol_ratio = (vol_last / vavg) if vavg > 0 else 0.0
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low

    # sell-side sweep: wick pokes above prior high, closes back below it
    if high > prior_high and close < prior_high and upper_wick_ratio(last) >= 0.25:
        return {
            "value": True,
            "side": "sell",
            "level": round(prior_high, 5),
            "wick_atr": round(upper_wick / atr, 3) if atr > 0 else 0.0,
            "vol_ratio": round(vol_ratio, 3),
            "evidence": f"wick_high={high:.5f}>prior_high={prior_high:.5f},close={close:.5f} back inside",
        }
    # buy-side sweep: wick pokes below prior low, closes back above it
    if low < prior_low and close > prior_low and lower_wick_ratio(last) >= 0.25:
        return {
            "value": True,
            "side": "buy",
            "level": round(prior_low, 5),
            "wick_atr": round(lower_wick / atr, 3) if atr > 0 else 0.0,
            "vol_ratio": round(vol_ratio, 3),
            "evidence": f"wick_low={low:.5f}<prior_low={prior_low:.5f},close={close:.5f} back inside",
        }
    return {"value": False, "side": None, "level": None, "evidence": "no_sweep"}


def reclaim(bars: list[Bar], level: float, side: str | None = None) -> dict[str, Any]:
    """Check whether the last closed bar reclaimed ``level``.

    side="buy": close back ABOVE a broken level (was lost, now regained).
    side="sell": close back BELOW a broken level.
    If ``side`` is omitted, infer from whether the bar closed above/below
    level relative to its open (direction of the reclaiming candle).
    """
    if not bars:
        return {"value": False, "side": side, "level": level, "evidence": "no_bars"}
    last = bars[-1]
    close = _f(last.get("close"))
    open_ = _f(last.get("open"))
    inferred_side = side or ("buy" if close > open_ else "sell")

    if inferred_side == "buy":
        ok = close > level and low_was_below(bars, level)
        evidence = f"close={close:.5f} > level={level:.5f} after prior break below"
    else:
        ok = close < level and high_was_above(bars, level)
        evidence = f"close={close:.5f} < level={level:.5f} after prior break above"

    return {"value": bool(ok), "side": inferred_side, "level": round(level, 5), "evidence": evidence}


def low_was_below(bars: list[Bar], level: float, lookback: int = 6) -> bool:
    sample = bars[-(lookback + 1) : -1] if len(bars) > 1 else []
    return any(_f(b.get("low")) < level for b in sample)


def high_was_above(bars: list[Bar], level: float, lookback: int = 6) -> bool:
    sample = bars[-(lookback + 1) : -1] if len(bars) > 1 else []
    return any(_f(b.get("high")) > level for b in sample)


# ---------------------------------------------------------------------------
# displacement / compression-release / close-location pressure
# ---------------------------------------------------------------------------


def displacement(bars: list[Bar], quantile_window: int = 20, hot_quantile: float = 0.75) -> dict[str, Any]:
    """Score the last bar's range/true-range against recent quantiles (no ATR).

    A displacement bar has true-range at/above the ``hot_quantile`` of the
    trailing true-range distribution AND a strong directional body.
    """
    if len(bars) < 4:
        return {"value": False, "body_ratio": 0.0, "tr_quantile_rank": 0.0, "evidence": "insufficient_bars"}

    sample = bars[-(quantile_window + 1) :] if len(bars) > quantile_window else bars
    trs = true_ranges(sample)
    if len(trs) < 2:
        return {"value": False, "body_ratio": 0.0, "tr_quantile_rank": 0.0, "evidence": "insufficient_tr_history"}

    last_tr = trs[-1]
    reference_trs = trs[:-1]
    hot_level = _quantile(reference_trs, hot_quantile)
    body = body_ratio(bars[-1])
    rank = _rank_of(last_tr, reference_trs)

    is_displacement = last_tr >= hot_level and body >= 0.5
    return {
        "value": bool(is_displacement),
        "body_ratio": round(body, 4),
        "tr_quantile_rank": round(rank, 4),
        "true_range": round(last_tr, 5),
        "hot_level": round(hot_level, 5),
        "direction": "buy" if is_green(bars[-1]) else ("sell" if is_red(bars[-1]) else None),
        "evidence": f"tr={last_tr:.5f} vs p{int(hot_quantile*100)}={hot_level:.5f}, body_ratio={body:.2f}",
    }


def _rank_of(value: float, population: list[float]) -> float:
    if not population:
        return 0.5
    below = sum(1 for v in population if v <= value)
    return below / len(population)


def compression_release(
    bars: list[Bar],
    compression_window: int = 6,
    compression_quantile: float = 0.35,
    release_quantile: float = 0.7,
) -> dict[str, Any]:
    """Detect a contracting-range window followed by an expansion bar.

    Compression: mean true-range of the ``compression_window`` bars BEFORE
    the last bar sits at/below the ``compression_quantile`` of the
    true-range history that PRECEDES the compression window (the "normal"
    regime before the squeeze — deliberately excluded from the compression
    window itself so a long squeeze cannot dilute its own threshold).
    Release: the last bar's true-range is at/above the ``release_quantile``
    of that same pre-compression reference population.
    """
    if len(bars) < compression_window + 3:
        return {"value": False, "direction": None, "evidence": "insufficient_bars"}

    trs = true_ranges(bars)
    if len(trs) < compression_window + 2:
        return {"value": False, "direction": None, "evidence": "insufficient_tr_history"}

    pre_window = trs[-(compression_window + 1) : -1]
    reference_history = trs[: -(compression_window + 1)]
    last_tr = trs[-1]

    if not reference_history:
        return {"value": False, "direction": None, "evidence": "insufficient_reference_history"}

    compression_level = _quantile(reference_history, compression_quantile)
    release_level = _quantile(reference_history, release_quantile)
    mean_pre = sum(pre_window) / len(pre_window) if pre_window else 0.0

    compressed = mean_pre > 0 and mean_pre <= compression_level
    released = last_tr >= release_level and last_tr > 0
    last_bar = bars[-1]
    direction = "buy" if is_green(last_bar) else ("sell" if is_red(last_bar) else None)

    ok = compressed and released and direction is not None
    return {
        "value": bool(ok),
        "direction": direction if ok else None,
        "mean_pre_range": round(mean_pre, 5),
        "compression_level": round(compression_level, 5),
        "release_level": round(release_level, 5),
        "last_true_range": round(last_tr, 5),
        "evidence": (
            f"pre_mean_tr={mean_pre:.5f}<=p{int(compression_quantile*100)}={compression_level:.5f}, "
            f"last_tr={last_tr:.5f}>=p{int(release_quantile*100)}={release_level:.5f}"
        ),
    }


def close_location_pressure(bars: list[Bar], n: int = 5) -> dict[str, Any]:
    """Mean close-location (0..1) over the last ``n`` bars — sustained pressure."""
    sample = bars[-n:] if len(bars) >= n else bars
    if not sample:
        return {"value": 0.5, "n": 0, "evidence": "no_bars"}
    locs = [close_location(b) for b in sample]
    mean_loc = sum(locs) / len(locs)
    bias = "buy" if mean_loc >= 0.6 else ("sell" if mean_loc <= 0.4 else None)
    return {
        "value": round(mean_loc, 4),
        "n": len(sample),
        "bias": bias,
        "evidence": f"mean_close_location over last {len(sample)} bars = {mean_loc:.3f}",
    }


# ---------------------------------------------------------------------------
# day-range position (Dragon shelf logic) + session + volatility regime
# ---------------------------------------------------------------------------


def day_range_position(bars_m5: list[Bar]) -> dict[str, Any]:
    """Current price position (0..1) within today's M5-derived range.

    Uses the max high / min low across the supplied M5 bars as a proxy for
    "today's range" (caller is expected to pass in bars already filtered to
    the current trading day — Dragon shelf logic in
    ``scripts/xau_intraday_dragon.py`` reads day_hi/day_lo the same way from
    a spot snapshot; here we derive it purely from bars for lens purposes).
    """
    if not bars_m5:
        return {"value": 0.5, "day_hi": None, "day_lo": None, "evidence": "no_bars"}
    day_hi = max(_f(b.get("high")) for b in bars_m5)
    day_lo = min(_f(b.get("low")) for b in bars_m5)
    mid = _f(bars_m5[-1].get("close"))
    span = day_hi - day_lo
    pos = 0.5 if span <= 0 else _clamp((mid - day_lo) / span)
    zone = "upper_shelf" if pos >= 0.78 else ("lower_shelf" if pos <= 0.22 else "mid_range")
    return {
        "value": round(pos, 4),
        "day_hi": round(day_hi, 5),
        "day_lo": round(day_lo, 5),
        "mid": round(mid, 5),
        "zone": zone,
        "evidence": f"pos={pos:.3f} in [{day_lo:.5f}, {day_hi:.5f}], zone={zone}",
    }


def session_context(ts_utc: str) -> dict[str, Any]:
    """Tag a UTC ISO-8601 timestamp with an XAU session label.

    Sessions (UTC): asian 00:00-07:00, london 07:00-12:00,
    overlap 12:00-16:00 (london/ny), ny 16:00-21:00, off_hours 21:00-24:00.
    BTC trades 24/7 — callers tag it anyway for feature parity/telemetry.
    """
    hour = _parse_hour_utc(ts_utc)
    if hour is None:
        return {"value": "unknown", "hour_utc": None, "evidence": "unparseable_timestamp"}
    if 0 <= hour < 7:
        label = "asian"
    elif 7 <= hour < 12:
        label = "london"
    elif 12 <= hour < 16:
        label = "overlap"
    elif 16 <= hour < 21:
        label = "ny"
    else:
        label = "off_hours"
    return {"value": label, "hour_utc": hour, "evidence": f"hour_utc={hour} -> {label}"}


def _parse_hour_utc(ts_utc: str) -> int | None:
    if not ts_utc:
        return None
    try:
        text = ts_utc.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        from datetime import datetime

        dt = datetime.fromisoformat(text)
        return dt.hour
    except (ValueError, TypeError):
        return None


def volatility_state(bars: list[Bar], window: int = 60, dead_q: float = 0.25, hot_q: float = 0.75) -> dict[str, Any]:
    """Classify current true-range regime: dead/normal/hot via TR quantiles."""
    if len(bars) < 5:
        return {"value": "normal", "evidence": "insufficient_bars"}
    sample = bars[-(window + 1) :] if len(bars) > window else bars
    trs = true_ranges(sample)
    if len(trs) < 2:
        return {"value": "normal", "evidence": "insufficient_tr_history"}
    last_tr = trs[-1]
    history = trs[:-1]
    dead_level = _quantile(history, dead_q)
    hot_level = _quantile(history, hot_q)
    if last_tr <= dead_level:
        label = "dead"
    elif last_tr >= hot_level:
        label = "hot"
    else:
        label = "normal"
    return {
        "value": label,
        "true_range": round(last_tr, 5),
        "dead_level": round(dead_level, 5),
        "hot_level": round(hot_level, 5),
        "evidence": f"tr={last_tr:.5f} vs [dead<={dead_level:.5f}, hot>={hot_level:.5f}]",
    }


# ---------------------------------------------------------------------------
# leader score — weighted composite mirroring btc_scalp_monitor's leader gate
# ---------------------------------------------------------------------------

# Weights intentionally mirror the point budget in
# scripts/btc_scalp_monitor.py::market_leader_side (sweep_reclaim=0.28,
# impulse_break=0.22, displacement=0.18, close_pressure=0.14,
# compression_release=0.10, m5_pressure=0.10, recent_pressure=0.06) collapsed
# onto the lens features computed above. Documented here as constants so the
# weighting is auditable and tunable without touching the scoring logic.
LEADER_WEIGHT_SWEEP_RECLAIM = 0.30
LEADER_WEIGHT_DISPLACEMENT = 0.25
LEADER_WEIGHT_COMPRESSION_RELEASE = 0.15
LEADER_WEIGHT_CLOSE_PRESSURE = 0.20
LEADER_WEIGHT_SWING_ALIGNMENT = 0.10

LEADER_MIN_SCORE = 0.64
LEADER_STRONG_SCORE = 0.74


def leader_score(features: dict[str, Any]) -> dict[str, Any]:
    """Weighted composite leader score (0..1) from already-computed features.

    Expects ``features`` to contain the outputs of ``liquidity_sweep``,
    ``displacement``, ``compression_release``, ``close_location_pressure``,
    and ``swing_structure`` (any missing key scores 0 for that component —
    callers may pass a partial feature set for cheap checks).
    """
    sweep = features.get("liquidity_sweep") or {}
    disp = features.get("displacement") or {}
    comp = features.get("compression_release") or {}
    clp = features.get("close_location_pressure") or {}
    swing = features.get("swing_structure") or {}

    sweep_side = sweep.get("side") if sweep.get("value") else None
    disp_side = disp.get("direction") if disp.get("value") else None
    comp_side = comp.get("direction") if comp.get("value") else None
    clp_bias = clp.get("bias")
    swing_dir = {"uptrend": "buy", "downtrend": "sell"}.get(swing.get("value"))

    votes: dict[str, float] = {"buy": 0.0, "sell": 0.0}
    if sweep_side in votes:
        votes[sweep_side] += LEADER_WEIGHT_SWEEP_RECLAIM
    if disp_side in votes:
        votes[disp_side] += LEADER_WEIGHT_DISPLACEMENT
    if comp_side in votes:
        votes[comp_side] += LEADER_WEIGHT_COMPRESSION_RELEASE
    if clp_bias in votes:
        votes[clp_bias] += LEADER_WEIGHT_CLOSE_PRESSURE
    if swing_dir in votes:
        votes[swing_dir] += LEADER_WEIGHT_SWING_ALIGNMENT

    buy_score = _clamp(votes["buy"])
    sell_score = _clamp(votes["sell"])
    if buy_score >= sell_score:
        side, score = "buy", buy_score
    else:
        side, score = "sell", sell_score
    gap = abs(buy_score - sell_score)

    band = "strong" if score >= LEADER_STRONG_SCORE else ("min" if score >= LEADER_MIN_SCORE else "weak")
    return {
        "value": round(score, 4),
        "side": side if score >= LEADER_MIN_SCORE else None,
        "band": band,
        "buy_score": round(buy_score, 4),
        "sell_score": round(sell_score, 4),
        "gap": round(gap, 4),
        "evidence": f"buy={buy_score:.3f} sell={sell_score:.3f} gap={gap:.3f} -> side={side} band={band}",
    }

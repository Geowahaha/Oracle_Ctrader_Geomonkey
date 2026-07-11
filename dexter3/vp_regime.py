"""No-lookahead regime features for the volume-profile producer.

This is deliberately measurement-only: it describes the profile context of a
signal without accepting, rejecting, or sizing an order.
"""
from __future__ import annotations

from typing import Any


def profile_regime(bars: list[dict[str, Any]], *, lookback: int = 48) -> dict[str, float | str]:
    """Return causal trend efficiency and relative tick-volume quality."""
    window = list(bars[-lookback:])
    if len(window) < 3:
        return {"state": "unknown", "efficiency": 0.0, "volume_ratio": 0.0}
    closes = [float(b.get("close") or 0.0) for b in window]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0
    vols = [max(0.0, float(b.get("volume") or 0.0)) for b in window]
    baseline = sum(vols[:-1]) / max(1, len(vols) - 1)
    ratio = vols[-1] / baseline if baseline > 0 else 0.0
    state = "directional" if efficiency >= 0.35 else "rotational"
    return {"state": state, "efficiency": round(efficiency, 4), "volume_ratio": round(ratio, 4)}


# ---------------------------------------------------------------------------
# Rolling walk-forward regime gate (owner spec 2026-07-12) — coexists with
# profile_regime() above (codex's measurement-only summary). These power
# scripts/dexter3_vp_regime_wf.py: decision-time market-state features +
# a deliberately TRANSPARENT per-window rule (single decision stump), so the
# point is learning WHY the VP edge switches on — not fitting noise.
# ---------------------------------------------------------------------------

from dexter3.volume_profile import build_profile  # noqa: E402

FEATURES = (
    "concentration",      # top-5 bin volume share of the 288-bar profile
    "va_width_atr",       # value-area width / ATR14
    "dist_poc_atr",       # |close - POC| / ATR14
    "atr_pct",            # ATR14 percentile within the trailing 1440 bars
    "vol_ratio",          # median tick volume last 288 / last 1440
    "eff_h1",             # efficiency ratio of last 144 M5 closes (~12 H1)
)

MIN_STUMP_FRACTION = 0.40   # a stump's kept side must retain >= 40% of train trades


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def atr_series(bars: list[dict[str, Any]], n: int = 14) -> list[float]:
    """Rolling ATR(n) per bar (simple TR mean; index-aligned with bars)."""
    out: list[float] = []
    trs: list[float] = []
    prev_close: float | None = None
    for b in bars:
        hi, lo, cl = _f(b.get("high")), _f(b.get("low")), _f(b.get("close"))
        tr = hi - lo
        if prev_close is not None:
            tr = max(tr, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = cl
        window = trs[-n:]
        out.append(sum(window) / len(window) if window else 0.0)
    return out


def efficiency_ratio(closes: list[float]) -> float:
    """|net move| / sum(|bar-to-bar moves|) — 1.0 = perfect trend, ~0 = chop."""
    if len(closes) < 3:
        return 0.0
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
    return (net / path) if path > 0 else 0.0


def regime_features(prefix: list[dict[str, Any]], atr_arr: list[float], i: int) -> dict[str, float] | None:
    """Decision-time market-state features at bar index ``i`` (prefix = bars[:i+1]).

    Uses ONLY prefix data: the 288-bar profile ends at the bar BEFORE the
    signal; ATR percentile ranks within the trailing 1440 bars. Returns None
    when there is not enough history (caller treats as gate-closed)."""
    if i < 300 or len(prefix) < 300:
        return None
    profile = build_profile(prefix[-289:-1])
    if profile is None:
        return None
    atr = atr_arr[i] if i < len(atr_arr) else 0.0
    if atr <= 0:
        return None
    vols_sorted = sorted(profile.bin_volumes, reverse=True)
    total_v = sum(profile.bin_volumes) or 1.0
    concentration = sum(vols_sorted[:5]) / total_v
    close = _f(prefix[-1].get("close"))
    trail = atr_arr[max(0, i - 1440): i + 1]
    below = sum(1 for a in trail if a <= atr)
    atr_pct = below / len(trail) if trail else 0.5

    def _median(xs: list[float]) -> float:
        if not xs:
            return 0.0
        ss = sorted(xs)
        return ss[len(ss) // 2]

    v288 = _median([_f(b.get("volume")) for b in prefix[-289:-1]])
    v1440 = _median([_f(b.get("volume")) for b in prefix[-1441:-1]]) or 1.0
    closes_144 = [_f(b.get("close")) for b in prefix[-145:-1]]
    return {
        "concentration": concentration,
        "va_width_atr": (profile.va_hi - profile.va_lo) / atr,
        "dist_poc_atr": abs(close - profile.poc_price) / atr,
        "atr_pct": atr_pct,
        "vol_ratio": v288 / v1440,
        "eff_h1": efficiency_ratio(closes_144),
    }


def _pf(pnls: list[float]) -> float:
    gw = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    return (gw / abs(gl)) if gl < 0 else (999.0 if gw > 0 else 0.0)


def derive_stump(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick THE single (feature, side, threshold=median) condition whose kept
    subset maximizes train PF while retaining >= MIN_STUMP_FRACTION of trades.

    ``trades`` — [{"features": {...}, "pnl": float}]. Returns None when no
    stump improves on the ungated train PF (gate stays open that window)."""
    if len(trades) < 10:
        return None
    base_pf = _pf([t["pnl"] for t in trades])
    best: dict[str, Any] | None = None
    for feat in FEATURES:
        vals = sorted(t["features"][feat] for t in trades if feat in t.get("features", {}))
        if len(vals) < len(trades) * 0.9:
            continue
        thr = vals[len(vals) // 2]
        for op in (">=", "<"):
            kept = [
                t["pnl"] for t in trades
                if (t["features"][feat] >= thr) == (op == ">=")
            ]
            if len(kept) < len(trades) * MIN_STUMP_FRACTION:
                continue
            pf = _pf(kept)
            if pf > base_pf and (best is None or pf > best["train_pf"]):
                best = {
                    "feature": feat, "op": op, "threshold": thr,
                    "train_pf": pf, "train_base_pf": base_pf,
                    "kept_frac": len(kept) / len(trades),
                }
    return best


def apply_stump(rule: dict[str, Any] | None, features: dict[str, float] | None) -> bool:
    """True = trade allowed. No rule -> open gate; no features -> closed."""
    if rule is None:
        return True
    if not features or rule["feature"] not in features:
        return False
    ge = features[rule["feature"]] >= rule["threshold"]
    return ge if rule["op"] == ">=" else not ge

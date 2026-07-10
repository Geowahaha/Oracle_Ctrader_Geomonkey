"""Dexter3 volume-profile entry producer — NEW entry logic candidate #1.

Born 2026-07-11 after the disciplined optimizer certified the existing
decision engines (hunt committee + selective brain) config-space EDGE-FREE on
a month of XAU M5: the mission's next edge must come from NEW logic, and the
trading philosophy's first unbuilt layer is Volume Profile (HVN/LVN/POC
structural entries) — see docs/TRADING_PHILOSOPHY.md "Future edge layers".

Pure module: no I/O, no MCP, no randomness (same posture as hunt_mode).
``decide_vp()`` returns a ``hunter_brain.Decision`` so it drops into the same
journal/executor/replay plumbing as the other producers.

TEMPORAL HONESTY: the profile for a decision at bar i is built ONLY from the
``window`` bars strictly BEFORE the signal bar (the last CLOSED bar acts as
the signal; the profile never includes it). Volume is broker tick-volume
(ProtoOATrendbar.volume) — a standard proxy for traded volume on spot metals.

Setups (all structural, SL/TP from profile nodes, RR floor shared):
  * lvn_rejection   — the signal bar ENTERED a low-volume node and CLOSED back
                      outside it (rejection through thin liquidity): enter
                      away from the LVN; SL beyond the LVN far edge; TP at the
                      next high-volume node / POC in trade direction.
  * poc_reversion   — the signal bar closed OUTSIDE the value area and shows a
                      reversal close back toward it: enter toward the POC;
                      SL beyond the signal extreme; TP at the value-area edge.
  * hvn_break_retest— the PRIOR bar broke through an HVN shelf and the signal
                      bar retested it and held (close back beyond the node):
                      continuation entry; SL beyond the node's far side; TP at
                      the next node (or a range-fraction extension).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts

Bar = dict[str, Any]

# -- tunables (module constants, auditable in one place) ---------------------
PROFILE_WINDOW = 288          # bars in the rolling profile (24h of M5)
PROFILE_BINS = 40             # price bins across the window's range
VALUE_AREA_FRAC = 0.70        # classic 70% value area
HVN_MIN_REL = 1.30            # bin >= 1.3x mean bin volume -> high-volume node
LVN_MAX_REL = 0.55            # bin <= 0.55x mean bin volume -> low-volume node
MIN_REWARD_RISK = 1.2         # same floor as hunt/brain
MIN_BARS = PROFILE_WINDOW + 3 # profile window + signal context
P_WIN_BASE = 0.45             # conservative prior until empirically measured


@dataclass(frozen=True)
class ProfileNode:
    kind: str        # "hvn" | "lvn"
    price_lo: float
    price_hi: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.price_lo + self.price_hi) / 2.0


@dataclass(frozen=True)
class VolumeProfile:
    lo: float
    hi: float
    bin_volumes: tuple[float, ...]
    poc_price: float             # mid of the max-volume bin
    va_lo: float                 # value-area low edge
    va_hi: float                 # value-area high edge
    nodes: tuple[ProfileNode, ...]

    def bin_width(self) -> float:
        return (self.hi - self.lo) / max(1, len(self.bin_volumes))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def build_profile(bars: list[Bar], bins: int = PROFILE_BINS) -> VolumeProfile | None:
    """Distribute each bar's tick volume uniformly across the price bins its
    high-low range covers. Returns None when the inputs cannot form a profile
    (no range, no volume anywhere, fewer than 2 bars)."""
    if len(bars) < 2:
        return None
    lo = min(_f(b.get("low")) for b in bars)
    hi = max(_f(b.get("high")) for b in bars)
    if not (hi > lo):
        return None
    width = (hi - lo) / bins
    vols = [0.0] * bins
    any_volume = False
    for b in bars:
        b_lo, b_hi = _f(b.get("low")), _f(b.get("high"))
        v = _f(b.get("volume"))
        if v <= 0 or not (b_hi >= b_lo):
            continue
        any_volume = True
        # bins covered by this bar's range (inclusive); uniform allocation
        i0 = min(bins - 1, max(0, int((b_lo - lo) / width)))
        i1 = min(bins - 1, max(0, int((b_hi - lo) / width - 1e-9)))
        share = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vols[i] += share
    if not any_volume:
        return None
    poc_i = max(range(bins), key=lambda i: vols[i])
    poc_price = lo + (poc_i + 0.5) * width

    # value area: expand from POC until VALUE_AREA_FRAC of total volume
    total = sum(vols)
    covered = vols[poc_i]
    va_l = va_r = poc_i
    while covered < VALUE_AREA_FRAC * total and (va_l > 0 or va_r < bins - 1):
        left = vols[va_l - 1] if va_l > 0 else -1.0
        right = vols[va_r + 1] if va_r < bins - 1 else -1.0
        if right >= left:
            va_r += 1
            covered += max(0.0, right)
        else:
            va_l -= 1
            covered += max(0.0, left)
    mean_v = total / bins

    # classify bins, then MERGE contiguous same-kind bins into ZONES — market
    # profile semantics: a 10-bin thin gap is ONE low-volume zone, and a
    # rejection means closing beyond the whole zone, not beyond one bin.
    kinds: list[str | None] = []
    for i in range(bins):
        if vols[i] >= HVN_MIN_REL * mean_v:
            kinds.append("hvn")
        elif vols[i] <= LVN_MAX_REL * mean_v:
            kinds.append("lvn")
        else:
            kinds.append(None)
    nodes: list[ProfileNode] = []
    i = 0
    while i < bins:
        kind = kinds[i]
        if kind is None:
            i += 1
            continue
        j = i
        zone_vol = 0.0
        while j < bins and kinds[j] == kind:
            zone_vol += vols[j]
            j += 1
        nodes.append(ProfileNode(kind, lo + i * width, lo + j * width, zone_vol))
        i = j
    return VolumeProfile(
        lo=lo, hi=hi, bin_volumes=tuple(vols), poc_price=poc_price,
        va_lo=lo + va_l * width, va_hi=lo + (va_r + 1) * width,
        nodes=tuple(nodes),
    )


def _nearest_node(profile: VolumeProfile, price: float, kind: str, direction: int) -> ProfileNode | None:
    """Nearest node of ``kind`` strictly in ``direction`` (+1 above / -1 below price)."""
    best: ProfileNode | None = None
    for n in profile.nodes:
        if n.kind != kind:
            continue
        if direction > 0 and n.mid <= price:
            continue
        if direction < 0 and n.mid >= price:
            continue
        if best is None or abs(n.mid - price) < abs(best.mid - price):
            best = n
    return best


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"vp_skip: {reason}"], features={},
    )


def _enter(ts_close: str, symbol: str, setup: str, side: str, entry: float,
           sl: float, tp: float, session: str, reasons: list[str],
           features: dict[str, Any]) -> Decision | None:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or (reward / risk) < MIN_REWARD_RISK:
        return None
    if side == "buy" and not (sl < entry < tp):
        return None
    if side == "sell" and not (tp < entry < sl):
        return None
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(entry, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=P_WIN_BASE, setup=setup,
        reasons=reasons + [f"rr={reward / max(risk, 1e-9):.2f}>={MIN_REWARD_RISK}"],
        features=features, session=session,
    )


def decide_vp(symbol: str, m5_bars: list[Bar], spread_abs: float,
              session: str = "unknown") -> Decision:
    """Volume-profile decision on the last CLOSED bar of ``m5_bars``.

    Profile = the PROFILE_WINDOW bars strictly before the signal bar. The
    signal bar (m5_bars[-1]) provides the trigger pattern only.
    """
    ts_close = _bar_close_ts(str(m5_bars[-1].get("ts") or "")) if m5_bars else ""
    if len(m5_bars) < MIN_BARS:
        return _skip(ts_close, symbol, f"bars<{MIN_BARS}")
    signal = m5_bars[-1]
    prior = m5_bars[-2]
    profile = build_profile(m5_bars[-(PROFILE_WINDOW + 1):-1])
    if profile is None:
        return _skip(ts_close, symbol, "no_profile (missing volume?)")
    width = profile.bin_width()
    s_o, s_h, s_l, s_c = (_f(signal.get(k)) for k in ("open", "high", "low", "close"))
    features: dict[str, Any] = {
        "vp_poc": profile.poc_price, "vp_va": [profile.va_lo, profile.va_hi],
        "vp_nodes": len(profile.nodes),
    }

    # -- setup 1: LVN rejection ------------------------------------------
    # Invalidation is STRUCTURAL-BY-WICK: the thesis is "thin zone rejected
    # the probe" — it dies if price re-enters and passes the probe's extreme,
    # NOT at the zone's far edge (wide zones would make every RR fail).
    for n in profile.nodes:
        if n.kind != "lvn":
            continue
        wick_in = s_l <= n.price_hi and s_h >= n.price_lo   # touched the LVN zone
        if not wick_in:
            continue
        if s_c > n.price_hi and s_c > s_o and s_l <= n.price_hi:  # probed in, closed above the ZONE
            hvn_up = _nearest_node(profile, s_c, "hvn", +1)
            tp = (hvn_up.price_hi - width / 2 if hvn_up
                  else (profile.poc_price if profile.poc_price > s_c else s_c + 3 * width))
            d = _enter(ts_close, symbol, "vp_lvn_rejection", "buy", s_c,
                       s_l - width - spread_abs, tp, session,
                       [f"LVN zone {n.price_lo:.2f}-{n.price_hi:.2f} rejected up (wick {s_l:.2f})"], features)
            if d:
                return d
        if s_c < n.price_lo and s_c < s_o and s_h >= n.price_lo:  # probed in, closed below the ZONE
            hvn_dn = _nearest_node(profile, s_c, "hvn", -1)
            tp = (hvn_dn.price_lo + width / 2 if hvn_dn
                  else (profile.poc_price if profile.poc_price < s_c else s_c - 3 * width))
            d = _enter(ts_close, symbol, "vp_lvn_rejection", "sell", s_c,
                       s_h + width + spread_abs, tp, session,
                       [f"LVN zone {n.price_lo:.2f}-{n.price_hi:.2f} rejected down (wick {s_h:.2f})"], features)
            if d:
                return d

    # -- setup 2: POC reversion (from outside the value area) --------------
    if s_c > profile.va_hi and s_c < s_o and s_h > _f(prior.get("high")):
        d = _enter(ts_close, symbol, "vp_poc_reversion", "sell", s_c,
                   s_h + width + spread_abs, profile.va_hi, session,
                   [f"above VA {profile.va_hi:.2f}, reversal close"], features)
        if d:
            return d
    if s_c < profile.va_lo and s_c > s_o and s_l < _f(prior.get("low")):
        d = _enter(ts_close, symbol, "vp_poc_reversion", "buy", s_c,
                   s_l - width - spread_abs, profile.va_lo, session,
                   [f"below VA {profile.va_lo:.2f}, reversal close"], features)
        if d:
            return d

    # -- setup 3: HVN breakout-retest --------------------------------------
    p_c = _f(prior.get("close"))
    for n in profile.nodes:
        if n.kind != "hvn":
            continue
        broke_up = p_c > n.price_hi and _f(prior.get("open")) <= n.price_hi
        retest_held_up = broke_up and s_l <= n.price_hi and s_c > n.price_hi
        if retest_held_up:
            hvn_up = _nearest_node(profile, s_c + width, "hvn", +1)
            tp = hvn_up.mid if hvn_up else s_c + 4 * width
            d = _enter(ts_close, symbol, "vp_hvn_break_retest", "buy", s_c,
                       n.price_lo - spread_abs, tp, session,
                       [f"HVN {n.price_lo:.2f}-{n.price_hi:.2f} broke+retested up"], features)
            if d:
                return d
        broke_dn = p_c < n.price_lo and _f(prior.get("open")) >= n.price_lo
        retest_held_dn = broke_dn and s_h >= n.price_lo and s_c < n.price_lo
        if retest_held_dn:
            hvn_dn = _nearest_node(profile, s_c - width, "hvn", -1)
            tp = hvn_dn.mid if hvn_dn else s_c - 4 * width
            d = _enter(ts_close, symbol, "vp_hvn_break_retest", "sell", s_c,
                       n.price_hi + spread_abs, tp, session,
                       [f"HVN {n.price_lo:.2f}-{n.price_hi:.2f} broke+retested down"], features)
            if d:
                return d

    return _skip(ts_close, symbol, "no_vp_setup")

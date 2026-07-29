"""SNIPER producer — owner's SHADOWCODES [PRO SNIPER] Pine v6 script translated
to a dexter3 lane (owner directive 2026-07-30: "study this pine script then
make the best new lane to live test on XAUUSD and USTEC").

WHAT THE SCRIPT IS (three mechanisms, all closed-bar):
  1. SUPPLY/DEMAND ZONES — a low-body BASE bar, engulfed by a STRONG impulse
     bar (body > 0.6 x range, range > ATR14) whose direction FLIPS the bar
     before the base: the base bar's high/low becomes the zone. A zone dies
     when any later bar CLOSES beyond its far edge (mitigation) or after
     ``zone_max_age`` bars.
  2. ST2 (wick rejection in-zone) — the newest bar's low prints INSIDE a
     demand zone with a lower wick > st2_ratio x body AND > wick_dom x the
     upper wick -> BUY at the close (mirror for supply -> SELL).
  3. QM (liquidity sweep reclaim) — the newest bar takes out the prior
     20-bar high, closes back BELOW it, closes red AND below the prior bar's
     low -> SELL (a swept high + displacement). Mirror for BUY. This is the
     exact bucket the owner's own reviewed NASDAQ trade family lives in
     ("NASDAQ มักมี Liquidity Sweep ก่อนวิ่งจริง").

GEOMETRY (script section 5): SL = signal-bar extreme -/+ 0.5 x ATR; the
script draws TP1/2/3 at 2R/3R/4R. Live translation: PLAIN broker TP at
``tp_r`` (default 2.0 = the script's TP1) — the 07-25 audit measured fixed
broker TP as 66.5% of all live gross profit and partial closes are impossible
at the 1-oz/1-unit volume floor anyway, so ONE fixed target is the honest
version of the script's multi-TP, not a distortion. The lane unit also
carries the SL floor env (DEXTER3_SL_FLOOR_TR_MULT=1.3) — the single
audit-proven edge (+0.51R swing) — which can only WIDEN the script's stop.

STATE MODEL (the replay-vs-live lesson, 2026-07-25: parameter parity is
worthless if the state model differs): zones are REBUILT deterministically
from the trailing ``scan_window`` closed bars on every decision — no
persistent engine, no drift between replay and live by construction. Pine's
top-down order is preserved: zones born/mitigated up to and INCLUDING the
newest bar, then signals checked on that same bar.

VALIDATION STANCE: forward canary A/B exactly like chf/dpull-cs — replay is
run pre-deploy as a sanity gate (a strongly negative replay kills it), but
the honest arbiter is FORWARD realized PnL at canary size. Off-by-default;
only DEXTER3_MODE=sniper / DEXTER3_PRODUCER=sniper activates anything, so
every other lane is byte-identical when the SNIPER envs are absent
(additive-only rule).

Every function is PURE (env readers aside). tests/test_dexter3_sniper.py
pins this module.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.hunter_brain import Decision, _bar_close_ts

Bar = dict[str, Any]

# Broker label for the SNIPER canary lane — peer-isolation contract (loops
# never touch foreign-labelled positions). shadow_runner forces
# executor.LABEL to this when DEXTER3_MODE=sniper.
#
# PER-SYMBOL ISOLATION (XAUUSD vs USTEC units): the two units are separate
# processes and MUST NOT share a label — `_lane_realized_today` matches by
# label family, so a shared label would make each process's governor count
# the OTHER symbol's realized PnL as its own day. `DEXTER3_SNIPER_SUFFIX`
# (set only on the USTEC unit) forks the family to "dexter3:sniper-ustec",
# which `executor.label_matches_family` treats as a DIFFERENT family from
# "dexter3:sniper" (hyphen not followed by a version token — the same rule
# that keeps dpull and dpull-cs apart). Read at import time, same pattern as
# executor.VERSION.
_SUFFIX = os.environ.get("DEXTER3_SNIPER_SUFFIX", "").strip().lower()
SNIPER_LABEL_FAMILY = "dexter3:sniper" + (f"-{_SUFFIX}" if _SUFFIX else "")
SNIPER_LABEL = SNIPER_LABEL_FAMILY + ":canary"

# -- env knobs (single source of truth; defaults = the script's own inputs) --
ENV_ATR_LEN = "DEXTER3_SNIPER_ATR_LEN"                # ta.atr length (14)
ENV_BASE_BODY_FRAC = "DEXTER3_SNIPER_BASE_BODY_FRAC"  # base bar body < frac x range (0.3)
ENV_IMPULSE_BODY_FRAC = "DEXTER3_SNIPER_IMPULSE_BODY_FRAC"  # impulse body > frac x range (0.6)
ENV_ZONE_MAX_AGE = "DEXTER3_SNIPER_ZONE_MAX_AGE"      # zone lifetime in bars (100)
ENV_ST2_RATIO = "DEXTER3_SNIPER_ST2_RATIO"            # wick > ratio x body (2.5)
ENV_WICK_DOM = "DEXTER3_SNIPER_WICK_DOM"              # wick > dom x opposite wick (2.0)
ENV_SWEEP_LOOKBACK = "DEXTER3_SNIPER_SWEEP_LOOKBACK"  # QM prior-extreme lookback (20)
ENV_SL_ATR_BUF = "DEXTER3_SNIPER_SL_ATR_BUF"          # SL buffer beyond the extreme (0.5)
ENV_TP_R = "DEXTER3_SNIPER_TP_R"                      # fixed TP in R (2.0 = script TP1)
ENV_MAX_RISK_ATR = "DEXTER3_SNIPER_MAX_RISK_ATR"      # reject risk > this x ATR (3.0)
ENV_SCAN_WINDOW = "DEXTER3_SNIPER_SCAN_WINDOW"        # trailing bars for the zone rebuild (300)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    return _f(os.environ.get(key), default)


def _env_int(key: str, default: int) -> int:
    return int(_env_float(key, float(default)))


def sniper_mode_enabled() -> bool:
    """Same trigger set as shadow_runner._sniper_producer_enabled — kept in
    sync by tests, not imports (no circular dependency on the runner)."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "sniper":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "sniper"


def _atr_series(bars: list[Bar], length: int) -> list[float]:
    """Per-bar ATR: simple mean of the trailing ``length`` true ranges as of
    each bar (index-aligned with ``bars``; 0.0 while there is not enough
    history). A mean-TR proxy for Pine's RMA ta.atr — the impulse gate only
    asks "is this bar's range bigger than typical", where the smoothing
    flavour is immaterial; the proxy keeps the module pure and replayable."""
    n = len(bars)
    trs: list[float] = [0.0] * n
    for i in range(n):
        hi, lo = _f(bars[i].get("high")), _f(bars[i].get("low"))
        if i == 0:
            trs[i] = hi - lo
        else:
            pc = _f(bars[i - 1].get("close"))
            trs[i] = max(hi - lo, abs(hi - pc), abs(lo - pc))
    out: list[float] = [0.0] * n
    for i in range(n):
        if i + 1 < length:
            continue
        window = trs[i + 1 - length: i + 1]
        out[i] = sum(window) / length
    return out


def build_zones(bars: list[Bar], atr: list[float] | None = None) -> list[dict[str, Any]]:
    """Rebuild the ALIVE supply/demand zones over ``bars`` (Pine section 3).

    A zone is born at impulse bar ``i`` from the triplet (i-2 flip, i-1 base,
    i impulse) and is keyed to the BASE bar's high/low. It dies when any bar
    ``j > i`` (including the newest bar — Pine runs mitigation before the
    signal block) CLOSES beyond its far edge, or when older than
    ``zone_max_age`` bars. Deterministic pure function of the bar window —
    identical state model in replay and live by construction.
    """
    base_frac = _env_float(ENV_BASE_BODY_FRAC, 0.3)
    impulse_frac = _env_float(ENV_IMPULSE_BODY_FRAC, 0.6)
    max_age = max(1, _env_int(ENV_ZONE_MAX_AGE, 100))
    atr_len = max(1, _env_int(ENV_ATR_LEN, 14))
    n = len(bars)
    if n < 3:
        return []
    if atr is None:
        atr = _atr_series(bars, atr_len)

    zones: list[dict[str, Any]] = []
    last_i = n - 1
    for i in range(2, n):
        b2, b1, b0 = bars[i - 2], bars[i - 1], bars[i]
        o1, c1 = _f(b1.get("open")), _f(b1.get("close"))
        h1, l1 = _f(b1.get("high")), _f(b1.get("low"))
        rng1 = h1 - l1
        if rng1 <= 0 or abs(c1 - o1) >= rng1 * base_frac:
            continue  # bars[i-1] is not a base bar
        o0, c0 = _f(b0.get("open")), _f(b0.get("close"))
        h0, l0 = _f(b0.get("high")), _f(b0.get("low"))
        rng0, body0 = h0 - l0, abs(c0 - o0)
        a = atr[i] if i < len(atr) else 0.0
        if rng0 <= 0 or a <= 0 or body0 <= rng0 * impulse_frac or rng0 <= a:
            continue  # bars[i] is not a strong impulse
        o2, c2 = _f(b2.get("open")), _f(b2.get("close"))
        side: str | None = None
        if c0 > o0 and c2 < o2:
            side = "demand"
        elif c0 < o0 and c2 > o2:
            side = "supply"
        if side is None:
            continue
        if last_i - i > max_age:
            continue  # would already be age-expired at the newest bar
        # mitigation scan: any later close beyond the far edge kills it
        alive = True
        for j in range(i + 1, n):
            cj = _f(bars[j].get("close"))
            if side == "demand" and cj < l1:
                alive = False
                break
            if side == "supply" and cj > h1:
                alive = False
                break
        if alive:
            zones.append({"side": side, "top": h1, "bottom": l1, "born": i,
                          "age": last_i - i})
    return zones


def _skip(ts_close: str, symbol: str, reason: str) -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action="skip", side=None,
        entry_type=None, entry=None, sl=None, tp=None, size_class="none",
        leader_score=0.0, p_win_est=0.0, setup="none",
        reasons=[f"sniper_skip: {reason}"], features={},
    )


def _enter(ts_close: str, symbol: str, side: str, entry: float, sl: float,
           setup: str, session: str, reasons: list[str],
           features: dict[str, Any]) -> Decision | None:
    """Geometry guard + the script's fixed-R target (TP1 by default). A
    zero/inverted risk is no trade — returns None so the caller reports a
    clean skip instead of shipping a broken order."""
    tp_r = _env_float(ENV_TP_R, 2.0)
    risk = abs(entry - sl)
    if risk <= 0 or tp_r <= 0:
        return None
    tp = entry + tp_r * risk if side == "buy" else entry - tp_r * risk
    if side == "buy" and not (sl < entry < tp):
        return None
    if side == "sell" and not (tp < entry < sl):
        return None
    return Decision(
        ts_close=ts_close, symbol=symbol, action="enter", side=side,
        entry_type="market", entry=round(entry, 5), sl=round(sl, 5),
        tp=round(tp, 5), size_class="small", leader_score=0.5,
        p_win_est=0.45, setup=setup,
        reasons=reasons + [f"tp={tp_r:.1f}R (script TP1)"],
        features=features, session=session,
    )


def decide_sniper(symbol: str, m5_bars: list[Bar], spread_abs: float,
                  session: str = "unknown") -> Decision:
    """SHADOWCODES decision on the last CLOSED bar of ``m5_bars``.

    Priority when both fire on one bar: QM sweep over ST2 (the sweep is the
    more specific event — prior-extreme raid + displacement — and the pair
    can only conflict in direction, in which case QM's displacement evidence
    outranks a wick inside a zone). Opposite-direction double signals within
    the SAME mechanism cannot happen (each is direction-exclusive by its own
    close/open condition). Never raises on malformed bars — degrades to skip
    (a zone lane must fail closed, never blind-enter)."""
    atr_len = max(1, _env_int(ENV_ATR_LEN, 14))
    st2_ratio = _env_float(ENV_ST2_RATIO, 2.5)
    wick_dom = _env_float(ENV_WICK_DOM, 2.0)
    sweep_lb = max(2, _env_int(ENV_SWEEP_LOOKBACK, 20))
    sl_buf = _env_float(ENV_SL_ATR_BUF, 0.5)
    max_risk_atr = _env_float(ENV_MAX_RISK_ATR, 3.0)
    scan_window = max(atr_len + 3, _env_int(ENV_SCAN_WINDOW, 300))

    ts_close = _bar_close_ts(str(m5_bars[-1].get("ts") or "")) if m5_bars else ""
    min_bars = max(sweep_lb + 2, atr_len + 3)
    if len(m5_bars) < min_bars:
        return _skip(ts_close, symbol, f"bars<{min_bars}")

    window = m5_bars[-scan_window:]
    atr = _atr_series(window, atr_len)
    a = atr[-1]
    if a <= 0:
        return _skip(ts_close, symbol, "atr<=0")

    zones = build_zones(window, atr)
    demand = [z for z in zones if z["side"] == "demand"]
    supply = [z for z in zones if z["side"] == "supply"]

    last = window[-1]
    o, c = _f(last.get("open")), _f(last.get("close"))
    hi, lo = _f(last.get("high")), _f(last.get("low"))
    body = abs(c - o)
    up_wick = hi - max(o, c)
    dn_wick = min(o, c) - lo

    in_demand = any(z["bottom"] <= lo <= z["top"] for z in demand)
    in_supply = any(z["bottom"] <= hi <= z["top"] for z in supply)

    feat: dict[str, Any] = {
        "sniper_atr": round(a, 5),
        "sniper_zones_demand": len(demand), "sniper_zones_supply": len(supply),
        "sniper_in_demand": in_demand, "sniper_in_supply": in_supply,
        "sniper_up_wick": round(up_wick, 5), "sniper_dn_wick": round(dn_wick, 5),
        "sniper_body": round(body, 5),
    }

    # -- QM sweep reclaim (checked FIRST — see priority note above) ----------
    prior = window[-(sweep_lb + 1):-1]
    hi_prev = max(_f(b.get("high")) for b in prior)
    lo_prev = min(_f(b.get("low")) for b in prior)
    prev_bar = window[-2]
    sweep_h = hi > hi_prev and c < hi_prev and c < o
    sweep_l = lo < lo_prev and c > lo_prev and c > o
    qm_sell = sweep_h and c < _f(prev_bar.get("low"))
    qm_buy = sweep_l and c > _f(prev_bar.get("high"))
    if qm_buy or qm_sell:
        side = "buy" if qm_buy else "sell"
        sl = (lo - sl_buf * a) if qm_buy else (hi + sl_buf * a)
        risk = abs(c - sl)
        if 0 < risk <= max_risk_atr * a:
            swept = lo_prev if qm_buy else hi_prev
            d = _enter(ts_close, symbol, side, c, sl, "sniper_qm_sweep", session,
                       [f"QM {side}: swept {sweep_lb}-bar extreme {swept:.2f}, "
                        f"reclaimed + displaced past prev bar"],
                       {**feat, "sniper_swept_level": round(swept, 5)})
            if d:
                return d
        return _skip(ts_close, symbol, f"qm_risk {risk:.2f} outside (0, {max_risk_atr}xATR]")

    # -- ST2 wick rejection inside a zone ------------------------------------
    st2_buy = in_demand and dn_wick > body * st2_ratio and dn_wick > up_wick * wick_dom
    st2_sell = in_supply and up_wick > body * st2_ratio and up_wick > dn_wick * wick_dom
    if st2_buy and st2_sell:
        return _skip(ts_close, symbol, "st2_both_sides (doji in overlapping zones)")
    if st2_buy or st2_sell:
        side = "buy" if st2_buy else "sell"
        sl = (lo - sl_buf * a) if st2_buy else (hi + sl_buf * a)
        risk = abs(c - sl)
        if 0 < risk <= max_risk_atr * a:
            d = _enter(ts_close, symbol, side, c, sl, "sniper_st2_zone", session,
                       [f"ST2 {side}: wick rejection in "
                        f"{'demand' if st2_buy else 'supply'} zone "
                        f"(wick/body={(dn_wick if st2_buy else up_wick) / max(body, 1e-9):.1f})"],
                       feat)
            if d:
                return d
        return _skip(ts_close, symbol, f"st2_risk {risk:.2f} outside (0, {max_risk_atr}xATR]")

    return _skip(ts_close, symbol, "no_setup")

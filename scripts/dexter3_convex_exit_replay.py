#!/usr/bin/env python3
"""Dexter3 CONVEX EXIT replay -- does the LIVE ladder amputate the fat tail?

Owner measurement (2026-07-16, 986 v17-accepted XAU trades, 6000 M5 bars,
HOLD=48, SL-first honest replay):
  * 16.2%% of trades reach MFE >= 3R (mean MFE 6.11R); 21.9%% reach >= 2R
    (mean 5.17R); max MFE 20.64R -- the opportunity set is FAT-TAILED.
  * LIVE captures mean +0.24R, max EVER +0.44R -- we amputate the tail.
  * Trough (max adverse before peak) is EARLY (p50 bar 0, p75 bar 4); peak is
    LATE (p50 bar 23, p90 bar 46).
  * Tail runners' MAE-before-peak: p50 0.40R, p90 0.86R, NEVER > 1.0R -- a
    1.0R stop kills 0.0%% of 3R+ runners (a 0.5R stop would kill 37.5%%), so
    KEEP the 1R stop, do not tighten it.
  * XAU M5 ATR ~= 4.53 pts; median 1R ~= 6.88 pts (1R ~= 1.52 ATR).

The live ladder (env DEXTER3_OM_LADDER_CSV, dexter3/opening_manager.py
``ladder_floor_r``) arms a floor once peak_r crosses the first breakpoint and
only ever raises it. Its givebacks (peak - floor) are 0.35-0.83 ATR -- SUB-BAR
NOISE -- and it arms on the very first small pop, so the NORMAL ~0.40R dip
that precedes almost every real runner (see MAE-before-peak above) knocks it
out near breakeven before the actual move even starts. Every prior exit-
geometry replay in this repo (``dexter3_geometry_optimizer.py``) compared
candidates against a plain-TP or smart-close reference -- NEVER against the
ladder the live system actually runs. This script fixes that: the ladder
itself is simulated as the baseline every convex candidate must beat.

Three exit simulators, all SL-first conservative on the same bar, all
measured against the trade's OWN entry/sl (risk = abs(entry - sl)):
  1. ``_simulate_ladder``    -- THE TRUE LIVE BASELINE (discrete rung table).
  2. ``_simulate_convex``    -- NO TP; a single continuous trail that does
                                 not exist until peak_r >= arm_at_r, then
                                 trails ``giveback_atr`` x ATR behind the peak.
  3. ``_simulate_plain_tp``  -- reference: today's fixed TP/SL (reuses
                                 ``scripts.dexter3_edge_discovery._simulate``).

PYRAMID overlay (owner idea, measured +0.42R/runner in a separate study):
``--pyramid`` fills a SECOND leg via a LIMIT at ``entry -/+ pyramid_dip_r *
risk`` if that dip is touched within ``pyramid_window_bars`` of entry, using
the SAME sl and the SAME exit policy under test. The combined R is reported
in ORIGINAL-risk (leg1) units so it stays comparable across combos.

Anti-overfit discipline (mirrors ``dexter3_geometry_optimizer.py``):
  1. bars fetched ONCE, decide_hunt() decisions computed ONCE per bar;
  2. every combo scored on the DERIVE segment (first --split fraction) only;
  3. the VALIDATE segment is touched exactly once, by the finalists;
  4. a combo is BEATS-LADDER only if positive on BOTH segments AND its
     validate netR beats the live ladder baseline's validate netR.

Usage (VM, through the daemon):
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
    DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=60 \
        python scripts/dexter3_convex_exit_replay.py --symbol XAUUSD --count 6000 \
            --gates v17,v17-mission,none --pyramid

Research script only -- no live behavior change. Promotion path: a
BEATS-LADDER combo -> owner review -> env-tunable canary -> forward
measurement (this class of finding has died out-of-sample 4x already in this
repo -- one replay pass is evidence, not proof).
"""
from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dexter3_edge_discovery import (  # noqa: E402
    MIN_M5,
    _apply_entry_gate,
    _completed_by,
    _epoch,
    _simulate,
    _stamp_entry_gate_features,
)
from scripts.dexter3_geometry_optimizer import _equity  # noqa: E402
from dexter3 import hunt_mode  # noqa: E402
from dexter3.transport import make_client  # noqa: E402

ExitFn = Callable[[str, float, float, list, int], tuple[str, float, int]]


# ---------------------------------------------------------------------------
# ATR + ladder-csv parsing
# ---------------------------------------------------------------------------


def _atr_mean(bars: list) -> float:
    """Mean true range across ``bars`` (standard ATR TR formula), averaged
    over the WHOLE series rather than a rolling window -- this replay uses
    ONE scalar ATR for the entire convex-exit grid, matching how the owner
    reported the session figure ("XAU M5 ATR ~= 4.53 pts") as a single
    number rather than a per-bar rolling value."""
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = float(bars[0].get("close", 0.0))
    for b in bars[1:]:
        hi = float(b.get("high", 0.0))
        lo = float(b.get("low", 0.0))
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = float(b.get("close", 0.0))
    return sum(trs) / len(trs) if trs else 0.0


def _parse_ladder_csv(csv_str: str) -> list[tuple[float, float]]:
    """Parse ``peak_r:floor_r,...`` into an ascending list of (peak_r, floor_r)
    rungs -- the same shape as ``dexter3.opening_manager.OMConfig.ladder_points``
    (default value here is the current live ``DEXTER3_OM_LADDER_CSV``)."""
    rungs: list[tuple[float, float]] = []
    for part in csv_str.split(","):
        part = part.strip()
        if not part:
            continue
        peak_s, floor_s = part.split(":")
        rungs.append((float(peak_s), float(floor_s)))
    return sorted(rungs, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Exit simulators -- all SL-first conservative on the same bar
# ---------------------------------------------------------------------------


def _simulate_ladder(side: str, entry: float, sl: float, future: list,
                      rungs: list[tuple[float, float]], max_hold: int) -> tuple[str, float, int]:
    """THE TRUE LIVE BASELINE -- a discrete-rung replay of the live DRAGON
    LADDER (``dexter3.opening_manager.ladder_floor_r``, simplified from its
    piecewise-LINEAR interpolation to discrete steps per the replay spec: the
    live code interpolates between rungs, this walks the rung TABLE directly,
    which is the conservative/simpler approximation for a single-trade OHLC
    replay). Each bar: track the best-ever favorable excursion (``peak_r``,
    using bar high for buy / low for sell -- never decreases). The armed
    floor is the highest rung whose ``peak_r`` threshold has been reached; if
    price ever retraces to that floor (bar low for buy / high for sell), the
    position exits AT the floor level. SL is checked first (conservative,
    same convention as every other simulator in this repo). Times out at
    ``max_hold`` -> mark to the last walked bar's close, same as ``_simulate``."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    rungs_sorted = sorted(rungs, key=lambda x: x[0])
    peak_r = 0.0
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        if side == "buy":
            if lo <= sl:                       # conservative: SL wins ties
                return "loss", -1.0, held
            bar_peak_r = (hi - entry) / risk
        else:
            if hi >= sl:
                return "loss", -1.0, held
            bar_peak_r = (entry - lo) / risk
        if bar_peak_r > peak_r:
            peak_r = bar_peak_r
        floor_r = None
        for rung_peak, rung_floor in rungs_sorted:
            if rung_peak <= peak_r:
                floor_r = rung_floor
            else:
                break
        if floor_r is not None:
            adverse_r = (lo - entry) / risk if side == "buy" else (entry - hi) / risk
            if adverse_r <= floor_r:
                return "ladder", floor_r, held
    held = min(max_hold, len(future)) - 1
    last = float(future[max_hold - 1].get("close", entry)) if len(future) >= max_hold else float(future[-1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _simulate_convex(side: str, entry: float, sl: float, future: list, arm_at_r: float,
                      giveback_atr: float, atr: float, max_hold: int,
                      close_stop: bool = False,
                      close_stop_hard_mult: float = 0.0,
                      close_stop_vol_gate: float = 0.0) -> tuple[str, float, int]:
    """NO TP. The trail does not exist at all until the best-ever favorable
    excursion (``peak_r``, same tracking as ``_simulate_ladder``) reaches
    ``arm_at_r``. Once armed (permanently -- it never un-arms even if peak_r
    later dips before rising again), the exit level is a SINGLE continuous
    line ``peak_r - (giveback_atr * atr / risk)`` -- unlike the ladder's
    discrete steps, this never widens in jumps, so a fast runner keeps far
    more of its move than a stepped floor that lags behind price. SL-first
    conservative on the same bar; times out at ``max_hold`` -> last close.

    ``close_stop`` (2026-07-23, dpull wick-out investigation): when True, the
    HARD stop triggers only on a bar that CLOSES beyond ``sl`` (not an
    intrabar wick), and the loss is measured at that close (so a bar closing
    well past the stop costs > 1R -- the honest price of holding through
    noise). Motivated by two facts: (1) live dpull trade pid 655111580 was
    wicked out (M5 low 4143.28 < sl 4144.14) on a bar that CLOSED 4144.34
    ABOVE the stop then recovered; (2) the convex-replay's own MAE study --
    tail runners' worst adverse move is p90 0.86R, never > 1R -- means a
    ~0.5R stop (what a limit -0.5R entry creates) wicks out ~37.5% of the
    3R+ runners. A close-based stop keeps the 0.5R nominal risk but grants
    wick-immunity, so a runner that only dips intrabar survives. The arm and
    trail legs stay intrabar (favorable/profit-protection, unaffected)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    giveback_r = (giveback_atr * atr) / risk
    peak_r = 0.0
    armed = False
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        cl = float(bar.get("close", 0.0))
        # vol gate (2026-07-23): the crash/derive damage comes from holding a
        # close-stop through BIG volatile bars (a real move continuing). The
        # breaching bar's OWN range vs entry ATR is a lookahead-free
        # regime-at-the-bar signal: range > gate*ATR = a crash bar -> revert
        # to the intrabar -1R stop (cut fast, no extra loss); a calm bar with
        # a wick (range <= gate*ATR) keeps the close-stop wick-immunity that
        # saved the ranging/validate runners. Separates "noise wick" from
        # "crash-start wick" without forecasting the regime.
        volatile_bar = close_stop and close_stop_vol_gate > 0.0 and (hi - lo) > close_stop_vol_gate * atr
        if side == "buy":
            if close_stop and close_stop_hard_mult > 0.0 and lo <= sl - close_stop_hard_mult * risk:
                return "loss", -(1.0 + close_stop_hard_mult), held
            if volatile_bar and lo <= sl:
                return "loss", -1.0, held          # crash bar: cut intrabar
            stopped = (cl <= sl) if close_stop else (lo <= sl)
            if stopped:
                return "loss", ((cl - entry) / risk if close_stop else -1.0), held
            bar_peak_r = (hi - entry) / risk
        else:
            if close_stop and close_stop_hard_mult > 0.0 and hi >= sl + close_stop_hard_mult * risk:
                return "loss", -(1.0 + close_stop_hard_mult), held
            if volatile_bar and hi >= sl:
                return "loss", -1.0, held
            stopped = (cl >= sl) if close_stop else (hi >= sl)
            if stopped:
                return "loss", ((entry - cl) / risk if close_stop else -1.0), held
            bar_peak_r = (entry - lo) / risk
        if bar_peak_r > peak_r:
            peak_r = bar_peak_r
        if not armed and peak_r >= arm_at_r:
            armed = True
        if armed:
            trail_level_r = peak_r - giveback_r
            adverse_r = (lo - entry) / risk if side == "buy" else (entry - hi) / risk
            if adverse_r <= trail_level_r:
                return "trail", trail_level_r, held
    held = min(max_hold, len(future)) - 1
    last = float(future[max_hold - 1].get("close", entry)) if len(future) >= max_hold else float(future[-1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _simulate_plain_tp(side: str, entry: float, sl: float, tp: float, future: list,
                        max_hold: int) -> tuple[str, float, int]:
    """Reference row -- today's fixed TP/SL. Thin wrapper so this module has
    its own name for the third simulator per the spec; the actual logic is
    ``scripts.dexter3_edge_discovery._simulate`` (SL-first conservative, same
    timeout convention)."""
    return _simulate(side, entry, sl, tp, future, max_hold)


# ---------------------------------------------------------------------------
# Pyramid overlay
# ---------------------------------------------------------------------------


def _pyramid_fill(side: str, entry: float, sl: float, future: list, dip_r: float,
                   window_bars: int) -> tuple[bool, float | None, int | None]:
    """First bar (0-based index into ``future``, within ``window_bars``) whose
    range touches the pyramid LIMIT price ``entry -/+ dip_r * risk``. Returns
    (filled, fill_price, fill_idx) -- (False, None, None) if never touched."""
    risk = abs(entry - sl)
    if risk <= 0:
        return False, None, None
    dip_price = entry - dip_r * risk if side == "buy" else entry + dip_r * risk
    for idx, bar in enumerate(future[:window_bars]):
        lo = float(bar.get("low", 0.0))
        hi = float(bar.get("high", 0.0))
        touched = lo <= dip_price if side == "buy" else hi >= dip_price
        if touched:
            return True, dip_price, idx
    return False, None, None


def _leg_r(exit_fn: ExitFn, side: str, entry: float, sl: float, future: list, max_hold: int,
           spread_abs: float, commission_r: float) -> float | None:
    """One leg's cost-adjusted R, in that leg's OWN risk units (None = skip)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    outcome, r, _held = exit_fn(side, entry, sl, future, max_hold)
    if outcome == "skip":
        return None
    return r - ((spread_abs / risk) + commission_r)


def _combo_r(trade: dict, exit_fn: ExitFn, max_hold: int, spread_abs: float, commission_r: float,
             pyramid: bool, dip_r: float, window_bars: int) -> tuple[float, bool] | None:
    """Cost-adjusted R for one accepted trade under one exit policy. Returns
    (r, filled) where ``filled`` is always False when ``pyramid`` is off. When
    pyramid is on and the dip fills, the SECOND leg is simulated with the SAME
    ``exit_fn`` from its own (smaller) risk, then rescaled into leg1's
    (original) risk units before being added: leg2 profits $ = r2 * risk2, so
    in leg1-risk units that is r2 * (risk2 / risk1) -- this keeps combined R
    comparable across combos regardless of the second leg's smaller stop
    distance. Each leg pays its own spread/commission cost in its own R units
    before the rescale (a real second order still crosses the spread)."""
    side, entry, sl, future = trade["side"], trade["entry"], trade["sl"], trade["future"]
    risk1 = abs(entry - sl)
    if risk1 <= 0:
        return None
    r1 = _leg_r(exit_fn, side, entry, sl, future, max_hold, spread_abs, commission_r)
    if r1 is None:
        return None
    if not pyramid:
        return r1, False
    filled, fill_price, fill_idx = _pyramid_fill(side, entry, sl, future, dip_r, window_bars)
    if not filled:
        return r1, False
    future2 = future[fill_idx + 1:]
    r2 = _leg_r(exit_fn, side, fill_price, sl, future2, max_hold, spread_abs, commission_r)
    if r2 is None:
        return r1, True
    risk2 = abs(fill_price - sl)
    combined = r1 + r2 * (risk2 / risk1)
    return combined, True


# ---------------------------------------------------------------------------
# Exit-fn factories (per-trade, so plain-TP can close over each trade's own tp)
# ---------------------------------------------------------------------------


def _ladder_factory(rungs: list[tuple[float, float]]) -> Callable[[dict], ExitFn]:
    def factory(_trade: dict) -> ExitFn:
        def fn(side: str, entry: float, sl: float, future: list, max_hold: int) -> tuple[str, float, int]:
            return _simulate_ladder(side, entry, sl, future, rungs, max_hold)
        return fn
    return factory


def _convex_factory(arm_at_r: float, giveback_atr: float, atr: float) -> Callable[[dict], ExitFn]:
    def factory(_trade: dict) -> ExitFn:
        def fn(side: str, entry: float, sl: float, future: list, max_hold: int) -> tuple[str, float, int]:
            return _simulate_convex(side, entry, sl, future, arm_at_r, giveback_atr, atr, max_hold)
        return fn
    return factory


def _plain_factory() -> Callable[[dict], ExitFn]:
    def factory(trade: dict) -> ExitFn:
        tp = float(trade["tp"])

        def fn(side: str, entry: float, sl: float, future: list, max_hold: int) -> tuple[str, float, int]:
            return _simulate_plain_tp(side, entry, sl, tp, future, max_hold)
        return fn
    return factory


def _score_segment(trades: list[dict], exit_fn_factory: Callable[[dict], ExitFn], max_hold: int,
                    spread_abs: float, commission_r: float, pyramid: bool, dip_r: float,
                    window_bars: int) -> dict | None:
    rs: list[float] = []
    fills = 0
    for t in trades:
        exit_fn = exit_fn_factory(t)
        res = _combo_r(t, exit_fn, max_hold, spread_abs, commission_r, pyramid, dip_r, window_bars)
        if res is None:
            continue
        r, filled = res
        rs.append(r)
        if filled:
            fills += 1
    if not rs:
        return None
    net, pf, dd = _equity(rs)
    wr = 100.0 * sum(1 for r in rs if r > 0) / len(rs)
    return {
        "n": len(rs), "net": net, "pf": pf, "dd": dd, "wr": wr,
        "fill_rate": (100.0 * fills / len(rs)) if pyramid else None,
    }


def _print_row(gate: str, label: str, d_score: dict | None, v_score: dict | None, v_days: float,
               base_risk_usd: float, pyramid: bool, ladder_ref_net: float | None,
               verdict_override: str | None = None) -> None:
    if d_score is None or v_score is None:
        print(f"{gate:<12} {label:<32} | (insufficient trades)")
        return
    usd_day = v_score["net"] / v_days * base_risk_usd
    fill_str = f"{v_score['fill_rate']:.0f}%" if (pyramid and v_score.get("fill_rate") is not None) else "-"
    if verdict_override:
        verdict = verdict_override
    else:
        both_pos = d_score["net"] > 0 and v_score["net"] > 0
        beats = ladder_ref_net is not None and v_score["net"] > ladder_ref_net
        verdict = "BEATS-LADDER" if (both_pos and beats) else ("both+ but <=ladder" if both_pos else "fails validate")
    print(f"{gate:<12} {label:<32} | {d_score['n']:>4} {d_score['net']:>+8.2f} {d_score['pf']:>5.2f} | "
          f"{v_score['n']:>4} {v_score['net']:>+8.2f} {v_score['pf']:>5.2f} {v_score['dd']:>6.2f} "
          f"{v_score['wr']:>5.1f} {fill_str:>6} {usd_day:>+7.0f} {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=6000)
    ap.add_argument("--split", type=float, default=0.6, help="derive fraction (rest = validate, touched once)")
    ap.add_argument("--spread-abs", type=float, default=0.12)
    ap.add_argument("--commission-r", type=float, default=0.03)
    ap.add_argument("--gates", default="v17,v17-mission,none")
    ap.add_argument("--baseline-max-hold", type=int, default=48,
                     help="max M5 bars held for the ladder-baseline and plain-TP reference rows")
    ap.add_argument("--arm-at", default="1.0,1.5,2.0")
    ap.add_argument("--giveback-atr", default="1.0,1.5,2.0,3.0")
    ap.add_argument("--max-holds", default="24,48,96")
    ap.add_argument(
        "--ladder-csv",
        default="0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25",
        help="peak_r:floor_r pairs -- the live DEXTER3_OM_LADDER_CSV default",
    )
    ap.add_argument("--top-k", type=int, default=8, help="convex combos re-scored on the validate segment")
    ap.add_argument("--min-derive-trades", type=int, default=80, help="combos with fewer derive trades are ignored")
    ap.add_argument("--base-risk-usd", type=float, default=12.0, help="$ per 1R for the $/day translation")
    ap.add_argument("--pyramid", action="store_true", help="also fill a second LIMIT leg on the pre-run dip")
    ap.add_argument("--pyramid-dip-r", type=float, default=0.4, help="LIMIT offset from entry, in R of the original stop")
    ap.add_argument("--pyramid-window-bars", type=int, default=6, help="bars after entry the dip must fill within")
    args = ap.parse_args()

    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    m15 = c.get_trendbars(args.symbol, "m15", args.count)
    h1 = c.get_trendbars(args.symbol, "h1", max(200, args.count // 4))
    if len(m5) < MIN_M5 + 50:
        print(f"not enough M5 bars: {len(m5)}")
        return 2
    print(f"bars: M5={len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']})")

    atr = _atr_mean(m5)
    print(f"M5 ATR (mean TR, whole series) = {atr:.3f} pts")

    # -- phase 1: decisions ONCE per bar (independent of gate/exit) ----------
    decisions: list[tuple[int, object]] = []
    for i in range(MIN_M5, len(m5) - 2):
        prefix = m5[: i + 1]
        ts = str(m5[i].get("ts") or "")
        close_epoch = _epoch(ts) + 300
        m15c = [b for b in m15 if _completed_by(str(b.get("ts") or ""), close_epoch, 15)]
        h1c = [b for b in h1 if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
        try:
            d = hunt_mode.decide_hunt(args.symbol, prefix, m15c, h1c, None, args.spread_abs)
        except Exception:
            continue
        if d.action != "enter" or d.side is None or d.sl is None or d.tp is None:
            continue
        _stamp_entry_gate_features(d, prefix, h1c)
        decisions.append((i, d))
    print(f"decisions: {len(decisions)} enter candidates")

    # -- phase 2: gate acceptance per mode (entry-side, exit-independent) ----
    gate_modes = [g.strip() for g in args.gates.split(",") if g.strip()]
    accepted_by_gate: dict[str, list[dict]] = {}
    for mode in gate_modes:
        acc: list[dict] = []
        for i, d in decisions:
            gate = _apply_entry_gate(d, mode if mode != "none" else "none", str(d.ts_close or ""))
            if not bool(gate.get("allow", True)):
                continue
            acc.append({
                "i": i, "side": str(d.side), "entry": float(d.entry),
                "sl": float(d.sl), "tp": float(d.tp), "future": m5[i + 1:],
            })
        accepted_by_gate[mode] = acc
        print(f"gate={mode}: accepted {len(acc)}")

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)
    rungs = _parse_ladder_csv(args.ladder_csv)
    max_holds = [int(x) for x in args.max_holds.split(",") if x.strip()]
    arm_ats = [float(x) for x in args.arm_at.split(",") if x.strip()]
    givebacks = [float(x) for x in args.giveback_atr.split(",") if x.strip()]

    v_days = max(0.1, (_epoch(str(m5[-1]["ts"])) - _epoch(str(m5[split_bar]["ts"]))) / 86400.0)
    print(f"\nderive/validate split at bar {split_bar} (validate segment ~= {v_days:.1f} days)")

    fill_hdr = "fill%" if args.pyramid else "  -  "
    header = (f"{'gate':<12} {'combo':<32} | {'dN':>4} {'d_net':>8} {'d_PF':>5} | "
              f"{'vN':>4} {'v_net':>8} {'v_PF':>5} {'v_DD':>6} {'WR%':>5} {fill_hdr:>6} {'$/day':>7} verdict")

    for mode in gate_modes:
        trades = accepted_by_gate[mode]
        derive_trades = [t for t in trades if t["i"] < split_bar]
        validate_trades = [t for t in trades if t["i"] >= split_bar]
        print(f"\n=== gate={mode} (derive n={len(derive_trades)}, validate n={len(validate_trades)}) ===")
        if len(derive_trades) < args.min_derive_trades:
            print(f"only {len(derive_trades)} derive trades (< --min-derive-trades={args.min_derive_trades}) -- skipped")
            continue
        print(header)
        print("-" * len(header))

        # (a) ladder baseline -- THE reference every convex combo must beat
        ladder_factory = _ladder_factory(rungs)
        d_ladder = _score_segment(derive_trades, ladder_factory, args.baseline_max_hold, args.spread_abs,
                                   args.commission_r, args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
        v_ladder = _score_segment(validate_trades, ladder_factory, args.baseline_max_hold, args.spread_abs,
                                   args.commission_r, args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
        _print_row(mode, "LADDER-BASELINE (live)", d_ladder, v_ladder, v_days, args.base_risk_usd,
                   args.pyramid, None, verdict_override="REFERENCE")
        ladder_ref_net = v_ladder["net"] if v_ladder else None

        # (b) plain-TP reference
        plain_factory = _plain_factory()
        d_plain = _score_segment(derive_trades, plain_factory, args.baseline_max_hold, args.spread_abs,
                                  args.commission_r, args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
        v_plain = _score_segment(validate_trades, plain_factory, args.baseline_max_hold, args.spread_abs,
                                  args.commission_r, args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
        _print_row(mode, "PLAIN-TP (reference)", d_plain, v_plain, v_days, args.base_risk_usd,
                   args.pyramid, ladder_ref_net)

        # (c) convex grid -- scored on DERIVE only, top-K advance to validate
        combo_results = []
        for arm_at, gb, mh in product(arm_ats, givebacks, max_holds):
            factory = _convex_factory(arm_at, gb, atr)
            d_score = _score_segment(derive_trades, factory, mh, args.spread_abs, args.commission_r,
                                      args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
            if d_score is None or d_score["n"] < args.min_derive_trades:
                continue
            combo_results.append({"arm_at": arm_at, "giveback_atr": gb, "max_hold": mh, "derive": d_score})
        combo_results.sort(key=lambda c: (c["derive"]["net"], c["derive"]["pf"]), reverse=True)
        print(f"(convex grid: {len(combo_results)} combos met the derive floor; top {args.top_k} re-scored on validate)")

        for combo in combo_results[: args.top_k]:
            factory = _convex_factory(combo["arm_at"], combo["giveback_atr"], atr)
            v_score = _score_segment(validate_trades, factory, combo["max_hold"], args.spread_abs,
                                      args.commission_r, args.pyramid, args.pyramid_dip_r, args.pyramid_window_bars)
            label = f"convex arm{combo['arm_at']:.1f} gb{combo['giveback_atr']:.1f} h{combo['max_hold']}"
            _print_row(mode, label, combo["derive"], v_score, v_days, args.base_risk_usd,
                       args.pyramid, ladder_ref_net)

    print("\nRULES: report/act on VALIDATE numbers only. BEATS-LADDER requires positive net R on BOTH "
          "segments AND a validate netR strictly greater than the live-ladder baseline's validate netR "
          "on the SAME gate -- the ladder row above is the bar to clear, not a plain TP or a smart-close "
          "reference. This exact class of finding (an exit-geometry edge that looks decisive in one "
          "replay pass) has died out-of-sample 4 times already in this repo -- treat anything short of a "
          "clean both-segments beat as noise until it survives a second, independent replay window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

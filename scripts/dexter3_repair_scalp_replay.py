#!/usr/bin/env python3
"""Dexter3 REPAIR-SCALP REPLAY (owner hypothesis, 2026-07-15).

OWNER HYPOTHESIS: when a lane position is TRAPPED (underwater past the repair
trigger), today's basket engine opens ONE opposite repair leg and stops
("khor pai thi laeo job") -- today's live example took its own SL, wounding
the basket twice. This script measures an alternative: from the repair
trigger onward, repeatedly SCALP the mirror side v1.0-style -- enter at each
M5 close with no scalp open, bank the whole scalp at the first close where
its own floating R >= --bank-target-r (the v1.0 close_all_in_profit rule,
commit 3ca3341 basket_manager.py:48, replayed here via
``scripts.dexter3_geometry_optimizer._simulate_bank``), re-enter at the next
M5 close, repeat while the PARENT stays open -- harvesting the move that is
hurting the parent, so the trapped episode's TOTAL loss shrinks or even nets
positive despite the parent's eventual SL.

RIGOR (same discipline as dexter3_edge_discovery.py / dexter3_geometry_optimizer.py):
  * NO LOOKAHEAD anywhere: the parent's own decision, its repair trigger, and
    every scalp inside the episode only ever see bars up to their own close.
  * Every scalp future is TRUNCATED at the parent's resolution bar T -- when
    the parent resolves, the repair episode ends; no scalp can see beyond it.
  * Conservative SL-first fills (inherited from ``_simulate`` / ``_simulate_bank``).
  * COST-ADJUSTED: every scalp pays spread (over its OWN risk distance) +
    flat commission in R, identically to how the optimizer costs its combos.
  * Trades whose floating R never breaches ``-abs(--repair-trigger-r)`` before
    the parent resolves are NOT a "trapped" episode -- excluded from the
    study, and the exclusion count is reported (never silently dropped).
  * derive/validate split follows the SAME --split contract as the geometry
    optimizer: the split point is fixed by bar index once, and both the
    parent-trade selection (via decide_hunt + entry-gate) and the episode
    scoring reuse that split -- no numbers reported here are re-tuned on
    validate data.

Research-only script. Report-only: no live behavior change, no orders, no
state writes. Promotion path (if the hypothesis holds): owner review of the
VALIDATE numbers -> forward/shadow proof -> only then any live change to the
basket repair-leg policy.

PARENT MODEL (settled 2026-07-15 after two degenerate attempts -- keep this
history so nobody re-walks it): a close-based trigger past -1.0R requires a
parent model whose closes can SURVIVE beyond -1.0R.
  * "plain" (hard SL at the original distance) caps floating closes at
    -1.0R: the sl wick that would allow a worse close kills the parent on
    that same bar. Trigger >= 1.0 lands on T itself -> 0-scalp episodes.
  * "smart" (``_simulate_smart``) was the first fix attempt and is ALSO
    degenerate -- proven by the 10k-bar run (1015 studied episodes, 0
    scalps, +0.00 improvement everywhere): its close-confirmed SL branch
    (``cl <= sl`` / ``cl >= sl``) exits the parent on the very first close
    beyond -1.0R, so the trigger bar always coincides with T.
  * "wide" (the CLI default) is the model the LIVE evidence demands: real
    trade 652652362 (2026-07-15) floated at -1.33R for 3 HOURS before a
    time-based cap_stop -- at the 1oz volume floor the broker stop sits at
    the disaster distance, not the design sl. So: plain hard-SL simulation
    with the SL widened to entry +/- (--parent-sl-mult x the ORIGINAL
    |entry-sl|), original TP kept, NO close-based early exit, and
    --parent-max-hold 36 (the live 3h cap_stop in M5 bars). All R is
    reported in ORIGINAL-risk units (a wide parent's SL death = -mult R);
    the trigger and the scalp risk distance are original-risk based in
    every style.
"plain" and "smart" remain selectable for comparison and for the synthetic
unit tests, whose hand-computed geometry uses sub-1.0 triggers.

Usage (VM, through the daemon):
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
    DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=60 \
        python scripts/dexter3_repair_scalp_replay.py --count 3000

    python scripts/dexter3_repair_scalp_replay.py --count 3000 --per-episode
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dexter3_edge_discovery import (  # noqa: E402
    MIN_M5,
    _apply_entry_gate,
    _completed_by,
    _epoch,
    _simulate,
    _simulate_smart,
    _stamp_entry_gate_features,
)
from scripts.dexter3_geometry_optimizer import _equity, _simulate_bank  # noqa: E402
from dexter3 import hunt_mode  # noqa: E402
from dexter3.transport import make_client  # noqa: E402


def _mirror_side(side: str) -> str:
    return "sell" if side == "buy" else "buy"


def _find_repair_trigger(side: str, entry: float, sl: float, future_upto_resolution: list,
                          trigger_r: float) -> int | None:
    """Walk ``future_upto_resolution`` (the parent's own future bars, already
    truncated to end at its resolution bar T inclusive) at each M5 CLOSE,
    tracking the parent's floating R. Returns the 0-based index of the first
    bar whose close-R drops to <= -abs(trigger_r), or None if it never does
    (the trade is not a "trapped" episode)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    thresh = -abs(trigger_r)
    for idx, bar in enumerate(future_upto_resolution):
        cl = float(bar.get("close", 0.0))
        r = (cl - entry) / risk if side == "buy" else (entry - cl) / risk
        if r <= thresh:
            return idx
    return None


def _run_repair_scalps(parent_side: str, parent_entry: float, parent_sl: float, episode_bars: list,
                        scalp_sl_frac: float, scalp_max_hold: int, bank_target_r: float,
                        spread_abs: float, commission_r: float) -> list[float]:
    """From the trigger bar (``episode_bars[0]``) to the parent's resolution
    bar T (``episode_bars[-1]``), repeatedly open a MIRROR-side scalp at each
    M5 close with no scalp open. Each scalp's risk distance is
    ``scalp_sl_frac`` x the PARENT's |entry-sl|; it resolves via
    ``_simulate_bank`` (v1.0 bank-the-whole-position rule) against the bars
    strictly after its own open, which are already bounded by T since
    ``episode_bars`` itself never extends past it -- a scalp still open when
    only T remains marks to T's close (``_simulate_bank``'s own timeout
    convention). A fresh scalp is never opened ON T's own bar (T is the
    terminal mark point, not a new entry point -- the parent is gone by then).
    Returns the cost-adjusted R of every scalp taken, in order."""
    n = len(episode_bars)
    parent_risk = abs(parent_entry - parent_sl)
    scalp_risk = scalp_sl_frac * parent_risk
    mirror = _mirror_side(parent_side)
    scalp_rs: list[float] = []
    i = 0
    while i < n - 1:  # never open a fresh scalp at T's own bar
        entry_scalp = float(episode_bars[i].get("close", 0.0))
        if mirror == "buy":
            sl_scalp = entry_scalp - scalp_risk
        else:
            sl_scalp = entry_scalp + scalp_risk
        remaining = episode_bars[i + 1:]
        outcome, r, held = _simulate_bank(mirror, entry_scalp, sl_scalp, remaining,
                                           scalp_max_hold, bank_target_r)
        if outcome == "skip":
            break  # degenerate zero-risk scalp -- stop scalping this episode
        cost_r = (spread_abs / scalp_risk if scalp_risk > 0 else 0.0) + commission_r
        scalp_rs.append(r - cost_r)
        i = i + 1 + held + 1  # next scalp opens at the M5 close AFTER this one resolved
    return scalp_rs


def _build_episode(trade: dict, parent_max_hold: int, trigger_r: float, scalp_sl_frac: float,
                    scalp_max_hold: int, bank_target_r: float, spread_abs: float,
                    commission_r: float, parent_style: str = "plain",
                    parent_disaster: float = 2.0, parent_sl_mult: float = 2.0) -> dict:
    """Score one accepted parent trade end to end: resolve it under the
    chosen parent exit model to get its resolution bar T and cost-adjusted R,
    find its repair trigger (if any) within [0, T], and -- only for trapped
    episodes -- run the repair-scalp engine truncated at T.

    ``parent_style`` (the CLI defaults to "wide"; "plain" stays the function
    default so the synthetic unit tests keep their hand-computed geometry):
      * "plain" = hard SL/TP at the original sl. Structurally caps floating
        closes at -1.0R (the sl wick that would allow a worse close kills the
        parent on that same bar), so a close-based trigger >= 1.0 can never
        fire mid-life -- degenerate for this study, kept for the unit tests.
      * "smart" = close-confirmed SL + disaster wick (``_simulate_smart``,
        ``parent_disaster`` x risk). ALSO degenerate here (proven by the 10k
        run of 2026-07-15: 1015 episodes, 0 scalps): its ``cl <= sl`` /
        ``cl >= sl`` branch exits the parent on the very first close beyond
        -1.0R, so the trigger bar always coincides with T.
      * "wide" = plain hard-SL simulation with the SL WIDENED to
        entry +/- (``parent_sl_mult`` x the ORIGINAL |entry-sl|), original TP
        kept, NO close-based early exit. This models the live evidence (trade
        652652362, 2026-07-15: held -1.33R for 3 hours before a time-based
        cap_stop) -- the broker stop at the 1oz floor sits at the disaster
        distance, not at the design sl, so closes between -1.0R and
        -``parent_sl_mult`` R are survivable and the 1.2R trigger is
        reachable mid-life.

    ALL returned R values are in ORIGINAL-risk units (risk = the original
    |entry-sl|): "wide" simulates against the widened distance and converts
    back via r_widened * parent_sl_mult, so a wide parent's SL death reports
    as -``parent_sl_mult`` R; "plain"/"smart" already measure R against the
    original distance. The repair trigger and the scalp risk distance are
    original-risk based in every style (both consume the trade's original
    ``sl`` directly).

    Returns a dict with ``excluded`` = None (studied), ``"zero_risk"``
    (parent has no risk distance), or ``"no_trigger"`` (never breached the
    trigger before resolving -- not a trapped episode)."""
    side, entry, sl, tp = trade["side"], trade["entry"], trade["sl"], trade["tp"]
    future = trade["future"]
    risk_orig = abs(entry - sl)
    if parent_style == "smart":
        outcome, r_raw, held = _simulate_smart(side, entry, sl, tp, future, parent_max_hold, parent_disaster)
    elif parent_style == "wide":
        if parent_sl_mult <= 0:
            raise ValueError(f"parent_sl_mult must be > 0, got {parent_sl_mult}")
        sl_wide = (entry - parent_sl_mult * (entry - sl)) if side == "buy" \
            else (entry + parent_sl_mult * (sl - entry))
        outcome, r_widened, held = _simulate(side, entry, sl_wide, tp, future, parent_max_hold)
        # _simulate measured R against the WIDENED distance; convert back to
        # ORIGINAL-risk units (SL death = -1.0 widened = -parent_sl_mult orig)
        r_raw = r_widened * parent_sl_mult
    else:
        outcome, r_raw, held = _simulate(side, entry, sl, tp, future, parent_max_hold)
    if outcome == "skip":
        return {"excluded": "zero_risk", "T": None, "trigger_idx": None,
                "baseline_r": None, "scalp_rs": [], "repaired_total": None}
    t_idx = held  # 0-based index into `future` of the parent's resolution bar
    # cost in ORIGINAL-risk units -- same formula _combo_r applies for its
    # sl_mult=1.0 styles (spread over the original risk distance + flat
    # commission R); inlined here because "wide" needs the original-risk
    # denominator while its simulation ran against the widened one
    baseline_r = r_raw - ((spread_abs / risk_orig if risk_orig > 0 else 0.0) + commission_r)
    future_upto_t = future[: t_idx + 1]
    trigger_idx = _find_repair_trigger(side, entry, sl, future_upto_t, trigger_r)
    if trigger_idx is None:
        return {"excluded": "no_trigger", "T": t_idx, "trigger_idx": None,
                "baseline_r": baseline_r, "scalp_rs": [], "repaired_total": None}
    episode_bars = future_upto_t[trigger_idx:]
    scalp_rs = _run_repair_scalps(side, entry, sl, episode_bars, scalp_sl_frac, scalp_max_hold,
                                   bank_target_r, spread_abs, commission_r)
    repaired_total = baseline_r + sum(scalp_rs)
    return {
        "excluded": None,
        "T": t_idx,
        "trigger_idx": trigger_idx,
        "baseline_r": baseline_r,
        "scalp_rs": scalp_rs,
        "repaired_total": repaired_total,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=3000)
    ap.add_argument("--split", type=float, default=0.6, help="derive fraction (rest = validate)")
    ap.add_argument("--spread-abs", type=float, default=0.12)
    ap.add_argument("--commission-r", type=float, default=0.03)
    ap.add_argument("--gates", default="v17,v17-mission,none")
    ap.add_argument("--parent-max-hold", type=int, default=36,
                    help="parent resolution cap in M5 bars (live basket cap_stop = 3h = 36 bars, "
                         "per trade 652652362 on 2026-07-15)")
    ap.add_argument("--repair-trigger-r", type=float, default=1.2,
                    help=(
                        "parent floating R at an M5 close (ORIGINAL-risk units) that marks the "
                        "position TRAPPED. Reachable mid-life only under --parent-style wide: "
                        "plain caps closes at -1.0R (sl wick kills first) and smart exits on the "
                        "first close beyond -1.0R, so under those styles a >= 1.0 trigger always "
                        "lands on T itself (0-scalp episode) -- see _build_episode's docstring."
                    ))
    ap.add_argument("--scalp-sl-frac", type=float, default=1.0,
                    help="scalp risk distance = this x the parent's ORIGINAL |entry-sl|")
    ap.add_argument("--scalp-max-hold", type=int, default=12, help="max M5 bars to hold one scalp")
    ap.add_argument(
        "--parent-style", choices=("plain", "smart", "wide"), default="wide",
        help=(
            "parent exit model: wide (DEFAULT -- hard SL widened to entry +/- "
            "parent-sl-mult x the ORIGINAL risk, no close-based early exit; matches live "
            "evidence: trade 652652362 held -1.33R for 3h before a time-based cap_stop), "
            "smart (close-confirmed SL -- exits on the first close beyond -1.0R, so the "
            "1.2R trigger degenerates to T; kept for comparison), or plain (hard SL at "
            "the original distance -- same degeneracy; kept for the unit tests)"
        ),
    )
    ap.add_argument("--parent-disaster", type=float, default=2.0,
                    help="disaster stop multiple for --parent-style smart (live ref 2.0)")
    ap.add_argument("--parent-sl-mult", type=float, default=2.0,
                    help="--parent-style wide: broker stop distance = this x the ORIGINAL "
                         "|entry-sl|; the parent's SL death reports as -this R in original units")
    ap.add_argument("--bank-target-r", type=float, default=0.2,
                    help="bank the whole scalp at the first close >= this R (v1.0 default)")
    ap.add_argument("--per-episode", action="store_true",
                    help="print one line per trapped episode (for eyeballing)")
    args = ap.parse_args()

    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    m15 = c.get_trendbars(args.symbol, "m15", args.count)
    h1 = c.get_trendbars(args.symbol, "h1", max(200, args.count // 4))
    if len(m5) < MIN_M5 + 50:
        print(f"not enough M5 bars: {len(m5)}")
        return 2
    print(f"bars: M5={len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']})")

    # -- phase 1: decisions ONCE per bar (mirrors geometry_optimizer phase 1) --
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

    # -- phase 2: gate acceptance per mode (mirrors geometry_optimizer phase 2) --
    gate_modes = [g.strip() for g in args.gates.split(",") if g.strip()]
    accepted_by_gate: dict[str, list[dict]] = {}
    for mode in gate_modes:
        acc: list[dict] = []
        for i, d in decisions:
            gate = _apply_entry_gate(d, mode if mode != "none" else "none", str(d.ts_close or ""))
            if not bool(gate.get("allow", True)):
                continue
            acc.append({
                "i": i,
                "side": str(d.side),
                "entry": float(d.entry),
                "sl": float(d.sl),
                "tp": float(d.tp),
                "future": m5[i + 1:],
            })
        accepted_by_gate[mode] = acc
        print(f"gate={mode}: accepted {len(acc)}")

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)

    # -- phase 3: build every episode, split derive/validate -----------------
    header = (f"{'segment':<9} {'gate':<12} {'N':>4} {'base_mean':>9} {'base_net':>9} "
              f"{'rep_mean':>9} {'rep_net':>9} {'improve':>8} {'%improved':>9} {'worst_dR':>8} "
              f"{'scalps/ep':>9} {'scalp_WR':>8} {'scalp_R':>7}")
    print()
    print(header)
    print("-" * len(header))

    per_episode_lines: list[str] = []
    for mode in gate_modes:
        trades = accepted_by_gate[mode]
        derive = [t for t in trades if t["i"] < split_bar]
        validate = [t for t in trades if t["i"] >= split_bar]
        for segment, seg_trades in (("derive", derive), ("validate", validate)):
            episodes = []
            excl_zero = excl_no_trigger = 0
            for t in seg_trades:
                ep = _build_episode(t, args.parent_max_hold, args.repair_trigger_r,
                                     args.scalp_sl_frac, args.scalp_max_hold, args.bank_target_r,
                                     args.spread_abs, args.commission_r,
                                     parent_style=args.parent_style,
                                     parent_disaster=args.parent_disaster,
                                     parent_sl_mult=args.parent_sl_mult)
                if ep["excluded"] == "zero_risk":
                    excl_zero += 1
                    continue
                if ep["excluded"] == "no_trigger":
                    excl_no_trigger += 1
                    continue
                episodes.append((t, ep))
                if args.per_episode:
                    per_episode_lines.append(
                        f"{segment:<9} {mode:<12} i={t['i']:>6} T={ep['T']:>3} "
                        f"trig={ep['trigger_idx']:>3} base={ep['baseline_r']:>+7.3f}R "
                        f"repaired={ep['repaired_total']:>+7.3f}R delta={(ep['repaired_total'] - ep['baseline_r']):>+7.3f}R "
                        f"n_scalps={len(ep['scalp_rs']):>2}"
                    )
            n = len(episodes)
            if n == 0:
                print(f"{segment:<9} {mode:<12} {n:>4}  -- no trapped episodes "
                      f"(excluded: {excl_zero} zero-risk, {excl_no_trigger} never triggered)")
                continue
            baseline_rs = [ep["baseline_r"] for _, ep in episodes]
            repaired_rs = [ep["repaired_total"] for _, ep in episodes]
            deltas = [rep - base for base, rep in zip(baseline_rs, repaired_rs)]
            all_scalp_rs = [r for _, ep in episodes for r in ep["scalp_rs"]]
            base_net, _, _ = _equity(baseline_rs)
            rep_net, _, _ = _equity(repaired_rs)
            base_mean = base_net / n
            rep_mean = rep_net / n
            improve = rep_net - base_net
            pct_improved = sum(1 for d in deltas if d > 0) / n * 100.0
            worst_delta = min(deltas)
            scalps_per_ep = len(all_scalp_rs) / n
            scalp_wr = (sum(1 for r in all_scalp_rs if r > 0) / len(all_scalp_rs) * 100.0) if all_scalp_rs else 0.0
            scalp_mean = (sum(all_scalp_rs) / len(all_scalp_rs)) if all_scalp_rs else 0.0
            print(f"{segment:<9} {mode:<12} {n:>4} {base_mean:>+9.3f} {base_net:>+9.2f} "
                  f"{rep_mean:>+9.3f} {rep_net:>+9.2f} {improve:>+8.2f} {pct_improved:>8.0f}% "
                  f"{worst_delta:>+8.3f} {scalps_per_ep:>9.2f} {scalp_wr:>7.0f}% {scalp_mean:>+7.3f}"
                  f"  (excluded: {excl_zero} zero-risk, {excl_no_trigger} never triggered)")

    if args.per_episode and per_episode_lines:
        print("\n=== PER-EPISODE (trapped episodes only) ===")
        for line in per_episode_lines:
            print(line)

    print("\nRULES: report/act on VALIDATE numbers only. 'worst_dR' is the honesty column -- chop "
          "can bleed BOTH the parent and the repair scalps at once; a positive derive+validate "
          "improvement that still has a large negative worst_dR means the win is not universal. "
          "This replay approximates live OM exits and the basket's true aggregate risk -- a "
          "canary must still prove itself forward before any change to the live repair-leg policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

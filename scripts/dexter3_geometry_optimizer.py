#!/usr/bin/env python3
"""Dexter3 disciplined exit-geometry optimizer ($100/day mission, 2026-07-11).

Searches the EXIT-GEOMETRY + GATE-MODE space (the levers that apply uniformly
to every trade — far less overfit-prone than bucket rules, which the promotion
gate has already killed twice) with the anti-overfit discipline built in:

  1. bars are fetched ONCE; decide_hunt() decisions are computed ONCE per bar
     (entry decisions are independent of exit params and gate mode);
  2. every combo is scored on the DERIVE segment (first --split fraction of
     bars) only;
  3. the VALIDATE segment is touched exactly once — by the final top-K combos —
     and the numbers reported for promotion are the VALIDATE numbers;
  4. a combo is canary-eligible only if it is positive on BOTH segments and
     beats the CURRENT-LIVE reference combo on the validate segment.

Usage (VM, through the daemon):
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
    DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=60 \
        python scripts/dexter3_geometry_optimizer.py --count 3000

Research script only — no live behavior change. Promotion path: validate-PASS
combo -> owner review -> env-tunable canary -> forward measurement.
"""
from __future__ import annotations

import argparse
import sys
from itertools import product
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
from dexter3 import hunt_mode  # noqa: E402
from dexter3.vp_regime import profile_regime  # noqa: E402
from dexter3.transport import make_client  # noqa: E402


def _equity(rs: list[float]) -> tuple[float, float, float]:
    """(net, PF, maxDD) of an R sequence in trade order."""
    gw = sum(x for x in rs if x > 0)
    gl = sum(x for x in rs if x <= 0)
    pf = (gw / abs(gl)) if gl < 0 else float("inf")
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return sum(rs), pf, dd


def _combo_r(trade: dict, style: str, max_hold: int, disaster: float, sl_mult: float,
             spread_abs: float, commission_r: float) -> float | None:
    """Cost-adjusted R for one accepted trade under one exit combo (None=skip)."""
    entry, sl, tp = trade["entry"], trade["sl"], trade["tp"]
    side = trade["side"]
    if sl_mult != 1.0:
        sl = entry - (entry - sl) * sl_mult if side == "buy" else entry + (sl - entry) * sl_mult
    if style == "smart":
        outcome, r, _ = _simulate_smart(side, entry, sl, tp, trade["future"], max_hold, disaster)
    else:
        outcome, r, _ = _simulate(side, entry, sl, tp, trade["future"], max_hold)
    if outcome == "skip":
        return None
    risk = abs(entry - sl)
    return r - ((spread_abs / risk if risk > 0 else 0.0) + commission_r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=3000)
    ap.add_argument("--split", type=float, default=0.6, help="derive fraction (rest = validate, touched once)")
    ap.add_argument("--spread-abs", type=float, default=0.12)
    ap.add_argument("--commission-r", type=float, default=0.03)
    ap.add_argument("--gates", default="v17,v17-mission,v18,none")
    ap.add_argument("--top-k", type=int, default=8, help="combos re-scored on the validate segment")
    ap.add_argument("--min-derive-trades", type=int, default=80, help="combos with fewer derive trades are ignored")
    ap.add_argument("--base-risk-usd", type=float, default=12.0, help="$ per 1R for the $/day translation")
    ap.add_argument(
        "--brain",
        action="store_true",
        help="deprecated alias for --producer brain",
    )
    ap.add_argument(
        "--producer",
        choices=("hunt", "brain", "vp"),
        default="hunt",
        help=(
            "decision producer: hunt = decide_hunt (participation-first, LIVE default); "
            "brain = hunter_brain.decide (selective setups + RR floor); "
            "vp = dexter3.volume_profile.decide_vp (NEW logic 2026-07-11: LVN rejection / "
            "POC reversion / HVN break-retest from a rolling tick-volume profile — needs "
            "bars with volume, i.e. openapi transport at >= the volume-passthrough fix)."
        ),
    )
    args = ap.parse_args()
    if args.brain:
        args.producer = "brain"

    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    m15 = c.get_trendbars(args.symbol, "m15", args.count)
    h1 = c.get_trendbars(args.symbol, "h1", max(200, args.count // 4))
    if len(m5) < MIN_M5 + 50:
        print(f"not enough M5 bars: {len(m5)}")
        return 2
    print(f"bars: M5={len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']})")

    # -- phase 1: decisions ONCE per bar (independent of gate/exit) ----------
    decisions: list[tuple[int, object]] = []
    for i in range(MIN_M5, len(m5) - 2):
        prefix = m5[: i + 1]
        ts = str(m5[i].get("ts") or "")
        close_epoch = _epoch(ts) + 300
        m15c = [b for b in m15 if _completed_by(str(b.get("ts") or ""), close_epoch, 15)]
        h1c = [b for b in h1 if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
        try:
            if args.producer == "brain":
                from dexter3 import hunter_brain
                d = hunter_brain.decide(args.symbol, None, prefix, m15c, h1c, journal_stats=None)
            elif args.producer == "vp":
                from dexter3 import market_lens, volume_profile
                session = str(market_lens.session_context(ts).get("value") or "unknown")
                d = volume_profile.decide_vp(args.symbol, prefix, args.spread_abs, session=session)
            else:
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
                "i": i,
                "side": str(d.side),
                "entry": float(d.entry),
                "sl": float(d.sl),
                "tp": float(d.tp),
                "future": m5[i + 1:],
                "vp_regime": profile_regime(m5[:i]),
            })
        accepted_by_gate[mode] = acc
        print(f"gate={mode}: accepted {len(acc)}")

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)

    # -- phase 3: grid scored on DERIVE only ---------------------------------
    styles = ["plain", "smart"]
    max_holds = [12, 24, 48, 96]
    disasters = [1.5, 2.0, 2.5, 3.0]
    sl_mults = [1.0, 1.25, 1.5]
    results: list[dict] = []
    for mode in gate_modes:
        trades = accepted_by_gate[mode]
        derive_trades = [t for t in trades if t["i"] < split_bar]
        for style, mh, dis, slm in product(styles, max_holds, disasters, sl_mults):
            if style == "plain" and dis != disasters[0]:
                continue  # disaster only applies to smart
            rs = [
                r for t in derive_trades
                if (r := _combo_r(t, style, mh, dis, slm, args.spread_abs, args.commission_r)) is not None
            ]
            if len(rs) < args.min_derive_trades:
                continue
            net, pf, dd = _equity(rs)
            results.append({
                "gate": mode, "style": style, "max_hold": mh,
                "disaster": dis if style == "smart" else None, "sl_mult": slm,
                "d_n": len(rs), "d_net": net, "d_pf": pf, "d_dd": dd,
            })
    if not results:
        print("no combo met the min-derive-trades floor")
        return 1
    # rank: derive net first, PF tiebreak
    results.sort(key=lambda x: (x["d_net"], x["d_pf"]), reverse=True)
    print(f"\ngrid: {len(results)} combos scored on derive (first {args.split:.0%}); top {args.top_k} advance")

    # current-live reference: v17 + smart + hold 24 + disaster 2.0 + slm 1.0
    ref = {"gate": "v17", "style": "smart", "max_hold": 24, "disaster": 2.0, "sl_mult": 1.0}

    # -- phase 4: VALIDATE — touched exactly once, by the finalists + ref ----
    def _validate(combo: dict) -> tuple[int, float, float, float]:
        trades = [t for t in accepted_by_gate[combo["gate"]] if t["i"] >= split_bar]
        rs = [
            r for t in trades
            if (r := _combo_r(t, combo["style"], combo["max_hold"], combo["disaster"] or 2.0,
                              combo["sl_mult"], args.spread_abs, args.commission_r)) is not None
        ]
        net, pf, dd = _equity(rs)
        return len(rs), net, pf, dd

    v_days = max(0.1, (_epoch(str(m5[-1]["ts"])) - _epoch(str(m5[split_bar]["ts"]))) / 86400.0)
    ref_n, ref_net, ref_pf, ref_dd = _validate(ref)
    print(f"\nvalidate segment ≈ {v_days:.1f} days | CURRENT-LIVE REF (v17/smart/24/2.0/1.0): "
          f"n={ref_n} net={ref_net:+.2f}R PF={ref_pf:.2f} maxDD={ref_dd:.2f}R "
          f"(~${ref_net / v_days * args.base_risk_usd:+.0f}/day at ${args.base_risk_usd:.0f}/R)\n")

    header = (f"{'gate':<12} {'style':<6} {'hold':>4} {'dis':>4} {'slm':>4} | "
              f"{'dN':>4} {'d_net':>8} {'d_PF':>5} | {'vN':>4} {'v_net':>8} {'v_PF':>5} {'v_DD':>6} {'$/day':>7} verdict")
    print(header)
    print("-" * len(header))
    for combo in results[: args.top_k]:
        v_n, v_net, v_pf, v_dd = _validate(combo)
        usd_day = v_net / v_days * args.base_risk_usd
        both_pos = combo["d_net"] > 0 and v_net > 0
        beats_ref = v_net > ref_net
        verdict = "CANARY-ELIGIBLE" if (both_pos and beats_ref) else ("both+ but <=ref" if both_pos else "fails validate")
        print(f"{combo['gate']:<12} {combo['style']:<6} {combo['max_hold']:>4} "
              f"{(combo['disaster'] or 0):>4.1f} {combo['sl_mult']:>4.2f} | "
              f"{combo['d_n']:>4} {combo['d_net']:>+8.2f} {combo['d_pf']:>5.2f} | "
              f"{v_n:>4} {v_net:>+8.2f} {v_pf:>5.2f} {v_dd:>6.2f} {usd_day:>+7.0f} {verdict}")

    if args.producer == "vp":
        # Measurement only: identify whether VP's apparent edge is regime-local
        # before anyone proposes a filter.
        print("\n=== VP REGIME DIAGNOSTIC (report-only; no filter applied) ===")
        for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
            rows = [t for t in accepted_by_gate.get("v17-mission", []) if pred(t)]
            for state in ("directional", "rotational", "unknown"):
                rs = [_combo_r(t, "plain", 48, 2.0, 1.0, args.spread_abs, args.commission_r) for t in rows if t["vp_regime"]["state"] == state]
                rs = [r for r in rs if r is not None]
                if rs:
                    net, pf, _ = _equity(rs)
                    print(f"{segment:8} {state:11} N={len(rs):>3} net={net:+.2f}R PF={pf:.2f}")
    print("\nRULES: report/act on VALIDATE numbers only; canary requires both-segments-positive "
          "AND beats the current-live ref on validate. Replay approximates live OM exits — a "
          "canary must still prove itself forward before any scale-up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

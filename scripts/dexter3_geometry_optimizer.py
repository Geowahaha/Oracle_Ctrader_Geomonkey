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
from dexter3 import hunt_mode, price_action_eye  # noqa: E402
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


def _simulate_bank(side: str, entry: float, sl: float, future: list, max_hold: int,
                    target_r: float) -> tuple[str, float, int]:
    """Replays the ORIGINAL Dexter3 v1.0 basket exit (2026-07-05, commit
    3ca3341, basket_manager.py close_all_in_profit): bank the WHOLE position
    at the first M5 CLOSE where floating R >= target_r (no TP — banking is
    the only profit exit). Conservative SL-first on the same bar, same as
    ``_simulate``. Single-trade approximation of a basket-aggregate rule."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        cl = float(bar.get("close", 0.0))
        if side == "buy":
            if lo <= sl:                      # conservative: SL wins ties
                return "loss", -1.0, held
            r = (cl - entry) / risk
        else:
            if hi >= sl:
                return "loss", -1.0, held
            r = (entry - cl) / risk
        if r >= target_r:
            return "bank", r, held
    # timed out — mark to the last bar's close (same convention as _simulate)
    held = min(max_hold, len(future)) - 1
    last = float(future[max_hold - 1].get("close", entry)) if len(future) >= max_hold else float(future[-1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _combo_r(trade: dict, style: str, max_hold: int, disaster: float, sl_mult: float,
             spread_abs: float, commission_r: float, bank_target_r: float | None = None) -> float | None:
    """Cost-adjusted R for one accepted trade under one exit combo (None=skip)."""
    entry, sl, tp = trade["entry"], trade["sl"], trade["tp"]
    side = trade["side"]
    if sl_mult != 1.0:
        sl = entry - (entry - sl) * sl_mult if side == "buy" else entry + (sl - entry) * sl_mult
    if style == "smart":
        outcome, r, _ = _simulate_smart(side, entry, sl, tp, trade["future"], max_hold, disaster)
    elif style == "bank":
        if bank_target_r is None:
            raise ValueError("style='bank' requires bank_target_r")
        outcome, r, _ = _simulate_bank(side, entry, sl, trade["future"], max_hold, bank_target_r)
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
    ap.add_argument(
        "--bank-targets", default="0.2,0.4,0.8",
        help=(
            "comma R thresholds for style=bank (replays v1.0 basket "
            "close_all_in_profit; 0.2 = v1.0 resolve_target_r default, "
            "commit 3ca3341 basket_manager.py:48)"
        ),
    )
    ap.add_argument("--min-derive-trades", type=int, default=80, help="combos with fewer derive trades are ignored")
    ap.add_argument("--base-risk-usd", type=float, default=12.0, help="$ per 1R for the $/day translation")
    ap.add_argument(
        "--pa-eye",
        action="store_true",
        help=(
            "Price Action Eye Phase A replay diagnostic (2026-07-15, REPORT-ONLY, no "
            "filtering of trades): compute the Eye's verdict (support/neutral/oppose) "
            "against each accepted trade's own prefix bars and print a per-gate x "
            "verdict table (N, meanR, netR, PF) for derive AND validate segments, "
            "using the current-live-ref exit combo for R -- this is promotion "
            "evidence, not a filter (see docs/DEXTER3_PRICE_ACTION_EYE_DESIGN.md's "
            "promotion policy: a detector only earns power after its own "
            "journaled-vs-outcome replay shows separation)."
        ),
    )
    ap.add_argument(
        "--leader-buckets",
        action="store_true",
        help=(
            "leader_score bucket replay diagnostic (2026-07-16, REPORT-ONLY, no "
            "filtering of trades): the 48-trade live journal sample showed the "
            "hunt committee's own leader_score conviction ANTI-predictive "
            "(0.10-0.18 -> +13.24R/56.2%%WR; 0.18-0.25 -> -31.84R/35.7%%WR; "
            "0.25-0.35 -> -13.53R/33.3%%WR; 0.35+ -> +1.00R/40%%WR) while the "
            "live gate's min_leader_score=0.18 admits the losing bands and "
            "excludes the only profitable one. N=48 is far too small to act "
            "on -- this prints the derive/validate replay verdict (plus "
            "gate=none SIDE and SETUP cuts) needed before anyone proposes a "
            "gate change. Buckets a trade for the table only; never filters."
        ),
    )
    ap.add_argument(
        "--leader-bucket-edges",
        default="0.10,0.18,0.25,0.35",
        help="comma leader_score bucket edges (open lower / closed upper -> next edge; last bucket is [edge,inf))",
    )
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
            trade = {
                "i": i,
                "side": str(d.side),
                "entry": float(d.entry),
                "sl": float(d.sl),
                "tp": float(d.tp),
                "future": m5[i + 1:],
                "vp_regime": profile_regime(m5[:i]),
                # carried for --leader-buckets (2026-07-16, report-only) --
                # every producer returns the shared hunter_brain.Decision
                # dataclass, so these fields always exist.
                "leader_score": float(d.leader_score),
                "p_win_est": float(d.p_win_est),
                "setup": str(d.setup or "none"),
            }
            if args.pa_eye:
                # Report-only replay evidence (2026-07-15 Phase A): the Eye
                # itself stays powerless here too -- this verdict is NEVER
                # used to accept/reject the trade, only to bucket it for the
                # diagnostic table printed after phase 4 below.
                try:
                    trade["pa_eye_verdict"] = price_action_eye.evaluate(m5[: i + 1], str(d.side)).get(
                        "verdict", "neutral"
                    )
                except Exception:
                    trade["pa_eye_verdict"] = "neutral"
            acc.append(trade)
        accepted_by_gate[mode] = acc
        print(f"gate={mode}: accepted {len(acc)}")

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)

    # -- phase 3: grid scored on DERIVE only ---------------------------------
    styles = ["plain", "smart", "bank"]
    max_holds = [12, 24, 48, 96]
    disasters = [1.5, 2.0, 2.5, 3.0]
    sl_mults = [1.0, 1.25, 1.5]
    bank_targets = [float(x) for x in args.bank_targets.split(",") if x.strip()]
    results: list[dict] = []
    for mode in gate_modes:
        trades = accepted_by_gate[mode]
        derive_trades = [t for t in trades if t["i"] < split_bar]
        for style, mh, dis, slm in product(styles, max_holds, disasters, sl_mults):
            if style in ("plain", "bank") and dis != disasters[0]:
                continue  # disaster only applies to smart; bank sweeps bank_targets below instead
            for bt in (bank_targets if style == "bank" else [None]):
                rs = [
                    r for t in derive_trades
                    if (r := _combo_r(t, style, mh, dis, slm, args.spread_abs, args.commission_r,
                                       bank_target_r=bt)) is not None
                ]
                if len(rs) < args.min_derive_trades:
                    continue
                net, pf, dd = _equity(rs)
                results.append({
                    "gate": mode, "style": style, "max_hold": mh,
                    "disaster": dis if style == "smart" else None, "sl_mult": slm,
                    "bank_r": bt if style == "bank" else None,
                    "d_n": len(rs), "d_net": net, "d_pf": pf, "d_dd": dd,
                })
    if not results:
        print("no combo met the min-derive-trades floor")
        return 1
    # rank: derive net first, PF tiebreak
    results.sort(key=lambda x: (x["d_net"], x["d_pf"]), reverse=True)
    print(f"\ngrid: {len(results)} combos scored on derive (first {args.split:.0%}); top {args.top_k} advance")

    # current-live reference: v17 + smart + hold 24 + disaster 2.0 + slm 1.0
    # (falls back to the first requested gate when v17 isn't in --gates —
    # found 2026-07-12: --gates v17-mission alone crashed the ref lookup)
    ref_gate = "v17" if "v17" in accepted_by_gate else gate_modes[0]
    ref = {"gate": ref_gate, "style": "smart", "max_hold": 24, "disaster": 2.0, "sl_mult": 1.0}

    # -- phase 4: VALIDATE — touched exactly once, by the finalists + ref ----
    def _validate(combo: dict) -> tuple[int, float, float, float]:
        trades = [t for t in accepted_by_gate[combo["gate"]] if t["i"] >= split_bar]
        rs = [
            r for t in trades
            if (r := _combo_r(t, combo["style"], combo["max_hold"], combo["disaster"] or 2.0,
                              combo["sl_mult"], args.spread_abs, args.commission_r,
                              bank_target_r=combo.get("bank_r"))) is not None
        ]
        net, pf, dd = _equity(rs)
        return len(rs), net, pf, dd

    v_days = max(0.1, (_epoch(str(m5[-1]["ts"])) - _epoch(str(m5[split_bar]["ts"]))) / 86400.0)
    ref_n, ref_net, ref_pf, ref_dd = _validate(ref)
    print(f"\nvalidate segment ≈ {v_days:.1f} days | CURRENT-LIVE REF (v17/smart/24/2.0/1.0): "
          f"n={ref_n} net={ref_net:+.2f}R PF={ref_pf:.2f} maxDD={ref_dd:.2f}R "
          f"(~${ref_net / v_days * args.base_risk_usd:+.0f}/day at ${args.base_risk_usd:.0f}/R)\n")

    header = (f"{'gate':<12} {'style':<6} {'hold':>4} {'dis':>4} {'bankR':>5} {'slm':>4} | "
              f"{'dN':>4} {'d_net':>8} {'d_PF':>5} | {'vN':>4} {'v_net':>8} {'v_PF':>5} {'v_DD':>6} {'$/day':>7} verdict")
    print(header)
    print("-" * len(header))
    for combo in results[: args.top_k]:
        v_n, v_net, v_pf, v_dd = _validate(combo)
        usd_day = v_net / v_days * args.base_risk_usd
        both_pos = combo["d_net"] > 0 and v_net > 0
        beats_ref = v_net > ref_net
        verdict = "CANARY-ELIGIBLE" if (both_pos and beats_ref) else ("both+ but <=ref" if both_pos else "fails validate")
        bank_r_str = f"{combo['bank_r']:.2f}" if combo.get("bank_r") is not None else "-"
        print(f"{combo['gate']:<12} {combo['style']:<6} {combo['max_hold']:>4} "
              f"{(combo['disaster'] or 0):>4.1f} {bank_r_str:>5} {combo['sl_mult']:>4.2f} | "
              f"{combo['d_n']:>4} {combo['d_net']:>+8.2f} {combo['d_pf']:>5.2f} | "
              f"{v_n:>4} {v_net:>+8.2f} {v_pf:>5.2f} {v_dd:>6.2f} {usd_day:>+7.0f} {verdict}")

    # bank-style visibility: if no bank combo reached the finalists, print the
    # best one's DERIVE stats (validate stays untouched — finalists only).
    best_bank = next((c for c in results if c["style"] == "bank"), None)
    if best_bank is not None and best_bank not in results[: args.top_k]:
        print(f"\nbest bank combo by derive (outside top-{args.top_k}; validate untouched): "
              f"gate={best_bank['gate']} hold={best_bank['max_hold']} bankR={best_bank['bank_r']} "
              f"slm={best_bank['sl_mult']} dN={best_bank['d_n']} "
              f"d_net={best_bank['d_net']:+.2f}R d_PF={best_bank['d_pf']:.2f}")

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

    if args.pa_eye:
        # PRICE ACTION EYE DIAGNOSTIC (Phase A, 2026-07-15): report-only,
        # mirrors the VP regime diagnostic just above -- NO trades are
        # filtered by verdict here; this table is the promotion evidence a
        # future --pa-eye-gate mode would need before the design doc's
        # promotion gate lets any verdict earn power (support/oppose vote or
        # veto). R is computed with the CURRENT-LIVE reference exit combo
        # (same `ref` used by the validate-segment table above) so verdict
        # buckets are compared on an apples-to-apples exit, not each combo's
        # own best-fit exit.
        print("\n=== PRICE ACTION EYE DIAGNOSTIC (report-only; no filtering applied) ===")
        print(
            f"{'segment':<9} {'gate':<12} {'verdict':<8} {'N':>5} {'meanR':>7} {'netR':>8} {'PF':>6}"
        )
        for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
            for mode in gate_modes:
                rows = [t for t in accepted_by_gate.get(mode, []) if pred(t)]
                for verdict in ("support", "neutral", "oppose"):
                    subset = [t for t in rows if t.get("pa_eye_verdict") == verdict]
                    rs = [
                        r
                        for t in subset
                        if (
                            r := _combo_r(
                                t, ref["style"], ref["max_hold"], ref["disaster"], ref["sl_mult"],
                                args.spread_abs, args.commission_r,
                            )
                        )
                        is not None
                    ]
                    if not rs:
                        continue
                    net, pf, _dd = _equity(rs)
                    mean_r = net / len(rs)
                    print(
                        f"{segment:<9} {mode:<12} {verdict:<8} {len(rs):>5} "
                        f"{mean_r:>+7.3f} {net:>+8.2f} {pf:>6.2f}"
                    )
        # MIRROR (fade) hypothesis — owner 2026-07-15: an anatomy verdict is
        # direction-RELATIVE, so oppose(buy) at a rejection level is a
        # candidate ENTRY for the fade side (Brooks failed-breakout logic),
        # not merely a veto. Test it: for every verdicted trade, simulate the
        # EXACT mirror (side flipped, SL/TP reflected around entry — inherits
        # the committee's geometry, a known first-pass simplification) under
        # the same live-ref exit. Report-only; oppose-mirror is the bucket
        # the hypothesis lives or dies on.
        print("\n=== PA EYE MIRROR (fade) DIAGNOSTIC (report-only) ===")
        print(
            f"{'segment':<9} {'gate':<12} {'verdict':<8} {'N':>5} {'meanR':>7} {'netR':>8} {'PF':>6}  (mirror side)"
        )
        for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
            for mode in gate_modes:
                rows = [t for t in accepted_by_gate.get(mode, []) if pred(t)]
                for verdict in ("support", "neutral", "oppose"):
                    subset = [t for t in rows if t.get("pa_eye_verdict") == verdict]
                    rs = []
                    for t in subset:
                        mirrored = dict(
                            t,
                            side=("sell" if t["side"] == "buy" else "buy"),
                            sl=2.0 * t["entry"] - t["sl"],
                            tp=2.0 * t["entry"] - t["tp"],
                        )
                        r = _combo_r(
                            mirrored, ref["style"], ref["max_hold"], ref["disaster"], ref["sl_mult"],
                            args.spread_abs, args.commission_r,
                        )
                        if r is not None:
                            rs.append(r)
                    if not rs:
                        continue
                    net, pf, _dd = _equity(rs)
                    mean_r = net / len(rs)
                    print(
                        f"{segment:<9} {mode:<12} {verdict:<8} {len(rs):>5} "
                        f"{mean_r:>+7.3f} {net:>+8.2f} {pf:>6.2f}"
                    )
    if args.leader_buckets:
        # LEADER-SCORE BUCKET DIAGNOSTIC (2026-07-16): report-only, mirrors
        # the PA-Eye diagnostic above -- NO trades are filtered by
        # leader_score here; this table is the out-of-sample check the
        # 48-trade live journal finding needs before anyone touches
        # min_leader_score=0.18 (owner directive: this class of finding --
        # VP, bucket-router, PA-Eye, empirical sizing -- has died
        # out-of-sample 4 times already in this repo; regime-local until
        # proven otherwise). R uses the CURRENT-LIVE ref exit combo (same
        # `ref` as the PA-Eye table) so buckets compare on an identical exit.
        edges = sorted(float(x) for x in args.leader_bucket_edges.split(",") if x.strip())
        bucket_bounds = [0.0] + edges + [float("inf")]

        def _bucket_label(lo: float, hi: float) -> str:
            hi_s = "inf" if hi == float("inf") else f"{hi:.2f}"
            return f"[{lo:.2f},{hi_s})"

        def _cut_row(rows: list[dict]) -> tuple[int, float, float, float, float] | None:
            rs = [
                r for t in rows
                if (r := _combo_r(t, ref["style"], ref["max_hold"], ref["disaster"], ref["sl_mult"],
                                   args.spread_abs, args.commission_r)) is not None
            ]
            if not rs:
                return None
            net, pf, _dd = _equity(rs)
            wr = 100.0 * sum(1 for r in rs if r > 0) / len(rs)
            return len(rs), net / len(rs), net, pf, wr

        # the live gate's min_leader_score=0.18 confounds the raw relationship
        # -- gate=none is the key cut; v17 is the current-live gate for
        # comparison. Falls back to whatever --gates provided if neither
        # requested mode is present (same fallback posture as ref_gate above).
        target_gates = [g for g in ("none", "v17") if g in accepted_by_gate]
        if not target_gates:
            target_gates = gate_modes[:1]
            print(f"\nnote: neither 'none' nor 'v17' in --gates; leader-score buckets using '{target_gates[0]}' instead")

        print("\n=== LEADER-SCORE BUCKET DIAGNOSTIC (report-only; no filtering applied) ===")
        print(
            f"{'segment':<9} {'gate':<12} {'bucket':<13} {'N':>5} {'meanR':>7} {'netR':>8} {'PF':>6} {'WR%':>6}"
        )
        for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
            for mode in target_gates:
                rows = [t for t in accepted_by_gate.get(mode, []) if pred(t)]
                for lo, hi in zip(bucket_bounds[:-1], bucket_bounds[1:]):
                    subset = [t for t in rows if lo <= t["leader_score"] < hi]
                    cut = _cut_row(subset)
                    if cut is None:
                        continue
                    n, mean_r, net, pf, wr = cut
                    print(
                        f"{segment:<9} {mode:<12} {_bucket_label(lo, hi):<13} {n:>5} "
                        f"{mean_r:>+7.3f} {net:>+8.2f} {pf:>6.2f} {wr:>6.1f}"
                    )

        if "none" in accepted_by_gate:
            print("\n=== LEADER-SCORE CONTEXT: SIDE CUT (gate=none, report-only) ===")
            print(f"{'segment':<9} {'side':<6} {'N':>5} {'meanR':>7} {'netR':>8} {'WR%':>6}")
            for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
                rows = [t for t in accepted_by_gate["none"] if pred(t)]
                for side in ("buy", "sell"):
                    cut = _cut_row([t for t in rows if t["side"] == side])
                    if cut is None:
                        continue
                    n, mean_r, net, _pf, wr = cut
                    print(f"{segment:<9} {side:<6} {n:>5} {mean_r:>+7.3f} {net:>+8.2f} {wr:>6.1f}")

            print("\n=== LEADER-SCORE CONTEXT: SETUP CUT (gate=none, report-only) ===")
            print(f"{'segment':<9} {'setup':<28} {'N':>5} {'meanR':>7} {'netR':>8} {'WR%':>6}")
            for segment, pred in (("derive", lambda t: t["i"] < split_bar), ("validate", lambda t: t["i"] >= split_bar)):
                rows = [t for t in accepted_by_gate["none"] if pred(t)]
                for setup in sorted({t["setup"] for t in rows}):
                    cut = _cut_row([t for t in rows if t["setup"] == setup])
                    if cut is None:
                        continue
                    n, mean_r, net, _pf, wr = cut
                    print(f"{segment:<9} {setup:<28} {n:>5} {mean_r:>+7.3f} {net:>+8.2f} {wr:>6.1f}")
        else:
            print("\n(gate=none not in --gates; SIDE/SETUP cuts skipped -- add 'none' to --gates to see them)")

        print("\nRULES (leader-score buckets): a bucket/side/setup cut only earns power if the "
              "pattern holds on BOTH derive and validate segments -- report/act on VALIDATE "
              "numbers only. This exact class of finding (a raw-looking edge in a small live "
              "sample) has died out-of-sample 4 times already in this repo (VP, bucket-router, "
              "PA-Eye, empirical sizing) -- treat a single-segment or N=48-sized pattern as noise "
              "until this 10k-bar replay confirms it on both segments.")

    print("\nRULES: report/act on VALIDATE numbers only; canary requires both-segments-positive "
          "AND beats the current-live ref on validate. Replay approximates live OM exits — a "
          "canary must still prove itself forward before any scale-up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dexter3 EDGE DISCOVERY ENGINE — Layer 1 (historical backtest sweep).

Owner directive 2026-07-08: stop ASSUMING conviction is predictive — MEASURE
which buckets actually win, from data, so real money is only escalated on
proven edge. This is the fast breadth layer: replay the live ``hunt_mode``
committee over all available history, bucket every M5 decision, simulate its
forward outcome, and print a per-bucket win-rate / expectancy table.

RIGOR (fable5 skill — do not fool ourselves):
  * NO LOOKAHEAD. At M5 index i the decision uses only bars[:i+1] (+ M15/H1
    bars completed by that close); the outcome uses only bars[i+1:].
  * CONSERVATIVE fills. OHLC replay can't see intrabar path, so when BOTH SL
    and TP fall inside the same future bar we assume SL hit first (pessimistic
    — better to under- than over-state edge).
  * COST-ADJUSTED. Each trade pays the spread (entry crosses it) + a flat
    commission estimate, expressed in R.
  * This layer RANKS buckets (relative edge). Absolute edge must still be
    confirmed by Layer 2 (shadow-forward real fills) before betting big —
    printed as a reminder. Small per-bucket N => wide error; a 95% Wilson
    lower bound on win-rate is shown so a lucky-looking thin bucket is obvious.

Read-only: pulls bars via dexter3.mcp_client, imports the real hunt_mode /
market_lens. Touches no live state, places no orders.

Usage:
    python -X utf8 scripts/dexter3_edge_discovery.py --symbol XAUUSD --count 1000
    python -X utf8 scripts/dexter3_edge_discovery.py --symbol XAUUSD --count 1000 --max-hold 24 --commission-r 0.05
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dexter3 import empirical_stats, hunt_mode, market_lens
from dexter3 import edge_buckets
from dexter3.mcp_client import Dexter3McpClient
from dexter3.v16_entry_quality import V16EntryQualityConfig, evaluate_v16_entry_gate

MIN_M5 = 60          # bars of context the committee needs
M15_CTX = 60
H1_CTX = 60


def _epoch(ts: str) -> float:
    from datetime import datetime, timezone
    t = ts.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return 0.0


def _completed_by(bar_ts: str, close_epoch: float, tf_min: int) -> bool:
    e = _epoch(bar_ts)
    return e > 0 and (e + tf_min * 60) <= close_epoch + 1e-6


def _wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """95% Wilson lower bound on win-rate — honest floor for small samples."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _h1_trend_sign(h1_ctx: list, n: int = 6) -> int:
    """Direction of the last n completed H1 bars (net close change)."""
    if len(h1_ctx) < 2:
        return 0
    seg = h1_ctx[-n:]
    net = float(seg[-1].get("close", 0)) - float(seg[0].get("close", 0))
    if net > 0:
        return 1
    if net < 0:
        return -1
    return 0


def _pullback_resume(bars: list, side: str, leg: int = 8, pull: int = 3, clp_min: float = 0.60) -> bool:
    """Pullback-exhaustion-resumption setup (owner directive 2026-07-08):
    a LEG in the entry direction, then a PULLBACK against it, then the last
    bar RESUMES (closes strongly back in the entry direction = exhaustion of
    the pullback + resumption). Entering HERE (after the pullback, at the
    swing that held) instead of chasing momentum puts the SL behind a REAL
    level, so a tight stop is structural, not noise."""
    if len(bars) < leg + 2:
        return False
    cl = [float(b.get("close", 0.0)) for b in bars]
    last = bars[-1]
    o, c = float(last.get("open", 0.0)), float(last.get("close", 0.0))
    h, l = float(last.get("high", 0.0)), float(last.get("low", 0.0))
    rng = h - l
    clp = (c - l) / rng if rng > 0 else 0.5           # 1 = closed at the high
    leg_move = cl[-pull - 1] - cl[-leg]               # move over the leg window (>0 = up)
    pull_move = cl[-1] - cl[-pull - 1]                # move over the pullback window
    if side == "buy":
        has_leg = leg_move > 0                         # prior up-leg
        had_pullback = min(cl[-pull:]) < cl[-pull - 1] # dipped during the pullback
        resumes = c > o and clp >= clp_min             # strong green close near high
        return has_leg and had_pullback and resumes
    else:
        has_leg = leg_move < 0
        had_pullback = max(cl[-pull:]) > cl[-pull - 1]
        resumes = c < o and (1 - clp) >= clp_min       # strong red close near low
        return has_leg and had_pullback and resumes


def _regime(h1_ctx: list, n: int = 8, thresh: float = 0.35) -> str:
    """Directional efficiency = |net move| / sum(|bar-to-bar moves|) over the
    last n H1 bars. High = trending (the move went somewhere), low = ranging
    (lots of movement, no net progress = chop). This is the dimension that
    decides whether FADE (mean-reversion) or FOLLOW (trend) should win."""
    seg = h1_ctx[-n:] if len(h1_ctx) >= 2 else []
    if len(seg) < 3:
        return "unknown"
    closes = [float(b.get("close", 0.0)) for b in seg]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
    if path <= 0:
        return "unknown"
    eff = net / path
    return "trending" if eff >= thresh else "ranging"


def _simulate(side: str, entry: float, sl: float, tp: float, future: list, max_hold: int) -> tuple[str, float, int]:
    """Walk future bars, return (outcome, R, bars_held). Conservative SL-first
    on same-bar. ``bars_held`` = 0-based offset into ``future`` where the trade
    RESOLVED — the walk-forward empirical mode needs it for temporal honesty
    (an outcome is knowable only from its resolution bar onward, not from its
    entry bar)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        if side == "buy":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
            if hit_sl:                       # conservative: SL wins ties
                return "loss", -1.0, held
            if hit_tp:
                return "win", (tp - entry) / risk, held
        else:  # sell
            hit_sl = hi >= sl
            hit_tp = lo <= tp
            if hit_sl:
                return "loss", -1.0, held
            if hit_tp:
                return "win", (entry - tp) / risk, held
    # timed out — mark to the last bar's close
    held = min(max_hold, len(future)) - 1
    last = float(future[max_hold - 1].get("close", entry)) if len(future) >= max_hold else float(future[-1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _simulate_smart(side: str, entry: float, sl: float, tp: float, future: list, max_hold: int,
                    disaster_mult: float) -> tuple[str, float, int]:
    """SMART exit (owner directive 2026-07-08): the SL level is not a hard
    wick-triggered line — a wick BEYOND it that CLOSES back inside is NOISE
    and is survived; we only exit-as-loss when a bar CLOSES beyond the
    invalidation (a CONFIRMED break). A wide disaster stop (disaster_mult x
    the SL distance) still hard-cuts a catastrophic wick so tail risk is
    capped. TP is still wick-triggered (banking a profit spike is fine).
    R is measured against the original (tight) SL distance for comparability."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    disaster = entry - disaster_mult * risk if side == "buy" else entry + disaster_mult * risk
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        cl = float(bar.get("close", 0.0))
        if side == "buy":
            if lo <= disaster:                    # catastrophic wick — hard cut
                return "loss", -disaster_mult, held
            if hi >= tp:                           # TP spike — bank it
                return "win", (tp - entry) / risk, held
            if cl <= sl:                           # CONFIRMED break (close beyond) — thesis dead
                return "loss", (cl - entry) / risk, held
        else:
            if hi >= disaster:
                return "loss", -disaster_mult, held
            if lo <= tp:
                return "win", (entry - tp) / risk, held
            if cl >= sl:
                return "loss", (entry - cl) / risk, held
    last = float(future[min(max_hold, len(future)) - 1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, min(max_hold, len(future)) - 1)


def _entry_gate_config(mode: str) -> V16EntryQualityConfig:
    """Config snapshots for exact production-gate replay.

    Cool-down is disabled here because the replay does not reconstruct live OM
    close stamps. That matches the current launcher for V1.7 mission control.
    """
    common = {"cooldown_enabled": False}
    if mode == "v16":
        return V16EntryQualityConfig(
            **common,
            a_plus_bypasses_chase=False,
            block_chase_bypass_on_aligned_trending=False,
        )
    if mode == "v17":
        return V16EntryQualityConfig(
            **common,
            a_plus_bypasses_chase=True,
            block_chase_bypass_on_aligned_trending=False,
        )
    if mode == "v17-mission":
        return V16EntryQualityConfig(
            **common,
            a_plus_bypasses_chase=True,
            block_chase_bypass_on_aligned_trending=True,
        )
    if mode == "v18":
        # Live V1.8 size-the-edge launcher config (size-policy race P5):
        # V1.7 selection + B-tier scout band; boost/rescue affect SIZE only.
        return V16EntryQualityConfig(
            **common,
            a_plus_bypasses_chase=True,
            block_chase_bypass_on_aligned_trending=False,
            winner_boost_enabled=True,
            chase_rescue_enabled=True,
            b_tier_enabled=True,
        )
    raise ValueError(f"unknown entry gate mode: {mode}")


def _stamp_entry_gate_features(decision, m5_prefix: list, h1_ctx: list) -> None:
    """Mirror live shadow_runner feature stamping before entry-quality gate."""
    if not isinstance(getattr(decision, "features", None), dict):
        decision.features = {}
    _, anti = edge_buckets.anti_chase_risk_mult(decision.side, h1_ctx)
    _, pullback = edge_buckets.pullback_size_mult(decision.side, m5_prefix)
    decision.features["anti_chase"] = anti
    decision.features["pullback_gate"] = pullback


def _apply_entry_gate(decision, mode: str, now_iso: str) -> dict:
    if mode == "none":
        return {"allow": True, "reason": "gate_disabled", "features": {}}
    return evaluate_v16_entry_gate(
        decision=decision,
        state={},
        mcp_consec_errors=0,
        now_iso=now_iso,
        cfg=_entry_gate_config(mode),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=1000, help="M5 bars of history to sweep")
    ap.add_argument("--max-hold", type=int, default=24, help="max M5 bars to hold before mark-to-market")
    ap.add_argument("--spread-abs", type=float, default=0.12, help="assumed XAU spread (price units)")
    ap.add_argument("--commission-r", type=float, default=0.03, help="flat cost per trade in R (commission)")
    ap.add_argument("--sl-mult", type=float, default=1.0, help="widen SL only (TP price fixed) by this factor")
    ap.add_argument("--smart-exit", action="store_true", help="close-confirmed SL (survive noise wicks) + wide disaster stop")
    ap.add_argument("--disaster-mult", type=float, default=2.5, help="disaster hard-stop = this x the SL distance (smart-exit only)")
    ap.add_argument("--pullback-only", action="store_true", help="only take pullback-exhaustion-resumption entries (test entry-quality edge)")
    ap.add_argument(
        "--entry-gate",
        choices=("none", "v16", "v17", "v17-mission", "v18"),
        default="none",
        help="replay production entry-quality gate before simulating accepted trades",
    )
    ap.add_argument(
        "--size-policy-race",
        action="store_true",
        help="race sizing policies (winner-boost / A+ chase rescue / B-tier scout) on the "
        "gate-accepted set — reports $-weighted totals so policies compare in money, not R",
    )
    ap.add_argument("--base-risk-usd", type=float, default=12.0, help="full-size $ risk for the policy race")
    ap.add_argument(
        "--walkforward-empirical",
        action="store_true",
        help=(
            "PROMOTION GATE for learner sizing (2026-07-11): before each decision, "
            "empirical (setup, session) stats are built from outcomes RESOLVED strictly "
            "before that bar (temporal honesty — resolution bar, not entry bar); a "
            "DOWNSIZE-ONLY policy (blend can only shrink size, never boost) is measured "
            "against the full-size baseline on the SAME accepted trades: net R, PF, max "
            "drawdown. Learner sizing may go live only if the policy wins here."
        ),
    )
    ap.add_argument("--wf-size-floor", type=float, default=0.25, help="downsize-only policy floor multiplier")
    args = ap.parse_args()

    # DEXTER3_TRANSPORT-aware (2026-07-11): local_mcp on the PC (default,
    # byte-identical), openapi+daemon on the VM — same pattern as
    # ops/dexter3_lane_tally.py.
    from dexter3.transport import make_client

    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    m15 = c.get_trendbars(args.symbol, "m15", args.count)
    h1 = c.get_trendbars(args.symbol, "h1", max(200, args.count // 4))
    if len(m5) < MIN_M5 + 30:
        print(f"not enough M5 history: {len(m5)}")
        return 1
    print(f"swept: M5={len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']}), M15={len(m15)}, H1={len(h1)}")

    buckets: dict[tuple, list] = defaultdict(list)   # (align, session) -> [R,...]
    regime_buckets: dict[tuple, list] = defaultdict(list)  # (align, regime) -> [R,...]
    side_split: dict[str, list] = defaultdict(list)
    gate_blocks: dict[str, list] = defaultdict(list)
    accepted_records: list[dict] = []   # size-policy race: gate-accepted trades
    b_pool: list[dict] = []             # size-policy race: near-miss (0.15-0.18) non-chase skips
    n_eval = n_candidates = n_enter = 0

    # --walkforward-empirical rolling state: an outcome becomes KNOWN only at
    # its RESOLUTION bar (entry bar would be look-ahead — the exact trap the
    # fear-cost P0 removed elsewhere). wf_stats is rebuilt only when new
    # outcomes mature, and always from wf_known (strictly-prior resolutions).
    wf_pending: list[dict] = []          # {resolve_i, setup, session, pnl}
    wf_known: list[dict] = []
    wf_stats: dict = {}
    wf_records: list[tuple[float, float]] = []   # (r_net, policy_mult)
    wf_downsized = 0

    # walk forward: decision uses [:i+1], outcome uses [i+1:]
    for i in range(MIN_M5, len(m5) - 2):
        if args.walkforward_empirical and wf_pending:
            matured = [p for p in wf_pending if p["resolve_i"] <= i]
            if matured:
                wf_pending = [p for p in wf_pending if p["resolve_i"] > i]
                wf_known.extend(matured)
                wf_stats = empirical_stats.p_win_estimates(wf_known, args.symbol)
        prefix = m5[: i + 1]
        ts = str(m5[i].get("ts") or "")
        close_epoch = _epoch(ts) + 300
        m15c = [b for b in m15 if _completed_by(str(b.get("ts") or ""), close_epoch, 15)]
        h1c = [b for b in h1 if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
        n_eval += 1
        try:
            d = hunt_mode.decide_hunt(args.symbol, prefix, m15c, h1c, None, args.spread_abs)
        except Exception:
            continue
        if d.action != "enter" or d.side is None or d.sl is None or d.tp is None:
            continue
        _stamp_entry_gate_features(d, prefix, h1c)
        if args.pullback_only and not _pullback_resume(prefix, str(d.side)):
            continue
        n_candidates += 1
        future = m5[i + 1:]
        # SL-widen experiment: push SL (and TP, keeping RR) further from entry
        entry_p, sl_p, tp_p = float(d.entry), float(d.sl), float(d.tp)
        if args.sl_mult != 1.0:
            # widen SL ONLY (TP price fixed at its structural target) — tests
            # 'survive noise, reach the SAME target more often'. R is measured
            # against the NEW (wider) risk, so a loss is still -1R.
            sl_p = entry_p - (entry_p - sl_p) * args.sl_mult if d.side == "buy" else entry_p + (sl_p - entry_p) * args.sl_mult
        if args.smart_exit:
            outcome, r, held = _simulate_smart(str(d.side), entry_p, sl_p, tp_p, future, args.max_hold, args.disaster_mult)
        else:
            outcome, r, held = _simulate(str(d.side), entry_p, sl_p, tp_p, future, args.max_hold)
        if outcome == "skip":
            continue
        # cost: entry crosses spread (in R) + flat commission R
        risk = abs(float(d.entry) - float(d.sl))
        cost_r = (args.spread_abs / risk if risk > 0 else 0.0) + args.commission_r
        r_net = r - cost_r
        gate = gate_features = None
        if args.entry_gate != "none" or args.size_policy_race:
            gate = _apply_entry_gate(d, args.entry_gate if args.entry_gate != "none" else "v17", str(d.ts_close or ts))
            gate_features = gate.get("features") or {}
        if gate is not None and not bool(gate.get("allow", True)):
            gate_blocks[str(gate.get("reason") or "blocked")].append(r_net)
            # B-tier candidate pool: blocked ONLY by min score, near-miss band,
            # non-chase (the +EV bucket family) — the race prices these.
            if (
                args.size_policy_race
                and str(gate.get("reason")) == "min_leader_score"
                and not bool(gate_features.get("is_chase", False))
                and float(gate_features.get("leader_score") or 0.0) >= 0.15
            ):
                b_pool.append({"r": r_net, "pull": bool(gate_features.get("is_pullback", False))})
            continue
        if args.size_policy_race and gate_features is not None:
            accepted_records.append(
                {
                    "r": r_net,
                    "chase": bool(gate_features.get("is_chase", False)),
                    "pull": bool(gate_features.get("is_pullback", False)),
                    "a_plus": bool(gate.get("a_plus", False)),
                    "score": float(gate_features.get("leader_score") or 0.0),
                }
            )
        n_enter += 1
        # bucket dims
        align = "aligned" if (_h1_trend_sign(h1c) == (1 if d.side == "buy" else -1)) else \
                ("counter" if _h1_trend_sign(h1c) != 0 else "no_trend")
        session = str(market_lens.session_context(ts).get("value") or "unknown")
        regime = _regime(h1c)
        if args.walkforward_empirical:
            # DOWNSIZE-ONLY policy: mature-bucket blend may shrink size toward
            # the floor, never boost above 1.0 (codex promotion-gate spec).
            base_p = float(d.p_win_est or 0.0)
            blended_p = empirical_stats.blended_p_win(base_p, str(d.setup), session, wf_stats)
            mult = 1.0
            if base_p > 0 and blended_p < base_p:
                mult = max(float(args.wf_size_floor), blended_p / base_p)
                if mult < 1.0:
                    wf_downsized += 1
            wf_records.append((r_net, mult))
            # this trade's outcome becomes knowable only from its resolution bar
            wf_pending.append(
                {"resolve_i": i + 1 + int(held), "setup": str(d.setup), "session": session, "pnl": r_net}
            )
        buckets[(align, session)].append(r_net)
        regime_buckets[(align, regime)].append(r_net)
        side_split[str(d.side)].append(r_net)

    print(
        f"evaluated {n_eval} M5 closes, {n_candidates} candidates, {n_enter} accepted entries "
        f"({n_enter/max(1,n_eval)*100:.0f}% participation), entry_gate={args.entry_gate}\n"
    )

    if args.walkforward_empirical:
        def _equity_stats(rs: list[float]) -> tuple[float, float, float]:
            """(net, PF, max drawdown) of an R-sequence in trade order."""
            gw = sum(x for x in rs if x > 0)
            gl = sum(x for x in rs if x <= 0)
            pf = (gw / abs(gl)) if gl < 0 else float("inf")
            eq = peak = maxdd = 0.0
            for x in rs:
                eq += x
                peak = max(peak, eq)
                maxdd = max(maxdd, peak - eq)
            return sum(rs), pf, maxdd

        base_rs = [r for r, _ in wf_records]
        pol_rs = [r * m for r, m in wf_records]
        b_net, b_pf, b_dd = _equity_stats(base_rs)
        p_net, p_pf, p_dd = _equity_stats(pol_rs)
        mature = sum(1 for v in wf_stats.values() if not v.get("below_min_samples", True))
        print("== WALK-FORWARD EMPIRICAL (downsize-only promotion gate) ==")
        print(f"trades={len(wf_records)} downsized={wf_downsized} "
              f"mature_buckets={mature}/{len(wf_stats)} (MIN_SAMPLES={empirical_stats.MIN_SAMPLES}) "
              f"size_floor={args.wf_size_floor}")
        print(f"baseline : net={b_net:+.2f}R PF={b_pf:.2f} maxDD={b_dd:.2f}R")
        print(f"policy   : net={p_net:+.2f}R PF={p_pf:.2f} maxDD={p_dd:.2f}R")
        wins = p_net >= b_net and p_dd <= b_dd and wf_downsized > 0
        verdict = "PASS (policy >= baseline net AND <= baseline maxDD, with real downsizes)" if wins else \
                  ("INSUFFICIENT (no mature bucket ever downsized — need more history)" if wf_downsized == 0 else
                   "FAIL (policy loses on this window — do NOT promote learner sizing)")
        print(f"verdict  : {verdict}\n")

    def _row(name, rs):
        n = len(rs)
        if n == 0:
            return None
        wins = sum(1 for x in rs if x > 0)
        wr = wins / n
        avg_w = sum(x for x in rs if x > 0) / max(1, wins)
        losses = [x for x in rs if x <= 0]
        avg_l = sum(losses) / max(1, len(losses))
        exp = sum(rs) / n
        wl = _wilson_lower(wins, n)
        return (name, n, wr, avg_w, avg_l, exp, wl, sum(rs))

    header = f"{'BUCKET (align x session)':32} {'N':>4} {'WR':>5} {'avgW':>6} {'avgL':>6} {'EXP/tr':>7} {'WR_lo95':>7} {'totR':>7}"
    print("=== EDGE BY BUCKET (cost-adjusted R, sorted by expectancy) ===")
    print(header); print("-" * len(header))
    rows = [_row(f"{a} x {s}", rs) for (a, s), rs in buckets.items()]
    rows = [r for r in rows if r]
    for r in sorted(rows, key=lambda x: -x[5]):
        name, n, wr, aw, al, exp, wl, tot = r
        flag = "  <== +EV (proven-ish)" if (exp > 0 and wl > 0.5 and n >= 20) else ("  (thin/uncertain)" if n < 20 else "")
        print(f"{name:32} {n:>4} {wr*100:>4.0f}% {aw:>+6.2f} {al:>+6.2f} {exp:>+7.3f} {wl*100:>6.0f}% {tot:>+7.1f}{flag}")

    print("\n=== HYPOTHESIS TEST: does FADE win in RANGING, FOLLOW win in TRENDING? ===")
    print(f"{'align x regime':24} {'N':>4} {'WR':>5} {'EXP/tr':>7} {'WR_lo95':>7} {'totR':>7}")
    print("-" * 58)
    rrows = [_row(f"{a} x {rg}", rs) for (a, rg), rs in regime_buckets.items()]
    rrows = [r for r in rrows if r]
    for r in sorted(rrows, key=lambda x: (x[0].split(' x ')[1], -x[5])):
        name, n, wr, aw, al, exp, wl, tot = r
        print(f"{name:24} {n:>4} {wr*100:>4.0f}% {exp:>+7.3f} {wl*100:>6.0f}% {tot:>+7.1f}")

    print("\n=== BY SIDE ===")
    for side, rs in side_split.items():
        r = _row(side, rs)
        if r:
            print(f"  {side:6} N={r[1]:>4} WR={r[2]*100:.0f}% exp/tr={r[5]:+.3f}R totR={r[7]:+.1f}")

    if gate_blocks:
        print("\n=== ENTRY GATE BLOCKS (counterfactual R avoided if negative) ===")
        print(f"{'reason':32} {'N':>4} {'avgR':>7} {'totR':>7}")
        print("-" * 54)
        for reason, rs in sorted(gate_blocks.items(), key=lambda kv: sum(kv[1])):
            print(f"{reason:32} {len(rs):>4} {sum(rs)/len(rs):>+7.3f} {sum(rs):>+7.1f}")

    if args.size_policy_race and accepted_records:
        # -- SIZE POLICY RACE ------------------------------------------------
        # Same entry selection (v17 gate), different SIZING. Weight = the live
        # sizing chain (chase 0.15 / non-pullback 0.35) with each policy's
        # modification on top. $-weighted total = sum(r * weight) * base_risk.
        hours = max(1.0, (len(m5) * 5.0) / 60.0)
        cap_mult = 25.0 / max(1e-9, args.base_risk_usd)  # governor hard cap 2.5% of $1000

        def _chain(rec: dict) -> float:
            return (0.15 if rec["chase"] else 1.0) * (1.0 if rec["pull"] else 0.35)

        def _policy_total(name: str, boost: bool, rescue: bool, b_tier: bool) -> tuple:
            wtot = 0.0
            n = 0
            for rec in accepted_records:
                w = _chain(rec)
                if boost and rec["a_plus"] and not rec["chase"] and rec["pull"]:
                    w = min(w * 1.6, cap_mult)
                if rescue and rec["a_plus"] and rec["chase"]:
                    w = max(w, 0.5)
                wtot += rec["r"] * w
                n += 1
            if b_tier:
                for rec in b_pool:
                    w = 0.5 * (1.0 if rec["pull"] else 0.35)
                    wtot += rec["r"] * w
                    n += 1
            usd = wtot * args.base_risk_usd
            return (name, n, wtot, usd, usd / hours * 24.0)

        print("\n=== SIZE POLICY RACE ($-weighted; same v17 entry edge, different sizing) ===")
        print(f"window={hours:.0f}h  base_risk=${args.base_risk_usd:.0f}  cap=2.5%/$1000")
        print(f"{'policy':44} {'N':>4} {'wR':>8} {'$window':>9} {'$/day':>8}")
        print("-" * 78)
        for row in (
            _policy_total("P0 current (chase .15 / non-pull .35)", False, False, False),
            _policy_total("P1 winner-boost x1.6 (A+ non-chase pull)", True, False, False),
            _policy_total("P2 A+ chase rescue -> 0.5x", False, True, False),
            _policy_total("P3 = P1 + P2", True, True, False),
            _policy_total("P4 B-tier scout 0.5x (0.15-0.18 non-chase)", False, False, True),
            _policy_total("P5 = P1 + P2 + B-tier", True, True, True),
        ):
            name, n, wtot, usd, per_day = row
            print(f"{name:44} {n:>4} {wtot:>+8.2f} {usd:>+9.2f} {per_day:>+8.2f}")

        def _grp(name, recs):
            if not recs:
                return
            rs = [x["r"] for x in recs]
            print(f"  {name:38} N={len(rs):>3} avgR={sum(rs)/len(rs):+.3f} totR={sum(rs):+.1f}")

        print("\n--- accepted-set anatomy (unweighted R — is each lever's premise true?) ---")
        _grp("A+ non-chase pullback (boost target)", [x for x in accepted_records if x["a_plus"] and not x["chase"] and x["pull"]])
        _grp("A+ chase (rescue target)", [x for x in accepted_records if x["a_plus"] and x["chase"]])
        _grp("other accepted", [x for x in accepted_records if not (x["a_plus"] and not x["chase"] and x["pull"]) and not (x["a_plus"] and x["chase"])])
        _grp("B-pool near-miss non-chase (b-tier target)", b_pool)

    allr = [x for rs in buckets.values() for x in rs]
    if allr:
        ov = _row("ALL", allr)
        print(f"\n=== OVERALL === N={ov[1]} WR={ov[2]*100:.0f}% avgW={ov[3]:+.2f}R avgL={ov[4]:+.2f}R EXP/trade={ov[5]:+.3f}R totR={ov[7]:+.1f}")
        print(f"    breakeven WR for this payoff = {abs(ov[4])/(ov[3]+abs(ov[4]))*100:.1f}%  (actual {ov[2]*100:.0f}%)")
    print("\nCAVEAT: OHLC conservative SL-first fills + no intrabar path. Ranks buckets (relative);")
    print("confirm absolute edge with Layer-2 shadow-forward real fills before escalating size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

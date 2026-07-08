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

from dexter3 import hunt_mode, market_lens
from dexter3.mcp_client import Dexter3McpClient

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


def _simulate(side: str, entry: float, sl: float, tp: float, future: list, max_hold: int) -> tuple[str, float]:
    """Walk future bars, return (outcome, R). Conservative SL-first on same-bar."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0
    for bar in future[:max_hold]:
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        if side == "buy":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
            if hit_sl:                       # conservative: SL wins ties
                return "loss", -1.0
            if hit_tp:
                return "win", (tp - entry) / risk
        else:  # sell
            hit_sl = hi >= sl
            hit_tp = lo <= tp
            if hit_sl:
                return "loss", -1.0
            if hit_tp:
                return "win", (entry - tp) / risk
    # timed out — mark to the last bar's close
    last = float(future[max_hold - 1].get("close", entry)) if len(future) >= max_hold else float(future[-1].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=1000, help="M5 bars of history to sweep")
    ap.add_argument("--max-hold", type=int, default=24, help="max M5 bars to hold before mark-to-market")
    ap.add_argument("--spread-abs", type=float, default=0.12, help="assumed XAU spread (price units)")
    ap.add_argument("--commission-r", type=float, default=0.03, help="flat cost per trade in R (commission)")
    args = ap.parse_args()

    c = Dexter3McpClient()
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
    n_eval = n_enter = 0

    # walk forward: decision uses [:i+1], outcome uses [i+1:]
    for i in range(MIN_M5, len(m5) - 2):
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
        n_enter += 1
        future = m5[i + 1:]
        outcome, r = _simulate(str(d.side), float(d.entry), float(d.sl), float(d.tp), future, args.max_hold)
        if outcome == "skip":
            continue
        # cost: entry crosses spread (in R) + flat commission R
        risk = abs(float(d.entry) - float(d.sl))
        cost_r = (args.spread_abs / risk if risk > 0 else 0.0) + args.commission_r
        r_net = r - cost_r
        # bucket dims
        align = "aligned" if (_h1_trend_sign(h1c) == (1 if d.side == "buy" else -1)) else \
                ("counter" if _h1_trend_sign(h1c) != 0 else "no_trend")
        session = str(market_lens.session_context(ts).get("value") or "unknown")
        regime = _regime(h1c)
        buckets[(align, session)].append(r_net)
        regime_buckets[(align, regime)].append(r_net)
        side_split[str(d.side)].append(r_net)

    print(f"evaluated {n_eval} M5 closes, {n_enter} entries ({n_enter/max(1,n_eval)*100:.0f}% participation)\n")

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

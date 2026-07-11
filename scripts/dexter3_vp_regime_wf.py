#!/usr/bin/env python3
"""VP regime-gate ROLLING walk-forward (owner spec 2026-07-12).

Train 20 trading days -> validate 5, slide by 5 across the full history.
Rules are derived PER WINDOW from train trades only (transparent stumps —
see dexter3/vp_regime.py); the validate segment of each window is untouched
by its rule derivation. FORBIDDEN: deriving anything from the whole history.

Three arms per window, reported side by side:
  ungated   — all VP trades in the window
  stump     — trades allowed by that window's train-derived market-state stump
  equity    — simple baseline: trade only while the rolling PF of the last 30
              RESOLVED VP trades (resolution-bar honest) is >= 1.0. If the
              fancy features cannot beat this, parsimony wins.

Owner pass criteria: >= 4 consecutive validate windows PF > 1.15 with
positive net after costs, and the gate's bad windows must be no-trade /
genuinely lower drawdown (not merely fewer trades).

Usage (VM):
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
    DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=240 \
        python scripts/dexter3_vp_regime_wf.py --count 14000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dexter3_edge_discovery import _simulate  # noqa: E402
from dexter3 import market_lens, volume_profile, vp_regime  # noqa: E402
from dexter3.transport import make_client  # noqa: E402

BARS_PER_DAY = 276          # XAU M5 bars per trading day (~23h session)
SPREAD_ABS = 0.12
COMMISSION_R = 0.03
MAX_HOLD = 48               # the gate-winning VP exit posture (plain, hold 48)
ROLLING_EQ_N = 30


def _pf(pnls: list[float]) -> float:
    gw = sum(x for x in pnls if x > 0)
    gl = sum(x for x in pnls if x <= 0)
    return (gw / abs(gl)) if gl < 0 else (999.0 if gw > 0 else 0.0)


def _maxdd(pnls: list[float]) -> float:
    eq = peak = dd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=14000)
    ap.add_argument("--train-days", type=int, default=20)
    ap.add_argument("--validate-days", type=int, default=5)
    args = ap.parse_args()

    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    print(f"bars: {len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']})")
    atr_arr = vp_regime.atr_series(m5)

    # -- pass 1: every VP trade with features + resolution index -------------
    trades: list[dict] = []
    for i in range(300, len(m5) - 2):
        prefix = m5[: i + 1]
        ts = str(m5[i].get("ts") or "")
        session = str(market_lens.session_context(ts).get("value") or "unknown")
        d = volume_profile.decide_vp(args.symbol, prefix, SPREAD_ABS, session=session)
        if d.action != "enter":
            continue
        outcome, r, held = _simulate(str(d.side), float(d.entry), float(d.sl), float(d.tp), m5[i + 1:], MAX_HOLD)
        if outcome == "skip":
            continue
        risk = abs(float(d.entry) - float(d.sl))
        r_net = r - ((SPREAD_ABS / risk if risk > 0 else 0.0) + COMMISSION_R)
        feats = vp_regime.regime_features(prefix, atr_arr, i)
        trades.append({
            "i": i, "resolve_i": i + 1 + int(held), "pnl": r_net,
            "features": feats or {}, "setup": d.setup,
        })
    print(f"VP trades with outcomes: {len(trades)}")
    if len(trades) < 60:
        print("not enough trades for rolling WF")
        return 1

    # -- equity baseline: rolling PF of last N RESOLVED trades at entry time --
    for t in trades:
        resolved = [u for u in trades if u["resolve_i"] <= t["i"]]
        recent = sorted(resolved, key=lambda u: u["resolve_i"])[-ROLLING_EQ_N:]
        t["eq_gate"] = (_pf([u["pnl"] for u in recent]) >= 1.0) if len(recent) >= 10 else True

    # -- rolling windows ------------------------------------------------------
    train_bars = args.train_days * BARS_PER_DAY
    val_bars = args.validate_days * BARS_PER_DAY
    windows = []
    start = 300
    w = 0
    print(f"\n{'win':>3} {'validate span':<28} | {'arm':<7} {'n':>4} {'net':>8} {'PF':>6} {'maxDD':>6}  rule")
    consec = {"ungated": 0, "stump": 0, "equity": 0}
    best_consec = {"ungated": 0, "stump": 0, "equity": 0}
    while True:
        t_end = start + train_bars
        v_end = t_end + val_bars
        if v_end > len(m5) - 2:
            break
        w += 1
        train = [t for t in trades if start <= t["i"] < t_end and t["resolve_i"] < t_end]
        val = [t for t in trades if t_end <= t["i"] < v_end]
        rule = vp_regime.derive_stump(train)
        span = f"{m5[t_end]['ts'][:10]} -> {m5[min(v_end, len(m5)-1)]['ts'][:10]}"
        arms = {
            "ungated": [t["pnl"] for t in val],
            "stump": [t["pnl"] for t in val if vp_regime.apply_stump(rule, t["features"])],
            "equity": [t["pnl"] for t in val if t["eq_gate"]],
        }
        rule_txt = (f"{rule['feature']}{rule['op']}{rule['threshold']:.3f} "
                    f"(trainPF {rule['train_base_pf']:.2f}->{rule['train_pf']:.2f})") if rule else "no-rule(open)"
        for arm, pnls in arms.items():
            pf = _pf(pnls) if pnls else 0.0
            ok = pf > 1.15 and sum(pnls) > 0
            consec[arm] = consec[arm] + 1 if ok else 0
            best_consec[arm] = max(best_consec[arm], consec[arm])
            print(f"{w:>3} {span:<28} | {arm:<7} {len(pnls):>4} {sum(pnls):>+8.2f} {pf:>6.2f} {_maxdd(pnls):>6.2f}"
                  f"  {rule_txt if arm == 'stump' else ''}")
        windows.append(w)
        start += val_bars

    print(f"\nbest consecutive PF>1.15 windows: ungated={best_consec['ungated']} "
          f"stump={best_consec['stump']} equity={best_consec['equity']} (owner bar: >=4)")
    for arm in ("stump", "equity"):
        verdict = "PASS" if best_consec[arm] >= 4 else "FAIL"
        print(f"{arm}: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

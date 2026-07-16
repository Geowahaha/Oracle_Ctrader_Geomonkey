#!/usr/bin/env python3
"""Dexter3 ENTRY-POSITION compound replay -- right direction x low-risk entry.

Owner directive 2026-07-16: "debug แก้ไข strategy ที่ยังหลงทาง -- เข้าให้ถูกทาง
ในตำแหน่งที่เสี่ยงน้อยและมีโอกาสทำกำไรได้จริง". The v18 baseline is a certified
loser (ladder PF 0.39 derive / 0.47 validate, ~-$86/day) and the handoff's two
diseases are regime-split: derive (crash) is killed by the BUY side, validate
(flat) is killed by the LADDER. This replay tests the first compound fix, every
lever PRE-REGISTERED from measurements that already exist (SESSION_HANDOFF_
20260716_POST_OPUS.md S2/S4 -- this is a confirmatory matrix, not a mining
sweep):

  LEVER 1 -- direction filter ("เข้าให้ถูกทาง"): skip a buy when the H1 trend
    (last 6 completed H1 closes, ``_h1_trend_sign`` -- decision-time data, no
    lookahead) points DOWN. Hypothesis source: gate=none derive buy -453R vs
    sell +332R in a -467pt crash; never verified at v18 -- this run settles it.
  LEVER 2 -- trough-limit entry ("ตำแหน่งที่เสี่ยงน้อย"): the signal's market
    entry is replaced by a LIMIT at entry -/+ dip_r * risk. Measured basis:
    trough is EARLY (p50 bar 0, p75 bar 4), tail runners' MAE-before-peak p50
    0.40R / never >1.0R -- the market comes to us before it runs. SL stays at
    the ORIGINAL structural level, so the filled stop distance shrinks to
    (1-dip_r) x risk: a better price AND a smaller absolute risk, while the
    invalidation level is unchanged. Signals that never dip are MISSED -- the
    miss counterfactual is reported so the selection bias is visible, because
    a limit entry systematically catches every future SL-loser (they all pass
    through the dip on the way down) and misses the instant runners. Whether
    the improved geometry pays for that adverse selection is exactly what
    this measures -- it cannot be reasoned out.
  LEVER 3 -- exit: the live LADDER baseline vs the two convex combos that won
    validate in the 2026-07-16 v18 run (arm1.0 gb3.0 h48, arm2.0 gb3.0 h24 --
    fixed HERE as inputs, not re-derived, to avoid double-dipping the derive
    segment).

RIGOR (same conventions as dexter3_convex_exit_replay.py):
  * decisions computed ONCE per bar from prefix-only data; gate = the REAL
    ``evaluate_v16_entry_gate`` at the requested preset (default v18 = live).
  * SL-first conservative everywhere. A limit fill whose bar ALSO breaches the
    original SL is counted as filled-and-stopped (-1R of the NEW risk) -- the
    intrabar path is unknowable from OHLC, so the worst ordering is assumed.
  * The fill bar's favorable excursion is IGNORED (exit sim starts on the next
    bar) -- pessimistic for the limit rows.
  * Limit rows still pay the FULL spread + commission (a real limit is passive
    and would save the spread -- deliberately conservative so any win is
    geometry, not cost modeling).
  * R is in OWN-risk units per trade, matching live sizing (every trade risks
    ``--base-risk-usd`` regardless of stop distance), so $/day compares rows
    fairly even when the limit rows take fewer, smaller-stop trades.
  * Derive/validate time split identical to the convex replay. Every row is
    printed on BOTH segments; the matrix is small and a-priori, but ONE replay
    pass is still evidence, not proof -- the repo has killed 4 exit-geometry
    findings out-of-sample. BEATS-LADDER = positive both segments AND validate
    net > the (dir=none, market, ladder) reference on the same gate.

Also prints a per-segment BUY/SELL split at the gate (market+ladder) -- the
handoff's open question "is the buy side broken AT V18?" is answered by the
same run for free.

Usage (VM, through the daemon):
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
    DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=90 \
        python -u scripts/dexter3_entry_position_replay.py --symbol XAUUSD \
            --count 6000 --gates v18

Research script only -- no live behavior change, no deploy without owner
sign-off + a second independent window.
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
    _h1_trend_sign,
    _stamp_entry_gate_features,
)
from scripts.dexter3_convex_exit_replay import (  # noqa: E402
    _atr_mean,
    _parse_ladder_csv,
    _simulate_convex,
    _simulate_ladder,
)
from scripts.dexter3_geometry_optimizer import _equity  # noqa: E402
from dexter3 import hunt_mode  # noqa: E402
from dexter3.transport import make_client  # noqa: E402


# ---------------------------------------------------------------------------
# Lever 2 -- trough-limit entry
# ---------------------------------------------------------------------------


def _limit_fill(side: str, entry: float, sl: float, future: list, dip_r: float,
                window_bars: int) -> tuple[str, float | None, int | None]:
    """Walk ``future[:window_bars]`` for the first bar touching the limit at
    ``entry -/+ dip_r * risk``. Returns (status, fill_price, fill_idx):
      * ("filled", price, idx)         -- clean fill, original SL not breached
                                          on the fill bar;
      * ("filled_stopped", price, idx) -- the fill bar's range ALSO breached
                                          the original SL: OHLC cannot order
                                          the intrabar path, so conservatively
                                          the trade fills AND stops same-bar;
      * ("miss", None, None)           -- limit never touched in the window.
    For a buy the SL sits BELOW the limit, so any bar reaching the SL has by
    construction touched the limit too -- the first touching bar is always
    found before or on any stop-breaching bar."""
    risk = abs(entry - sl)
    if risk <= 0 or dip_r <= 0 or dip_r >= 1.0:
        return "miss", None, None
    limit_price = entry - dip_r * risk if side == "buy" else entry + dip_r * risk
    for idx, bar in enumerate(future[:window_bars]):
        lo = float(bar.get("low", 0.0))
        hi = float(bar.get("high", 0.0))
        if side == "buy":
            if lo <= limit_price:
                return ("filled_stopped" if lo <= sl else "filled"), limit_price, idx
        else:
            if hi >= limit_price:
                return ("filled_stopped" if hi >= sl else "filled"), limit_price, idx
    return "miss", None, None


def _trade_r(trade: dict, entry_model: str, dip_r: float, window_bars: int,
             exit_kind: str, exit_params: dict, atr: float, max_hold: int,
             spread_abs: float, commission_r: float) -> tuple[str, float | None]:
    """One accepted signal under one (entry_model, exit) row. Returns
    (status, r_net) where status is "taken" (r_net counts), "miss" (limit
    never filled -- r_net is None), or "skip" (degenerate risk).

    R is measured against the TAKEN stop distance (market: the signal's own
    risk; limit: |fill - original SL| = (1-dip_r) x risk), i.e. own-risk
    units -- the unit live $-risk sizing actually pays out in."""
    side, entry, sl, future = trade["side"], trade["entry"], trade["sl"], trade["future"]
    risk1 = abs(entry - sl)
    if risk1 <= 0:
        return "skip", None

    if entry_model == "market":
        sim_entry, sim_sl, sim_future = entry, sl, future
    else:  # "limit"
        status, fill_price, fill_idx = _limit_fill(side, entry, sl, future, dip_r, window_bars)
        if status == "miss":
            return "miss", None
        sim_entry, sim_sl = float(fill_price), sl
        new_risk = abs(sim_entry - sim_sl)
        if new_risk <= 0:
            return "skip", None
        cost = spread_abs / new_risk + commission_r
        if status == "filled_stopped":
            return "taken", -1.0 - cost
        sim_future = future[fill_idx + 1:]

    risk = abs(sim_entry - sim_sl)
    cost = spread_abs / risk + commission_r
    if exit_kind == "ladder":
        _outcome, r, _held = _simulate_ladder(side, sim_entry, sim_sl, sim_future,
                                              exit_params["rungs"], max_hold)
    else:  # "convex"
        _outcome, r, _held = _simulate_convex(side, sim_entry, sim_sl, sim_future,
                                              exit_params["arm_at"], exit_params["giveback_atr"],
                                              atr, max_hold)
    return "taken", r - cost


# ---------------------------------------------------------------------------
# Lever 1 -- direction filter (decision-time H1 trend sign, no lookahead)
# ---------------------------------------------------------------------------


def _dir_allows(mode: str, side: str, trend_sign: int) -> bool:
    if mode == "none":
        return True
    if mode == "nobuy-h1down":
        return not (side == "buy" and trend_sign == -1)
    if mode == "nocounter":
        return not ((side == "buy" and trend_sign == -1) or (side == "sell" and trend_sign == 1))
    raise ValueError(f"unknown dir mode: {mode}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(trades: list[dict], entry_model: str, dip_r: float, window_bars: int,
           exit_kind: str, exit_params: dict, atr: float, max_hold: int,
           spread_abs: float, commission_r: float) -> dict | None:
    """Score one row on one segment. Misses are counted and their
    counterfactual (market entry, SAME exit) is accumulated so the limit
    rows' selection bias is visible in the output."""
    rs: list[float] = []
    miss_cf: list[float] = []
    for t in trades:
        status, r = _trade_r(t, entry_model, dip_r, window_bars, exit_kind, exit_params,
                             atr, max_hold, spread_abs, commission_r)
        if status == "taken" and r is not None:
            rs.append(r)
        elif status == "miss":
            _s2, r_cf = _trade_r(t, "market", 0.0, 0, exit_kind, exit_params,
                                 atr, max_hold, spread_abs, commission_r)
            if r_cf is not None:
                miss_cf.append(r_cf)
    if not rs:
        return None
    net, pf, dd = _equity(rs)
    wr = 100.0 * sum(1 for r in rs if r > 0) / len(rs)
    return {
        "n": len(rs), "net": net, "pf": pf, "dd": dd, "wr": wr,
        "miss_n": len(miss_cf), "miss_net": sum(miss_cf),
        "fill_pct": 100.0 * len(rs) / max(1, len(rs) + len(miss_cf)),
    }


def _print_row(dir_mode: str, label: str, d: dict | None, v: dict | None, v_days: float,
               base_risk_usd: float, ladder_ref_net: float | None,
               verdict_override: str | None = None) -> None:
    if d is None or v is None:
        print(f"{dir_mode:<14} {label:<30} | (insufficient trades)")
        return
    usd_day = v["net"] / v_days * base_risk_usd
    if verdict_override:
        verdict = verdict_override
    else:
        both_pos = d["net"] > 0 and v["net"] > 0
        beats = ladder_ref_net is not None and v["net"] > ladder_ref_net
        verdict = "BEATS-LADDER" if (both_pos and beats) else (
            "both+ but <=ladder" if both_pos else
            ("validate+ only" if v["net"] > 0 else "fails validate"))
    print(f"{dir_mode:<14} {label:<30} | {d['n']:>4} {d['net']:>+8.2f} {d['pf']:>5.2f} | "
          f"{v['n']:>4} {v['net']:>+8.2f} {v['pf']:>5.2f} {v['wr']:>5.1f} | "
          f"{v['fill_pct']:>5.1f} {v['miss_n']:>5} {v['miss_net']:>+8.2f} | {usd_day:>+7.0f} {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--count", type=int, default=6000)
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--spread-abs", type=float, default=0.12)
    ap.add_argument("--commission-r", type=float, default=0.03)
    ap.add_argument("--gates", default="v18", help="entry-gate presets (v18 = live)")
    ap.add_argument("--max-hold", type=int, default=48,
                    help="ladder/market max hold; convex combos carry their own hold")
    ap.add_argument("--ladder-csv",
                    default="0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25")
    ap.add_argument("--convex-combos", default="1.0:3.0:48,2.0:3.0:24",
                    help="arm_at:giveback_atr:max_hold, PRE-REGISTERED from the prior v18 run")
    ap.add_argument("--limit-variants", default="0.3:6,0.4:6,0.5:6,0.4:12",
                    help="dip_r:window_bars limit-entry variants; 0.4:6 is the registered "
                         "primary (trough p50 bar0/p75 bar4, runner MAE p50 0.40R)")
    ap.add_argument("--dir-modes", default="none,nobuy-h1down,nocounter")
    ap.add_argument("--base-risk-usd", type=float, default=12.0)
    ap.add_argument("--min-derive-trades", type=int, default=60)
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

    # -- phase 1: decisions ONCE per bar, H1 trend sign stamped at decision time
    decisions: list[tuple[int, object, int]] = []
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
        decisions.append((i, d, _h1_trend_sign(h1c)))
    print(f"decisions: {len(decisions)} enter candidates")

    gate_modes = [g.strip() for g in args.gates.split(",") if g.strip()]
    rungs = _parse_ladder_csv(args.ladder_csv)
    convex_combos = []
    for part in args.convex_combos.split(","):
        arm_s, gb_s, mh_s = part.strip().split(":")
        convex_combos.append((float(arm_s), float(gb_s), int(mh_s)))
    limit_variants = []
    for part in args.limit_variants.split(","):
        dip_s, win_s = part.strip().split(":")
        limit_variants.append((float(dip_s), int(win_s)))
    dir_modes = [x.strip() for x in args.dir_modes.split(",") if x.strip()]

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)
    v_days = max(0.1, (_epoch(str(m5[-1]["ts"])) - _epoch(str(m5[split_bar]["ts"]))) / 86400.0)
    print(f"derive/validate split at bar {split_bar} (validate ~= {v_days:.1f} days)")

    exits: list[tuple[str, str, dict, int]] = [("ladder(live)", "ladder", {"rungs": rungs}, args.max_hold)]
    for arm, gb, mh in convex_combos:
        exits.append((f"convex a{arm:.1f} gb{gb:.1f} h{mh}", "convex",
                      {"arm_at": arm, "giveback_atr": gb}, mh))
    entries: list[tuple[str, str, float, int]] = [("market", "market", 0.0, 0)]
    for dip, win in limit_variants:
        entries.append((f"limit -{dip:.1f}R w{win}", "limit", dip, win))

    for mode in gate_modes:
        accepted: list[dict] = []
        for i, d, tsign in decisions:
            gate = _apply_entry_gate(d, mode if mode != "none" else "none", str(d.ts_close or ""))
            if not bool(gate.get("allow", True)):
                continue
            accepted.append({
                "i": i, "side": str(d.side), "entry": float(d.entry),
                "sl": float(d.sl), "tp": float(d.tp), "future": m5[i + 1:],
                "trend_sign": tsign,
            })
        derive_all = [t for t in accepted if t["i"] < split_bar]
        validate_all = [t for t in accepted if t["i"] >= split_bar]
        print(f"\n=== gate={mode}: accepted {len(accepted)} (derive {len(derive_all)} / validate {len(validate_all)}) ===")
        if len(derive_all) < args.min_derive_trades:
            print("insufficient derive trades -- skipped")
            continue

        # -- free diagnostic: BUY/SELL split per segment at this gate (market+ladder)
        print("\n-- side split at this gate (market entry, live ladder exit) --")
        for seg_name, seg in (("derive", derive_all), ("validate", validate_all)):
            for side in ("buy", "sell"):
                st = [t for t in seg if t["side"] == side]
                sc = _score(st, "market", 0.0, 0, "ladder", {"rungs": rungs}, atr,
                            args.max_hold, args.spread_abs, args.commission_r)
                if sc:
                    print(f"  {seg_name:<9} {side:<4} N={sc['n']:>4} net={sc['net']:>+8.2f}R "
                          f"PF={sc['pf']:.2f} WR={sc['wr']:.1f}%")

        header = (f"{'dir':<14} {'entry x exit':<30} | {'dN':>4} {'d_net':>8} {'d_PF':>5} | "
                  f"{'vN':>4} {'v_net':>8} {'v_PF':>5} {'WR%':>5} | {'fill%':>5} {'missN':>5} "
                  f"{'miss_cf':>8} | {'$/day':>7} verdict")
        ladder_ref_net: float | None = None

        for dmode in dir_modes:
            derive_t = [t for t in derive_all if _dir_allows(dmode, t["side"], t["trend_sign"])]
            validate_t = [t for t in validate_all if _dir_allows(dmode, t["side"], t["trend_sign"])]
            print(f"\n-- dir={dmode}: derive {len(derive_t)}/{len(derive_all)}, "
                  f"validate {len(validate_t)}/{len(validate_all)} --")
            print(header)
            print("-" * len(header))
            for e_label, e_model, dip, win in entries:
                for x_label, x_kind, x_params, x_hold in exits:
                    d_sc = _score(derive_t, e_model, dip, win, x_kind, x_params, atr,
                                  x_hold, args.spread_abs, args.commission_r)
                    v_sc = _score(validate_t, e_model, dip, win, x_kind, x_params, atr,
                                  x_hold, args.spread_abs, args.commission_r)
                    label = f"{e_label} x {x_label}"
                    is_ref = dmode == "none" and e_model == "market" and x_kind == "ladder"
                    if is_ref and v_sc is not None:
                        ladder_ref_net = v_sc["net"]
                    _print_row(dmode, label, d_sc, v_sc, v_days, args.base_risk_usd,
                               ladder_ref_net, verdict_override="REFERENCE" if is_ref else None)

    print("\nRULES: single-pass confirmatory matrix on pre-registered levers. BEATS-LADDER = "
          "positive net R on BOTH segments AND validate net > the (dir=none, market, ladder) "
          "reference at the same gate. Even then: this repo has killed 4 exit-geometry findings "
          "out-of-sample -- any winner needs a second independent window + owner sign-off before "
          "canary. Live implementation of a direction filter must be DOWNSIZE, never a hard block "
          "(demo rule). The miss_cf column is the R the skipped signals would have made at market "
          "entry under the same exit -- if it is large and positive, the limit entry is buying its "
          "geometry with real forgone edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

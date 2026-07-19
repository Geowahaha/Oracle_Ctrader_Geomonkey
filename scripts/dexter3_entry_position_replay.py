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
from bisect import bisect_left
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dexter3_edge_discovery import (  # noqa: E402
    MIN_M5,
    _apply_entry_gate,
    _completed_by,
    _epoch,
    _h1_trend_sign,
    _simulate,
    _stamp_entry_gate_features,
)
from scripts.dexter3_convex_exit_replay import (  # noqa: E402
    _atr_mean,
    _parse_ladder_csv,
    _simulate_convex,
    _simulate_ladder,
)
from scripts.dexter3_geometry_optimizer import _equity  # noqa: E402
from dexter3 import hunt_mode, market_lens, volume_profile  # noqa: E402
from dexter3.sd_zones import SDZoneEngine, decide_sdzone  # noqa: E402
from dexter3.transport import make_client  # noqa: E402


_ZONE_PREM_CAP = [0.0]   # set from --zone-prem-cap in main()
_CONFIRM_QUALITY = [""]  # ""|wick|engulf|wick-or-engulf -- --confirm-quality
_CONFIRM_WICK_K = [0.33]  # rejection wick >= K x bar range -- --confirm-wick-k
_CONFIRM_VOL_K = [0.0]   # confirm bar volume >= K x volMA20 (0=off) -- --confirm-vol


def _confirm_quality_ok(side: str, bar: dict, prev: dict | None) -> bool:
    """Owner directive 2026-07-19 (AJ Karn Trend/Zone/Rejection framework):
    the confirm bar must be a REAL rejection candle, not merely any close back
    in the entry direction. Modes (pre-registered, measured before live):
      * wick      -- pin-bar reading: the wick probing INTO the zone (lower
                     wick for a buy) >= K x the bar's full range;
      * engulf    -- the confirm body engulfs the prior bar's opposite-color
                     body (bullish engulfing at demand for a buy);
      * wick-or-engulf -- either reading qualifies.
    Independently, --confirm-vol requires bar volume >= K x its trailing
    volMA20 (stamped as bar["volma"] in main; a bar without volMA passes --
    the gate must never silently veto on missing data)."""
    mode = _CONFIRM_QUALITY[0]
    o = float(bar.get("open", 0.0))
    c = float(bar.get("close", 0.0))
    hi = float(bar.get("high", 0.0))
    lo = float(bar.get("low", 0.0))
    if mode:
        rng = hi - lo
        wick_ok = False
        if rng > 0:
            wick = (min(o, c) - lo) if side == "buy" else (hi - max(o, c))
            wick_ok = wick >= _CONFIRM_WICK_K[0] * rng
        eng_ok = False
        if prev is not None:
            po = float(prev.get("open", 0.0))
            pc = float(prev.get("close", 0.0))
            if side == "buy":
                eng_ok = c > o and pc < po and c >= po and o <= pc
            else:
                eng_ok = c < o and pc > po and c <= po and o >= pc
        if mode == "wick" and not wick_ok:
            return False
        if mode == "engulf" and not eng_ok:
            return False
        if mode == "wick-or-engulf" and not (wick_ok or eng_ok):
            return False
    if _CONFIRM_VOL_K[0] > 0:
        vma = float(bar.get("volma") or 0.0)
        vol = float(bar.get("volume") or 0.0)
        if vma > 0 and vol < _CONFIRM_VOL_K[0] * vma:
            return False
    return True


def _stamp_volma(bars: list, period: int = 20) -> None:
    """Trailing volume SMA stamped in-place as bar["volma"] (running sum,
    trailing-only -- bar i sees volumes of bars [i-19..i])."""
    run = 0.0
    for k, b in enumerate(bars):
        run += float(b.get("volume", 0.0) or 0.0)
        if k >= period:
            run -= float(bars[k - period].get("volume", 0.0) or 0.0)
        b["volma"] = run / min(k + 1, period)


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


def _simulate_plain_be(side: str, entry: float, sl: float, tp: float, future: list,
                       arm_r: float, be_floor_r: float, max_hold: int
                       ) -> tuple[str, float, int]:
    """PLAIN + one-step BREAKEVEN RATCHET (owner surgery 2026-07-17: fable
    trades peaked +0.99/+0.83/+1.60R and ALL round-tripped to full -1R under
    plain — "เห็นกำไรแล้วปล่อยตาย"). Same SL-first/TP conventions as
    _simulate, plus: once the favorable excursion reaches ``arm_r``, the
    stop ratchets ONCE to ``be_floor_r`` (entry + epsilon) — a single
    protective step, NOT the multi-rung ladder the 3-window measure already
    killed. Arm/exit same-bar allowed (ladder-sim convention)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    armed = False
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        if side == "buy":
            if lo <= sl:
                return "loss", -1.0, held
            if hi >= tp:
                return "win", (tp - entry) / risk, held
            if not armed and (hi - entry) / risk >= arm_r:
                armed = True
            if armed and (lo - entry) / risk <= be_floor_r:
                return "be", be_floor_r, held
        else:
            if hi >= sl:
                return "loss", -1.0, held
            if lo <= tp:
                return "win", (entry - tp) / risk, held
            if not armed and (entry - lo) / risk >= arm_r:
                armed = True
            if armed and (entry - hi) / risk <= be_floor_r:
                return "be", be_floor_r, held
    held = min(max_hold, len(future)) - 1
    last = float(future[held].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _simulate_bank(side: str, entry: float, sl: float, future: list, bank_r: float,
                   max_hold: int) -> tuple[str, float, int]:
    """v1.0 BANK-GREEN scalp exit (owner 2026-07-17 "V.1.0 original ของเรายัง
    ทำงานดีกว่า grok"): SL-first conservative on wicks; BANK the trade at the
    first M5 bar whose CLOSE reaches entry +/- bank_r x risk (close-based —
    banking a confirmed green, not a wick); hard time cap at max_hold bars ->
    mark to the last close. Parameters 0.4R/12 bars are the harvester-sweep
    winners (derive +48.20R / validate +13.27R — the only scalp concept that
    ever passed both segments in this repo)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0, 0
    for held, bar in enumerate(future[:max_hold]):
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        c = float(bar.get("close", 0.0))
        if side == "buy":
            if lo <= sl:
                return "loss", -1.0, held
            if c >= entry + bank_r * risk:
                return "bank", (c - entry) / risk, held
        else:
            if hi >= sl:
                return "loss", -1.0, held
            if c <= entry - bank_r * risk:
                return "bank", (entry - c) / risk, held
    held = min(max_hold, len(future)) - 1
    last = float(future[held].get("close", entry)) if future else entry
    r = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return ("win" if r > 0 else "loss"), r, max(0, held)


def _zone_confirm_entry(side: str, entry: float, sl: float, future: list, dip_r: float,
                        window_bars: int, prem_cap: float = 0.0
                        ) -> tuple[str, float | None, int | None]:
    """Owner directive 2026-07-16: "เปลี่ยนจากวาง limit เป็นโซน ตรวจสอบเบรคจริง
    + สัญญาณกลับตัว" -- the discount level becomes a ZONE instead of a resting
    LIMIT:
      * TOUCH: a bar's range reaches zone_top = entry -/+ dip_r * risk;
      * REAL-BREAK CHECK: any bar that CLOSES beyond the original SL kills the
        setup ("zone_break", no trade) -- but a WICK beyond the SL is survived,
        because there is no position yet. This is the mechanism a blind limit
        cannot have: the same wick fills-and-stops the limit at -1R;
      * REVERSAL SIGNAL: at/after the touch, the first bar that closes back in
        the entry direction (green close above zone_top for a buy) while the
        discount is still intact (close still below the signal entry) ->
        ENTER at that bar's CLOSE (M5-close decision = live-faithful market
        order). The touch bar itself may confirm.
    The confirmed entry is WORSE-priced than the limit (close in
    (zone_top, entry) vs exactly zone_top) -- knife protection is bought with
    entry price; whether that trade is net-positive is what the replay
    measures. Returns (status, entry_px, idx):
    filled | no_touch | no_confirm | zone_break."""
    risk = abs(entry - sl)
    if risk <= 0 or dip_r <= 0 or dip_r >= 1.0:
        return "no_touch", None, None
    zone_top = entry - dip_r * risk if side == "buy" else entry + dip_r * risk
    touched = False
    for idx, bar in enumerate(future[:window_bars]):
        o = float(bar.get("open", 0.0))
        c = float(bar.get("close", 0.0))
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        stop_pts = abs(zone_top - sl)
        if side == "buy":
            if c <= sl:                              # CONFIRMED break -- thesis dead
                return "zone_break", None, None
            if not touched and lo <= zone_top:
                touched = True
            if touched and c > o and c > zone_top and c < entry:
                if prem_cap > 0 and (c - zone_top) > prem_cap * stop_pts:
                    continue                         # confirm too far from the zone -- wait
                if not _confirm_quality_ok("buy", bar, future[idx - 1] if idx >= 1 else None):
                    continue                         # not a real rejection candle -- wait
                return "filled", c, idx
        else:
            if c >= sl:
                return "zone_break", None, None
            if not touched and hi >= zone_top:
                touched = True
            if touched and c < o and c < zone_top and c > entry:
                if prem_cap > 0 and (zone_top - c) > prem_cap * stop_pts:
                    continue
                if not _confirm_quality_ok("sell", bar, future[idx - 1] if idx >= 1 else None):
                    continue
                return "filled", c, idx
    return ("no_confirm" if touched else "no_touch"), None, None


def _zone_confirm_entry_m1(side: str, entry: float, sl: float, m1_future: list,
                           dip_r: float, deadline_epoch: float
                           ) -> tuple[str, float | None, float | None]:
    """Owner idea 2026-07-16 ("เข้าจุดที่ดีที่สุดใน M1 โดย pattern ที่ M5 ไฟเขียว"):
    the SAME zone mechanism, but confirmation runs on M1 bars — the M5 pattern
    supplies the zone (green light), M1 supplies the trigger. Vs the M5-close
    confirm this enters ~5x sooner after the touch, so the price is closer to
    the zone (recovering most of the limit's discount) while keeping the
    knife-protection (an M1 CLOSE beyond the SL kills the setup — and is seen
    up to 4 minutes sooner than the M5 close). Walks ``m1_future`` (M1 bars at/
    after the signal close) until ``deadline_epoch``. Returns
    (status, entry_px, entry_epoch)."""
    risk = abs(entry - sl)
    if risk <= 0 or dip_r <= 0 or dip_r >= 1.0:
        return "no_touch", None, None
    zone_top = entry - dip_r * risk if side == "buy" else entry + dip_r * risk
    touched = False
    prev_bar: dict | None = None
    for bar in m1_future:
        e = _epoch(str(bar.get("ts") or ""))
        if e >= deadline_epoch:
            break
        o = float(bar.get("open", 0.0))
        c = float(bar.get("close", 0.0))
        hi = float(bar.get("high", 0.0))
        lo = float(bar.get("low", 0.0))
        if side == "buy":
            if c <= sl:
                return "zone_break", None, None
            if not touched and lo <= zone_top:
                touched = True
            if touched and c > o and c > zone_top and c < entry:
                if _confirm_quality_ok("buy", bar, prev_bar):
                    return "filled", c, e
        else:
            if c >= sl:
                return "zone_break", None, None
            if not touched and hi >= zone_top:
                touched = True
            if touched and c < o and c < zone_top and c > entry:
                if _confirm_quality_ok("sell", bar, prev_bar):
                    return "filled", c, e
        prev_bar = bar
    return ("no_confirm" if touched else "no_touch"), None, None


def _m1_entry_to_m5_exit(side: str, entry_px: float, sl: float, entry_epoch: float,
                         m1_future: list, m5_future: list) -> tuple[list, bool]:
    """Bridge an M1-timed entry back onto the M5 exit stream with NO blind
    spot: the remainder of the entry's own M5 period is checked for SL wicks
    on the REAL M1 bars (favorable excursion there is ignored — pessimistic,
    same convention as the fill-bar rule elsewhere). Returns (m5_bars_from_
    next_period, stopped_in_entry_period)."""
    boundary = (entry_epoch // 300) * 300 + 300
    for bar in m1_future:
        e = _epoch(str(bar.get("ts") or ""))
        if e <= entry_epoch or e >= boundary:
            continue
        lo = float(bar.get("low", 0.0))
        hi = float(bar.get("high", 0.0))
        if (side == "buy" and lo <= sl) or (side == "sell" and hi >= sl):
            return [], True
    rest = [b for b in m5_future if _epoch(str(b.get("ts") or "")) >= boundary]
    return rest, False


def _trade_r(trade: dict, entry_model: str, dip_r: float, window_bars: int,
             exit_kind: str, exit_params: dict, atr: float, max_hold: int,
             spread_abs: float, commission_r: float) -> tuple[str, float | None]:
    """One accepted signal under one (entry_model, exit) row. Returns
    (status, r_net, resolve_offset): "taken" (r_net counts), "skip", or a
    miss class ("miss_no_touch" limit/zone never reached; "miss_no_confirm"
    zone touched but no reversal bar; "miss_break" zone broken by a close
    beyond the SL -- the knife the break-check refused to catch).
    ``resolve_offset`` = 0-based offset into ``future`` where the trade
    resolved (None for misses/skips) -- the --no-overlap mode needs it to
    model a single-position lane.

    R is measured against the TAKEN stop distance (market: the signal's own
    risk; limit: (1-dip_r) x risk; zone: |confirm close - original SL|),
    i.e. own-risk units -- the unit live $-risk sizing pays out in."""
    side, entry, sl, future = trade["side"], trade["entry"], trade["sl"], trade["future"]
    risk1 = abs(entry - sl)
    if risk1 <= 0:
        return "skip", None, None

    base_idx = 0
    if entry_model == "market":
        sim_entry, sim_sl, sim_future = entry, sl, future
    elif entry_model == "limit":
        status, fill_price, fill_idx = _limit_fill(side, entry, sl, future, dip_r, window_bars)
        if status == "miss":
            return "miss_no_touch", None, None
        sim_entry, sim_sl = float(fill_price), sl
        new_risk = abs(sim_entry - sim_sl)
        if new_risk <= 0:
            return "skip", None, None
        cost = spread_abs / new_risk + commission_r
        if status == "filled_stopped":
            return "taken", -1.0 - cost, fill_idx
        base_idx = fill_idx + 1
        sim_future = future[base_idx:]
    elif entry_model == "zone":  # touch + real-break check + reversal confirmation
        status, fill_price, fill_idx = _zone_confirm_entry(side, entry, sl, future,
                                                           dip_r, window_bars,
                                                           prem_cap=_ZONE_PREM_CAP[0])
        if status != "filled":
            return f"miss_{status}", None, None
        sim_entry, sim_sl = float(fill_price), sl
        if abs(sim_entry - sim_sl) <= 0:
            return "skip", None, None
        base_idx = fill_idx + 1
        sim_future = future[base_idx:]
    else:  # "zone_m1" -- M5 green-light zone, M1-resolution confirm + break check
        m1_future = trade.get("m1_future") or []
        if not m1_future:
            return "miss_no_m1", None, None
        deadline = trade["close_epoch"] + window_bars * 300
        status, fill_price, fill_epoch = _zone_confirm_entry_m1(side, entry, sl, m1_future,
                                                                dip_r, deadline)
        if status != "filled":
            return f"miss_{status}", None, None
        sim_entry, sim_sl = float(fill_price), sl
        if abs(sim_entry - sim_sl) <= 0:
            return "skip", None, None
        sim_future, stopped = _m1_entry_to_m5_exit(side, sim_entry, sl, float(fill_epoch),
                                                   m1_future, future)
        if stopped:
            new_risk = abs(sim_entry - sim_sl)
            return "taken", -1.0 - (spread_abs / new_risk + commission_r), 0

    risk = abs(sim_entry - sim_sl)
    cost = spread_abs / risk + commission_r
    if exit_kind == "ladder":
        _outcome, r, held = _simulate_ladder(side, sim_entry, sim_sl, sim_future,
                                             exit_params["rungs"], max_hold)
    elif exit_kind == "plain_be":
        _outcome, r, held = _simulate_plain_be(side, sim_entry, sim_sl, float(trade["tp"]),
                                               sim_future, exit_params.get("arm_r", 0.7),
                                               exit_params.get("be_floor_r", 0.05), max_hold)
    elif exit_kind == "bank":
        _outcome, r, held = _simulate_bank(side, sim_entry, sim_sl, sim_future,
                                           exit_params.get("bank_r", 0.4),
                                           max_hold)
    elif exit_kind == "plain":
        # the signal's own TP price (VP's gate-winning posture was plain h48);
        # from a discounted entry the same TP level is simply further in R.
        _outcome, r, held = _simulate(side, sim_entry, sim_sl, float(trade["tp"]),
                                      sim_future, max_hold)
    else:  # "convex"
        _outcome, r, held = _simulate_convex(side, sim_entry, sim_sl, sim_future,
                                             exit_params["arm_at"], exit_params["giveback_atr"],
                                             atr, max_hold)
    return "taken", r - cost, base_idx + max(0, int(held))


# ---------------------------------------------------------------------------
# Lever 1 -- direction filters (all decision-time data, no lookahead)
# ---------------------------------------------------------------------------

# Owner idea 2026-07-16 ("เทรนของวัน" -- solid line on the owner's DZ/SZ
# indicator = the DAY OPEN): after 1-3 hours, price BELOW the day open
# ("ต่ำเปิด") = look for sells only; price ABOVE it ("ยืนเปิด") = buys only;
# a session change means wait and re-judge. Anchors here:
#   d0   = 00:00 UTC daily open;
#   d22  = 22:00 UTC daily open (the gold/Globex day roll -- closest to the
#          broker "day" the owner's chart draws);
#   sess = most recent of 00/07/12 UTC (asia/london/ny session opens -- the
#          "เปลี่ยนทามโซน ให้รอพิจารณาใหม่" reset, re-anchored each session).
# The raw bias is sign(close - anchor open); each MODE applies its own
# min-hours gate (bias too young -> neutral -> both sides allowed). Sunday
# reopen note: the d0 anchor's "open" on Sundays is the reopen bar itself
# (no 00:00Z bar exists), so its early-hours bias is weak there; d22 does
# not have this problem, which is why both anchors are tested.
BIAS_ANCHORS: dict[str, list[int]] = {"d0": [0], "d22": [22], "sess": [0, 7, 12]}


def _anchor_bias_fields(m5: list, anchors: dict[str, list[int]] = BIAS_ANCHORS) -> list[dict]:
    """Per-bar {bias_<key>: -1|0|+1, hrs_<key>: float} for each anchor set.
    The anchor period's OPEN is the open of the first bar at/after the most
    recent anchor hour; bias compares the CURRENT bar's close to it (both
    decision-time facts). One O(n) pass, no lookahead."""
    out: list[dict] = []
    prev_anchor: dict[str, float | None] = {k: None for k in anchors}
    open_px: dict[str, float | None] = {k: None for k in anchors}
    for b in m5:
        e = _epoch(str(b.get("ts") or ""))
        row: dict = {}
        for key, hours in anchors.items():
            cand = max(((e - h * 3600) // 86400) * 86400 + h * 3600 for h in hours)
            if cand != prev_anchor[key]:
                prev_anchor[key] = cand
                open_px[key] = float(b.get("open", 0.0))
            if open_px[key] is None or e <= 0:
                row[f"bias_{key}"] = 0
                row[f"hrs_{key}"] = 0.0
            else:
                diff = float(b.get("close", 0.0)) - float(open_px[key])
                row[f"bias_{key}"] = 1 if diff > 0 else (-1 if diff < 0 else 0)
                row[f"hrs_{key}"] = (e - float(prev_anchor[key])) / 3600.0
        out.append(row)
    return out


def decide_daytrend(m5_prefix: list, bias_row: dict, atr: float,
                    pull_atr: float = 0.8, swing_bars: int = 6,
                    buffer_atr: float = 0.1, min_hours: float = 1.0,
                    max_risk_atr: float = 2.0, range_cap_atr: float = 0.0,
                    last_entry_hour: float = 0.0) -> dict | None:
    """WITH-BIAS continuation producer (owner live lesson 2026-07-16: a
    40-pt sell-only day where every counter-trend producer was correctly
    bias-blocked and every with-trend hunt signal was gate-blocked — the
    system had direction and entry machinery but NO producer firing WITH
    the day). Pre-registered, minimal params:
      * day bias established (>=min_hours, from the SAME 00Z anchor the live
        gate uses);
      * price has PULLED BACK >= pull_atr x ATR against the bias from the
        day's running extreme (sell day: bounce off the low — the retest of
        what just broke, the owner's "ราคาพัก" read);
      * the newest bar CLOSES back in the bias direction (reversal-resume);
      * SL beyond the pullback swing (max/min of the last swing_bars) +
        buffer; skip if that risk > max_risk_atr x ATR (blown structure).
    TP = the day extreme (retest target). Returns {side, entry, sl, tp} or
    None. Uses ONLY closed-bar data (no lookahead)."""
    if len(m5_prefix) < swing_bars + 3 or atr <= 0:
        return None
    bias = int(bias_row.get("bias_d0", 0))
    hrs = float(bias_row.get("hrs_d0", 0.0))
    if bias == 0 or hrs < min_hours:
        return None
    # G2 (owner 2026-07-16, after the first live SL: "แนวนี้คงเป็น DZ ของวัน
    # มีโอกาสรีบาวด์"): session maturity -- no NEW continuation entries
    # after this many hours into the day (late extreme = the day's DZ/SZ).
    if last_entry_hour > 0 and hrs > last_entry_hour:
        return None
    last = m5_prefix[-1]
    o, c = float(last.get("open", 0.0)), float(last.get("close", 0.0))
    # bars since the day anchor (approx: use trailing window of the day so
    # far — the bias hours tell us how deep to look)
    day_bars = min(len(m5_prefix), max(swing_bars + 2, int(hrs * 12) + 1))
    day = m5_prefix[-day_bars:]
    # G1 (same owner observation): capitulation-day exhaustion -- when the
    # day has already traveled more than range_cap_atr x ATR high-to-low,
    # the extreme is a demand/supply zone, not a continuation target.
    if range_cap_atr > 0:
        d_hi = max(float(b.get("high", 0.0)) for b in day)
        d_lo = min(float(b.get("low", 0.0)) for b in day)
        if (d_hi - d_lo) > range_cap_atr * atr:
            return None
    if bias < 0:  # sell-only day: extreme = the day's low
        extreme = min(float(b.get("low", 0.0)) for b in day)
        pullback = c - extreme
        resumes = c < o                       # red close = resuming down
        if not (pullback >= pull_atr * atr and resumes):
            return None
        swing_hi = max(float(b.get("high", 0.0)) for b in m5_prefix[-swing_bars:])
        sl = swing_hi + buffer_atr * atr
        risk = sl - c
        if risk <= 0 or risk > max_risk_atr * atr:
            return None
        return {"side": "sell", "entry": c, "sl": sl, "tp": extreme}
    extreme = max(float(b.get("high", 0.0)) for b in day)
    pullback = extreme - c
    resumes = c > o
    if not (pullback >= pull_atr * atr and resumes):
        return None
    swing_lo = min(float(b.get("low", 0.0)) for b in m5_prefix[-swing_bars:])
    sl = swing_lo - buffer_atr * atr
    risk = c - sl
    if risk <= 0 or risk > max_risk_atr * atr:
        return None
    return {"side": "buy", "entry": c, "sl": sl, "tp": extreme}


def decide_dayreversal(m5_prefix: list, bias_row: dict, atr: float,
                       range_arm_atr: float = 12.0, near_extreme_atr: float = 2.0,
                       swing_k: int = 2, buffer_atr: float = 0.1,
                       retrace_frac: float = 0.5, min_hours: float = 1.0,
                       max_risk_atr: float = 2.0) -> dict | None:
    """DZ/SZ EXHAUSTION-REVERSAL producer (owner question 2026-07-16: "ในเมื่อ
    เป็น DZ ของวันแล้ว ทำไมไม่เปลี่ยนเป็น Buy เมื่อยก low ยก high — สัญญาณกลับตัว
    classic"). The mirror twin of daytrend: the SAME capitulation condition
    that should stop continuation entries ARMS the reversal hunt.

    Pre-registered conditions (sell-day mirror for buys; all closed-bar):
      1. day bias established (>=min_hours) -> there IS a day extreme;
      2. CAPITULATION: day high-low range >= range_arm_atr x ATR (the G1
         trigger, reused as the arming condition);
      3. price within near_extreme_atr x ATR of the day extreme zone;
      4. CLASSIC STRUCTURE FLIP on M5: the last swing low is HIGHER than the
         previous swing low (ยก low, fractal k=swing_k) AND the newest bar
         CLOSES above the last swing high (ยก high = micro-structure break);
      5. SL below min(day low, last swing low) - buffer (beyond the DZ);
         TP at extreme + retrace_frac x day range (the 50% retrace of the
         capitulation leg). Skip if risk > max_risk_atr x ATR.
    Returns {side, entry, sl, tp} or None."""
    n = len(m5_prefix)
    if n < swing_k * 2 + 8 or atr <= 0:
        return None
    bias = int(bias_row.get("bias_d0", 0))
    hrs = float(bias_row.get("hrs_d0", 0.0))
    if bias == 0 or hrs < min_hours:
        return None
    day_bars = min(n, max(swing_k * 2 + 8, int(hrs * 12) + 1))
    day = m5_prefix[-day_bars:]
    d_hi = max(float(b.get("high", 0.0)) for b in day)
    d_lo = min(float(b.get("low", 0.0)) for b in day)
    day_range = d_hi - d_lo
    if day_range < range_arm_atr * atr:
        return None                                   # no capitulation -> no reversal hunt
    last = m5_prefix[-1]
    c = float(last.get("close", 0.0))

    def _swings(vals: list, is_low: bool) -> list:
        out = []
        for j in range(swing_k, len(vals) - swing_k):
            w = vals[j - swing_k: j + swing_k + 1]
            if (is_low and vals[j] == min(w)) or ((not is_low) and vals[j] == max(w)):
                out.append((j, vals[j]))
        return out

    if bias < 0:                                      # sell day -> hunt the BUY reversal at the DZ
        if c - d_lo > near_extreme_atr * atr:
            return None                               # not at the extreme zone
        lows = [float(b.get("low", 0.0)) for b in day]
        highs = [float(b.get("high", 0.0)) for b in day]
        sw_lo = _swings(lows, True)
        sw_hi = _swings(highs, False)
        if len(sw_lo) < 2 or not sw_hi:
            return None
        (_, prev_low), (last_idx, last_low) = sw_lo[-2], sw_lo[-1]
        if not (last_low > prev_low):                 # ยก low
            return None
        hi_after = [v for j, v in sw_hi if j >= last_idx - swing_k]
        ref_hi = hi_after[-1] if hi_after else sw_hi[-1][1]
        if not (c > ref_hi):                          # ยก high: close breaks the last swing high
            return None
        sl = min(d_lo, last_low) - buffer_atr * atr
        risk = c - sl
        if risk <= 0 or risk > max_risk_atr * atr:
            return None
        return {"side": "buy", "entry": c, "sl": sl, "tp": d_lo + retrace_frac * day_range}
    # buy day -> hunt the SELL reversal at the SZ (mirror)
    if d_hi - c > near_extreme_atr * atr:
        return None
    lows = [float(b.get("low", 0.0)) for b in day]
    highs = [float(b.get("high", 0.0)) for b in day]
    sw_hi = _swings(highs, False)
    sw_lo = _swings(lows, True)
    if len(sw_hi) < 2 or not sw_lo:
        return None
    (_, prev_hi), (last_idx, last_hi) = sw_hi[-2], sw_hi[-1]
    if not (last_hi < prev_hi):
        return None
    lo_after = [v for j, v in sw_lo if j >= last_idx - swing_k]
    ref_lo = lo_after[-1] if lo_after else sw_lo[-1][1]
    if not (c < ref_lo):
        return None
    sl = max(d_hi, last_hi) + buffer_atr * atr
    risk = sl - c
    if risk <= 0 or risk > max_risk_atr * atr:
        return None
    return {"side": "sell", "entry": c, "sl": sl, "tp": d_hi - retrace_frac * day_range}


def decide_channelfade(m5_prefix: list, atr: float, window: int = 36,
                       min_h_atr: float = 1.5, max_h_atr: float = 4.0,
                       max_eff: float = 0.35, edge_frac: float = 0.2,
                       min_touches: int = 2, sl_buf_atr: float = 0.35,
                       max_risk_atr: float = 2.0, tp_frac: float = 0.5) -> dict | None:
    """CHANNEL-EDGE FADE producer (owner 2026-07-16/17: "ตลาด sideway ใน H1 DZ
    มี ch ชัดเจน เราไม่ควรเข้ากลางทาง ควรเก็บ Sell/Buy ขอบ ch บนล่าง") — the
    RANGE phase of the day grammar, the one phase no lane covers. All
    pre-registered, closed-bar only:
      * channel over the trailing ``window`` M5 bars: height H in
        [min_h_atr, max_h_atr] x ATR (too tight = spread noise, too wide =
        trending), directional efficiency |drift|/H <= max_eff (sideways),
        and >= min_touches bar-touches of EACH edge band (a real box, not a
        drift);
      * NO mid-channel entries: the newest bar must touch an edge band
        (within edge_frac x H of the edge) AND close back INSIDE in the fade
        direction (reversal close — the same M5-close confirm convention as
        every other producer; the live lane's M1 ENTRY_CONFIRM refines it);
      * SL beyond the edge + sl_buf_atr x ATR; TP = mid-channel (primary —
        the conservative half-box target).
    Returns {side, entry, sl, tp} or None."""
    if len(m5_prefix) < window + 2 or atr <= 0:
        return None
    seg = m5_prefix[-window:]
    highs = [float(b.get("high", 0.0)) for b in seg]
    lows = [float(b.get("low", 0.0)) for b in seg]
    closes = [float(b.get("close", 0.0)) for b in seg]
    ch_hi, ch_lo = max(highs), min(lows)
    height = ch_hi - ch_lo
    if not (min_h_atr * atr <= height <= max_h_atr * atr):
        return None
    if abs(closes[-1] - closes[0]) / height > max_eff:
        return None
    band = edge_frac * height
    if sum(1 for h in highs if h >= ch_hi - band) < min_touches:
        return None
    if sum(1 for l in lows if l <= ch_lo + band) < min_touches:
        return None
    last = seg[-1]
    o = float(last.get("open", 0.0))
    c = float(last.get("close", 0.0))
    hi = float(last.get("high", 0.0))
    lo = float(last.get("low", 0.0))
    mid = (ch_hi + ch_lo) / 2.0
    # SELL the top edge: bar touched the top band, closed red back inside,
    # close still in the upper half (never a mid-channel entry).
    if hi >= ch_hi - band and c < o and c > mid:
        sl = ch_hi + sl_buf_atr * atr
        risk = sl - c
        if 0 < risk <= max_risk_atr * atr:
            return {"side": "sell", "entry": c, "sl": sl, "tp": ch_hi - tp_frac * height}
    if lo <= ch_lo + band and c > o and c < mid:
        sl = ch_lo - sl_buf_atr * atr
        risk = c - sl
        if 0 < risk <= max_risk_atr * atr:
            return {"side": "buy", "entry": c, "sl": sl, "tp": ch_lo + tp_frac * height}
    return None


def _dir_allows(mode: str, side: str, ctx: dict) -> bool:
    """ctx carries the decision-time direction facts stamped on the trade:
    trend_sign (H1 6-bar) + bias_d0/bias_d22/bias_sess (+ hrs_*)."""
    if mode == "none":
        return True
    tsign = int(ctx.get("trend_sign", 0))
    if mode == "nobuy-h1down":
        return not (side == "buy" and tsign == -1)
    if mode == "nocounter":
        return not ((side == "buy" and tsign == -1) or (side == "sell" and tsign == 1))
    if mode.startswith("dayopen") or mode.startswith("sessopen"):
        # dayopen0-h1 / dayopen22-h3 / sessopen-h1 -> (anchor key, min hours)
        head, _, h_part = mode.partition("-h")
        min_h = float(h_part)
        key = {"dayopen0": "d0", "dayopen22": "d22", "sessopen": "sess"}[head]
        bias = int(ctx.get(f"bias_{key}", 0))
        if float(ctx.get(f"hrs_{key}", 0.0)) < min_h:
            bias = 0                      # too young -> neutral, allow both
        if bias == 0:
            return True
        return (side == "buy") == (bias > 0)
    raise ValueError(f"unknown dir mode: {mode}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(trades: list[dict], entry_model: str, dip_r: float, window_bars: int,
           exit_kind: str, exit_params: dict, atr: float, max_hold: int,
           spread_abs: float, commission_r: float, no_overlap: bool = False) -> dict | None:
    """Score one row on one segment. Misses are counted PER CLASS and each
    class's counterfactual (market entry, SAME exit) is accumulated so both
    the limit rows' selection bias AND the zone rows' break-check savings
    are visible in the output (cf of miss_break should be deeply negative
    if the break-check is doing its knife-refusal job)."""
    rs: list[float] = []
    miss_cf: dict[str, list[float]] = {"no_touch": [], "no_confirm": [], "break": []}
    busy_until = -1
    for t in sorted(trades, key=lambda x: x.get("i", 0)):
        if no_overlap and t.get("i", 0) < busy_until:
            continue                    # single-position lane is still in a trade
        status, r, resolve_off = _trade_r(t, entry_model, dip_r, window_bars, exit_kind,
                                          exit_params, atr, max_hold, spread_abs, commission_r)
        if status == "taken" and r is not None:
            rs.append(r)
            if no_overlap and resolve_off is not None:
                busy_until = t.get("i", 0) + 1 + int(resolve_off)
        elif status.startswith("miss"):
            key = status.replace("miss_", "").replace("zone_", "") or "no_touch"
            _s2, r_cf, _ro = _trade_r(t, "market", 0.0, 0, exit_kind, exit_params,
                                      atr, max_hold, spread_abs, commission_r)
            if r_cf is not None:
                miss_cf.setdefault(key, []).append(r_cf)
    if not rs:
        return None
    net, pf, dd = _equity(rs)
    wr = 100.0 * sum(1 for r in rs if r > 0) / len(rs)
    all_miss = [x for v in miss_cf.values() for x in v]
    return {
        "n": len(rs), "net": net, "pf": pf, "dd": dd, "wr": wr,
        "miss_n": len(all_miss), "miss_net": sum(all_miss),
        "fill_pct": 100.0 * len(rs) / max(1, len(rs) + len(all_miss)),
        "miss_by": {k: (len(v), sum(v)) for k, v in miss_cf.items() if v},
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
    if d.get("miss_by") and len(d["miss_by"]) > 1:
        def _mb(sc: dict) -> str:
            return " ".join(f"{k}:{n}cf{s:+.0f}" for k, (n, s) in sorted(sc["miss_by"].items()))
        print(f"{'':<14} {'  zone miss classes':<30} | d[{_mb(d)}] v[{_mb(v)}]")


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
    ap.add_argument("--zone-variants", default="",
                    help="dip_r:window_bars ZONE-CONFIRM entry variants (touch + real-break "
                         "check + reversal-close entry) -- owner directive 2026-07-16")
    ap.add_argument("--zone-m1-variants", default="",
                    help="dip_r:window_bars zone variants confirmed on M1 bars (M5 green "
                         "light, M1 best-entry trigger) -- owner idea 2026-07-16. NOTE: "
                         "daemon M1 history is ~14 days; rows outside it report miss_no_m1")
    ap.add_argument("--producer", choices=("hunt", "vp", "daytrend", "dayreversal", "channelfade", "sdzone"), default="hunt",
                    help="signal producer: hunt = live decide_hunt committee; vp = "
                         "volume_profile.decide_vp (the only gate-passer in repo history); "
                         "daytrend = with-bias pullback-continuation (owner live lesson "
                         "2026-07-16). vp/daytrend force gates=none")
    ap.add_argument("--exits", default="ladder,convex",
                    help="comma set from ladder,plain,convex; plain h48 = VP's "
                         "gate-winning posture (signal TP, SL-first, hold 48)")
    ap.add_argument("--dir-modes", default="none,nobuy-h1down,nocounter")
    ap.add_argument("--zone-prem-cap", type=float, default=0.0,
                    help="zone-confirm entry: reject confirms paying more than this "
                         "fraction of the stop above the level (0=off)")
    ap.add_argument("--be-arms", default="0.6,0.8",
                    help="plain_be exit: arm thresholds (peak R) for the one-step BE ratchet")
    ap.add_argument("--bank-r", type=float, default=0.4,
                    help="bank exit: close-based take at this R (harvester-proven 0.4)")
    ap.add_argument("--bank-hold", type=int, default=12,
                    help="bank exit: hard time cap in M5 bars (harvester-proven 12)")
    ap.add_argument("--sdz-rr", type=float, default=2.0,
                    help="sdzone TP as RR multiple of the zone-anchored risk")
    ap.add_argument("--sdz-tp", choices=("rr", "zone", "zone-minrr1"), default="rr",
                    help="sdzone TP placement (owner framework 2026-07-19 'TP ที่ Zone "
                         "ถัดไป'): rr = fixed --sdz-rr (live today); zone = nearest "
                         "OPPOSING zone edge, fallback rr when none exists; zone-minrr1 "
                         "= zone target but SKIP the signal when that target pays <1R")
    ap.add_argument("--confirm-quality", choices=("", "wick", "engulf", "wick-or-engulf"),
                    default="",
                    help="zone-confirm rejection-quality gate (owner framework "
                         "2026-07-19): the confirm bar must be a pin-bar (wick) and/or "
                         "engulfing candle, not merely any reversal close")
    ap.add_argument("--confirm-wick-k", type=float, default=0.33,
                    help="wick mode: rejection wick must be >= this fraction of the "
                         "confirm bar's full range")
    ap.add_argument("--confirm-vol", type=float, default=0.0,
                    help="confirm bar volume must be >= this x trailing volMA20 "
                         "(0=off); bars without volMA pass")
    ap.add_argument("--chf-tp-frac", type=float, default=0.5,
                    help="channelfade TP as fraction of box height from the faded edge "
                         "(0.5=mid primary, 0.85=near opposite edge secondary)")
    ap.add_argument("--drev-arm-atr", type=float, default=12.0,
                    help="dayreversal: capitulation arming threshold (day range in ATR)")
    ap.add_argument("--dt-range-cap-atr", type=float, default=0.0,
                    help="daytrend G1: skip entries once the day's high-low range "
                         "exceeds this x ATR (0=off) -- capitulation exhaustion guard")
    ap.add_argument("--dt-last-hour", type=float, default=0.0,
                    help="daytrend G2: no new entries after this many hours into the "
                         "day (0=off) -- late extreme = the day's DZ/SZ")
    ap.add_argument("--no-overlap", action="store_true",
                    help="model a SINGLE-POSITION lane: a signal is skipped while a prior "
                         "trade is still open -- the lane-realistic number (overlapping "
                         "signals otherwise overstate one lane capture)")
    ap.add_argument("--base-risk-usd", type=float, default=12.0)
    ap.add_argument("--min-derive-trades", type=int, default=60)
    args = ap.parse_args()

    _ZONE_PREM_CAP[0] = args.zone_prem_cap
    _CONFIRM_QUALITY[0] = args.confirm_quality
    _CONFIRM_WICK_K[0] = args.confirm_wick_k
    _CONFIRM_VOL_K[0] = args.confirm_vol
    c = make_client()
    m5 = c.get_trendbars(args.symbol, "m5", args.count)
    m15 = c.get_trendbars(args.symbol, "m15", args.count)
    h1 = c.get_trendbars(args.symbol, "h1", max(200, args.count // 4))
    if len(m5) < MIN_M5 + 50:
        print(f"not enough M5 bars: {len(m5)}")
        return 2
    print(f"bars: M5={len(m5)} ({m5[0]['ts']} -> {m5[-1]['ts']})")
    if args.confirm_vol > 0:
        _stamp_volma(m5)
    atr = _atr_mean(m5)
    print(f"M5 ATR (mean TR, whole series) = {atr:.3f} pts")

    # -- phase 1: decisions ONCE per bar, H1 trend sign stamped at decision time
    bias_rows = _anchor_bias_fields(m5)
    # sdzone producer: rolling ATR14 + volume SMA20 (trailing-only) + engine
    _sd_engine = SDZoneEngine()
    _atr14 = [0.0] * len(m5)
    _volma = [0.0] * len(m5)
    if args.producer == "sdzone":
        trs = [0.0] * len(m5)
        for k in range(1, len(m5)):
            hi_k = float(m5[k].get("high", 0.0))
            lo_k = float(m5[k].get("low", 0.0))
            pc = float(m5[k - 1].get("close", 0.0))
            trs[k] = max(hi_k - lo_k, abs(hi_k - pc), abs(lo_k - pc))
            if k >= 14:
                _atr14[k] = sum(trs[k - 13:k + 1]) / 14.0
            vols = [float(m5[x].get("volume", 0.0) or 0.0) for x in range(max(0, k - 19), k + 1)]
            _volma[k] = sum(vols) / len(vols) if vols else 0.0
    dir_modes = [x.strip() for x in args.dir_modes.split(",") if x.strip()]
    needs_h1_sign = args.producer == "hunt" or any(
        m in ("nobuy-h1down", "nocounter") for m in dir_modes)
    start_i = MIN_M5 if args.producer == "hunt" else max(300, volume_profile.MIN_BARS)
    decisions: list[tuple[int, object, int]] = []
    for i in range(start_i, len(m5) - 2):
        prefix = m5[: i + 1]
        ts = str(m5[i].get("ts") or "")
        close_epoch = _epoch(ts) + 300
        if args.producer == "vp":
            session = str(market_lens.session_context(ts).get("value") or "unknown")
            try:
                d = volume_profile.decide_vp(args.symbol, prefix, args.spread_abs, session=session)
            except Exception:
                continue
            if d.action != "enter" or d.side is None or d.sl is None or d.tp is None:
                continue
            tsign = 0
            if needs_h1_sign:
                h1c = [b for b in h1 if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
                tsign = _h1_trend_sign(h1c)
            decisions.append((i, d, tsign))
            continue
        if args.producer == "sdzone":
            _sd_engine.update(i, m5, _atr14[i], _volma[i])
            sig = decide_sdzone(m5, i, _sd_engine, _atr14[i], rr=args.sdz_rr)
            if sig is None:
                continue
            if args.confirm_quality or args.confirm_vol > 0:
                # sdzone's OWN reversal-close bar is its confirm (the lane
                # enters at this bar's close) -- the rejection-quality gate
                # must therefore read THIS bar, not the zone-confirm walk.
                # The producer re-fires on later qualifying bars, so a
                # refusal here is exactly "wait for a real rejection candle".
                dbar = dict(m5[i])
                dbar["volma"] = _volma[i]
                if not _confirm_quality_ok(sig["side"], dbar,
                                           m5[i - 1] if i >= 1 else None):
                    continue
            if args.sdz_tp != "rr":
                # owner framework 2026-07-19: "ตั้ง Take Profit ที่ Zone ถัดไป" --
                # TP at the nearest OPPOSING zone edge (a buy exits where supply
                # begins). No zone beyond the entry -> keep the RR fallback;
                # zone-minrr1 additionally SKIPS signals whose zone target pays
                # <1R (the framework's own "poor RR to target = no trade").
                e_px, s_px = float(sig["entry"]), float(sig["sl"])
                z_risk = abs(e_px - s_px)
                if sig["side"] == "buy":
                    tgts = [z["bottom"] for z in _sd_engine.zones
                            if z["kind"] == "supply" and z["born"] < i and z["bottom"] > e_px]
                else:
                    tgts = [z["top"] for z in _sd_engine.zones
                            if z["kind"] == "demand" and z["born"] < i and z["top"] < e_px]
                if tgts:
                    tgt = min(tgts) if sig["side"] == "buy" else max(tgts)
                    if args.sdz_tp == "zone-minrr1" and z_risk > 0 and abs(tgt - e_px) < z_risk:
                        continue
                    sig["tp"] = tgt
            from types import SimpleNamespace
            d = SimpleNamespace(action="enter", side=sig["side"], entry=sig["entry"],
                                sl=sig["sl"], tp=sig["tp"], ts_close=ts,
                                setup="sdzone", features={})
            decisions.append((i, d, 0))
            continue
        if args.producer in ("daytrend", "dayreversal", "channelfade"):
            if args.producer == "daytrend":
                sig = decide_daytrend(prefix, bias_rows[i], atr,
                                      range_cap_atr=args.dt_range_cap_atr,
                                      last_entry_hour=args.dt_last_hour)
            elif args.producer == "dayreversal":
                sig = decide_dayreversal(prefix, bias_rows[i], atr,
                                         range_arm_atr=args.drev_arm_atr)
            else:
                sig = decide_channelfade(prefix, atr, tp_frac=args.chf_tp_frac)
            if sig is None:
                continue
            from types import SimpleNamespace
            d = SimpleNamespace(action="enter", side=sig["side"], entry=sig["entry"],
                                sl=sig["sl"], tp=sig["tp"], ts_close=ts,
                                setup=args.producer, features={})
            decisions.append((i, d, 0))
            continue
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
    print(f"decisions: {len(decisions)} enter candidates (producer={args.producer})")

    gate_modes = [g.strip() for g in args.gates.split(",") if g.strip()]
    if args.producer in ("vp", "daytrend", "dayreversal", "channelfade", "sdzone") and gate_modes != ["none"]:
        print(f"producer={args.producer}: forcing gates=none")
        gate_modes = ["none"]
    rungs = _parse_ladder_csv(args.ladder_csv)
    convex_combos = []
    for part in args.convex_combos.split(","):
        arm_s, gb_s, mh_s = part.strip().split(":")
        convex_combos.append((float(arm_s), float(gb_s), int(mh_s)))
    limit_variants = []
    for part in args.limit_variants.split(","):
        if not part.strip():
            continue
        dip_s, win_s = part.strip().split(":")
        limit_variants.append((float(dip_s), int(win_s)))
    zone_variants = []
    for part in args.zone_variants.split(","):
        if not part.strip():
            continue
        dip_s, win_s = part.strip().split(":")
        zone_variants.append((float(dip_s), int(win_s)))
    zone_m1_variants = []
    for part in args.zone_m1_variants.split(","):
        if not part.strip():
            continue
        dip_s, win_s = part.strip().split(":")
        zone_m1_variants.append((float(dip_s), int(win_s)))
    m1: list = []
    m1_epochs: list[float] = []
    if zone_m1_variants:
        m1 = c.get_trendbars(args.symbol, "m1", 50000)   # daemon caps at its max history
        if args.confirm_vol > 0:
            _stamp_volma(m1)
        m1_epochs = [_epoch(str(b.get("ts") or "")) for b in m1]
        print(f"M1 bars for zone-m1 confirm: {len(m1)} "
              f"({m1[0]['ts'] if m1 else '-'} -> {m1[-1]['ts'] if m1 else '-'})")

    split_bar = MIN_M5 + int((len(m5) - 2 - MIN_M5) * args.split)
    v_days = max(0.1, (_epoch(str(m5[-1]["ts"])) - _epoch(str(m5[split_bar]["ts"]))) / 86400.0)
    print(f"derive/validate split at bar {split_bar} (validate ~= {v_days:.1f} days)")

    exit_set = {x.strip() for x in args.exits.split(",") if x.strip()}
    exits: list[tuple[str, str, dict, int]] = []
    if "ladder" in exit_set:
        exits.append(("ladder(live)", "ladder", {"rungs": rungs}, args.max_hold))
    if "plain" in exit_set:
        exits.append((f"plain-tp h{args.max_hold}", "plain", {}, args.max_hold))
    if "plain_be" in exit_set:
        for arm in (float(x) for x in args.be_arms.split(",") if x.strip()):
            exits.append((f"plainBE arm{arm:g} h{args.max_hold}", "plain_be",
                          {"arm_r": arm, "be_floor_r": 0.05}, args.max_hold))
    if "bank" in exit_set:
        exits.append((f"bank {args.bank_r:g}R h{args.bank_hold}", "bank",
                      {"bank_r": args.bank_r}, args.bank_hold))
    if "convex" in exit_set:
        for arm, gb, mh in convex_combos:
            exits.append((f"convex a{arm:.1f} gb{gb:.1f} h{mh}", "convex",
                          {"arm_at": arm, "giveback_atr": gb}, mh))
    entries: list[tuple[str, str, float, int]] = [("market", "market", 0.0, 0)]
    for dip, win in limit_variants:
        entries.append((f"limit -{dip:.1f}R w{win}", "limit", dip, win))
    for dip, win in zone_variants:
        entries.append((f"zone -{dip:.1f}R w{win}", "zone", dip, win))
    for dip, win in zone_m1_variants:
        entries.append((f"zoneM1 -{dip:.1f}R w{win}", "zone_m1", dip, win))

    for mode in gate_modes:
        # "v18-align-chase" / "v18-align-all" (owner live lesson 2026-07-16:
        # during a 40-pt with-bias collapse the v18 gate blocked EVERY
        # continuation sell via chase_hard_block / min_leader_score): run the
        # BASE gate, then un-block a refused entry when its side is ALIGNED
        # with an established day-open bias — "chase" un-blocks only
        # chase-class reasons, "all" un-blocks any gate reason.
        base_mode, _, align_kind = mode.partition("-align-")
        accepted: list[dict] = []
        for i, d, tsign in decisions:
            gate = _apply_entry_gate(d, base_mode if base_mode != "none" else "none", str(d.ts_close or ""))
            allow = bool(gate.get("allow", True))
            if not allow and align_kind:
                bias = int(bias_rows[i].get("bias_d0", 0))
                aligned = (
                    bias != 0 and float(bias_rows[i].get("hrs_d0", 0.0)) >= 1.0
                    and ((str(d.side) == "buy") == (bias > 0))
                )
                reason = str(gate.get("reason") or "")
                if aligned and (align_kind == "all" or "chase" in reason):
                    allow = True
            if not allow:
                continue
            close_epoch = _epoch(str(m5[i].get("ts") or "")) + 300
            rec = {
                "i": i, "side": str(d.side), "entry": float(d.entry),
                "sl": float(d.sl), "tp": float(d.tp), "future": m5[i + 1:],
                "trend_sign": tsign, "close_epoch": close_epoch, **bias_rows[i],
            }
            if m1:
                rec["m1_future"] = m1[bisect_left(m1_epochs, close_epoch):]
            accepted.append(rec)
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
            derive_t = [t for t in derive_all if _dir_allows(dmode, t["side"], t)]
            validate_t = [t for t in validate_all if _dir_allows(dmode, t["side"], t)]
            print(f"\n-- dir={dmode}: derive {len(derive_t)}/{len(derive_all)}, "
                  f"validate {len(validate_t)}/{len(validate_all)} --")
            print(header)
            print("-" * len(header))
            for e_label, e_model, dip, win in entries:
                for x_label, x_kind, x_params, x_hold in exits:
                    d_sc = _score(derive_t, e_model, dip, win, x_kind, x_params, atr,
                                  x_hold, args.spread_abs, args.commission_r,
                                  no_overlap=args.no_overlap)
                    v_sc = _score(validate_t, e_model, dip, win, x_kind, x_params, atr,
                                  x_hold, args.spread_abs, args.commission_r,
                                  no_overlap=args.no_overlap)
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

"""Supply/Demand zone engine — single source of truth for BOTH the replay
harness and the live daytrend lane (owner order 2026-07-17 "โอกาสหายาก เปิดเลย").

Faithful port of the owner's Pine v6 Supply & Demand Zones indicator; see
SDZoneEngine's docstring for the anatomy (sweep + displacement + volume,
close-based break/flip). Evidence (no-overlap, market x plain RR2, VM
/tmp/entry_pos_sdzone_*.log): validate positive on ALL three windows
(PF 1.60/1.83/1.73), and x day-open bias: 6k +8.32/+5.86, 10k -4.60/+14.44
(PF 5.60), 14k +2.29/+15.22 (PF 3.43) — 4/6 cells positive at thin N
(~1 signal/day); owner explicitly chose live-canary over shadow, forward
numbers are the trial.

LIVE NOTE: the lane recomputes zones from its 340-bar prefix each M5 close
(stateless -> restart-proof, deterministic); zones older than ~28h are
therefore not visible live, a small conservative drift vs the replay's
longer windows (multi-day zones are rare and usually broken by then).
"""
from __future__ import annotations


class SDZoneEngine:
    """Faithful port of the owner's Pine v6 Supply & Demand Zones indicator
    (2026-07-17): zone = liquidity SWEEP past the last fractal pivot (4/4) +
    DISPLACEMENT back (body > 1.0xATR14 and body >= 55% of range, or an FVG
    gap > 0.4xATR) + VOLUME spike (> 1.2 x SMA20) — the sharper edges the
    channelfade autopsy called for, with the owner's morning volume-at-
    reversal question built in. Lifecycle: a zone dies on a CLOSE beyond it
    with an opposite strong move (wick survives — no-knife convention), and
    FLIPS role on a displacement+volume break (the live-verified 3986-93
    role reversal). Stateful single pass, closed bars only, no lookahead
    (pivots confirm pivot_r bars late, exactly like ta.pivotlow)."""

    def __init__(self, pivot_l=4, pivot_r=4, disp_atr=1.0, body_pct=55.0,
                 vol_mult=1.2, vol_len=20, spacing=5, max_zones=8,
                 confirm_bars=2):
        self.pl, self.pr = pivot_l, pivot_r
        self.disp_atr, self.body_pct = disp_atr, body_pct
        self.vol_mult, self.vol_len = vol_mult, vol_len
        self.spacing, self.max_zones = spacing, max_zones
        self.confirm_bars = confirm_bars
        self.zones: list[dict] = []          # {top,bottom,kind,born}
        self.last_ph = self.last_pl = None
        self.last_ph_i = self.last_pl_i = -10**9
        self.last_d_zone_i = self.last_s_zone_i = -10**9

    def _overlaps(self, top, bottom):
        h_new = top - bottom
        for z in self.zones:
            ov = min(top, z["top"]) - max(bottom, z["bottom"])
            if ov > 0 and h_new > 0 and ov > 0.5 * h_new:
                return True
        return False

    def update(self, i, bars, atr, vol_ma):
        """Advance to closed bar ``i``. ``atr``/``vol_ma`` are the rolling
        ATR14 / vol SMA20 values AT bar i (precomputed, trailing-only)."""
        j = i - self.pr
        if j >= self.pl:
            w = bars[j - self.pl: j + self.pr + 1]
            hj = float(bars[j].get("high", 0.0))
            lj = float(bars[j].get("low", 0.0))
            if hj == max(float(b.get("high", 0.0)) for b in w):
                self.last_ph, self.last_ph_i = hj, j
            if lj == min(float(b.get("low", 0.0)) for b in w):
                self.last_pl, self.last_pl_i = lj, j
        b = bars[i]
        o = float(b.get("open", 0.0))
        c = float(b.get("close", 0.0))
        hi = float(b.get("high", 0.0))
        lo = float(b.get("low", 0.0))
        vol = float(b.get("volume", 0.0) or 0.0)
        body = abs(c - o)
        rng = hi - lo
        body_ok = rng > 0 and (body / rng) * 100.0 >= self.body_pct
        bull_disp = c > o and body > atr * self.disp_atr and body_ok
        bear_disp = c < o and body > atr * self.disp_atr and body_ok
        bull_imb = (i >= 2 and lo > float(bars[i - 2].get("high", 0.0))
                    and (lo - float(bars[i - 2].get("high", 0.0))) > atr * 0.4)
        bear_imb = (i >= 2 and hi < float(bars[i - 2].get("low", 0.0))
                    and (float(bars[i - 2].get("low", 0.0)) - hi) > atr * 0.4)
        vol_ok = vol_ma <= 0 or vol > vol_ma * self.vol_mult
        bull_move = (bull_disp or bull_imb) and vol_ok
        bear_move = (bear_disp or bear_imb) and vol_ok

        kept = []
        for z in self.zones:
            if z["kind"] == "demand" and c < z["bottom"]:
                if bear_disp and vol_ok:
                    z["kind"] = "supply"
                    kept.append(z)                        # flip
                continue                                  # broken -> delete
            if z["kind"] == "supply" and c > z["top"]:
                if bull_disp and vol_ok:
                    z["kind"] = "demand"
                    kept.append(z)
                continue
            kept.append(z)
        self.zones = kept

        if (self.last_pl is not None and lo < self.last_pl
                and i > self.last_pl_i + self.pr + self.confirm_bars
                and bull_move and not self._overlaps(hi, lo)
                and i - self.last_d_zone_i >= self.spacing):
            self.zones.insert(0, {"top": hi, "bottom": lo, "kind": "demand", "born": i})
            self.last_d_zone_i = i
        if (self.last_ph is not None and hi > self.last_ph
                and i > self.last_ph_i + self.pr + self.confirm_bars
                and bear_move and not self._overlaps(hi, lo)
                and i - self.last_s_zone_i >= self.spacing):
            self.zones.insert(0, {"top": hi, "bottom": lo, "kind": "supply", "born": i})
            self.last_s_zone_i = i
        del self.zones[self.max_zones:]


def decide_sdzone(bars, i, engine, atr: float, buf_atr: float = 0.1,
                  rr: float = 2.0, max_risk_atr: float = 2.0) -> dict | None:
    """Zone-anchored entry on the CURRENT closed bar: price re-enters a live
    zone and CLOSES back outside it in the zone's direction (the same
    reversal-close confirm every producer here uses; live gets M1 refinement
    free via ENTRY_CONFIRM). SL beyond the zone + buffer; TP = rr x risk
    (pre-registered primary — zones give the entry anatomy, the target is a
    fixed-RR rule)."""
    b = bars[i]
    o = float(b.get("open", 0.0))
    c = float(b.get("close", 0.0))
    hi = float(b.get("high", 0.0))
    lo = float(b.get("low", 0.0))
    for z in engine.zones:
        if z["born"] >= i:
            continue                       # never trade the zone's own bar
        if z["kind"] == "demand" and lo <= z["top"] and c > z["top"] and c > o:
            sl = z["bottom"] - buf_atr * atr
            risk = c - sl
            if 0 < risk <= max_risk_atr * atr:
                return {"side": "buy", "entry": c, "sl": sl, "tp": c + rr * risk}
        if z["kind"] == "supply" and hi >= z["bottom"] and c < z["bottom"] and c < o:
            sl = z["top"] + buf_atr * atr
            risk = sl - c
            if 0 < risk <= max_risk_atr * atr:
                return {"side": "sell", "entry": c, "sl": sl, "tp": c - rr * risk}
    return None


def rolling_indicators(bars: list) -> tuple[list, list]:
    """Trailing-only ATR14 + volume SMA20 per index (the exact arrays the
    replay precomputes inline)."""
    n = len(bars)
    atr14 = [0.0] * n
    volma = [0.0] * n
    trs = [0.0] * n
    for k in range(1, n):
        hi_k = float(bars[k].get("high", 0.0))
        lo_k = float(bars[k].get("low", 0.0))
        pc = float(bars[k - 1].get("close", 0.0))
        trs[k] = max(hi_k - lo_k, abs(hi_k - pc), abs(lo_k - pc))
        if k >= 14:
            atr14[k] = sum(trs[k - 13:k + 1]) / 14.0
        vols = [float(bars[x].get("volume", 0.0) or 0.0) for x in range(max(0, k - 19), k + 1)]
        volma[k] = sum(vols) / len(vols) if vols else 0.0
    return atr14, volma


def zones_from_prefix(bars: list) -> tuple["SDZoneEngine", float]:
    """Run the engine over an entire (live) prefix and return it with the
    newest ATR14 — the stateless live entry point."""
    eng = SDZoneEngine()
    atr14, volma = rolling_indicators(bars)
    for k in range(15, len(bars)):
        eng.update(k, bars, atr14[k], volma[k])
    return eng, (atr14[-1] if atr14 else 0.0)

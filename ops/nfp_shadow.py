"""NFP forward shadow — journal-only, NO trades (Daily Lab N4 continuation).

WHY (owner order 2026-08-08 "เราไม่ได้ลืม N4"): the pre-registered NFP
magnitude test (docs/handoff/EVENT_MAGNITUDE_PREREG.md) CLOSED by its own
frozen gate (ratio 1.47x vs the declared 1.5x, separation 2.74 SE) and its
rule forbids re-tuning on the SAME events — but explicitly licenses a future
re-registration on NEW events. This script is the collector for those new
events: every first-Friday it records the coil, the 12-15Z expansion, and the
simulated bracket outcome to a JSONL journal. It never places an order.
After ~12 fresh events (~mid-2027) a NEW pre-registration judges them.

RUN: systemd timer `dexter3-nfp-shadow.timer` fires Fridays 15:20Z; the
script exits immediately unless today is the month's first Friday. One light
daemon call (30 H1 bars) — deliberately tiny after the 2026-08-08 fd
incident (heavy history pulls through the live daemon are banned).

Journal: /opt/dexter_pro/data/runtime/nfp_shadow.jsonl (append-only).
Fields mirror the frozen prereg's definitions EXACTLY (Wilder ATR14 on H1 at
11:00Z, coil = 11Z bar range/ATR, R_event = 12-15Z high-low/ATR, bracket =
prior-2-bar +/- spread stops, TTL 3 bars, TP 2x coil width, flat by 21Z,
spread 0.30, SL-first on both-touch) so the future judgment needs no
reconciliation layer.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SPREAD = 0.30
JOURNAL = Path(os.environ.get(
    "DEXTER3_NFP_SHADOW_JOURNAL",
    "/opt/dexter_pro/data/runtime/nfp_shadow.jsonl",
))


def is_first_friday(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def wilder_atr14(bars: list[dict]) -> float:
    if len(bars) < 16:
        return 0.0
    trs = []
    for k in range(1, len(bars)):
        hi, lo = float(bars[k]["high"]), float(bars[k]["low"])
        pc = float(bars[k - 1]["close"])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    return atr


def compute_row(bars: list[dict], day: str) -> dict | None:
    """Pure: the shadow row from today's H1 bars (prereg definitions)."""
    by_hour = {}
    for i, b in enumerate(bars):
        ts = str(b.get("ts") or "")
        if ts[:10] == day:
            by_hour[int(ts[11:13])] = i
    need = (11, 12, 13, 14)
    if any(h not in by_hour for h in need):
        return None
    i11 = by_hour[11]
    atr = wilder_atr14(bars[: i11 + 1])
    if atr <= 0:
        return None
    H = [float(b["high"]) for b in bars]
    L = [float(b["low"]) for b in bars]
    C = [float(b["close"]) for b in bars]
    win = [by_hour[12], by_hour[13], by_hour[14]]
    r_event = (max(H[i] for i in win) - min(L[i] for i in win)) / atr
    coil = (H[i11] - L[i11]) / atr

    i12 = by_hour[12]
    hi_t = max(H[i12 - 2], H[i12 - 1]) + SPREAD
    lo_t = min(L[i12 - 2], L[i12 - 1]) - SPREAD
    coil_w = hi_t - lo_t
    pnl, fill_side = None, None
    for b in range(i12, min(i12 + 3, len(bars))):
        up, dn = H[b] >= hi_t, L[b] <= lo_t
        if not (up or dn):
            continue
        side, fill = ("sell", lo_t) if (up and dn) else (("buy", hi_t) if up else ("sell", lo_t))
        fill_side = side
        sl = lo_t if side == "buy" else hi_t
        tp = fill + 2 * coil_w if side == "buy" else fill - 2 * coil_w
        for j in range(b, len(bars)):
            ts_j = str(bars[j].get("ts") or "")
            if ts_j[:10] != day or int(ts_j[11:13]) >= 21:
                break
            if side == "buy":
                if L[j] <= sl:
                    pnl = sl - fill - SPREAD; break
                if H[j] >= tp:
                    pnl = tp - fill - SPREAD; break
            else:
                if H[j] >= sl:
                    pnl = fill - sl - SPREAD; break
                if L[j] <= tp:
                    pnl = fill - tp - SPREAD; break
        if pnl is None:
            j = len(bars) - 1
            pnl = (C[j] - fill if side == "buy" else fill - C[j]) - SPREAD
        break
    return {
        "day": day, "atr11": round(atr, 3), "coil": round(coil, 3),
        "r_event": round(r_event, 3), "bracket_side": fill_side,
        "bracket_pnl": None if pnl is None else round(pnl, 2),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    today = datetime.now(timezone.utc).date()
    if not is_first_friday(today):
        return 0  # silent non-event exit — the timer fires every Friday
    from dexter3.openapi_client import Dexter3OpenApiClient

    bars = Dexter3OpenApiClient().get_trendbars("XAUUSD", "h1", 30)
    row = compute_row(bars, today.isoformat())
    if row is None:
        row = {"day": today.isoformat(), "error": "window_incomplete",
               "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(f"nfp_shadow recorded: {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

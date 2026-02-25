"""
scanners/xauusd.py - Professional XAUUSD (Gold) Scanner
Multi-timeframe analysis: D1 trend → H4 structure → H1/M15 entry
Incorporates SMC, session timing, key level detection
"""
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

from market.data_fetcher import xauusd_provider, session_manager
from analysis.technical import TechnicalAnalysis
from analysis.smc import SMCAnalyzer
from analysis.signals import SignalGenerator, TradeSignal
from config import config

logger = logging.getLogger(__name__)
ta = TechnicalAnalysis()
smc = SMCAnalyzer()
sig = SignalGenerator(min_confidence=config.MIN_SIGNAL_CONFIDENCE)


class XAUUSDScanner:
    """
    Professional XAUUSD scanner.
    Uses 3 timeframes:
      - D1 for overall trend bias
      - H4 for structure and key levels
      - H1 for precise entry timing
    Also tracks Asian range, London breakout, NY continuation.
    """

    def __init__(self):
        self.last_signal: Optional[TradeSignal] = None
        self.scan_count = 0
        self.signal_count = 0
        self._macro_ref_cache_ts: float = 0.0
        self._macro_ref_cache: dict = {}
        self._last_scan_diagnostics: dict = {}

    def get_last_scan_diagnostics(self) -> dict:
        return dict(self._last_scan_diagnostics or {})

    def _set_last_scan_diagnostics(self, **kwargs) -> None:
        try:
            self._last_scan_diagnostics = dict(kwargs or {})
        except Exception:
            self._last_scan_diagnostics = {}

    def get_asian_range(self) -> Optional[dict]:
        """Calculate the Asian session high/low range on H1."""
        df = xauusd_provider.fetch("1h", bars=24)
        if df is None or df.empty:
            return None

        now_utc = datetime.now(timezone.utc)
        try:
            # Asian session: 00:00-08:00 UTC
            asian_bars = df[df.index.hour.isin(range(0, 8))]
            if asian_bars.empty:
                return None
            return {
                "high": round(float(asian_bars["high"].max()), 2),
                "low": round(float(asian_bars["low"].min()), 2),
                "range_size": round(float(asian_bars["high"].max() - asian_bars["low"].min()), 2),
                "mid": round(float((asian_bars["high"].max() + asian_bars["low"].min()) / 2), 2),
            }
        except Exception as e:
            logger.error(f"Asian range error: {e}")
            return None

    def analyze_key_levels(self, current_price: float) -> dict:
        """Get key support/resistance levels for gold."""
        levels = {}

        # Psychological round numbers for gold (every $50, $100)
        mag = 50
        nearest_50_above = (int(current_price / mag) + 1) * mag
        nearest_50_below = int(current_price / mag) * mag

        levels["nearest_resistance"] = float(nearest_50_above)
        levels["nearest_support"] = float(nearest_50_below)
        levels["distance_to_res"] = round(nearest_50_above - current_price, 2)
        levels["distance_to_sup"] = round(current_price - nearest_50_below, 2)

        # Asian range
        asian = self.get_asian_range()
        if asian:
            levels["asian_high"] = asian["high"]
            levels["asian_low"] = asian["low"]
            levels["asian_range"] = asian["range_size"]
            levels["asian_mid"] = asian["mid"]

        return levels


    @staticmethod
    def _coerce_utc_index(df):
        if df is None or getattr(df, "empty", True):
            return df
        d = df.copy()
        try:
            idx = d.index
            if getattr(idx, "tz", None) is None:
                d.index = pd.to_datetime(idx, utc=True)
            else:
                d.index = idx.tz_convert(timezone.utc)
        except Exception:
            try:
                d.index = pd.to_datetime(d.index, utc=True)
            except Exception:
                return d
        return d.sort_index()

    @staticmethod
    def _slice_time_window(df, start_dt: datetime, end_dt: datetime):
        if df is None or getattr(df, "empty", True):
            return df
        return df[(df.index >= start_dt) & (df.index < end_dt)]

    @staticmethod
    def _hilo_summary(df) -> Optional[dict]:
        if df is None or getattr(df, "empty", True):
            return None
        try:
            return {"high": round(float(df["high"].max()), 2), "low": round(float(df["low"].min()), 2)}
        except Exception:
            return None

    @staticmethod
    def _session_vwap(df) -> Optional[float]:
        if df is None or getattr(df, "empty", True):
            return None
        try:
            vol = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0.0)
            if float(vol.sum()) <= 0:
                return None
            tp = (pd.to_numeric(df["high"], errors="coerce") + pd.to_numeric(df["low"], errors="coerce") + pd.to_numeric(df["close"], errors="coerce")) / 3.0
            return float((tp * vol).sum() / vol.sum())
        except Exception:
            return None

    def _news_freeze_context(self) -> dict:
        out = {
            "enabled": bool(getattr(config, "XAUUSD_NEWS_FREEZE_ENABLED", True)),
            "active": False,
            "nearest_min": -1,
            "window_min": int(getattr(config, "XAUUSD_NEWS_FREEZE_WINDOW_MIN", 20)),
            "events": [],
        }
        if not out["enabled"]:
            return out
        try:
            hits, nearest = self._nearby_usd_event_risk()
            out["nearest_min"] = int(nearest) if nearest is not None else -1
            out["events"] = [str(getattr(ev, "title", "")) for ev in hits[:3]]
            out["active"] = bool(hits) and nearest >= 0 and nearest <= out["window_min"]
        except Exception:
            pass
        return out

    def _macro_ref_series(self, ticker: str, period: str = "5d", interval: str = "5m") -> Optional[pd.Series]:
        try:
            cache_key = f"{ticker}|{period}|{interval}"
            now_ts = datetime.now(timezone.utc).timestamp()
            cached = self._macro_ref_cache.get(cache_key) if isinstance(self._macro_ref_cache, dict) else None
            if cached and (now_ts - float(cached.get("ts", 0))) < 60:
                return cached.get("series")
            raw = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False, timeout=10)
            if raw is None or raw.empty:
                return None
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw.columns = [str(c).strip().lower() for c in raw.columns]
            if "close" not in raw.columns:
                return None
            s = pd.to_numeric(raw["close"], errors="coerce").dropna()
            if s.empty:
                return None
            try:
                if getattr(s.index, "tz", None) is None:
                    s.index = pd.to_datetime(s.index, utc=True)
                else:
                    s.index = s.index.tz_convert(timezone.utc)
            except Exception:
                s.index = pd.to_datetime(s.index, utc=True)
            self._macro_ref_cache[cache_key] = {"ts": now_ts, "series": s}
            return s
        except Exception as e:
            logger.debug("[XAUUSD] macro ref fetch %s failed: %s", ticker, e)
            return None

    def _macro_shock_context(self, df_m5) -> dict:
        out = {
            "available": False,
            "dxy": {},
            "tnx": {},
            "xau": {},
            "adverse_for_long": False,
            "adverse_for_short": False,
            "summary": "unavailable",
        }
        try:
            dxy = self._macro_ref_series("DX-Y.NYB")
            tnx = self._macro_ref_series("^TNX")
            xau = None
            if df_m5 is not None and not getattr(df_m5, "empty", True):
                d = self._coerce_utc_index(df_m5)
                xau = pd.to_numeric(d.get("close"), errors="coerce").dropna() if "close" in d.columns else None

            def pct_ret(series, bars_back: int) -> Optional[float]:
                if series is None or len(series) <= bars_back:
                    return None
                prev = float(series.iloc[-(bars_back + 1)])
                last = float(series.iloc[-1])
                if prev == 0:
                    return None
                return (last / prev - 1.0) * 100.0

            dxy_15 = pct_ret(dxy, 3)
            dxy_60 = pct_ret(dxy, 12)
            tnx_15_bps = None
            tnx_60_bps = None
            if tnx is not None and len(tnx) > 12:
                tnx_15_bps = (float(tnx.iloc[-1]) - float(tnx.iloc[-4])) * 10.0
                tnx_60_bps = (float(tnx.iloc[-1]) - float(tnx.iloc[-13])) * 10.0
            xau_15 = pct_ret(xau, 3)
            xau_60 = pct_ret(xau, 12)
            out["dxy"] = {"ret_15m_pct": None if dxy_15 is None else round(dxy_15, 3), "ret_60m_pct": None if dxy_60 is None else round(dxy_60, 3)}
            out["tnx"] = {"chg_15m_bps": None if tnx_15_bps is None else round(tnx_15_bps, 2), "chg_60m_bps": None if tnx_60_bps is None else round(tnx_60_bps, 2)}
            out["xau"] = {"ret_15m_pct": None if xau_15 is None else round(xau_15, 3), "ret_60m_pct": None if xau_60 is None else round(xau_60, 3)}
            out["available"] = any(v is not None for v in (dxy_15, tnx_15_bps))
            dxy_thr = float(getattr(config, "XAUUSD_TRAP_DXY_SHOCK_PCT_15M", 0.18))
            tnx_thr = float(getattr(config, "XAUUSD_TRAP_TNX_SHOCK_BPS_15M", 2.0))
            dxy_up = dxy_15 is not None and dxy_15 >= dxy_thr
            dxy_down = dxy_15 is not None and dxy_15 <= -dxy_thr
            tnx_up = tnx_15_bps is not None and tnx_15_bps >= tnx_thr
            tnx_down = tnx_15_bps is not None and tnx_15_bps <= -tnx_thr
            out["adverse_for_long"] = bool(dxy_up or tnx_up)
            out["adverse_for_short"] = bool(dxy_down or tnx_down)
            if out["adverse_for_long"] and out["adverse_for_short"]:
                out["summary"] = "cross-asset shock mixed"
            elif out["adverse_for_long"]:
                out["summary"] = "DXY/TNX shock against XAU longs"
            elif out["adverse_for_short"]:
                out["summary"] = "DXY/TNX drop shock against XAU shorts"
            else:
                out["summary"] = "macro shock neutral"
        except Exception as e:
            logger.debug("[XAUUSD] macro shock context error: %s", e)
        return out

    def _volume_profile_proxy(self, df_h1, current_price: float) -> dict:
        out = {"hvn": [], "lvn": []}
        if df_h1 is None or getattr(df_h1, "empty", True):
            return out
        try:
            lookback = max(40, int(getattr(config, "XAUUSD_LIQUIDITY_VP_LOOKBACK_H1", 120)))
            bins_n = max(8, int(getattr(config, "XAUUSD_LIQUIDITY_VP_BINS", 24)))
            d = self._coerce_utc_index(df_h1).tail(lookback).copy()
            if d.empty:
                return out
            tp = (pd.to_numeric(d["high"], errors="coerce") + pd.to_numeric(d["low"], errors="coerce") + pd.to_numeric(d["close"], errors="coerce")) / 3.0
            vol = pd.to_numeric(d.get("volume"), errors="coerce").fillna(0.0)
            tp = tp.dropna()
            if tp.empty:
                return out
            pmin = float(tp.min()); pmax = float(tp.max())
            if pmax <= pmin:
                return out
            cats = pd.cut(tp, bins=bins_n, include_lowest=True, duplicates="drop")
            bucket_vol = vol.groupby(cats, observed=False).sum()
            rows = []
            for interval, vv in bucket_vol.items():
                if pd.isna(vv) or float(vv) <= 0:
                    continue
                mid = float((interval.left + interval.right) / 2.0)
                rows.append((mid, float(vv), abs(mid - float(current_price))))
            if not rows:
                return out
            rows.sort(key=lambda x: x[1], reverse=True)
            out["hvn"] = [round(r[0], 2) for r in rows[:2]]
            low_rows = sorted(rows, key=lambda x: (x[1], x[2]))
            out["lvn"] = [round(r[0], 2) for r in low_rows[:2]]
        except Exception as e:
            logger.debug("[XAUUSD] volume profile proxy error: %s", e)
        return out

    def _build_liquidity_map(self, current_price: float, df_h1, df_m5, h1_ta=None) -> dict:
        out = {
            "enabled": bool(getattr(config, "XAUUSD_LIQUIDITY_MAP_ENABLED", True)),
            "kill_zone": {"label": "off_kill_zone", "active": False},
            "levels": {},
            "sessions": {},
            "comex": {},
            "volume_profile": {},
            "imbalance": {},
            "sweep_probability": {"score": 0, "label": "low", "reasons": []},
        }
        if not out["enabled"]:
            return out
        try:
            now = datetime.now(timezone.utc)
            d_h1 = self._coerce_utc_index(df_h1) if df_h1 is not None else None
            d_m5 = self._coerce_utc_index(df_m5) if df_m5 is not None else None
            atr_h1 = None
            if h1_ta is not None and not getattr(h1_ta, "empty", True):
                atr_h1 = float(h1_ta.iloc[-1].get("atr_14", 0) or 0)
            elif d_h1 is not None and not d_h1.empty:
                try:
                    atr_h1 = float(ta.add_all(d_h1.copy()).iloc[-1].get("atr_14", 0) or 0)
                except Exception:
                    atr_h1 = None
            if not atr_h1 or atr_h1 <= 0:
                atr_h1 = max(5.0, abs(float(current_price)) * 0.002)

            if d_h1 is not None and not d_h1.empty:
                daily = d_h1.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
                today_floor = now.replace(hour=0, minute=0, second=0, microsecond=0)
                completed_days = daily[daily.index < today_floor]
                if not completed_days.empty:
                    pd_bar = completed_days.iloc[-1]
                    out["levels"].update({"pdh": round(float(pd_bar["high"]), 2), "pdl": round(float(pd_bar["low"]), 2)})
                    week_days = completed_days.tail(5)
                    if not week_days.empty:
                        out["levels"].update({"pwh": round(float(week_days["high"].max()), 2), "pwl": round(float(week_days["low"].min()), 2)})
                out["volume_profile"] = self._volume_profile_proxy(d_h1, current_price)
                try:
                    h1_enriched = h1_ta if h1_ta is not None else ta.add_all(d_h1.copy())
                    smc_ctx = smc.analyze(h1_enriched)
                    bull_fvgs = [f for f in (smc_ctx.fair_value_gaps or []) if getattr(f, "direction", "") == "bullish"]
                    bear_fvgs = [f for f in (smc_ctx.fair_value_gaps or []) if getattr(f, "direction", "") == "bearish"]
                    if bull_fvgs:
                        f = min(bull_fvgs, key=lambda x: min(abs(float(current_price) - float(x.lower)), abs(float(current_price) - float(x.upper))))
                        out["imbalance"]["nearest_bull_fvg"] = [round(float(f.lower), 2), round(float(f.upper), 2)]
                    if bear_fvgs:
                        f = min(bear_fvgs, key=lambda x: min(abs(float(current_price) - float(x.lower)), abs(float(current_price) - float(x.upper))))
                        out["imbalance"]["nearest_bear_fvg"] = [round(float(f.lower), 2), round(float(f.upper), 2)]
                except Exception as e:
                    logger.debug("[XAUUSD] imbalance map error: %s", e)

            if d_m5 is not None and not d_m5.empty:
                day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
                day1 = day0 + timedelta(days=1)
                day_m5 = self._slice_time_window(d_m5, day0, day1)
                windows = {"asia": (0, 0, 8, 0), "london": (7, 0, 13, 30), "new_york": (13, 30, 21, 0)}
                for name, (sh, smn, eh, em) in windows.items():
                    s = day0 + timedelta(hours=sh, minutes=smn)
                    e = day0 + timedelta(hours=eh, minutes=em)
                    out["sessions"][name] = self._hilo_summary(self._slice_time_window(day_m5, s, e)) or {}
                comex_start = day0 + timedelta(hours=13, minutes=30)
                comex_or_end = comex_start + timedelta(minutes=30)
                comex_end = day0 + timedelta(hours=20, minutes=0)
                comex_or = self._slice_time_window(day_m5, comex_start, comex_or_end)
                comex_run = self._slice_time_window(day_m5, comex_start, min(comex_end, now + timedelta(minutes=5)))
                out["comex"] = {"or_30m": self._hilo_summary(comex_or) or {}, "session_vwap": None}
                vwap = self._session_vwap(comex_run)
                if vwap is not None:
                    out["comex"]["session_vwap"] = round(float(vwap), 2)
                hhmm = now.strftime("%H:%M")
                if "13:30" <= hhmm <= "15:30":
                    out["kill_zone"] = {"label": "new_york_open_drive", "active": True}
                elif "12:00" <= hhmm <= "15:00":
                    out["kill_zone"] = {"label": "new_york_kill_zone", "active": True}
                elif "07:00" <= hhmm <= "10:00":
                    out["kill_zone"] = {"label": "london_kill_zone", "active": True}

            score = 0
            reasons = []
            rounds = self._nearest_round_levels(current_price)
            nearest_round_dist = min(abs(float(current_price) - float(rounds["nearest_50"])), abs(float(current_price) - float(rounds["nearest_100"])))
            if nearest_round_dist <= 0.30 * atr_h1:
                score += 22; reasons.append("near_round_number")
            for k in ("pdh", "pdl", "pwh", "pwl"):
                lv = out.get("levels", {}).get(k)
                if lv is not None and abs(float(current_price) - float(lv)) <= 0.40 * atr_h1:
                    score += 18; reasons.append(f"near_{k}")
            if out.get("kill_zone", {}).get("active"):
                score += 10; reasons.append(str(out["kill_zone"].get("label")))
            sweep = self._recent_liquidity_sweep(d_m5, current_price)
            if sweep.get("detected"):
                score += 28; reasons.append(str(sweep.get("reason") or "recent_sweep")); out["recent_m5_sweep"] = sweep
            if h1_ta is not None and not getattr(h1_ta, "empty", True):
                last = h1_ta.iloc[-1]
                ema21 = float(last.get("ema_21", current_price) or current_price)
                bb_pct = float(last.get("bb_pct", 0.5) or 0.5)
                ext_atr = abs(float(current_price) - ema21) / max(1e-9, atr_h1)
                out["extension"] = {"h1_ema21_atr": round(ext_atr, 2), "bb_pct": round(bb_pct, 3)}
                if ext_atr >= 1.0:
                    score += 12; reasons.append("h1_extension")
                if bb_pct >= 0.92 or bb_pct <= 0.08:
                    score += 8; reasons.append("bb_extreme")
            label = "low"
            if score >= 75:
                label = "extreme"
            elif score >= 55:
                label = "high"
            elif score >= 30:
                label = "medium"
            out["round_levels"] = {"r50": round(float(rounds["nearest_50"]), 2), "r100": round(float(rounds["nearest_100"]), 2)}
            out["atr_h1"] = round(float(atr_h1), 2)
            out["sweep_probability"] = {"score": int(score), "label": label, "reasons": reasons[:6]}
        except Exception as e:
            logger.debug("[XAUUSD] liquidity map build error: %s", e)
        return out


    @staticmethod
    def _nearest_round_levels(current_price: float) -> dict:
        p = float(current_price)
        return {
            "nearest_50": float(round(p / 50.0) * 50.0),
            "nearest_100": float(round(p / 100.0) * 100.0),
        }

    def _nearby_usd_event_risk(self) -> tuple[list, int]:
        try:
            from market.economic_calendar import economic_calendar
            now = datetime.now(timezone.utc)
            window_min = max(5, int(getattr(config, "XAUUSD_TRAP_EVENT_WINDOW_MIN", 30)))
            hits = []
            for ev in economic_calendar.fetch_events():
                if str(getattr(ev, "currency", "")).upper() != "USD":
                    continue
                if str(getattr(ev, "impact", "")).lower() != "high":
                    continue
                delta_min = int(abs((ev.time_utc - now).total_seconds()) // 60)
                if delta_min <= window_min:
                    hits.append((ev, delta_min))
            hits.sort(key=lambda x: x[1])
            return [e for e, _ in hits[:3]], (hits[0][1] if hits else -1)
        except Exception:
            return [], -1

    def _recent_liquidity_sweep(self, df_m5, current_price: float) -> dict:
        out = {
            "detected": False,
            "side": None,
            "bars_ago": None,
            "wick_ratio": None,
            "vol_ratio": None,
            "reason": "none",
        }
        if df_m5 is None or getattr(df_m5, "empty", True) or len(df_m5) < 40:
            return out
        try:
            d = ta.add_all(df_m5.copy())
            n = len(d)
            lookback = max(10, int(getattr(config, "XAUUSD_TRAP_REJECTION_M5_LOOKBACK", 36)))
            recent_bars = max(2, int(getattr(config, "XAUUSD_TRAP_REJECTION_RECENT_BARS", 4)))
            wick_min = float(getattr(config, "XAUUSD_TRAP_REJECTION_WICK_RATIO", 0.45))
            end_i = n - 1  # exclude latest bar (may still be forming)
            start_i = max(lookback + 2, end_i - recent_bars)
            for i in range(start_i, end_i):
                bar = d.iloc[i]
                prev = d.iloc[max(0, i - lookback):i]
                if len(prev) < 5:
                    continue
                hi = float(bar["high"]); lo = float(bar["low"]); op = float(bar["open"]); cl = float(bar["close"])
                rng = max(1e-9, hi - lo)
                upper_wick = hi - max(op, cl)
                lower_wick = min(op, cl) - lo
                prev_hi = float(prev["high"].max())
                prev_lo = float(prev["low"].min())
                vol_ratio = float(bar.get("vol_ratio", 1.0) or 1.0)

                if hi > prev_hi and cl < prev_hi and cl < op and (upper_wick / rng) >= wick_min:
                    out.update({
                        "detected": True,
                        "side": "bearish_rejection",
                        "bars_ago": int((n - 2) - i),
                        "wick_ratio": round(upper_wick / rng, 3),
                        "vol_ratio": round(vol_ratio, 2),
                        "reason": "m5_sweep_above_high_then_reject",
                    })
                    return out
                if lo < prev_lo and cl > prev_lo and cl > op and (lower_wick / rng) >= wick_min:
                    out.update({
                        "detected": True,
                        "side": "bullish_rejection",
                        "bars_ago": int((n - 2) - i),
                        "wick_ratio": round(lower_wick / rng, 3),
                        "vol_ratio": round(vol_ratio, 2),
                        "reason": "m5_sweep_below_low_then_reject",
                    })
                    return out
        except Exception as e:
            logger.debug("[XAUUSD] liquidity sweep analysis error: %s", e)
        return out

    def _apply_trade_location_guard(self, signal: TradeSignal, current_price: float, key_levels: dict, df_h1, df_m5) -> tuple[Optional[TradeSignal], dict]:
        guard = {
            "enabled": bool(getattr(config, "XAUUSD_SMART_TRAP_GUARD_ENABLED", True)),
            "penalty": 0.0,
            "blocked": False,
            "near_round": False,
            "no_chase": False,
            "event_risk": False,
            "news_freeze": {},
            "macro_shock": {},
            "liq_map": {},
            "sweep": {},
            "warnings": [],
        }
        if not guard["enabled"]:
            return signal, guard
        try:
            h1_ta = ta.add_all(df_h1.copy()) if df_h1 is not None and not df_h1.empty else None
            h1_last = h1_ta.iloc[-1] if h1_ta is not None and not h1_ta.empty else None
            atr_h1 = float(getattr(signal, "atr", 0) or 0)
            if (not atr_h1) and h1_last is not None:
                atr_h1 = float(h1_last.get("atr_14", 0) or 0)
            atr_h1 = atr_h1 if atr_h1 > 0 else max(5.0, abs(float(current_price)) * 0.002)

            near_round_atr = float(getattr(config, "XAUUSD_TRAP_NEAR_ROUND_ATR", 0.35))
            no_chase_ema21_atr = float(getattr(config, "XAUUSD_TRAP_NO_CHASE_EMA21_ATR", 1.0))
            no_chase_bb_pct = float(getattr(config, "XAUUSD_TRAP_NO_CHASE_BB_PCT", 0.92))

            rr = self._nearest_round_levels(current_price)
            nearest50 = float(rr["nearest_50"])
            nearest100 = float(rr["nearest_100"])
            dist_to_50_atr = abs(float(current_price) - nearest50) / max(1e-9, atr_h1)
            dist_to_100_atr = abs(float(current_price) - nearest100) / max(1e-9, atr_h1)
            guard["round_levels"] = {"50": nearest50, "100": nearest100}

            liq_map = self._build_liquidity_map(current_price, df_h1, df_m5, h1_ta=h1_ta)
            guard["liq_map"] = liq_map
            macro_ctx = self._macro_shock_context(df_m5)
            guard["macro_shock"] = macro_ctx
            news_freeze = self._news_freeze_context()
            guard["news_freeze"] = news_freeze

            if signal.direction == "long":
                dist_res = float(key_levels.get("distance_to_res", 9999) or 9999)
                dist_res_atr = dist_res / max(1e-9, atr_h1)
                if dist_res_atr <= near_round_atr or min(dist_to_50_atr, dist_to_100_atr) <= near_round_atr:
                    guard["near_round"] = True
                    guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_ROUND_RES", 8))
                    guard["warnings"].append(
                        f"⚠️ Round-number liquidity zone overhead: limited room to resistance (${key_levels.get('nearest_resistance', 0):.0f})"
                    )
            elif signal.direction == "short":
                dist_sup = float(key_levels.get("distance_to_sup", 9999) or 9999)
                dist_sup_atr = dist_sup / max(1e-9, atr_h1)
                if dist_sup_atr <= near_round_atr or min(dist_to_50_atr, dist_to_100_atr) <= near_round_atr:
                    guard["near_round"] = True
                    guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_ROUND_RES", 8))
                    guard["warnings"].append(
                        f"⚠️ Round-number liquidity zone below: limited room to support (${key_levels.get('nearest_support', 0):.0f})"
                    )

            if h1_last is not None:
                ema21 = float(h1_last.get("ema_21", current_price) or current_price)
                bb_pct = float(h1_last.get("bb_pct", 0.5) or 0.5)
                if signal.direction == "long":
                    ext_atr = (float(current_price) - ema21) / max(1e-9, atr_h1)
                    if ext_atr >= no_chase_ema21_atr and bb_pct >= no_chase_bb_pct:
                        guard["no_chase"] = True
                        guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_NO_CHASE", 10))
                        guard["warnings"].append(
                            f"⚠️ No-chase: price stretched {ext_atr:.2f} ATR above H1 EMA21 (BB% {bb_pct:.2f})"
                        )
                else:
                    ext_atr = (ema21 - float(current_price)) / max(1e-9, atr_h1)
                    if ext_atr >= no_chase_ema21_atr and bb_pct <= (1.0 - no_chase_bb_pct):
                        guard["no_chase"] = True
                        guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_NO_CHASE", 10))
                        guard["warnings"].append(
                            f"⚠️ No-chase: price stretched {ext_atr:.2f} ATR below H1 EMA21 (BB% {bb_pct:.2f})"
                        )
                guard["ext_atr"] = round(ext_atr, 3)
                guard["bb_pct"] = round(bb_pct, 3)

            sweep = self._recent_liquidity_sweep(df_m5, current_price)
            guard["sweep"] = sweep
            if sweep.get("detected"):
                bad_for_long = signal.direction == "long" and sweep.get("side") == "bearish_rejection"
                bad_for_short = signal.direction == "short" and sweep.get("side") == "bullish_rejection"
                if bad_for_long or bad_for_short:
                    guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_SWEEP", 18))
                    guard["warnings"].append(
                        f"⚠️ M5 liquidity sweep/rejection detected ({sweep.get('reason')}, {sweep.get('bars_ago')} bars ago, wick {sweep.get('wick_ratio')}, vol {sweep.get('vol_ratio')}x)"
                    )
                    if bool(getattr(config, "XAUUSD_TRAP_BLOCK_ON_SWEEP", True)) and (guard["near_round"] or guard["no_chase"]):
                        guard["blocked"] = True
                        guard["warnings"].append("⛔ Trap guard: sweep + round-level/chase confluence")

            evs, nearest_ev_min = self._nearby_usd_event_risk()
            if evs:
                guard["event_risk"] = True
                guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_EVENT", 12))
                guard["warnings"].append(f"⚠️ High-impact USD event proximity ({nearest_ev_min}m): stop-sweep risk elevated")

            sweep_prob = int((liq_map.get("sweep_probability") or {}).get("score", 0) or 0)
            if sweep_prob >= int(getattr(config, "XAUUSD_TRAP_SWEEP_PROB_BLOCK_SCORE", 78)) and (guard["near_round"] or guard["no_chase"]):
                guard["blocked"] = True
                guard["warnings"].append(f"⛔ Liquidity map trap block: sweep probability {sweep_prob} with chase/round confluence")
            elif sweep_prob >= 55:
                guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_SWEEP_PROB", 8))
                guard["warnings"].append(f"⚠️ Liquidity map sweep probability elevated ({sweep_prob}/100)")

            if macro_ctx.get("available"):
                macro_bad = bool(macro_ctx.get("adverse_for_long")) if signal.direction == "long" else bool(macro_ctx.get("adverse_for_short"))
                if macro_bad:
                    guard["penalty"] += float(getattr(config, "XAUUSD_TRAP_PENALTY_MACRO_SHOCK", 10))
                    guard["warnings"].append(f"⚠️ Macro shock filter: {macro_ctx.get('summary', 'adverse cross-asset move')}")

            if news_freeze.get("active"):
                nearest = int(news_freeze.get("nearest_min", -1))
                ev_name = str((news_freeze.get("events") or ["USD high-impact event"])[0])[:64]
                guard["warnings"].append(f"⚠️ News freeze window active ({nearest}m): {ev_name}")
                if bool(getattr(config, "XAUUSD_TRAP_BLOCK_ON_NEWS_FREEZE", True)):
                    guard["blocked"] = True
                    guard["warnings"].append("⛔ XAU news freeze: no fresh entries into high-impact USD window")

            signal.raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
            signal.raw_scores.update({
                "xau_guard_penalty": round(float(guard["penalty"]), 2),
                "xau_guard_blocked": bool(guard["blocked"]),
                "xau_guard_near_round": bool(guard["near_round"]),
                "xau_guard_no_chase": bool(guard["no_chase"]),
                "xau_guard_event_risk": bool(guard["event_risk"]),
                "xau_guard_sweep": bool((guard.get("sweep") or {}).get("detected")),
                "xau_guard_news_freeze": bool((guard.get("news_freeze") or {}).get("active")),
                "xau_guard_macro_shock": bool((guard.get("macro_shock") or {}).get("adverse_for_long") or (guard.get("macro_shock") or {}).get("adverse_for_short")),
                "xau_liq_sweep_prob": int(((guard.get("liq_map") or {}).get("sweep_probability") or {}).get("score", 0) or 0),
            })

            liq_prob = (guard.get("liq_map") or {}).get("sweep_probability") or {}
            if liq_prob:
                kz_label = str((guard.get("liq_map") or {}).get("kill_zone", {}).get("label", "off_kill_zone")).replace("_", " ")
                signal.reasons.append(f"🧭 Liquidity map: sweep risk {str(liq_prob.get('label','low')).upper()} ({liq_prob.get('score',0)}/100) in {kz_label}")
            if guard["near_round"]:
                signal.reasons.append(f"🧭 Trap map: round-number liquidity zone near {nearest50:.0f}/{nearest100:.0f}; prefer pullback/retest entry")
            for w in guard["warnings"]:
                if w not in signal.warnings:
                    signal.warnings.append(w)
            if guard["penalty"] > 0:
                before = float(signal.confidence)
                signal.confidence = max(0.0, before - float(guard["penalty"]))
                signal.warnings.append(f"⚠️ XAU trap-risk penalty applied: -{guard['penalty']:.1f} conf ({before:.1f}% → {signal.confidence:.1f}%)")

            if guard["blocked"]:
                logger.info(
                    "[XAUUSD] Trap guard blocked %s @ %.2f | penalty=%.1f near_round=%s no_chase=%s sweep=%s event=%s",
                    str(signal.direction).upper(), float(current_price), float(guard["penalty"]),
                    bool(guard["near_round"]), bool(guard["no_chase"]), bool((guard.get("sweep") or {}).get("detected")), bool(guard["event_risk"]),
                )
                return None, guard
            if guard["penalty"] > 0:
                logger.info(
                    "[XAUUSD] Trap guard penalty %s @ %.2f | penalty=%.1f conf=%.1f near_round=%s no_chase=%s sweep=%s event=%s",
                    str(signal.direction).upper(), float(current_price), float(guard["penalty"]), float(signal.confidence),
                    bool(guard["near_round"]), bool(guard["no_chase"]), bool((guard.get("sweep") or {}).get("detected")), bool(guard["event_risk"]),
                )
        except Exception as e:
            logger.warning("[XAUUSD] Trap guard error: %s", e)
        return signal, guard

    def scan(self) -> Optional[TradeSignal]:
        """
        Full XAUUSD scan. Returns a TradeSignal if opportunity found.
        """
        self.scan_count += 1
        session_info = session_manager.get_session_info()
        self._set_last_scan_diagnostics(
            status="scan_started",
            utc_time=str(session_info.get("utc_time", "-")),
            active_sessions=list(session_info.get("active_sessions", []) or []),
            unmet=[],
            notes=[],
        )
        logger.info(f"[XAUUSD] Scan #{self.scan_count} | {session_info['utc_time']} | "
                    f"Sessions: {session_info['active_sessions']}")

        # Fetch all timeframes
        df_d1 = xauusd_provider.fetch(config.XAUUSD_TREND_TF, bars=100)
        df_h4 = xauusd_provider.fetch(config.XAUUSD_STRUCTURE_TF, bars=150)
        df_h1 = xauusd_provider.fetch(config.XAUUSD_ENTRY_TF, bars=200)
        df_m5 = xauusd_provider.fetch("5m", bars=180)

        if df_h1 is None or df_h1.empty:
            self._set_last_scan_diagnostics(
                status="no_h1_data",
                utc_time=str(session_info.get("utc_time", "-")),
                active_sessions=list(session_info.get("active_sessions", []) or []),
                unmet=["h1_data"],
                notes=["failed_to_fetch_h1_data"],
            )
            logger.warning("[XAUUSD] Failed to fetch H1 data")
            return None

        h1_close = float(df_h1["close"].iloc[-1])
        live_price = xauusd_provider.get_current_price()
        current_price = float(live_price) if live_price is not None else h1_close
        logger.info(f"[XAUUSD] Current price: ${current_price:.2f}")
        self._set_last_scan_diagnostics(
            status="signal_eval",
            utc_time=str(session_info.get("utc_time", "-")),
            active_sessions=list(session_info.get("active_sessions", []) or []),
            current_price=round(float(current_price), 4),
            unmet=[],
            notes=[],
        )

        # Generate signal using entry TF (H1) and trend TF (D1)
        signal = sig.score_signal(
            df_entry=df_h1,
            df_trend=df_d1 if df_d1 is not None else df_h4,
            symbol="XAUUSD",
            timeframe=config.XAUUSD_ENTRY_TF,
            session_info=session_info,
        )

        if signal is None:
            self._set_last_scan_diagnostics(
                status="no_setup",
                utc_time=str(session_info.get("utc_time", "-")),
                active_sessions=list(session_info.get("active_sessions", []) or []),
                current_price=round(float(current_price), 4),
                unmet=["base_setup"],
                notes=["signal_generator_returned_none"],
            )
            return None

        # Enrich signal with XAUUSD-specific context
        if signal is not None:
            key_levels = self.analyze_key_levels(current_price)
            if live_price is not None:
                signal.reasons.append(f"💰 Live XAUUSD: ${current_price:.2f}")
            else:
                signal.warnings.append("⚠️ Live quote unavailable; using H1 close")
            signal.reasons.append(
                f"📊 Key levels — Support: ${key_levels['nearest_support']:.0f} | "
                f"Resistance: ${key_levels['nearest_resistance']:.0f}"
            )

            # Asian range context
            if "asian_high" in key_levels:
                ar_high = key_levels["asian_high"]
                ar_low = key_levels["asian_low"]
                if current_price > ar_high:
                    signal.reasons.append(f"✅ Price above Asian Range High (${ar_high:.2f}) - bullish breakout")
                elif current_price < ar_low:
                    signal.reasons.append(f"✅ Price below Asian Range Low (${ar_low:.2f}) - bearish breakdown")
                else:
                    signal.warnings.append(f"⚠️ Price inside Asian Range (${ar_low:.2f}-${ar_high:.2f})")

            # Session boost for XAUUSD
            if "london" in session_info["active_sessions"]:
                signal.reasons.append("🕐 London Session: High liquidity for Gold")
            if "new_york" in session_info["active_sessions"]:
                signal.reasons.append("🕐 New York Session: Gold most volatile")

            signal, _guard = self._apply_trade_location_guard(signal, current_price, key_levels, df_h1, df_m5)
            if signal is None:
                unmet = []
                if (_guard or {}).get("near_round"):
                    unmet.append("trap_near_round")
                if (_guard or {}).get("no_chase"):
                    unmet.append("trap_no_chase")
                if bool(((_guard or {}).get("sweep") or {}).get("detected")):
                    unmet.append("trap_sweep_rejection")
                if (_guard or {}).get("event_risk"):
                    unmet.append("event_risk")
                if bool(((_guard or {}).get("news_freeze") or {}).get("active")):
                    unmet.append("news_freeze")
                if bool(((_guard or {}).get("macro_shock") or {}).get("adverse_for_long")) or bool(((_guard or {}).get("macro_shock") or {}).get("adverse_for_short")):
                    unmet.append("macro_shock")
                self._set_last_scan_diagnostics(
                    status="trap_guard_blocked",
                    utc_time=str(session_info.get("utc_time", "-")),
                    active_sessions=list(session_info.get("active_sessions", []) or []),
                    current_price=round(float(current_price), 4),
                    unmet=unmet or ["trap_guard"],
                    notes=list(((_guard or {}).get("warnings") or [])[:3]),
                    guard_penalty=float((_guard or {}).get("penalty", 0.0) or 0.0),
                )
                return None

            self._set_last_scan_diagnostics(
                status="signal_generated",
                utc_time=str(session_info.get("utc_time", "-")),
                active_sessions=list(session_info.get("active_sessions", []) or []),
                current_price=round(float(current_price), 4),
                unmet=[],
                notes=[],
                confidence=round(float(getattr(signal, "confidence", 0.0) or 0.0), 1),
                direction=str(getattr(signal, "direction", "")),
            )

            self.last_signal = signal
            self.signal_count += 1
            logger.info(f"[XAUUSD] ✅ Signal generated: {signal.direction.upper()} "
                        f"@ ${signal.entry:.2f} | Confidence: {signal.confidence:.1f}%")

        return signal

    def get_market_overview(self) -> dict:
        """Return a full market overview for gold (no signal needed)."""
        overview = {
            "symbol": "XAUUSD",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "session": session_manager.get_session_info(),
        }

        df_d1 = xauusd_provider.fetch("1d", bars=50)
        df_h4 = xauusd_provider.fetch("4h", bars=100)
        df_h1 = xauusd_provider.fetch("1h", bars=140)
        df_m5 = xauusd_provider.fetch("5m", bars=320)

        h1_ta = None
        if df_h1 is not None and not df_h1.empty:
            h1_summary = ta.summary(df_h1)
            overview["h1_close"] = h1_summary.get("close")
            overview["h1"] = h1_summary
            try:
                h1_ta = ta.add_all(df_h1.copy())
            except Exception:
                h1_ta = None

        live_price = xauusd_provider.get_current_price()
        if live_price is not None:
            overview["price"] = round(float(live_price), 4)
            overview["price_source"] = "live_quote"
        elif "h1_close" in overview:
            overview["price"] = overview["h1_close"]
            overview["price_source"] = "h1_close"

        if df_h4 is not None and not df_h4.empty:
            overview["h4"] = ta.summary(df_h4)
            overview["h4_smc"] = self._smc_summary(df_h4)

        if df_d1 is not None and not df_d1.empty:
            overview["d1"] = ta.summary(df_d1)

        if overview.get("price"):
            overview["key_levels"] = self.analyze_key_levels(overview["price"])
            try:
                overview["liquidity_map"] = self._build_liquidity_map(float(overview["price"]), df_h1, df_m5, h1_ta=h1_ta)
            except Exception as e:
                logger.debug("[XAUUSD] overview liquidity map error: %s", e)
            try:
                overview["macro_shock"] = self._macro_shock_context(df_m5)
            except Exception as e:
                logger.debug("[XAUUSD] overview macro context error: %s", e)
            try:
                overview["news_freeze"] = self._news_freeze_context()
            except Exception as e:
                logger.debug("[XAUUSD] overview news freeze error: %s", e)

        return overview

    def _smc_summary(self, df) -> dict:
        ctx = smc.analyze(df)
        return {
            "bias": ctx.bias,
            "confidence": ctx.confidence,
            "order_blocks": len(ctx.order_blocks),
            "fvgs": len(ctx.fair_value_gaps),
            "trend": ctx.current_trend,
        }

    def get_stats(self) -> dict:
        return {
            "total_scans": self.scan_count,
            "signals_generated": self.signal_count,
            "hit_rate": f"{self.signal_count/max(self.scan_count,1)*100:.1f}%",
        }


xauusd_scanner = XAUUSDScanner()

"""
notifier/telegram_bot.py - Professional Telegram Signal Delivery
Sends beautifully formatted trade signals, market updates, scan summaries
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import math

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import config
from notifier.access_control import access_manager

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code from sync context safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class TelegramNotifier:
    """
    Sends professional trade alerts and market updates via Telegram.
    All signal formatting uses Markdown for rich display.
    """

    def __init__(self):
        self.enabled = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
        if not self.enabled:
            logger.warning("Telegram not configured. Alerts will be printed to console only.")
        self._bot: Optional[Bot] = None
        self.broadcast_enabled = bool(getattr(config, "TELEGRAM_BROADCAST_SIGNALS", False))
        self.auto_block_unreachable = bool(getattr(config, "TELEGRAM_AUTO_BLOCK_UNREACHABLE", True))
        self._auto_blocked_chat_ids: set[int] = set()

    @property
    def bot(self) -> Optional[Bot]:
        if self._bot is None and self.enabled:
            self._bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        return self._bot

    def _resolve_target_chat_ids(
        self,
        chat_id: Optional[int],
        feature: Optional[str],
        signal_symbol: str = "",
        signal_symbols: Optional[list[str]] = None,
    ) -> list[int]:
        targets: set[int] = set()
        if chat_id is not None:
            try:
                targets.add(int(chat_id))
            except Exception:
                pass
            return sorted(targets)

        raw_owner = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
        if raw_owner and raw_owner.lstrip("-").isdigit():
            targets.add(int(raw_owner))

        if self.broadcast_enabled and feature:
            try:
                for uid in access_manager.list_entitled_user_ids(
                    feature,
                    signal_symbol=signal_symbol,
                    signal_symbols=signal_symbols,
                ):
                    targets.add(int(uid))
            except Exception as e:
                logger.warning("[Telegram] subscriber lookup failed for feature=%s: %s", feature, e)
        return sorted(targets)

    @staticmethod
    def _owner_chat_id_int() -> int:
        raw_owner = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
        if raw_owner and raw_owner.lstrip("-").isdigit():
            return int(raw_owner)
        return 0

    @staticmethod
    def _parse_utc_offset(offset_text: Optional[str]):
        raw = str(offset_text or "").strip().upper()
        if not raw:
            return timezone.utc, "UTC"
        if raw in {"BANGKOK", "ASIA/BANGKOK", "TH", "THA", "THAILAND", "BKK"}:
            raw = "+07:00"
        if raw.startswith("UTC") or raw.startswith("GMT"):
            raw = raw[3:].strip()
        if raw and raw[0] not in {"+", "-"} and raw.isdigit():
            raw = f"+{raw}"
        if raw and ":" not in raw:
            # +7 -> +07:00
            sign = raw[0] if raw[0] in {"+", "-"} else "+"
            num = raw[1:] if raw[0] in {"+", "-"} else raw
            if num.isdigit():
                raw = f"{sign}{int(num):02d}:00"
        try:
            if len(raw) == 6 and raw[0] in {"+", "-"} and raw[3] == ":":
                sign = 1 if raw[0] == "+" else -1
                hh = int(raw[1:3])
                mm = int(raw[4:6])
                if 0 <= hh <= 14 and 0 <= mm < 60:
                    tz = timezone(sign * timedelta(hours=hh, minutes=mm))
                    return tz, f"UTC{raw}"
        except Exception:
            pass
        return timezone.utc, "UTC"

    def _tz_for_chat(self, chat_id: Optional[int]):
        try:
            cid = int(chat_id) if chat_id is not None else None
        except Exception:
            cid = None
        if cid is None:
            return timezone.utc, "UTC"
        try:
            saved = access_manager.get_user_news_utc_offset(cid)
        except Exception:
            saved = None
        return self._parse_utc_offset(saved)

    def _fmt_dt_for_chat(self, dt: datetime, chat_id: Optional[int], with_date: bool = True) -> str:
        tz_obj, tz_label = self._tz_for_chat(chat_id)
        src = dt if isinstance(dt, datetime) else datetime.now(timezone.utc)
        if src.tzinfo is None:
            src = src.replace(tzinfo=timezone.utc)
        local_dt = src.astimezone(tz_obj)
        if with_date:
            return f"{local_dt.strftime('%Y-%m-%d %H:%M')} {tz_label}"
        return f"{local_dt.strftime('%H:%M')} {tz_label}"

    @staticmethod
    def _fmt_duration_hms(total_seconds: int) -> str:
        sec = max(0, int(total_seconds))
        hours, rem = divmod(sec, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _macro_impact_phase(total_seconds: int) -> str:
        sec = max(0, int(total_seconds))
        if sec <= 15 * 60:
            return "IMMEDIATE"
        if sec <= 2 * 3600:
            return "DEVELOPING"
        if sec <= 8 * 3600:
            return "ACTIVE"
        return "FADING"

    @staticmethod
    def _is_unreachable_error(err_text: str) -> bool:
        x = str(err_text or "").lower()
        return (
            ("chat not found" in x)
            or ("bot was blocked by the user" in x)
            or ("user is deactivated" in x)
            or ("forbidden" in x and "bot" in x)
        )

    def _maybe_auto_block_chat(self, chat_id: int, err_text: str) -> None:
        if not self.auto_block_unreachable:
            return
        if chat_id in self._auto_blocked_chat_ids:
            return
        if not self._is_unreachable_error(err_text):
            return
        owner_id = self._owner_chat_id_int()
        if chat_id == owner_id or chat_id in config.get_admin_ids():
            return
        try:
            access_manager.set_status(chat_id, status="blocked", note="auto_blocked_unreachable_chat")
            self._auto_blocked_chat_ids.add(chat_id)
            logger.info("[Telegram] auto-blocked unreachable subscriber chat=%s", chat_id)
        except Exception as e:
            logger.warning("[Telegram] auto-block unreachable chat=%s failed: %s", chat_id, e)

    @staticmethod
    def _feature_from_signal(signal) -> str:
        symbol = str(getattr(signal, "symbol", "") or "").upper()
        if symbol == "XAUUSD":
            return "scan_gold"
        if "/USDT" in symbol:
            return "scan_crypto"
        try:
            if symbol in {s.upper() for s in config.get_fx_major_symbols()}:
                return "scan_fx"
        except Exception:
            pass
        return "scan_stocks"

    @staticmethod
    def _feature_from_stock_label(market_label: str) -> str:
        label = str(market_label or "").lower()
        if "value" in label or "vi" in label:
            return "scan_vi"
        if "thailand" in label or "set50" in label:
            return "scan_thai"
        if "us open" in label or "us market" in label or "us watch" in label:
            return "scan_us_open"
        return "scan_stocks"

    def _send(
        self,
        text: str,
        parse_mode: str = ParseMode.MARKDOWN_V2,
        disable_preview: bool = True,
        chat_id: Optional[int] = None,
        feature: Optional[str] = None,
        signal_symbol: str = "",
        signal_symbols: Optional[list[str]] = None,
    ) -> bool:
        """Send a message to one chat or all entitled subscribers."""
        if not self.enabled:
            print("\n" + "═" * 60)
            print(text)
            print("═" * 60 + "\n")
            return True
        target_ids = self._resolve_target_chat_ids(
            chat_id=chat_id,
            feature=feature,
            signal_symbol=signal_symbol,
            signal_symbols=signal_symbols,
        )
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=%s)", feature or "-")
            return False
        try:
            sent_count = {"ok": 0, "fail": 0}

            async def _send_async():
                # Use a fresh bot instance per send to avoid cross-event-loop reuse.
                async with Bot(token=config.TELEGRAM_BOT_TOKEN) as bot:
                    for target in target_ids:
                        try:
                            await bot.send_message(
                                chat_id=target,
                                text=text,
                                parse_mode=parse_mode,
                                disable_web_page_preview=disable_preview,
                            )
                            sent_count["ok"] += 1
                        except TelegramError as e:
                            err_txt = str(e)
                            if parse_mode == ParseMode.MARKDOWN_V2 and "can't parse entities" in err_txt.lower():
                                try:
                                    await bot.send_message(
                                        chat_id=target,
                                        text=text,
                                        parse_mode=None,
                                        disable_web_page_preview=disable_preview,
                                    )
                                    sent_count["ok"] += 1
                                    logger.warning(
                                        "[Telegram] markdown parse fallback sent chat=%s feature=%s err=%s",
                                        target,
                                        feature or "-",
                                        e,
                                    )
                                    continue
                                except TelegramError as e2:
                                    err_txt = str(e2)
                                    e = e2
                            sent_count["fail"] += 1
                            logger.warning(
                                "[Telegram] send failed chat=%s feature=%s err=%s",
                                target,
                                feature or "-",
                                e,
                            )
                            self._maybe_auto_block_chat(int(target), err_txt)
            _run_async(_send_async())
            return sent_count["ok"] > 0
        except TelegramError as e:
            logger.error(f"Telegram send error: {e}")
            return False

    @staticmethod
    def _escape(text: str) -> str:
        """Escape special characters for Telegram MarkdownV2."""
        special = r"_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{c}" if c in special else c for c in str(text))

    @staticmethod
    def _price_decimals(value: float) -> int:
        v = abs(float(value))
        if v >= 1000:
            return 2
        if v >= 100:
            return 3
        if v >= 1:
            return 4
        if v >= 0.1:
            return 5
        if v >= 0.01:
            return 6
        if v >= 0.001:
            return 7
        if v >= 0.0001:
            return 8
        if v >= 0.00001:
            return 9
        return 10

    def _fmt_price(self, value) -> str:
        try:
            v = float(value)
        except Exception:
            return str(value)
        if not math.isfinite(v):
            return str(value)
        return f"{v:.{self._price_decimals(v)}f}"

    @staticmethod
    def _fmt_compact(value: float) -> str:
        try:
            v = float(value)
        except Exception:
            return str(value)
        av = abs(v)
        if av >= 1_000_000_000:
            return f"{v / 1_000_000_000:.2f}B"
        if av >= 1_000_000:
            return f"{v / 1_000_000:.2f}M"
        if av >= 1_000:
            return f"{v / 1_000:.2f}K"
        return f"{v:.2f}"

    # ─── Trade Signal Formatter (Tiger Hunter) ──────────────────────────────
    def send_signal(self, signal, chat_id: Optional[int] = None) -> bool:
        """Send a beautifully formatted Tiger Hunter trade signal."""
        e = self._escape
        direction_emoji = "🟢 LONG" if signal.direction == "long" else "🔴 SHORT"
        conf_emoji = signal.confidence_emoji()
        bars = int(signal.confidence / 10)
        conf_bar = "█" * bars + "░" * (10 - bars)

        # Tiger Hunter metadata (backward-compatible)
        sl_type = str(getattr(signal, "sl_type", "") or "")
        tp_type = str(getattr(signal, "tp_type", "") or "")
        entry_type = str(getattr(signal, "entry_type", "") or "")
        sl_mapped = bool(getattr(signal, "sl_liquidity_mapped", False))
        lp_count = int(getattr(signal, "liquidity_pools_count", 0) or 0)

        sep1 = "═" * 35
        sep2 = "─" * 30
        lines = [
            sep1,
            f"🐯 *TIGER HUNTER SIGNAL* {conf_emoji}",
            sep1,
            "",
            f"*Symbol:* `{e(signal.symbol)}`",
            f"*Direction:* {direction_emoji}",
            f"*Pattern:* `{e(signal.pattern)}`",
            f"*Timeframe:* `{e(signal.timeframe)}`",
            f"*Session:* `{e(signal.session)}`",
            "",
            sep2,
            "📊 *TRADE LEVELS*",
            sep2,
            f"🎯 *Entry:*   `{e(self._fmt_price(signal.entry))}`",
        ]

        if entry_type == "limit":
            lines.append("   _🎯 Limit order \\(patience entry\\)_")

        sl_badge = " 🛡️ _Anti\\-Sweep_" if (sl_mapped or sl_type == "anti_sweep") else ""
        lines.append(f"🛑 *Stop:*    `{e(self._fmt_price(signal.stop_loss))}`{sl_badge}")

        tp_badge = " ⚡ _Liq Target_" if tp_type == "liquidity" else ""
        lines += [
            f"✅ *TP1 \\(1R\\):* `{e(self._fmt_price(signal.take_profit_1))}`",
            f"✅ *TP2 \\(2R\\):* `{e(self._fmt_price(signal.take_profit_2))}`{tp_badge}",
            f"🚀 *TP3 \\(3R\\):* `{e(self._fmt_price(signal.take_profit_3))}`",
            "",
            f"⚖️ *R:R Ratio:* `1:{e(signal.risk_reward)}`",
            f"📏 *ATR:* `{e(self._fmt_price(signal.atr))}`",
        ]
        if lp_count > 0:
            lines.append(f"🌊 *Liquidity Pools:* `{e(lp_count)}` mapped")

        lines += [
            "",
            sep2,
            "🧠 *ANALYSIS*",
            sep2,
            f"📈 *Trend:* `{e(signal.trend)}`",
            f"📊 *RSI:* `{e(signal.rsi)}`",
            "",
            sep2,
            "✅ *REASONS*",
        ]
        for reason in signal.reasons[:6]:
            lines.append(f"• {e(reason)}")
        if signal.warnings:
            lines.append("")
            lines.append("⚠️ *WARNINGS*")
            for warn in signal.warnings[:3]:
                lines.append(f"• {e(warn)}")

        # Tiger quality badges
        tiger_badges = []
        if sl_mapped:
            tiger_badges.append("🛡️ Anti\\-Sweep SL")
        if tp_type == "liquidity":
            tiger_badges.append("⚡ Liq TP")
        if entry_type == "limit":
            tiger_badges.append("🎯 Limit Entry")
        if tiger_badges:
            lines += [
                "",
                sep2,
                "🐯 *TIGER QUALITY*",
                sep2,
                "  ".join(tiger_badges),
            ]

        lines += [
            "",
            sep2,
            "🎯 *CONFIDENCE*",
            f"`{conf_bar}` `{e(signal.confidence)}%`",
            "",
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            sep1,
            "_🐯 Tiger Hunter AI \\| Dexter Pro V3_",
            "_⚠️ Not financial advice\\._",
        ]

        return self._send(
            "\n".join(lines),
            chat_id=chat_id,
            feature=self._feature_from_signal(signal),
            signal_symbol=str(getattr(signal, "symbol", "") or ""),
        )

    # ─── Market Summary ────────────────────────────────────────────────────────
    def send_xauusd_scan_status(self, status: dict, chat_id: Optional[int] = None) -> bool:
        """Compact XAU scan status (scheduled no-signal / filtered reasons)."""
        s = dict(status or {})
        diag = dict(s.get("diagnostics") or {})
        sess = dict(s.get("session_info") or {})
        active_sessions = ", ".join(list(sess.get("active_sessions", []) or [])) or "unknown"
        x_status = str(s.get("status") or diag.get("status") or "unknown")
        price = diag.get("current_price")
        unmet = [str(x) for x in list(diag.get("unmet") or []) if str(x).strip()]
        notes = [str(x) for x in list(diag.get("notes") or []) if str(x).strip()]

        labels = {
            "no_signal": "No new signal this round",
            "no_setup": "No setup passed base engine",
            "no_h1_data": "Missing H1 data",
            "trap_guard_blocked": "Blocked by XAU trap guard",
            "below_confidence": "Signal below confidence threshold",
            "cooldown_suppressed": "Signal suppressed by cooldown",
        }

        lines = ["🟡 XAUUSD MONITOR", f"Session: {active_sessions}"]
        if price is not None:
            try:
                lines.append(f"Price: ${float(price):.2f}")
            except Exception:
                lines.append(f"Price: {price}")
        lines.append(f"Status: {labels.get(x_status, x_status)}")

        if x_status == "below_confidence":
            sig = dict(s.get("signal") or {})
            conf = sig.get("confidence_adjusted", sig.get("confidence"))
            raw = sig.get("confidence_raw")
            th = s.get("confidence_threshold")
            try:
                if conf is not None and th is not None:
                    if raw is not None:
                        lines.append(f"Conf: adj {float(conf):.1f}% (raw {float(raw):.1f}%) < min {float(th):.1f}%")
                    else:
                        lines.append(f"Conf: {float(conf):.1f}% < min {float(th):.1f}%")
            except Exception:
                pass

        if x_status == "cooldown_suppressed":
            cd = dict(s.get("cooldown") or {})
            if cd:
                lines.append(f"Cooldown: {cd.get('reason','active')} ({cd.get('elapsed_min','-')}m/{cd.get('cooldown_min','-')}m)")

        if unmet:
            lines.append("Unmet: " + ", ".join(unmet[:5]))
        elif x_status == "no_signal":
            lines.append("Unmet: base_setup")

        prev = dict(s.get("previous_signal") or {})
        if prev:
            try:
                age_min = max(0.0, float(prev.get('age_sec', 0.0) or 0.0) / 60.0)
                pdir = str(prev.get('direction', '') or '').upper()
                pentry = float(prev.get('entry', 0.0) or 0.0)
                pconf = float(prev.get('confidence', 0.0) or 0.0)
                lines.append(f"Prev signal: {pdir} @ {pentry:.2f} ({age_min:.1f}m ago, conf {pconf:.1f}%)")
            except Exception:
                pass

        if notes:
            cleaned = []
            for n in notes[:2]:
                t = str(n).replace("⚠️ ", "").replace("⛔ ", "")
                cleaned.append(t)
            lines.append("Notes: " + " | ".join(cleaned))

        lines.append(f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        return self._send("\n".join(lines), chat_id=chat_id, feature="scan_gold", parse_mode=None)

    def send_xauusd_overview(self, overview: dict, chat_id: Optional[int] = None) -> bool:
        """Send a XAUUSD market overview summary."""
        e = self._escape
        price = overview.get("price", "N/A")
        h1 = overview.get("h1", {})
        h4 = overview.get("h4", {})
        d1 = overview.get("d1", {})
        session = overview.get("session", {})
        levels = overview.get("key_levels", {})
        price_source = overview.get("price_source", "unknown")
        liq = overview.get("liquidity_map", {}) or {}
        macro_ctx = overview.get("macro_shock", {}) or {}
        news_freeze = overview.get("news_freeze", {}) or {}

        trend_emoji = {
            "bullish": "🟢",
            "bearish": "🔴",
            "ranging": "🟡",
        }

        h1_trend = h1.get("trend", "unknown")
        h4_trend = h4.get("trend", "unknown")
        d1_trend = d1.get("trend", "unknown")

        lines = [
            f"{'═' * 35}",
            f"🏆 *XAUUSD \\(GOLD\\) OVERVIEW*",
            f"{'═' * 35}",
            f"",
            f"💰 *Price:* `${e(price)}`",
            f"🔎 *Source:* `{e(price_source)}`",
            f"🕐 *Session:* `{e(', '.join(session.get('active_sessions', ['unknown'])))}`",
            f"",
            f"{'─' * 30}",
            f"📊 *MULTI\\-TF TREND*",
            f"{'─' * 30}",
            f"{trend_emoji.get(d1_trend,'⚪')} *D1:*  `{e(d1_trend)}`  \\| RSI: `{e(d1.get('rsi','?'))}`",
            f"{trend_emoji.get(h4_trend,'⚪')} *H4:*  `{e(h4_trend)}`  \\| RSI: `{e(h4.get('rsi','?'))}`",
            f"{trend_emoji.get(h1_trend,'⚪')} *H1:*  `{e(h1_trend)}`  \\| RSI: `{e(h1.get('rsi','?'))}`",
            f"",
        ]

        if levels:
            lines += [
                f"{'─' * 30}",
                f"🗝️ *KEY LEVELS*",
                f"{'─' * 30}",
                f"🔺 *Resistance:* `${e(levels.get('nearest_resistance', 'N/A'))}`",
                f"🔻 *Support:*    `${e(levels.get('nearest_support', 'N/A'))}`",
            ]
            if "asian_high" in levels:
                lines += [
                    f"🌏 *Asian High:* `${e(levels.get('asian_high', 'N/A'))}`",
                    f"🌏 *Asian Low:*  `${e(levels.get('asian_low', 'N/A'))}`",
                ]

        if liq:
            lv = liq.get("levels", {}) or {}
            kz = liq.get("kill_zone", {}) or {}
            sp = liq.get("sweep_probability", {}) or {}
            comex = liq.get("comex", {}) or {}
            sessions_map = liq.get("sessions", {}) or {}
            vp = liq.get("volume_profile", {}) or {}
            imb = liq.get("imbalance", {}) or {}
            lines += [
                f"",
                f"{'─' * 30}",
                f"🧭 *LIQUIDITY MAP*",
                f"{'─' * 30}",
                f"🎯 *Kill Zone:* `{e(str(kz.get('label','off_kill_zone')).replace('_',' '))}`",
                f"🧪 *Sweep Risk:* `{e(str(sp.get('label','low')).upper())}` `({e(sp.get('score',0))}/100)`",
            ]
            if lv:
                lines += [
                    f"PDH/PDL: `${e(lv.get('pdh','N/A'))}` / `${e(lv.get('pdl','N/A'))}`",
                    f"PWH/PWL: `${e(lv.get('pwh','N/A'))}` / `${e(lv.get('pwl','N/A'))}`",
                ]
            rd = liq.get("round_levels", {}) or {}
            if rd:
                lines.append(f"Round 50/100: `${e(rd.get('r50','N/A'))}` / `${e(rd.get('r100','N/A'))}`")
            asia = sessions_map.get("asia", {}) or {}
            lon = sessions_map.get("london", {}) or {}
            ny = sessions_map.get("new_york", {}) or {}
            if asia or lon or ny:
                if asia:
                    lines.append(f"Asia H/L: `${e(asia.get('high','N/A'))}` / `${e(asia.get('low','N/A'))}`")
                if lon:
                    lines.append(f"London H/L: `${e(lon.get('high','N/A'))}` / `${e(lon.get('low','N/A'))}`")
                if ny:
                    lines.append(f"NY H/L: `${e(ny.get('high','N/A'))}` / `${e(ny.get('low','N/A'))}`")
            if comex:
                or30 = comex.get("or_30m", {}) or {}
                if or30:
                    lines.append(f"COMEX OR30 H/L: `${e(or30.get('high','N/A'))}` / `${e(or30.get('low','N/A'))}`")
                if comex.get("session_vwap") is not None:
                    lines.append(f"COMEX Session VWAP: `${e(comex.get('session_vwap'))}`")
            if vp.get("hvn") or vp.get("lvn"):
                hvn_txt = ", ".join(str(x) for x in (vp.get("hvn") or [])[:2]) or "N/A"
                lvn_txt = ", ".join(str(x) for x in (vp.get("lvn") or [])[:2]) or "N/A"
                lines.append(f"VP HVN/LVN: `{e(hvn_txt)}` / `{e(lvn_txt)}`")
            if imb.get("nearest_bull_fvg") or imb.get("nearest_bear_fvg"):
                bull = imb.get("nearest_bull_fvg")
                bear = imb.get("nearest_bear_fvg")
                if bull:
                    lines.append(f"Bull FVG: `${e(bull[0])}` to `${e(bull[1])}`")
                if bear:
                    lines.append(f"Bear FVG: `${e(bear[0])}` to `${e(bear[1])}`")

            if macro_ctx.get("available"):
                dxy15 = (macro_ctx.get("dxy") or {}).get("ret_15m_pct")
                tnx15 = (macro_ctx.get("tnx") or {}).get("chg_15m_bps")
                lines.append(
                    f"🌐 Macro shock: DXY15m `{e(dxy15 if dxy15 is not None else 'N/A')}%`, US10Y15m `{e(tnx15 if tnx15 is not None else 'N/A')}bps`"
                )
                lines.append(f"Macro view: `{e(macro_ctx.get('summary','neutral'))}`")
            if news_freeze:
                if news_freeze.get("active"):
                    nearest = int(news_freeze.get("nearest_min", -1))
                    ev_name = str((news_freeze.get("events") or ["USD high impact"])[0])[:42]
                    lines.append(f"⛔ News Freeze: `{e(nearest)}m` `{e(ev_name)}`")
                else:
                    nearest = news_freeze.get("nearest_min", -1)
                    if isinstance(nearest, int) and nearest >= 0:
                        lines.append(f"📰 USD event proximity: `{e(nearest)}m` \\(no freeze\\)")

        smc4 = overview.get("h4_smc", {})
        if smc4:
            bias_emoji = "🟢" if smc4.get("bias") == "long" else ("🔴" if smc4.get("bias") == "short" else "🟡")
            lines += [
                f"",
                f"{'─' * 30}",
                f"🧠 *SMC CONTEXT \\(H4\\)*",
                f"{'─' * 30}",
                f"{bias_emoji} *Bias:* `{e(smc4.get('bias','neutral'))}`  "
                f"*Conf:* `{e(int(smc4.get('confidence',0)*100))}%`",
                f"📦 *Order Blocks:* `{e(smc4.get('order_blocks',0))}` active",
                f"🔲 *FVGs:* `{e(smc4.get('fvgs',0))}` unfilled",
            ]

        lines += [
            f"",
            f"_Updated: {e(datetime.now(timezone.utc).strftime('%H:%M UTC'))}_",
            f"{'═' * 35}",
        ]

        return self._send("\n".join(lines), chat_id=chat_id, feature="gold_overview")

    # ─── Crypto Scan Summary ───────────────────────────────────────────────────
    def send_crypto_scan_summary(self, opportunities: list, chat_id: Optional[int] = None) -> bool:
        """Send top crypto opportunities from a scan."""
        e = self._escape
        if not opportunities:
            return self._send(
                "🔍 *CRYPTO SCAN COMPLETE*\n\nNo high\\-probability setups found at this time\\.\n"
                "_Markets may be in consolidation\\._",
                chat_id=chat_id,
                feature="scan_crypto",
            )

        lines = [
            f"{'═' * 35}",
            f"🎯 *CRYPTO SNIPER SCAN*",
            f"Found *{e(len(opportunities))}* opportunities",
            f"{'═' * 35}",
        ]

        for i, opp in enumerate(opportunities[:5], 1):
            sig = opp.signal
            dir_emoji = "📈" if sig.direction == "long" else "📉"
            lines += [
                f"",
                f"*{e(i)}\\. {dir_emoji} {e(sig.symbol)}*",
                f"   Setup: `{e(opp.setup_type)}`  Score: `{e(round(opp.composite_score,1))}`",
                f"   Entry: `{e(self._fmt_price(sig.entry))}`  SL: `{e(self._fmt_price(sig.stop_loss))}`  TP2: `{e(self._fmt_price(sig.take_profit_2))}`",
                f"   R:R `1:{e(sig.risk_reward)}`  Conf: `{e(sig.confidence)}%`",
            ]
            if opp.funding_rate is not None:
                fr_pct = round(opp.funding_rate * 100, 4)
                lines.append(f"   Funding: `{e(fr_pct)}%`")

        lines += [
            f"",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]

        symbols = [str(getattr(opp.signal, "symbol", "") or "") for opp in opportunities[:5]]
        return self._send(
            "\n".join(lines),
            chat_id=chat_id,
            feature="scan_crypto",
            signal_symbols=symbols,
        )

    def send_crypto_focus_status(self, focus_symbols: list[str], opportunities: list, chat_id: Optional[int] = None) -> bool:
        """Send compact BTC/ETH focused crypto status for scheduled monitoring."""
        e = self._escape
        focus_labels = [str(s or '').upper() for s in (focus_symbols or []) if str(s or '').strip()]
        if opportunities:
            lines = [
                f"{'═' * 35}",
                "🪙 *CRYPTO FOCUS MONITOR*",
                f"Focus: `{e(', '.join(focus_labels) or 'BTCUSD, ETHUSD')}`",
                f"Found *{e(len(opportunities))}* focus signal(s)",
                f"{'═' * 35}",
            ]
            for i, opp in enumerate(opportunities[:4], 1):
                s = opp.signal
                dir_emoji = "📈" if str(getattr(s, 'direction', '')).lower() == 'long' else "📉"
                lines += [
                    "",
                    f"*{e(i)}\\. {dir_emoji} {e(getattr(s, 'symbol', '-'))}*",
                    f"   Setup: `{e(getattr(opp, 'setup_type', '-'))}`  Conf: `{e(round(float(getattr(s, 'confidence', 0.0)), 1))}%`",
                    f"   Entry: `{e(self._fmt_price(getattr(s, 'entry', 0)))}`  SL: `{e(self._fmt_price(getattr(s, 'stop_loss', 0)))}`  TP2: `{e(self._fmt_price(getattr(s, 'take_profit_2', 0)))}`",
                ]
            lines += ["", f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_", f"{'═' * 35}"]
            symbols = [str(getattr(getattr(opp, 'signal', None), 'symbol', '') or '') for opp in opportunities[:4]]
            return self._send("\n".join(lines), chat_id=chat_id, feature="scan_crypto", signal_symbols=symbols)

        label = ', '.join(focus_labels) or 'BTCUSD, ETHUSD'
        text = (
            f"🪙 *CRYPTO FOCUS MONITOR*\n\n"
            f"Focus: `{e(label)}`\n"
            "No signal right now\\.\n"
            "_Still watching for BTC/ETH setups\\._"
        )
        return self._send(text, chat_id=chat_id, feature="scan_crypto")

    def send_fx_scan_summary(self, opportunities: list, chat_id: Optional[int] = None) -> bool:
        """Send top FX major opportunities from a scan."""
        e = self._escape
        if not opportunities:
            return self._send(
                "💱 *FX MAJOR SCAN*\n\nNo high\-probability FX setups found right now\.",
                chat_id=chat_id,
                feature="scan_fx",
            )

        lines = [
            f"{'═' * 35}",
            "💱 *FX MAJOR SCAN*",
            f"Found *{e(len(opportunities))}* opportunities",
            f"{'═' * 35}",
        ]
        for i, opp in enumerate(opportunities[:5], 1):
            s = opp.signal
            dir_emoji = "📈" if str(getattr(s, 'direction', '')).lower() == 'long' else "📉"
            try:
                _fx_vol = float(getattr(opp, 'vol_vs_avg', 1.0) or 0.0)
                fx_vol_text = "-" if (not math.isfinite(_fx_vol)) else f"{round(_fx_vol, 2)}x"
            except Exception:
                fx_vol_text = "-"
            lines += [
                "",
                f"*{e(i)}\. {dir_emoji} {e(getattr(s, 'symbol', '-'))}*",
                f"   Setup: `{e(getattr(opp, 'setup_type', getattr(s, 'pattern', '-')) or '-')}`  Score: `{e(round(float(getattr(opp, 'composite_score', 0.0) or 0.0),1))}`",
                f"   Entry: `{e(self._fmt_price(getattr(s, 'entry', 0)))}`  SL: `{e(self._fmt_price(getattr(s, 'stop_loss', 0)))}`  TP2: `{e(self._fmt_price(getattr(s, 'take_profit_2', 0)))}`",
                f"   R:R `1:{e(getattr(s, 'risk_reward', '-'))}`  Conf: `{e(round(float(getattr(s, 'confidence', 0.0) or 0.0),1))}%`  Vol: `{e(fx_vol_text)}`",
            ]
        lines += [
            "",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        symbols = [str(getattr(getattr(opp, 'signal', None), 'symbol', '') or '') for opp in opportunities[:5]]
        return self._send("\n".join(lines), chat_id=chat_id, feature="scan_fx", signal_symbols=symbols)

    # ─── Research Answer ───────────────────────────────────────────────────────
    def send_research_answer(self, question: str, answer: str, chat_id: Optional[int] = None) -> bool:
        """Send an AI research answer."""
        e = self._escape
        # Truncate if too long
        if len(answer) > 3500:
            answer = answer[:3500] + "...\n_(truncated)_"

        text = (
            f"🧠 *DEXTER PRO RESEARCH*\n"
            f"{'─' * 30}\n"
            f"❓ *Q:* {e(question)}\n\n"
            f"{'─' * 30}\n"
            f"💡 *A:*\n\n"
            f"{e(answer)}\n\n"
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_"
        )
        return self._send(text, chat_id=chat_id)

    # ─── Stock Signal ─────────────────────────────────────────────────────────
    def send_stock_signal(
        self,
        opp,
        chat_id: Optional[int] = None,
        feature_override: Optional[str] = None,
    ) -> bool:
        """Send a stock trade opportunity alert."""
        e = self._escape
        sig = opp.signal
        direction_emoji = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
        conf_emoji = sig.confidence_emoji()
        bars = int(sig.confidence / 10)
        conf_bar = "█" * bars + "░" * (10 - bars)

        market_flags = {
            "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
            "JP": "🇯🇵", "HK": "🇭🇰", "CN": "🇨🇳", "TH": "🇹🇭",
            "SG": "🇸🇬", "IN": "🇮🇳", "AU": "🇦🇺", "INDEX": "📊",
        }
        flag = market_flags.get(opp.market, "🌏")

        lines = [
            f"{'═' * 35}",
            f"📈 *STOCK SIGNAL* {conf_emoji} {flag}",
            f"{'═' * 35}",
            f"",
            f"*Symbol:*    `{e(sig.symbol)}`",
            f"*Market:*    `{e(opp.market)}`",
            f"*Direction:* {direction_emoji}",
            f"*Setup:*     `{e(opp.setup_type)}`",
            f"*Volume:*    `{e(opp.vol_vs_avg)}x avg`",
            f"*Quality:*   `{e(getattr(opp, 'quality_tag', 'N/A'))}`",
            f"",
            f"{'─' * 30}",
            f"📊 *TRADE LEVELS*",
            f"{'─' * 30}",
            f"🎯 *Entry:*      `{e(self._fmt_price(sig.entry))}`",
            f"🛑 *Stop Loss:*  `{e(self._fmt_price(sig.stop_loss))}`",
            f"✅ *TP1 \\(1R\\):*  `{e(self._fmt_price(sig.take_profit_1))}`",
            f"✅ *TP2 \\(2R\\):*  `{e(self._fmt_price(sig.take_profit_2))}`",
            f"🚀 *TP3 \\(3R\\):*  `{e(self._fmt_price(sig.take_profit_3))}`",
            f"⚖️ *R:R:*        `1:{e(sig.risk_reward)}`",
            f"",
            f"{'─' * 30}",
            f"🔍 *REASONS*",
        ]
        for r in sig.reasons[:5]:
            lines.append(f"• {e(r)}")
        if sig.warnings:
            lines.append("")
            lines.append("⚠️ *WARNINGS*")
            for w in sig.warnings[:2]:
                lines.append(f"• {e(w)}")
        if opp.pe_ratio:
            lines.append(f"📐 *P/E Ratio:* `{e(round(opp.pe_ratio, 1))}`")

        lines += [
            f"",
            f"{'─' * 30}",
            f"🎯 *CONFIDENCE*",
            f"`{conf_bar}` `{e(sig.confidence)}%`",
            f"",
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
            f"_⚠️ For research purposes only\\. Not financial advice\\._",
        ]
        target_feature = str(feature_override or self._feature_from_signal(sig))
        return self._send(
            "\n".join(lines),
            chat_id=chat_id,
            feature=target_feature,
            signal_symbol=str(getattr(sig, "symbol", "") or ""),
        )

    # ─── Stock Scan Summary ───────────────────────────────────────────────────
    def send_stock_scan_summary(self, opportunities: list,
                                 market_label: str = "GLOBAL", chat_id: Optional[int] = None) -> bool:
        """Send top stock opportunities from a scan."""
        e = self._escape
        if not opportunities:
            return self._send(
                f"🔍 *STOCK SCAN \\— {e(market_label)}*\n\n"
                f"No high\\-probability setups found\\. Markets may be consolidating\\.",
                chat_id=chat_id,
                feature=self._feature_from_stock_label(market_label),
            )

        market_flags = {
            "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
            "JP": "🇯🇵", "HK": "🇭🇰", "CN": "🇨🇳", "TH": "🇹🇭",
            "SG": "🇸🇬", "IN": "🇮🇳", "AU": "🇦🇺", "INDEX": "📊",
        }

        lines = [
            f"{'═' * 35}",
            f"🌏 *STOCK SCANNER — {e(market_label)}*",
            f"Found *{e(len(opportunities))}* opportunities",
            f"{'═' * 35}",
        ]

        for i, opp in enumerate(opportunities[:7], 1):
            s = opp.signal
            dir_emoji = "📈" if s.direction == "long" else "📉"
            flag = market_flags.get(opp.market, "🌏")
            lines += [
                f"",
                f"*{e(i)}\\. {dir_emoji} {flag} {e(s.symbol)}*",
                f"   Setup: `{e(opp.setup_type)}`  Score: `{e(round(opp.composite_score,1))}`",
                f"   Entry: `{e(self._fmt_price(s.entry))}`  SL: `{e(self._fmt_price(s.stop_loss))}`",
                f"   TP2: `{e(self._fmt_price(s.take_profit_2))}`  R:R `1:{e(s.risk_reward)}`",
                f"   Conf: `{e(s.confidence)}%`  Vol: `{e(opp.vol_vs_avg)}x`  Q: `{e(getattr(opp,'quality_tag','N/A'))}`",
            ]

        lines += [
            f"",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send(
            "\n".join(lines),
            chat_id=chat_id,
            feature=self._feature_from_stock_label(market_label),
        )

    def send_us_open_daytrade_summary(self, opportunities: list, chat_id: Optional[int] = None) -> bool:
        """Send top 10 US open day-trade candidates (volume + win-rate focused)."""
        e = self._escape
        if not opportunities:
            return self._send(
                "🇺🇸 *US OPEN DAYTRADE PLAN*\n\nNo qualifying candidates found right now\\."
                ,
                chat_id=chat_id,
                feature="scan_us_open",
            )

        lines = [
            f"{'═' * 35}",
            "🇺🇸 *US OPEN DAYTRADE PLAN*",
            "Top 10 by liquidity \\+ setup win\\-rate",
            f"{'═' * 35}",
            "",
            "_Window: first 1 to 2 hours after NY open_",
        ]

        for i, opp in enumerate(opportunities[:10], 1):
            s = opp.signal
            side = "📈" if s.direction == "long" else "📉"
            win_rate_pct = round(opp.setup_win_rate * 100, 1)
            dollar_vol = self._fmt_compact(opp.dollar_volume)
            lines += [
                "",
                f"*{e(i)}\\. {side} {e(s.symbol)}*",
                f"   Setup: `{e(opp.setup_type)}`  WinRate `{e(win_rate_pct)}%`",
                f"   Vol$: `{e(dollar_vol)}`  VolRatio: `{e(round(opp.vol_vs_avg, 2))}x`",
                f"   Entry: `{e(self._fmt_price(s.entry))}`  SL: `{e(self._fmt_price(s.stop_loss))}`  TP2: `{e(self._fmt_price(s.take_profit_2))}`",
                f"   Conf: `{e(s.confidence)}%`  R:R `1:{e(s.risk_reward)}`  Q: `{e(getattr(opp,'quality_tag','N/A'))}`",
            ]

        lines += [
            "",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            "_⚠️ For informational purposes only\\. Not financial advice\\._",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="scan_us_open")

    def send_us_open_monitor_update(
        self,
        opportunities: list,
        new_symbols: Optional[list[str]] = None,
        top_changed: bool = False,
        periodic_ping: bool = False,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Focused update during US open smart monitoring window."""
        e = self._escape
        if not opportunities:
            return self._send(
                "🇺🇸 *US OPEN MONITOR*\n\nNo active candidates right now\\.",
                chat_id=chat_id,
                feature="monitor_us",
            )

        top = opportunities[0].signal
        reason_parts = []
        if top_changed:
            reason_parts.append("leader changed")
        if new_symbols:
            reason_parts.append(f"new symbols: {', '.join(new_symbols[:4])}")
        if periodic_ping and not reason_parts:
            reason_parts.append("periodic check")
        reason = "; ".join(reason_parts) if reason_parts else "snapshot update"

        lines = [
            f"{'═' * 35}",
            "🇺🇸 *US OPEN SMART MONITOR*",
            f"Reason: `{e(reason)}`",
            f"{'═' * 35}",
            "",
            f"🥇 *Top now:* `{e(top.symbol)}` {('📈' if top.direction == 'long' else '📉')}",
            f"   Entry: `{e(self._fmt_price(top.entry))}`  SL: `{e(self._fmt_price(top.stop_loss))}`  TP2: `{e(self._fmt_price(top.take_profit_2))}`",
            f"   Conf: `{e(top.confidence)}%`  R:R `1:{e(top.risk_reward)}`",
            "",
            "*Top 3 snapshot:*",
        ]

        for i, opp in enumerate(opportunities[:3], 1):
            s = opp.signal
            lines.append(
                f"{e(i)}\\. `{e(s.symbol)}` "
                f"Conf `{e(s.confidence)}%` "
                f"Vol `{e(round(opp.vol_vs_avg,2))}x` "
                f"WR `{e(round(opp.setup_win_rate*100,1))}%`"
            )

        lines += [
            "",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us")

    def send_us_open_session_checkin(
        self,
        *,
        interval_min: int,
        premarket_lead_min: int,
        no_opp_ping_min: int,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Kickoff message when entering US-open premarket monitoring window."""
        e = self._escape
        lines = [
            f"{'═' * 35}",
            "🇺🇸 *US OPEN DAYTRADE SESSION*",
            e("Assistant mode: close-follow (pre-market + first 2h)"),
            f"{'═' * 35}",
            "",
            e(f"Monitor cadence: {max(1, int(interval_min))}m"),
            e(f"Starts: {max(0, int(premarket_lead_min))}m before NY cash open"),
            e(f"No-setup pulse: every {max(1, int(no_opp_ping_min))}m"),
            e("Focus: liquidity + momentum + setup win-rate"),
            e("Use /monitor_us for manual refresh any time"),
            e("Use /us_open_report for today's signal quality recap"),
            e("Use /us_open_dashboard for trader dashboard (today)"),
            "",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us")


    def send_us_open_signal_quality_recap(self, report: dict, chat_id: Optional[int] = None) -> bool:
        """US-open session signal quality recap (compact). Dashboard-aligned when dashboard payload is provided."""
        rpt = dict(report or {})

        # Preferred path: compact recap derived from us_open_trader_dashboard() payload
        if "summary" in rpt and "window" in rpt and "segments" in rpt:
            summary = dict(rpt.get("summary") or {})
            sim = dict(rpt.get("simulation") or {})
            window = dict(rpt.get("window") or {})
            qd = dict(rpt.get("quality_distribution") or {})
            best = list(rpt.get("best_symbols") or [])
            worst = list(rpt.get("worst_symbols") or [])
            active = list(rpt.get("most_active_symbols") or [])
            setup_rows = list(rpt.get("win_rate_by_setup") or [])
            all_session_symbols = list(rpt.get("all_session_symbols") or [])
            outside_summary = dict(rpt.get("outside_window_summary") or {})
            outside_active = list(rpt.get("outside_window_top_active") or [])
            lines = [
                "═══════════════════════════════════",
                "🇺🇸 US OPEN SIGNAL QUALITY",
                f"Today session recap (dashboard-aligned, US stocks | NY {rpt.get('ny_date','-')})",
                "═══════════════════════════════════",
                "",
                f"Window: {window.get('open_start_bkk','-')} to {window.get('core_end_bkk','-')} BKK (review to {window.get('review_end_bkk','-')})",
                "",
                f"Signals sent: {int(summary.get('sent',0) or 0)}  Resolved: {int(summary.get('resolved',0) or 0)}  Pending: {int(summary.get('pending',0) or 0)}",
                f"Wins/Losses: {int(summary.get('wins',0) or 0)} / {int(summary.get('losses',0) or 0)}  WinRate: {float(summary.get('win_rate',0.0) or 0.0):.1f}%",
                f"Net R: {float(summary.get('net_r',0.0) or 0.0):.4f}  PendingMarkR: {float(summary.get('pending_mark_r',0.0) or 0.0):.4f}",
                f"Simulation $1000 (1% risk): realized ${float(sim.get('realized_balance',1000.0) or 1000.0):.2f}  marked ${float(sim.get('marked_balance',1000.0) or 1000.0):.2f}",
            ]
            if active:
                lines += ["", "Most active symbols (by sent):"]
                for i, row in enumerate(active[:8], 1):
                    lines.append(
                        f"{i}. {row.get('symbol','-')} sent {int(row.get('sent',0) or 0)} res {int(row.get('resolved',0) or 0)} "
                        f"WR {float(row.get('win_rate',0.0) or 0.0):.1f}% sessionR {float(row.get('session_r',0.0) or 0.0):.3f}"
                    )
            if all_session_symbols:
                lines += ["", f"Session symbols (all {len(all_session_symbols)}):", ", ".join(all_session_symbols)]

            if best or worst:
                lines += ["", "Performance highlights:"]
                for row in best[:5]:
                    lines.append(f"+ {row.get('symbol','-')} R {float(row.get('session_r',0.0) or 0.0):.3f}")
                for row in worst[:5]:
                    lines.append(f"- {row.get('symbol','-')} R {float(row.get('session_r',0.0) or 0.0):.3f}")
            if setup_rows:
                lines += ["", "Setup summary (resolved):"]
                for row in setup_rows[:3]:
                    lines.append(
                        f"- {row.get('setup','-')}: sent {int(row.get('sent',0) or 0)} res {int(row.get('resolved',0) or 0)} "
                        f"WR {float(row.get('win_rate',0.0) or 0.0):.1f}% netR {float(row.get('net_r',0.0) or 0.0):.3f}"
                    )
            if int(outside_summary.get("sent", 0) or 0) > 0:
                lines += ["", "Outside US-open window today (not counted above):"]
                lines.append(
                    f"sent {int(outside_summary.get('sent',0) or 0)}  resolved {int(outside_summary.get('resolved',0) or 0)}  pending {int(outside_summary.get('pending',0) or 0)}  "
                    f"netR {float(outside_summary.get('net_r',0.0) or 0.0):.4f}  markR {float(outside_summary.get('pending_mark_r',0.0) or 0.0):.4f}"
                )
                if outside_active:
                    for i, row in enumerate(outside_active[:8], 1):
                        lines.append(
                            f"{i}. {row.get('symbol','-')} sent {int(row.get('sent',0) or 0)} res {int(row.get('resolved',0) or 0)} "
                            f"WR {float(row.get('win_rate',0.0) or 0.0):.1f}% dayR {float(row.get('session_r',0.0) or 0.0):.3f}"
                        )

            src_tiers = dict(qd.get('source_tiers') or {})
            if src_tiers:
                src_parts = [f"{k}:{v}" for k, v in list(src_tiers.items())[:6]]
                lines += ["", f"Counted sources: {' | '.join(src_parts)}"]
            lines += [
                "",
                "Tip: /monitor_us live | /scan_us_open refresh | /us_open_dashboard dashboard",
                f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                "═══════════════════════════════════",
            ]
            return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us", parse_mode=None)

        # Legacy path (source_filter report)
        e = self._escape
        days = int(rpt.get("days", 1) or 1)
        sent = int(rpt.get("sent", 0) or 0)
        resolved = int(rpt.get("resolved", 0) or 0)
        pending = int(rpt.get("pending", 0) or 0)
        tp1 = int(rpt.get("tp1", 0) or 0)
        tp2 = int(rpt.get("tp2", 0) or 0)
        tp3 = int(rpt.get("tp3", 0) or 0)
        sl = int(rpt.get("sl", 0) or 0)
        wins = int(rpt.get("wins", 0) or 0)
        win_rate = float(rpt.get("win_rate", 0.0) or 0.0)
        avg_r_resolved = rpt.get("avg_r_resolved", 0.0)
        avg_r_pending = rpt.get("avg_r_pending", 0.0)
        src = str(rpt.get("source_filter") or "us_open")
        top_symbols = list(rpt.get("top_symbols", []) or [])

        title = "🇺🇸 *US OPEN SIGNAL QUALITY*"
        subtitle = f"Today session recap \({e(days)}d filter: `{e(src)}`\)"
        lines = [
            f"{'═' * 35}",
            title,
            subtitle,
            f"{'═' * 35}",
            "",
            f"Signals sent: `{e(sent)}`  Resolved: `{e(resolved)}`  Pending: `{e(pending)}`",
            f"Wins: `{e(wins)}`  SL: `{e(sl)}`  WinRate: `{e(round(win_rate,1))}%`",
            f"TP1/TP2/TP3: `{e(tp1)}` / `{e(tp2)}` / `{e(tp3)}`",
            f"Avg R \(resolved\): `{e(avg_r_resolved)}`  Avg R \(pending\): `{e(avg_r_pending)}`",
        ]
        if top_symbols:
            lines += ["", "*Top symbols today:*"]
            for i, row in enumerate(top_symbols[:5], 1):
                lines.append(
                    f"{e(i)}\. `{e(str(row.get('symbol','-')) )}` "
                    f"sent `{e(int(row.get('sent',0) or 0))}` "
                    f"resolved `{e(int(row.get('resolved',0) or 0))}` "
                    f"WR `{e(float(row.get('win_rate',0.0) or 0.0))}%` "
                    f"netR `{e(row.get('net_r',0.0))}`"
                )
        lines += [
            "",
            "Tip: `/monitor_us` live \| `/scan_us_open` refresh \| `/us_open_dashboard` dashboard",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us")

    def send_us_open_trader_dashboard(self, report: dict, chat_id: Optional[int] = None) -> bool:
        """Trader dashboard for today's US-open signals (US stocks only)."""
        e = self._escape
        rpt = dict(report or {})
        if str(rpt.get("status")) == "no_data":
            lines = [
                f"{'═' * 35}",
                "🇺🇸 *US OPEN TRADER DASHBOARD*",
                f"{'═' * 35}",
                "",
                e(str(rpt.get("message") or "No data for today's US-open dashboard.")),
                "",
                f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
                f"{'═' * 35}",
            ]
            return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us")

        summary = dict(rpt.get("summary") or {})
        sim = dict(rpt.get("simulation") or {})
        seg = dict(rpt.get("segments") or {})
        core = dict(seg.get("core") or {})
        late = dict(seg.get("late") or {})
        qd = dict(rpt.get("quality_distribution") or {})
        window = dict(rpt.get("window") or {})
        best = list(rpt.get("best_symbols") or [])
        worst = list(rpt.get("worst_symbols") or [])
        setup_rows = list(rpt.get("win_rate_by_setup") or [])

        try:
            top_n = max(1, min(20, int(rpt.get("display_top_n", 5) or 5)))
        except Exception:
            top_n = 5
        verdict = str(seg.get("verdict") or "-")
        verdict_label = {
            "degraded": "DEGRADED \\(late open weaker\\)",
            "not_degraded": "NOT DEGRADED",
            "inconclusive": "INCONCLUSIVE \\(late sample too small\\)",
            "no_late_signals": "NO LATE SIGNALS",
        }.get(verdict, e(verdict))

        lines = [
            f"{'═' * 35}",
            "🇺🇸 *US OPEN TRADER DASHBOARD*",
            e(f"Today only · US stocks · NY {rpt.get('ny_date','-')}"),
            f"{'═' * 35}",
            "",
            f"Window: `{e(window.get('open_start_bkk','-'))}` to `{e(window.get('core_end_bkk','-'))}` BKK "
            f"\\(review to `{e(window.get('review_end_bkk','-'))}`\\)",
            "",
            "*Session Totals*",
            f"sent `{e(summary.get('sent',0))}`  resolved `{e(summary.get('resolved',0))}`  pending `{e(summary.get('pending',0))}`",
            f"netR `{e(summary.get('net_r',0.0))}`  pendingMarkR `{e(summary.get('pending_mark_r',0.0))}`",
            f"wins/losses `{e(summary.get('wins',0))}` / `{e(summary.get('losses',0))}`  WR `{e(summary.get('win_rate',0.0))}%`",
            "",
            "*Simulation \\($1000, fixed risk\\)*",
            f"risk/trade `{e(sim.get('risk_pct',1.0))}%` \\(\\~${e(sim.get('risk_amount_per_trade',0.0))}\\)",
            f"realized `${e(sim.get('realized_balance',0.0))}`  marked `${e(sim.get('marked_balance',0.0))}`",
        ]

        if best:
            lines += ["", "*Best symbols \\(session R\\)*"]
            for i, row in enumerate(best[:top_n], 1):
                lines.append(
                    f"{e(i)}\\. `{e(row.get('symbol','-'))}` "
                    f"R `{e(row.get('session_r',0.0))}` "
                    f"\\(real `{e(row.get('net_r',0.0))}` \\| mark `{e(row.get('pending_mark_r',0.0))}`\\)"
                )
        if worst:
            lines += ["", "*Worst symbols \\(session R\\)*"]
            for i, row in enumerate(worst[:top_n], 1):
                lines.append(
                    f"{e(i)}\\. `{e(row.get('symbol','-'))}` "
                    f"R `{e(row.get('session_r',0.0))}` "
                    f"\\(real `{e(row.get('net_r',0.0))}` \\| mark `{e(row.get('pending_mark_r',0.0))}`\\)"
                )

        if setup_rows:
            lines += ["", "*Win rate by setup \\(resolved\\)*"]
            for row in setup_rows[:top_n]:
                lines.append(
                    f"`{e(str(row.get('setup','-')) )}` "
                    f"sent `{e(row.get('sent',0))}` res `{e(row.get('resolved',0))}` "
                    f"WR `{e(row.get('win_rate',0.0))}%` netR `{e(row.get('net_r',0.0))}`"
                )

        lines += ["", "*Quality distribution*"]
        q_mode = str(qd.get("mode") or "proxy_only")
        q_scores = dict(qd.get("q_scores") or {})
        if q_mode in {"exact", "mixed"} and int(qd.get("exact_q_count", 0) or 0) > 0:
            lines.append(
                f"Q0/Q1/Q2/Q3: `{e(q_scores.get('Q0',0))}` / `{e(q_scores.get('Q1',0))}` / "
                f"`{e(q_scores.get('Q2',0))}` / `{e(q_scores.get('Q3',0))}` "
                f"\\(exact rows `{e(qd.get('exact_q_count',0))}`/{e(qd.get('rows_total',0))}`\\)"
            )
        else:
            lines.append("Exact `quality_score` not stored in today's rows \\(legacy records\\)\\. Showing proxy view:")
        conf_bands = dict(qd.get("confidence_bands") or {})
        lines.append(
            f"Conf `<70/70-74/75-79/80+`: `{e(conf_bands.get('<70',0))}` / `{e(conf_bands.get('70-74',0))}` / "
            f"`{e(conf_bands.get('75-79',0))}` / `{e(conf_bands.get('80+',0))}`"
        )
        source_tiers = dict(qd.get("source_tiers") or {})
        if source_tiers:
            src_parts = [f"{k}:{v}" for k, v in list(source_tiers.items())[:4]]
            lines.append(f"Source tiers: `{e(' | '.join(src_parts))}`")

        lines += ["", "*Late Open Degradation \\(mood stop check\\)*"]
        lines.append(
            f"Core \\(0\\-{e(window.get('hard_stop_min',90))}m\\): sent `{e(core.get('sent',0))}` netR `{e(core.get('net_r',0.0))}` "
            f"markR `{e(core.get('pending_mark_r',0.0))}` WR `{e(core.get('win_rate',0.0))}%` avgConf `{e(core.get('avg_conf',0.0))}`"
        )
        late_start = int(window.get("hard_stop_min", 90) or 90)
        review_end = int(window.get("review_max_min", 120) or 120)
        lines.append(
            f"Late \\({e(late_start)}\\-{e(review_end)}m\\): sent `{e(late.get('sent',0))}` netR `{e(late.get('net_r',0.0))}` "
            f"markR `{e(late.get('pending_mark_r',0.0))}` WR `{e(late.get('win_rate',0.0))}%` avgConf `{e(late.get('avg_conf',0.0))}`"
        )
        lines.append(f"Verdict: *{verdict_label}*")

        lines += [
            "",
            "Tip: `/monitor_us` live \\| `/scan_us_open` refresh plan \\| `/us_open_report` compact recap",
            f"_🕐 {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="monitor_us")

    def send_signal_trader_dashboard(self, report: dict, chat_id: Optional[int] = None) -> bool:
        """Daily all-signals trader dashboard (gold/thai/us/global/crypto)."""
        e = self._escape
        rpt = dict(report or {})
        title_line = "=" * 35

        if str(rpt.get("status")) == "no_data":
            msg = str(rpt.get("message") or "No data.")
            lines = [
                title_line,
                "*ALL SIGNALS TRADER DASHBOARD*",
                title_line,
                "",
                e(msg),
                "",
                f"_UTC {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'))}_",
                title_line,
            ]
            return self._send("\n".join(lines), chat_id=chat_id, feature="status")

        summary = dict(rpt.get("summary") or {})
        sim = dict(rpt.get("simulation") or {})
        buckets = dict(rpt.get("buckets") or {})
        bucket_order = list(rpt.get("bucket_order") or [])
        best = list(rpt.get("best_symbols") or [])
        worst = list(rpt.get("worst_symbols") or [])
        setup_rows = list(rpt.get("win_rate_by_setup") or [])

        conf_bands = dict(rpt.get("confidence_bands") or {})
        source_counts = dict(rpt.get("source_counts") or {})
        window = dict(rpt.get("window") or {})
        filter_label = str(rpt.get("market_filter_label") or "").strip()
        try:
            top_n = max(1, min(20, int(rpt.get("display_top_n", 5) or 5)))
        except Exception:
            top_n = 5

        if int(rpt.get("days", 1) or 1) == 1:
            subtitle = f"Today {rpt.get('local_date','-')} | {rpt.get('timezone','UTC')}"
        else:
            subtitle = f"Last {rpt.get('days',1)}d | {rpt.get('timezone','UTC')}"

        lines = [
            title_line,
            "*ALL SIGNALS TRADER DASHBOARD*",
            e(subtitle),
            title_line,
        ]
        if filter_label:
            lines += [f"Filter: `{e(filter_label)}`"]
        lines += [
            "",
            f"Window: `{e(window.get('start_local','-'))}` -> `{e(window.get('end_local','-'))}`",
            "",
            "*Overall*",
            f"sent `{e(summary.get('sent',0))}`  resolved `{e(summary.get('resolved',0))}`  pending `{e(summary.get('pending',0))}`",
            f"netR `{e(summary.get('net_r',0.0))}`  pendingMarkR `{e(summary.get('pending_mark_r',0.0))}`  WR `{e(summary.get('win_rate',0.0))}%`",
            "",
            "*Simulation ($1000, fixed risk)*",
            f"risk/trade `{e(sim.get('risk_pct',1.0))}%` (approx ${e(sim.get('risk_amount_per_trade',0.0))})",
            f"realized `{e(sim.get('realized_balance',0.0))}`  marked `{e(sim.get('marked_balance',0.0))}`",
            "",
            "*By Market Bucket*",
        ]

        header = f"{'Bucket':<14} {'S':>3} {'R':>3} {'P':>3} {'WR%':>5} {'netR':>8} {'markR':>8}"
        table_lines = [header, '-' * len(header)]
        label_map = {
            'Gold': 'Gold',
            'Thailand Stocks': 'TH Stocks',
            'US Stocks': 'US Stocks',
            'Global Stocks': 'Global',
            'Crypto': 'Crypto',
            'Other': 'Other',
        }
        for key in bucket_order:
            b = dict(buckets.get(key) or {})
            if not b:
                continue
            short = label_map.get(str(b.get('label') or key), str(b.get('label') or key))
            table_lines.append(
                f"{short:<14} {int(b.get('sent',0) or 0):>3} {int(b.get('resolved',0) or 0):>3} {int(b.get('pending',0) or 0):>3} "
                f"{float(b.get('win_rate',0.0) or 0.0):>5.1f} {float(b.get('net_r',0.0) or 0.0):>8.3f} {float(b.get('pending_mark_r',0.0) or 0.0):>8.3f}"
            )
        lines.append("```")
        lines.extend(table_lines)
        lines.append("```")

        def _bucket_label_for(row: dict) -> str:
            bkey = str(row.get('bucket') or '')
            return str((dict(buckets.get(bkey) or {})).get('label') or bkey or '-')

        if best:
            lines += ["", "*Best symbols (session R)*"]
            for i, row in enumerate(best[:top_n], 1):
                blabel = _bucket_label_for(row)
                lines.append(f"{e(i)}\\. `{e(row.get('symbol','-'))}` ({e(blabel)}) R `{e(row.get('session_r',0.0))}`")

        if worst:
            lines += ["", "*Worst symbols (session R)*"]
            for i, row in enumerate(worst[:top_n], 1):
                blabel = _bucket_label_for(row)
                lines.append(f"{e(i)}\\. `{e(row.get('symbol','-'))}` ({e(blabel)}) R `{e(row.get('session_r',0.0))}`")

        if setup_rows:
            lines += ["", "*Win rate by setup (resolved)*"]
            for row in setup_rows[:top_n]:
                lines.append(
                    f"`{e(str(row.get('setup','-')) )}` sent `{e(row.get('sent',0))}` res `{e(row.get('resolved',0))}` "
                    f"WR `{e(row.get('win_rate',0.0))}%` netR `{e(row.get('net_r',0.0))}`"
                )

        lines += [
            "",
            "*Confidence bands*",
            f"`<70/70-74/75-79/80+` = `{e(conf_bands.get('<70',0))}` / `{e(conf_bands.get('70-74',0))}` / `{e(conf_bands.get('75-79',0))}` / `{e(conf_bands.get('80+',0))}`",
        ]
        if source_counts:
            parts = [f"{k}:{v}" for k, v in list(source_counts.items())[:6]]
            lines.append(f"Sources: `{e(' | '.join(parts))}`")

        lines += [
            "",
            e("Tip: /signal_dashboard daily all-markets | /us_open_dashboard US-open only"),
            f"_UTC {e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'))}_",
            title_line,
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="status")

    def send_mt5_execution_update(self, signal, result, source: str, chat_id: Optional[int] = None) -> bool:
        """Notify when MT5 execution is filled/dry-run/rejected."""
        e = self._escape
        status = str(getattr(result, "status", "") or "").lower()
        ok = bool(getattr(result, "ok", False))
        sig_symbol = str(getattr(result, "signal_symbol", "") or getattr(signal, "symbol", "") or "-")
        broker_symbol = str(getattr(result, "broker_symbol", "") or "-")
        direction = str(getattr(signal, "direction", "") or "").lower()
        dir_text = "🟢 LONG" if direction == "long" else ("🔴 SHORT" if direction == "short" else "⚪ N/A")
        title = "✅ *MT5 EXECUTED*" if ok else "⚠️ *MT5 EXECUTION UPDATE*"

        lines = [
            f"{'═' * 35}",
            title,
            f"{'═' * 35}",
            f"*Source:* `{e(source)}`",
            f"*Signal:* `{e(sig_symbol)}`  {dir_text}",
            f"*Broker Symbol:* `{e(broker_symbol)}`",
            f"*Status:* `{e(status or '-')}`",
            f"*Confidence:* `{e(getattr(signal, 'confidence', 0))}%`",
            f"*Entry:* `{e(self._fmt_price(getattr(signal, 'entry', 0)))}`",
            f"*SL/TP2:* `{e(self._fmt_price(getattr(signal, 'stop_loss', 0)))}` / `{e(self._fmt_price(getattr(signal, 'take_profit_2', 0)))}`",
        ]
        exec_meta = dict(getattr(result, "execution_meta", {}) or {})
        if exec_meta:
            rr_base = exec_meta.get("rr_base")
            rr_target = exec_meta.get("rr_target")
            stop_scale = exec_meta.get("stop_scale")
            size_mult = exec_meta.get("size_multiplier")
            factors = dict(exec_meta.get("factors") or {})
            if rr_base is not None and rr_target is not None:
                lines.append(
                    f"*Adaptive Plan:* `RR {e(rr_base)} → {e(rr_target)} | SLx {e(stop_scale)} | Size x {e(size_mult)}`"
                )
            elif exec_meta.get("reason"):
                lines.append(f"*Adaptive Plan:* `{e(exec_meta.get('reason'))}`")
            extras = []
            if factors.get("samples") is not None:
                extras.append(f"samples {factors.get('samples')}")
            if factors.get("spread_pct") is not None:
                extras.append(f"spread {factors.get('spread_pct')}%")
            if factors.get("atr_pct") is not None:
                extras.append(f"atr {factors.get('atr_pct')}%")
            if extras:
                lines.append(f"*Adaptive Factors:* `{e(' | '.join(extras))}`")
        ticket = getattr(result, "ticket", None)
        position_id = getattr(result, "position_id", None)
        if ticket:
            lines.append(f"*Order/Deal Ticket:* `{e(ticket)}`")
        if position_id:
            lines.append(f"*Position ID:* `{e(position_id)}`")
        msg = str(getattr(result, "message", "") or "")
        if msg:
            lines.append(f"*Broker Note:* `{e(msg[:220])}`")

        lines += [
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="mt5_status")

    def send_mt5_backtest_report(self, report: dict, chat_id: Optional[int] = None) -> bool:
        """Send compact MT5 backtest and neural-brain status report."""
        e = self._escape
        if not report:
            return self._send("MT5 backtest report unavailable\\.", chat_id=chat_id, feature="mt5_backtest")

        sync = report.get("sync", {}) or {}
        model = report.get("model", {}) or {}
        status = str(report.get("status", "unknown"))
        trades = int(report.get("trades", 0) or 0)

        lines = [
            f"{'═' * 35}",
            "🧠 *DEXTER NEURAL BACKTEST*",
            f"{'═' * 35}",
            f"*Window:* `{e(report.get('days', 0))} days`",
            f"*Status:* `{e(status)}`",
            f"*Trades:* `{e(trades)}`",
            f"*Win Rate:* `{e(report.get('win_rate', 0))}%`",
            f"*Net PnL:* `{e(report.get('net_pnl', 0))}`",
            f"*Profit Factor:* `{e(report.get('profit_factor', 0))}`",
            "",
            "*Sync:*",
            f"• closed positions seen: `{e(sync.get('closed_positions', 0))}`",
            f"• newly labeled signals: `{e(sync.get('updated', 0))}`",
        ]

        if model.get("available"):
            lines += [
                "",
                "*Neural Model:*",
                f"• samples: `{e(model.get('samples', 0))}`",
                f"• train acc: `{e(round(float(model.get('train_accuracy', 0))*100, 1))}%`",
                f"• val acc: `{e(round(float(model.get('val_accuracy', 0))*100, 1))}%`",
                f"• win-rate baseline: `{e(round(float(model.get('win_rate', 0))*100, 1))}%`",
            ]
        else:
            lines += ["", "*Neural Model:* `not trained yet`"]

        top_symbols = list(report.get("top_symbols", []) or [])[:5]
        if top_symbols:
            lines += ["", "*Top Symbols \\(net pnl\\):*"]
            for i, row in enumerate(top_symbols, 1):
                lines.append(
                    f"{e(i)}\\. `{e(row.get('symbol','-'))}` "
                    f"PnL `{e(row.get('net_pnl',0))}` "
                    f"WR `{e(row.get('win_rate',0))}%` "
                    f"T `{e(row.get('trades',0))}`"
                )

        lines += [
            "",
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="mt5_backtest")

    def send_mt5_position_manager_update(self, report: dict, source: str = "scheduler", chat_id: Optional[int] = None) -> bool:
        """Notify MT5 Position Manager actions (BE / partial / trail / time-stop)."""
        e = self._escape
        rpt = dict(report or {})
        actions = list(rpt.get("actions", []) or [])
        if not actions:
            return True
        lines = [
            f"{'═' * 35}",
            "🤖 *MT5 POSITION MANAGER*",
            f"{'═' * 35}",
            f"*Source:* `{e(source)}`",
            f"*Account:* `{e(rpt.get('account_key', '-'))}`",
            f"*Positions/Checked/Managed:* `{e(rpt.get('positions', 0))}` / `{e(rpt.get('checked', 0))}` / `{e(rpt.get('managed', 0))}`",
        ]
        for i, a in enumerate(actions[:8], 1):
            old_sl = a.get("old_sl")
            new_sl = a.get("new_sl")
            exec_vol = a.get("executed_close_volume")
            req_vol = a.get("requested_close_volume")
            pos_vol = a.get("position_volume")
            r_now = a.get("r_now")
            age_min = a.get("age_min")
            trigger = a.get("trigger")
            spread_pct = a.get("spread_pct")
            lines += [
                "",
                f"{e(i)}\. `{e(a.get('symbol','-'))}` ticket `{e(a.get('ticket','-'))}`",
                f"Action: `{e(a.get('action','-'))}`  Status: `{e(a.get('status','-'))}`",
                f"Note: `{e(str(a.get('message','') or '')[:220])}`",
            ]
            try:
                rc = a.get("retcode")
                if rc is not None:
                    lines.append(f"Retcode: `{e(int(rc))}`")
            except Exception:
                pass
            if (old_sl is not None) and (new_sl is not None):
                try:
                    lines.append(f"SL: `{e(round(float(old_sl), 6))}` → `{e(round(float(new_sl), 6))}`")
                except Exception:
                    pass
            if req_vol is not None:
                try:
                    pv = "-" if pos_vol is None else str(round(float(pos_vol), 6))
                    rv = str(round(float(req_vol), 6))
                    ev = "-" if exec_vol is None else str(round(float(exec_vol), 6))
                    lines.append(f"Volume: pos `{e(pv)}` req_close `{e(rv)}` exec `{e(ev)}`")
                except Exception:
                    pass
            if (r_now is not None) or (age_min is not None):
                try:
                    rtxt = "-" if r_now is None else f"{float(r_now):.3f}R"
                    atxt = "-" if age_min is None else f"{float(age_min):.1f}m"
                    lines.append(f"Context: r_now `{e(rtxt)}` age `{e(atxt)}`")
                except Exception:
                    pass
            if trigger or spread_pct is not None:
                try:
                    sp = "-" if spread_pct is None else f"{float(spread_pct):.4f}%"
                    lines.append(f"Trigger: `{e(str(trigger or '-'))}` spread `{e(sp)}`")
                except Exception:
                    pass
        lines += [
            "",
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="mt5_manage")

    # ─── Global Market Hours Overview ─────────────────────────────────────────
    def send_market_hours_overview(self, overview: dict, chat_id: Optional[int] = None) -> bool:
        """Send a global market hours status board."""
        e = self._escape
        market_flags = {
            "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
            "JP": "🇯🇵", "HK": "🇭🇰", "TH": "🇹🇭",
            "SG": "🇸🇬", "IN": "🇮🇳", "AU": "🇦🇺",
        }
        lines = [
            f"{'═' * 35}",
            f"🌍 *GLOBAL MARKET STATUS*",
            f"{'═' * 35}",
            f"",
        ]
        for market, info in overview.get("all_markets", {}).items():
            flag = market_flags.get(market, "🌏")
            status = "🟢 OPEN" if info["open"] else "🔴 CLOSED"
            lines.append(
                f"{flag} *{e(market)}*  {status}  "
                f"`{e(info['hours_utc'])} UTC`"
            )
        lines += [
            f"",
            f"_Updated: {e(overview.get('timestamp',''))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature="markets")

    @staticmethod
    def _impact_emoji(impact: str) -> str:
        x = str(impact or "").lower()
        if "high" in x:
            return "🔴 HIGH"
        if "medium" in x:
            return "🟠 MEDIUM"
        return "🟡 LOW"

    def send_economic_calendar_alert(
        self,
        events: list,
        window_minutes: int,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Send upcoming high-impact economic event alert."""
        e = self._escape
        if not events:
            return True
        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="calendar")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=calendar)")
            return False

        def _render_for_target(target: int) -> str:
            _, tz_label = self._tz_for_chat(target)
            lines = [
                f"{'═' * 35}",
                "🗓️ *ECONOMIC CALENDAR ALERT*",
                f"Events in ~`{e(self._fmt_duration_hms(int(window_minutes) * 60))}`",
                f"_Times shown in {e(tz_label)}_",
                f"{'═' * 35}",
            ]
            for i, ev in enumerate(events[:6], 1):
                try:
                    sec_to_event = int((getattr(ev, "time_utc") - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    sec_to_event = max(0, int(getattr(ev, "minutes_to_event", 0))) * 60
                countdown = self._fmt_duration_hms(sec_to_event)
                forecast = str(getattr(ev, "forecast", "") or "-")
                previous = str(getattr(ev, "previous", "") or "-")
                local_time = self._fmt_dt_for_chat(getattr(ev, "time_utc"), target, with_date=False)
                lines += [
                    "",
                    f"*{e(i)}\\. {self._impact_emoji(getattr(ev, 'impact', ''))} {e(getattr(ev, 'currency', '-'))}*",
                    f"`{e(local_time)}` \\(T\\-{e(countdown)}\\)  {e(getattr(ev, 'title', '-'))}",
                    f"Forecast: `{e(forecast)}`  Prev: `{e(previous)}`",
                ]
            lines += [
                "",
                "_Use /calendar to view upcoming events_",
                f"🕐 _{e(self._fmt_dt_for_chat(datetime.now(timezone.utc), target, with_date=True))}_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        any_sent = False
        for target in target_ids:
            any_sent = self._send(_render_for_target(int(target)), chat_id=int(target), feature="calendar") or any_sent
        return any_sent

    def send_economic_calendar_snapshot(
        self,
        events: list,
        lookahead_hours: int = 24,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Send upcoming economic calendar snapshot."""
        e = self._escape
        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="calendar")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=calendar)")
            return False

        def _render_for_target(target: int) -> str:
            _, tz_label = self._tz_for_chat(target)
            if not events:
                return (
                    f"🗓️ *ECONOMIC CALENDAR*\n\n"
                    f"No medium/high impact events in next `{e(lookahead_hours)}h`\\.\n"
                    f"_Times shown in {e(tz_label)}_"
                )
            lines = [
                f"{'═' * 35}",
                "🗓️ *UPCOMING ECONOMIC EVENTS*",
                f"Next `{e(lookahead_hours)}h` \\(Medium/High\\)",
                f"_Times shown in {e(tz_label)}_",
                f"{'═' * 35}",
            ]
            for i, ev in enumerate(events[:10], 1):
                try:
                    sec_to_event = int((getattr(ev, "time_utc") - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    sec_to_event = max(0, int(getattr(ev, "minutes_to_event", 0))) * 60
                countdown = self._fmt_duration_hms(sec_to_event)
                local_time = self._fmt_dt_for_chat(getattr(ev, "time_utc"), target, with_date=True)
                lines += [
                    "",
                    f"*{e(i)}\\. {self._impact_emoji(getattr(ev, 'impact', ''))} {e(getattr(ev, 'currency', '-'))}*",
                    f"`{e(local_time)}` \\(T\\-{e(countdown)}\\)",
                    f"{e(getattr(ev, 'title', '-'))}",
                ]
            lines += [
                "",
                "_Potential volatility for Gold, FX, indices, and risk assets_",
                f"🕐 _{e(self._fmt_dt_for_chat(datetime.now(timezone.utc), target, with_date=True))}_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        any_sent = False
        for target in target_ids:
            any_sent = self._send(_render_for_target(int(target)), chat_id=int(target), feature="calendar") or any_sent
        return any_sent

    def send_macro_news_alert(self, headlines: list, chat_id: Optional[int] = None) -> bool:
        """Send high-impact macro/policy headline alerts."""
        e = self._escape
        if not headlines:
            return True
        from market.macro_news import macro_news

        def _render_macro_alert(batch: list, min_risk_label: str, target: int) -> str:
            _, tz_label = self._tz_for_chat(target)
            lines = [
                f"{'═' * 35}",
                "📰 *MACRO RISK ALERT*",
                f"Policy / headline shock watch \\(Min risk {e(min_risk_label)}\\)",
                f"_Times shown in {e(tz_label)}_",
                f"{'═' * 35}",
            ]
            for i, h in enumerate(batch[:5], 1):
                age_sec = max(0, int((datetime.now(timezone.utc) - h.published_utc).total_seconds()))
                age_hms = self._fmt_duration_hms(age_sec)
                themes = ", ".join(h.themes[:3]) if getattr(h, "themes", None) else "-"
                risk_stars = macro_news.score_to_stars(getattr(h, "score", 0))
                local_time = self._fmt_dt_for_chat(getattr(h, "published_utc"), target, with_date=False)
                phase = self._macro_impact_phase(age_sec)
                lines += [
                    "",
                    f"*{e(i)}\\. Risk {e(risk_stars)}*  `{e(local_time)}` \\(Age {e(age_hms)}\\)",
                    f"{e(h.title)}",
                    f"Themes: `{e(themes)}`",
                    f"Phase: `{e(phase)}`",
                    f"Impact: {e(h.impact_hint)}",
                ]
            lines += [
                "",
                "_Includes Trump/tariff/Fed/geopolitics headline risk_",
                f"🕐 _{e(self._fmt_dt_for_chat(datetime.now(timezone.utc), target, with_date=True))}_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="macro")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=macro)")
            return False

        any_sent = False
        default_min_score = max(1, int(getattr(config, "MACRO_NEWS_MIN_SCORE", 6)))
        for target in target_ids:
            try:
                user_pref = access_manager.get_user_macro_risk_filter(int(target))
            except Exception:
                user_pref = None
            min_score = macro_news.stars_to_min_score(user_pref) or default_min_score
            min_risk = macro_news.score_to_stars(min_score)
            batch = [h for h in headlines if int(getattr(h, "score", 0) or 0) >= int(min_score)]
            if not batch:
                continue
            text = _render_macro_alert(batch, min_risk, int(target))
            any_sent = self._send(text, chat_id=int(target), feature="macro") or any_sent
        return any_sent

    def send_macro_news_snapshot(
        self,
        headlines: list,
        lookback_hours: int = 24,
        chat_id: Optional[int] = None,
        min_risk_stars: Optional[str] = None,
    ) -> bool:
        """Send latest macro headline snapshot."""
        e = self._escape
        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="macro")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=macro)")
            return False

        from market.macro_news import macro_news

        def _resolve_risk_label(batch: list, requested: Optional[str]) -> str:
            label = str(requested or "").strip()
            if label:
                return label
            try:
                if batch:
                    min_seen = min(int(getattr(h, "score", 0) or 0) for h in batch)
                    return macro_news.score_to_stars(min_seen)
                return macro_news.score_to_stars(int(getattr(config, "MACRO_NEWS_MIN_SCORE", 6)))
            except Exception:
                return "-"

        def _render_for_target(target: int, batch: list, risk_label_local: str) -> str:
            _, tz_label = self._tz_for_chat(target)
            if not batch:
                return (
                    f"📰 *MACRO RISK WATCH*\n\n"
                    f"No macro headlines at min risk {e(risk_label_local)} in last `{e(lookback_hours)}h`\\.\n"
                    f"_Times shown in {e(tz_label)}_"
                )
            lines = [
                f"{'═' * 35}",
                "📰 *MACRO RISK WATCH*",
                f"Last `{e(lookback_hours)}h` headlines \\(Min risk {e(risk_label_local)}\\)",
                f"_Times shown in {e(tz_label)}_",
                f"{'═' * 35}",
            ]
            for i, h in enumerate(batch[:8], 1):
                risk_stars = macro_news.score_to_stars(getattr(h, "score", 0))
                local_time = self._fmt_dt_for_chat(getattr(h, "published_utc"), target, with_date=True)
                age_sec = max(0, int((datetime.now(timezone.utc) - getattr(h, "published_utc")).total_seconds()))
                age_hms = self._fmt_duration_hms(age_sec)
                phase = self._macro_impact_phase(age_sec)
                lines += [
                    "",
                    f"*{e(i)}\\. Risk {e(risk_stars)}*  `{e(local_time)}`",
                    f"{e(h.title)}",
                    f"Age: `{e(age_hms)}`  Phase: `{e(phase)}`",
                    f"Impact: {e(h.impact_hint)}",
                ]
            lines += [
                "",
                "_Use /macro \\*, /macro \\*\\*, /macro \\*\\*\\* to filter risk level_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        any_sent = False
        for target in target_ids:
            # If snapshot called without explicit filter, use saved user preference.
            batch = list(headlines or [])
            risk_label_local = str(min_risk_stars or "").strip()
            if not risk_label_local:
                try:
                    pref = access_manager.get_user_macro_risk_filter(int(target))
                except Exception:
                    pref = None
                min_score_local = macro_news.stars_to_min_score(pref)
                if min_score_local is not None:
                    batch = [h for h in batch if int(getattr(h, "score", 0) or 0) >= int(min_score_local)]
                    risk_label_local = macro_news.score_to_stars(min_score_local)
            risk_label_local = _resolve_risk_label(batch, risk_label_local)
            any_sent = self._send(
                _render_for_target(int(target), batch, risk_label_local),
                chat_id=int(target),
                feature="macro",
            ) or any_sent
        return any_sent

    def send_macro_impact_report(
        self,
        report: dict,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Send post-news impact tracker report."""
        e = self._escape
        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="macro_report")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=macro_report)")
            return False

        entries = list((report or {}).get("entries", []) or [])
        hours = int((report or {}).get("hours", 24) or 24)
        risk_label = str((report or {}).get("min_risk_stars", "") or "").strip() or "*"

        def _fmt_pct(v) -> str:
            try:
                x = float(v)
            except Exception:
                return "-"
            sign = "+" if x >= 0 else ""
            return f"{sign}{x:.2f}%"

        def _asset_line(asset: str, rec: dict) -> str:
            if not rec:
                return f"{asset}: -"
            parts = []
            for hz in (5, 15, 60):
                val = rec.get(f"t{hz}_pct")
                parts.append(f"T\\+{hz}:{e(_fmt_pct(val))}")
            cls_h = str(rec.get("classification_human") or rec.get("classification") or "-")
            return f"{e(asset)}  {' / '.join(parts)}  `{e(cls_h)}`"

        def _headline_status_emoji(label: str) -> str:
            x = str(label or "").lower()
            if x in {"impact_confirmed", "impact_developing"}:
                return "✅"
            if x in {"priced_in", "faded"}:
                return "🟡"
            if x in {"no_clear_impact"}:
                return "⚪"
            if x in {"pending"}:
                return "⏳"
            return "🔎"

        def _render_for_target(target: int) -> str:
            _, tz_label = self._tz_for_chat(target)
            if not entries:
                return (
                    "🧪 *POST\\-NEWS IMPACT REPORT*\n\n"
                    f"No tracked macro headlines for last `{e(hours)}h` at min risk {e(risk_label)}\\.\n"
                    f"_Times shown in {e(tz_label)}_"
                )
            lines = [
                f"{'═' * 35}",
                "🧪 *POST\\-NEWS IMPACT REPORT*",
                f"Last `{e(hours)}h` \\(Min risk {e(risk_label)}\\)",
                f"_Times shown in {e(tz_label)}_",
                f"{'═' * 35}",
            ]
            for i, item in enumerate(entries[:5], 1):
                title = str(item.get("title") or "-")
                risk = str(item.get("risk_stars") or "-")
                local_time = self._fmt_dt_for_chat(item.get("published_utc"), target, with_date=True)
                age_sec = int(item.get("age_sec", 0) or 0)
                age_hms = self._fmt_duration_hms(age_sec)
                cls_h = str(item.get("classification_human") or item.get("classification") or "-")
                summary = str(item.get("reaction_summary") or "-")
                label = str(item.get("classification") or "")
                lines += [
                    "",
                    f"*{e(i)}\\. {_headline_status_emoji(label)} Risk {e(risk)}*  `{e(local_time)}`",
                    f"{e(title)}",
                    f"Age: `{e(age_hms)}`  Result: `{e(cls_h)}`",
                    f"Summary: {e(summary)}",
                ]
                assets = dict(item.get("assets") or {})
                for asset in ("XAUUSD", "BTCUSD", "ETHUSD", "US500"):
                    if asset in assets:
                        lines.append(_asset_line(asset, assets.get(asset) or {}))
            theme_stats = list((report or {}).get("theme_stats", []) or [])
            if theme_stats:
                lines += ["", "*Theme Stats*"]
                adaptive = dict((report or {}).get("adaptive_weights", {}) or {})
                for ts in theme_stats[:3]:
                    w = adaptive.get(str(ts.get("theme")), {})
                    w_mult = w.get("weight_mult")
                    w_txt = f" x{float(w_mult):.2f}" if w_mult is not None else ""
                    lines.append(
                        f"• `{e(str(ts.get('theme'))[:18])}`{e(w_txt)} conf {e(ts.get('confirmed_rate'))}% / priced in {e(ts.get('priced_in_rate'))}% / no clear {e(ts.get('no_clear_rate'))}%"
                    )
            lines += [
                "",
                "_Use /macro_report \\*\\*\\* or /macro_report 48h for focused review_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        any_sent = False
        for target in target_ids:
            any_sent = self._send(_render_for_target(int(target)), chat_id=int(target), feature="macro_report") or any_sent
        return any_sent

    def send_macro_weights_report(
        self,
        report: dict,
        chat_id: Optional[int] = None,
    ) -> bool:
        """Send adaptive macro theme-weight diagnostics."""
        e = self._escape
        target_ids = self._resolve_target_chat_ids(chat_id=chat_id, feature="macro_weights")
        if not target_ids:
            logger.warning("[Telegram] no target chat IDs (feature=macro_weights)")
            return False

        rows = list((report or {}).get("rows", []) or [])
        top_n = int((report or {}).get("top_n", 8) or 8)
        thresholds = dict((report or {}).get("thresholds", {}) or {})
        alert_cfg = dict((report or {}).get("alert_adaptive", {}) or {})
        enabled = bool((report or {}).get("enabled", True))
        refresh_result = dict((report or {}).get("refresh_result", {}) or {})

        def _fmt_pct(v) -> str:
            try:
                return f"{float(v):.1f}%"
            except Exception:
                return "-"

        def _render() -> str:
            gen = (report or {}).get("generated_at_utc")
            gen_txt = "-"
            try:
                if isinstance(gen, datetime):
                    gen_txt = gen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass
            lines = [
                f"{'═' * 35}",
                "⚖️ *ADAPTIVE MACRO WEIGHTS*",
                f"Status: `{'ENABLED' if enabled else 'DISABLED'}`  Stored: `{e((report or {}).get('stored_count', 0))}`  Runtime: `{e((report or {}).get('runtime_count', 0))}`",
                f"Top `{e(top_n)}` themes by weight deviation",
                f"{'═' * 35}",
            ]
            if refresh_result:
                lines.append(
                    f"Refresh: `{e(refresh_result.get('status', 'ok'))}`  updated `{e(refresh_result.get('updated', refresh_result.get('count', 0)))} theme(s)`"
                )
            if not rows:
                lines += [
                    "No stored adaptive weights yet\\.",
                    "Run /macro\\_report a few times and allow tracker sync to collect samples\\.",
                ]
            else:
                for i, r in enumerate(rows, 1):
                    theme = str(r.get("theme") or "-")
                    mult = float(r.get("weight_mult", 1.0) or 1.0)
                    base_score = float(r.get("base_score", 0.0) or 0.0)
                    eff_score = float(r.get("effective_score", base_score * mult) or 0.0)
                    samples = int(r.get("sample_count", 0) or 0)
                    conf = _fmt_pct(r.get("confirmed_rate"))
                    nc = _fmt_pct(r.get("no_clear_rate"))
                    lines += [
                        "",
                        f"*{e(i)}\\. `{e(theme)}`  x`{e(f'{mult:.2f}')}`*",
                        f"base `{e(f'{base_score:.1f}')}` → eff `{e(f'{eff_score:.2f}')}`  samples `{e(samples)}`",
                        f"confirmed `{e(conf)}`  no\\-clear `{e(nc)}`",
                    ]
            lines += [
                "",
                "*Adaptive Weight Config*",
                f"learn min samples `{e(thresholds.get('min_samples', '-'))}`  bounds `{e(thresholds.get('min_mult', '-'))}` to `{e(thresholds.get('max_mult', '-'))}`  lookback `{e(thresholds.get('update_hours', '-'))}h`",
                "*Phase 3 Alert Priority*",
                f"enabled `{e(alert_cfg.get('enabled', '-'))}`  min theme mult `{e(alert_cfg.get('min_theme_mult', '-'))}`  skip no\\-clear at/above `{e(alert_cfg.get('skip_no_clear_rate', '-'))}%`  ultra floor `{e(alert_cfg.get('ultra_score_floor', '-'))}`",
                "",
                f"🕐 `{e(gen_txt)}`",
                "_Tip: /macro\\_weights refresh recomputes and reapplies theme weights now_",
                f"{'═' * 35}",
            ]
            return "\n".join(lines)

        any_sent = False
        for target in target_ids:
            any_sent = self._send(_render(), chat_id=int(target), feature="macro_weights") or any_sent
        return any_sent

    def send_vi_stock_summary(self, opportunities: list, chat_id: Optional[int] = None, region_label: str = "🇺🇸 US", feature_override: Optional[str] = None) -> bool:
        """Send VI value + trend stock candidates."""
        e = self._escape
        region = str(region_label or "🇺🇸 US")
        feature_name = str(feature_override or "scan_vi")
        if not opportunities:
            return self._send(
                f"📊 *{e(region)} VALUE \\+ TREND SCANNER*\n\nNo qualified VI candidates right now\\.",
                chat_id=chat_id,
                feature=feature_name,
            )

        def _profile_label(raw: dict) -> str:
            p = str(raw.get("vi_primary_profile", "") or "").upper()
            if p == "BUFFETT":
                return "🏛️ Buffett-inspired compounder"
            if p == "TURNAROUND":
                return "🔄 Turnaround / re-rating"
            return "⚖️ Blend (quality + re-rating)"

        def _profile_bucket(raw: dict) -> str:
            p = str(raw.get("vi_primary_profile", "") or "").upper()
            return p if p in {"BUFFETT", "TURNAROUND"} else "BLEND"

        groups = {"BUFFETT": [], "TURNAROUND": [], "BLEND": []}
        for opp in opportunities[:10]:
            raw = dict(getattr(opp.signal, "raw_scores", {}) or {})
            groups[_profile_bucket(raw)].append(opp)

        lines = [
            f"{'═' * 35}",
            f"{e(region)} *VALUE \\+ TREND SCANNER*",
            f"Top *{e(len(opportunities))}* candidates \\(Buffett/Turnaround separated\\)",
            (
                f"Mix: Buffett `{e(len(groups['BUFFETT']))}` "
                f"\\| Turnaround `{e(len(groups['TURNAROUND']))}` "
                f"\\| Blend `{e(len(groups['BLEND']))}`"
            ),
            f"{'═' * 35}",
        ]
        section_meta = [
            ("BUFFETT", "🏛️ *Buffett\\-Inspired Compounders*"),
            ("TURNAROUND", "🔄 *Turnaround / Multi\\-bagger Candidates*"),
            ("BLEND", "⚖️ *Blend Candidates*"),
        ]
        idx = 0
        for key, title in section_meta:
            bucket = groups.get(key) or []
            if not bucket:
                continue
            lines += ["", title]
            for opp in bucket:
                idx += 1
                s = opp.signal
                raw = dict(getattr(s, "raw_scores", {}) or {})
                vi_total = float(raw.get("vi_total_score", 0.0))
                vi_value = float(raw.get("vi_value_score", 0.0))
                vi_trend = float(raw.get("vi_trend_score", 0.0))
                comp_score = float(raw.get("vi_compounder_score", 0.0))
                turn_score = float(raw.get("vi_turnaround_score", 0.0))
                primary_score = float(raw.get("vi_primary_score", 0.0))
                pe_text = "-"
                if opp.pe_ratio is not None:
                    pe_text = f"{float(opp.pe_ratio):.1f}"
                pb = raw.get("vi_metric_price_to_book") or raw.get("vi_metric_pb")
                pb_text = "-"
                try:
                    if pb is not None:
                        pb_text = f"{float(pb):.2f}"
                except Exception:
                    pb_text = str(pb)
                rg = raw.get("vi_metric_revenue_growth")
                eg = raw.get("vi_metric_earnings_growth")
                rg_text = "-"
                eg_text = "-"
                try:
                    if rg is not None:
                        rg_text = f"{float(rg)*100:.1f}%"
                except Exception:
                    rg_text = str(rg)
                try:
                    if eg is not None:
                        eg_text = f"{float(eg)*100:.1f}%"
                except Exception:
                    eg_text = str(eg)
                mcap_bucket = str(raw.get("vi_market_cap_bucket", "-") or "-")
                range_pos = raw.get("vi_range_position_pct")
                range_text = "-"
                try:
                    if range_pos is not None:
                        range_text = f"{float(range_pos):.1f}%"
                except Exception:
                    range_text = str(range_pos)

                lines += [
                    "",
                    f"*{e(idx)}\\. {'📈' if s.direction == 'long' else '📉'} {e(opp.market or '-')} {e(s.symbol)}*",
                    f"   Profile: `{e(_profile_label(raw))}`  Primary `{e(round(primary_score,1))}`",
                    f"   VI Score: `{e(round(vi_total, 1))}`  \\(Value `{e(round(vi_value,1))}` \\| Trend `{e(round(vi_trend,1))}`\\)",
                    f"   Buffett `{e(round(comp_score,1))}` \\| Turnaround `{e(round(turn_score,1))}` \\| Cap `{e(mcap_bucket)}` \\| 52w Pos `{e(range_text)}`",
                    f"   Entry: `{e(self._fmt_price(s.entry))}`  SL: `{e(self._fmt_price(s.stop_loss))}`  TP2: `{e(self._fmt_price(s.take_profit_2))}`",
                    f"   Conf: `{e(round(float(s.confidence),1))}%`  Vol: `{e(round(opp.vol_vs_avg,2))}x`  P/E: `{e(pe_text)}`  P/B: `{e(pb_text)}`",
                    f"   Growth: Rev `{e(rg_text)}` \\| EPS `{e(eg_text)}`  Setup WR `{e(round(opp.setup_win_rate*100,1))}%`",
                ]
                mos_pct = raw.get("vi_metric_mos_pct")
                mos_band = str(raw.get("vi_metric_mos_band", "-") or "-")
                iv_est = raw.get("vi_metric_intrinsic_value_est")
                oey = raw.get("vi_metric_owner_earnings_yield")
                fcfy = raw.get("vi_metric_fcf_yield")
                iv_parts = []
                try:
                    if iv_est is not None:
                        iv_parts.append(f"Intrinsic\\~ `{e(self._fmt_price(float(iv_est)))}`")
                except Exception:
                    pass
                try:
                    if mos_pct is not None:
                        iv_parts.append(f"MOS `{e(round(float(mos_pct)*100,1))}%` \\({e(mos_band)}\\)")
                except Exception:
                    pass
                try:
                    if oey is not None:
                        iv_parts.append(f"OwnerYield `{e(round(float(oey)*100,1))}%`")
                    elif fcfy is not None:
                        iv_parts.append(f"FCFYield `{e(round(float(fcfy)*100,1))}%`")
                except Exception:
                    pass
                if iv_parts:
                    lines.append("   Intrinsic/MOS: " + "  \\| ".join(iv_parts))
                reasons = [str(x) for x in (raw.get("vi_reasons_detailed") or []) if str(x).strip()]
                if reasons:
                    lines.append("   Reasons:")
                    for r in reasons[:5]:
                        lines.append(f"   • {e(r)}")
        lines += [
            "",
            "_Framework: Buffett\\-inspired quality/value \\+ turnaround re\\-rating heuristics \\(bounded, not guaranteed\\)_",
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            f"{'═' * 35}",
        ]
        return self._send("\n".join(lines), chat_id=chat_id, feature=feature_name)

    def send_startup_message(self) -> bool:
        lines = [
            "🦞 *DEXTER PRO — ONLINE*",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "Autonomous AI trading agent activated\\.",
            "",
            "✅ XAUUSD \\(Gold\\) Scanner — Active",
            "✅ Crypto Sniper \\(Top 50\\) — Active",
            "✅ 🌍 Global Stock Scanner — Active",
            "   🇺🇸 US S&P500 \\+ NASDAQ",
            "   🇬🇧 UK FTSE100",
            "   🇩🇪 Germany DAX40",
            "   🇫🇷 France CAC40",
            "   🇯🇵 Japan Nikkei225",
            "   🇭🇰 Hong Kong Hang Seng",
            "   🇨🇳 China Stocks",
            "   🇹🇭 Thailand SET50",
            "   🇸🇬 Singapore STI",
            "   🇮🇳 India Nifty50",
            "   🇦🇺 Australia ASX",
            "✅ SMC Analysis Engine — Ready",
            "✅ AI Brain — Connected",
            "✅ Economic Calendar Alerts — Active",
            "✅ 🇺🇸 US VI Scanner — On\\-demand \\(/scan\\_vi\\)",
            "✅ Telegram Alerts — Online",
            "",
            "⚡ Commands:",
            "/scan\\_gold — Scan XAUUSD now",
            "/scan\\_crypto — Scan top crypto now",
            "/scan\\_stocks — Scan all open markets",
            "/scan\\_thai — Scan Thailand SET50",
            "/scan\\_thai\\_vi — Thailand value \\+ trend ideas \\(off\\-hours\\)",
            "/scan\\_us — US open top 10 daytrade plan",
            "/scan\\_us\\_open — US open top 10 daytrade plan",
            "/us\\_open\\_report — Today's US open signal quality recap",
            "/us\\_open\\_dashboard — Today's US open trader dashboard",
            "/signal\\_dashboard — Daily all\\-markets signal trader dashboard",
            "/scan\\_vi — US value \\+ trend stock ideas",
            "/scan\\_vi\\_buffett — US Buffett\\-style compounders",
            "/scan\\_vi\\_turnaround — US turnaround multi\\-bagger candidates",
            "/monitor\\_us — US open smart monitor update",
            "/calendar — Upcoming economic events",
            "/macro \\[\\*\\|\\*\\*\\|\\*\\*\\*\\] — Macro risk headlines \\(Trump/Fed/tariff\\)",
            "/macro\\_report \\[\\*\\|\\*\\*\\|\\*\\*\\*\\] \\[24h\\] — Post\\-news impact tracker",
            "/macro\\_weights \\[refresh\\] \\[top10\\] — Adaptive macro theme weights",
            "/tz \\[UTC\\+7\\|\\+07:00\\|bangkok\\] — Set your news timezone",
            "/mt5\\_status — MT5 bridge/execution status",
            "/mt5\\_affordable \\[ok\\] \\[crypto\\|fx\\|metal\\|index\\] — Live symbols affordable now \\(margin/spread/policy\\)",
            "/stock\\_mt5\\_filter \\[on\\|off\\|status\\] — Toggle stock broker\\-match filter \\(admin\\)",
            "/mt5\\_history \\[symbol\\] \\[24h\\] — Closed trade history \\(TP/SL/manual\\)",
            "/mt5\\_autopilot — MT5 risk governor \\+ forward\\-test journal",
            "/mt5\\_walkforward — Walk\\-forward validation \\+ canary",
            "/mt5\\_manage \\[watch\\] — Run PM cycle or inspect per\\-ticket watch state",
            "/mt5\\_pm\\_learning \\[30d\\] \\[top8\\] — PM action effectiveness by symbol/action",
            "/mt5\\_plan \\[symbol\\] — Preview adaptive RR/SL/TP/size before MT5 execution",
            "/mt5\\_policy \\[show\\|keys\\|preset\\|set\\|reset\\] — Per\\-account canary/risk policy",
            "/mt5\\_backtest — MT5 history backtest \\+ neural status",
            "/markets — Global market hours",
            "/gold\\_overview — Gold market overview",
            "/research \\[question\\] — Deep AI research",
            "/status — System status",
            "",
            "_The lobster is watching ALL markets 24/7\\._",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        return self._send("\n".join(lines))

    def send_error(self, error_msg: str) -> bool:
        return self._send(f"❌ *DEXTER PRO ERROR*\n\n`{self._escape(error_msg)}`")

    # ─── Tiger Daily Performance Summary ──────────────────────────────────────
    def send_daily_performance_summary(self, chat_id=None) -> bool:
        """Send daily Tiger Hunter performance summary for subscribers."""
        try:
            from api.signal_store import signal_store as _ss
            if _ss is None:
                return False
            stats = _ss.get_performance_stats()
            curve = _ss.get_equity_curve(initial_equity=15.0)
        except Exception:
            return False

        e = self._escape
        if stats["completed_signals"] == 0:
            return False

        eq = curve[-1]["equity"] if curve else 15.0
        growth = ((eq - 15.0) / 15.0 * 100) if eq > 0 else 0.0
        wr = stats["win_rate"]
        wr_e = "🟢" if wr >= 60 else ("🟡" if wr >= 50 else "🔴")
        pf = int(min(1.0, eq / 1_000_000.0) * 20)
        bar = "█" * pf + "░" * (20 - pf)

        sep1 = "═" * 35
        sep2 = "─" * 30
        ts = stats.get("tiger_stats", {})
        lns = [
            sep1, "🐯 *TIGER HUNTER DAILY REPORT*", sep1, "",
            "📊 *PERFORMANCE*", sep2,
            f"{wr_e} Win Rate: `{e(f'{wr:.1f}')}%`",
            f"💰 P&L: `${e(f'{stats['total_pnl_usd']:.2f}')}`",
            f"📈 Profit Factor: `{e(f'{stats['profit_factor']:.2f}')}`",
            "",
            "💎 *$15 → $1M*", sep2,
            f"💵 Equity: `${e(f'{eq:.2f}')}`  Growth: `{e(f'{growth:+.1f}')}%`",
            f"`{bar}`",
            "",
        ]
        if ts:
            lns += [
                "🐯 *TIGER STATS*", sep2,
                f"🛡️ Anti\\-Sweep: `{ts.get('anti_sweep_sl_pct',0):.0f}%`"
                f"  ⚡ Liq TP: `{ts.get('liquidity_tp_pct',0):.0f}%`",
                "",
            ]
        lns += [
            f"🕐 _{e(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
            sep1,
            "_🐯 Tiger Hunter AI \\| Dexter Pro V3_",
        ]
        return self._send("\n".join(lns), chat_id=chat_id, feature="daily_report")


notifier = TelegramNotifier()

"""
config.py - Central configuration for Dexter Pro
Loads from .env.local first (highest priority), then falls back to .env
"""
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# ── Load order: .env.local → .env ────────────────────────────────────────────
_BASE = Path(__file__).parent

# .env.local overrides everything — this is your real config file
_env_local = _BASE / ".env.local"
if _env_local.exists():
    load_dotenv(dotenv_path=_env_local, override=True)
    print("[Config] Loaded: .env.local")
else:
    # Fallback to plain .env if present
    _env_file = _BASE / ".env"
    if _env_file.exists():
        load_dotenv(dotenv_path=_env_file, override=True)
        print("[Config] Loaded: .env (tip: rename to .env.local)")
    else:
        print("[Config] WARNING: No .env.local found - using system environment variables only")


class Config:
    # ── AI Brain ───────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    GROQ_API_KEY:      str = os.getenv("GROQ_API_KEY", "")
    GEMINI_API_KEY:    str = os.getenv("GEMINI_API_KEY", "")
    OPENAI_API_KEY:    str = os.getenv("OPENAI_API_KEY", "")     # optional fallback
    AI_MODEL:          str = os.getenv("AI_MODEL", "claude-sonnet-4-6")
    GROQ_MODEL:        str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GEMINI_MODEL:      str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    AI_PROVIDER:       str = os.getenv("AI_PROVIDER", "auto")  # auto|groq|gemini|anthropic

    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "8536612154:AAGMbUo2mH45TSyWV1Eq22NX_-M_ZnlnPwA"   # @mrgeon8n_bot default
    )
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ADMIN_IDS: str = os.getenv("TELEGRAM_ADMIN_IDS", "")  # comma-separated user IDs
    TELEGRAM_BROADCAST_SIGNALS: bool = os.getenv("TELEGRAM_BROADCAST_SIGNALS", "1").strip().lower() in ("1", "true", "yes", "on")
    TELEGRAM_AUTO_BLOCK_UNREACHABLE: bool = os.getenv("TELEGRAM_AUTO_BLOCK_UNREACHABLE", "1").strip().lower() in ("1", "true", "yes", "on")

    # ── Crypto Exchange ────────────────────────────────────────────────────────
    CRYPTO_EXCHANGE:  str = os.getenv("CRYPTO_EXCHANGE", "binance")
    BINANCE_API_KEY:  str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET:   str = os.getenv("BINANCE_SECRET", "")
    BYBIT_API_KEY:    str = os.getenv("BYBIT_API_KEY", "")
    BYBIT_SECRET:     str = os.getenv("BYBIT_SECRET", "")

    # ── Scanner Intervals (seconds) ────────────────────────────────────────────
    CRYPTO_SCAN_INTERVAL: int = int(os.getenv("CRYPTO_SCAN_INTERVAL", "300"))    # 5 min
    XAUUSD_SCAN_INTERVAL: int = int(os.getenv("XAUUSD_SCAN_INTERVAL", "900"))    # 15 min
    FX_SCAN_INTERVAL:     int = int(os.getenv("FX_SCAN_INTERVAL", "300"))        # 5 min
    STOCK_SCAN_INTERVAL:  int = int(os.getenv("STOCK_SCAN_INTERVAL",  "1800"))   # 30 min
    US_OPEN_SMART_INTERVAL_MIN: int = int(os.getenv("US_OPEN_SMART_INTERVAL_MIN", "10"))
    US_OPEN_SMART_PREMARKET_LEAD_MIN: int = int(os.getenv("US_OPEN_SMART_PREMARKET_LEAD_MIN", "60"))
    US_OPEN_SMART_POST_OPEN_MAX_MIN: int = int(os.getenv("US_OPEN_SMART_POST_OPEN_MAX_MIN", "120"))
    US_OPEN_SMART_ALWAYS_REPORT: bool = os.getenv("US_OPEN_SMART_ALWAYS_REPORT", "0").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SMART_NO_OPP_PING_MIN: int = int(os.getenv("US_OPEN_SMART_NO_OPP_PING_MIN", "15"))
    US_OPEN_SESSION_CHECKIN_ENABLED: bool = os.getenv("US_OPEN_SESSION_CHECKIN_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_QUALITY_REPORT_INTERVAL_MIN: int = int(os.getenv("US_OPEN_QUALITY_REPORT_INTERVAL_MIN", "15"))
    US_OPEN_MOOD_STOP_ENABLED: bool = os.getenv("US_OPEN_MOOD_STOP_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_MOOD_CHECK_START_MIN: int = int(os.getenv("US_OPEN_MOOD_CHECK_START_MIN", "45"))
    US_OPEN_MOOD_WEAK_CYCLES_TO_STOP: int = int(os.getenv("US_OPEN_MOOD_WEAK_CYCLES_TO_STOP", "3"))
    US_OPEN_SYMBOL_ALERT_COOLDOWN_MIN: int = int(os.getenv("US_OPEN_SYMBOL_ALERT_COOLDOWN_MIN", "20"))
    US_OPEN_RECORD_NEW_SYMBOLS_ONLY: bool = os.getenv("US_OPEN_RECORD_NEW_SYMBOLS_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_MACRO_FREEZE_ENABLED: bool = os.getenv("US_OPEN_MACRO_FREEZE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_MACRO_FREEZE_MIN_SCORE: int = int(os.getenv("US_OPEN_MACRO_FREEZE_MIN_SCORE", "8"))
    US_OPEN_MACRO_FREEZE_MAX_AGE_MIN: int = int(os.getenv("US_OPEN_MACRO_FREEZE_MAX_AGE_MIN", "45"))
    US_OPEN_MACRO_FREEZE_PRIORITY_ONLY: bool = os.getenv("US_OPEN_MACRO_FREEZE_PRIORITY_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_CIRCUIT_BREAKER_ENABLED: bool = os.getenv("US_OPEN_CIRCUIT_BREAKER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_CIRCUIT_BREAKER_CHECK_START_MIN: int = int(os.getenv("US_OPEN_CIRCUIT_BREAKER_CHECK_START_MIN", "30"))
    US_OPEN_CIRCUIT_BREAKER_MIN_RESOLVED: int = int(os.getenv("US_OPEN_CIRCUIT_BREAKER_MIN_RESOLVED", "8"))
    US_OPEN_CIRCUIT_BREAKER_MAX_WIN_RATE: float = float(os.getenv("US_OPEN_CIRCUIT_BREAKER_MAX_WIN_RATE", "25"))
    US_OPEN_CIRCUIT_BREAKER_MIN_SL: int = int(os.getenv("US_OPEN_CIRCUIT_BREAKER_MIN_SL", "4"))
    US_OPEN_CIRCUIT_BREAKER_MAX_AVG_R: float = float(os.getenv("US_OPEN_CIRCUIT_BREAKER_MAX_AVG_R", "-0.50"))
    US_OPEN_SETUP_WEIGHTING_ENABLED: bool = os.getenv("US_OPEN_SETUP_WEIGHTING_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SETUP_STATS_MIN_RESOLVED: int = int(os.getenv("US_OPEN_SETUP_STATS_MIN_RESOLVED", "6"))
    US_OPEN_SETUP_POOR_WR: float = float(os.getenv("US_OPEN_SETUP_POOR_WR", "35"))
    US_OPEN_SETUP_POOR_NET_R: float = float(os.getenv("US_OPEN_SETUP_POOR_NET_R", "-1.0"))
    US_OPEN_SETUP_HARD_BLOCK_ENABLED: bool = os.getenv("US_OPEN_SETUP_HARD_BLOCK_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SETUP_HARD_BLOCK_MIN_RESOLVED: int = int(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MIN_RESOLVED", "10"))
    US_OPEN_SETUP_HARD_BLOCK_MAX_WR: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_WR", "8"))
    US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R", "-2.5"))
    US_OPEN_SETUP_BOOST_ENABLED: bool = os.getenv("US_OPEN_SETUP_BOOST_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SETUP_BOOST_MIN_RESOLVED: int = int(os.getenv("US_OPEN_SETUP_BOOST_MIN_RESOLVED", "8"))
    US_OPEN_SETUP_BOOST_MIN_WR: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_WR", "58"))
    US_OPEN_SETUP_BOOST_MIN_NET_R: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_NET_R", "0.8"))
    US_OPEN_SETUP_MAX_PENALTY_CHOCH: float = float(os.getenv("US_OPEN_SETUP_MAX_PENALTY_CHOCH", "8.0"))
    US_OPEN_SETUP_MAX_PENALTY_BB_SQUEEZE: float = float(os.getenv("US_OPEN_SETUP_MAX_PENALTY_BB_SQUEEZE", "6.0"))
    US_OPEN_SETUP_MAX_PENALTY_OB_BOUNCE: float = float(os.getenv("US_OPEN_SETUP_MAX_PENALTY_OB_BOUNCE", "3.0"))
    US_OPEN_SETUP_MAX_BOOST_OB_BOUNCE: float = float(os.getenv("US_OPEN_SETUP_MAX_BOOST_OB_BOUNCE", "2.0"))
    US_OPEN_SYMBOL_SESSION_LOSS_CAP_ENABLED: bool = os.getenv("US_OPEN_SYMBOL_SESSION_LOSS_CAP_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SYMBOL_SESSION_LOSS_CAP_MIN_RESOLVED: int = int(os.getenv("US_OPEN_SYMBOL_SESSION_LOSS_CAP_MIN_RESOLVED", "4"))
    US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_NEG_R: float = float(os.getenv("US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_NEG_R", "-2.0"))
    US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_LOSSES: int = int(os.getenv("US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_LOSSES", "4"))
    US_OPEN_SETUP_POOR_WR_CORE: float = float(os.getenv("US_OPEN_SETUP_POOR_WR_CORE", str(US_OPEN_SETUP_POOR_WR)))
    US_OPEN_SETUP_POOR_WR_LATE: float = float(os.getenv("US_OPEN_SETUP_POOR_WR_LATE", str(US_OPEN_SETUP_POOR_WR)))
    US_OPEN_SETUP_POOR_NET_R_CORE: float = float(os.getenv("US_OPEN_SETUP_POOR_NET_R_CORE", str(US_OPEN_SETUP_POOR_NET_R)))
    US_OPEN_SETUP_POOR_NET_R_LATE: float = float(os.getenv("US_OPEN_SETUP_POOR_NET_R_LATE", str(US_OPEN_SETUP_POOR_NET_R)))
    US_OPEN_SETUP_HARD_BLOCK_MAX_WR_CORE: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_WR_CORE", str(US_OPEN_SETUP_HARD_BLOCK_MAX_WR)))
    US_OPEN_SETUP_HARD_BLOCK_MAX_WR_LATE: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_WR_LATE", str(US_OPEN_SETUP_HARD_BLOCK_MAX_WR)))
    US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R_CORE: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R_CORE", str(US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R)))
    US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R_LATE: float = float(os.getenv("US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R_LATE", str(US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R)))
    US_OPEN_SETUP_BOOST_MIN_WR_CORE: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_WR_CORE", str(US_OPEN_SETUP_BOOST_MIN_WR)))
    US_OPEN_SETUP_BOOST_MIN_WR_LATE: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_WR_LATE", str(US_OPEN_SETUP_BOOST_MIN_WR)))
    US_OPEN_SETUP_BOOST_MIN_NET_R_CORE: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_NET_R_CORE", str(US_OPEN_SETUP_BOOST_MIN_NET_R)))
    US_OPEN_SETUP_BOOST_MIN_NET_R_LATE: float = float(os.getenv("US_OPEN_SETUP_BOOST_MIN_NET_R_LATE", str(US_OPEN_SETUP_BOOST_MIN_NET_R)))
    US_OPEN_SYMBOL_RECOVERY_ENABLED: bool = os.getenv("US_OPEN_SYMBOL_RECOVERY_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    US_OPEN_SYMBOL_RECOVERY_MIN_CONFIDENCE: float = float(os.getenv("US_OPEN_SYMBOL_RECOVERY_MIN_CONFIDENCE", "72"))
    US_OPEN_SYMBOL_RECOVERY_MIN_VOL_RATIO: float = float(os.getenv("US_OPEN_SYMBOL_RECOVERY_MIN_VOL_RATIO", "1.15"))
    US_OPEN_SYMBOL_RECOVERY_MIN_SETUP_WR: float = float(os.getenv("US_OPEN_SYMBOL_RECOVERY_MIN_SETUP_WR", "0.58"))
    US_OPEN_SYMBOL_RECOVERY_MIN_RANK_SCORE: float = float(os.getenv("US_OPEN_SYMBOL_RECOVERY_MIN_RANK_SCORE", "55"))
    US_OPEN_SYMBOL_RECOVERY_MAX_PER_SYMBOL: int = int(os.getenv("US_OPEN_SYMBOL_RECOVERY_MAX_PER_SYMBOL", "1"))
    US_OPEN_SYMBOL_RECOVERY_COOLDOWN_MIN: int = int(os.getenv("US_OPEN_SYMBOL_RECOVERY_COOLDOWN_MIN", "25"))
    XAUUSD_ALERT_COOLDOWN_SEC: int = int(os.getenv("XAUUSD_ALERT_COOLDOWN_SEC", "600"))
    XAUUSD_SMART_TRAP_GUARD_ENABLED: bool = os.getenv("XAUUSD_SMART_TRAP_GUARD_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    XAUUSD_TRAP_NEAR_ROUND_ATR: float = float(os.getenv("XAUUSD_TRAP_NEAR_ROUND_ATR", "0.35"))
    XAUUSD_TRAP_NO_CHASE_EMA21_ATR: float = float(os.getenv("XAUUSD_TRAP_NO_CHASE_EMA21_ATR", "1.00"))
    XAUUSD_TRAP_NO_CHASE_BB_PCT: float = float(os.getenv("XAUUSD_TRAP_NO_CHASE_BB_PCT", "0.92"))
    XAUUSD_TRAP_REJECTION_WICK_RATIO: float = float(os.getenv("XAUUSD_TRAP_REJECTION_WICK_RATIO", "0.45"))
    XAUUSD_TRAP_REJECTION_M5_LOOKBACK: int = int(os.getenv("XAUUSD_TRAP_REJECTION_M5_LOOKBACK", "36"))
    XAUUSD_TRAP_REJECTION_RECENT_BARS: int = int(os.getenv("XAUUSD_TRAP_REJECTION_RECENT_BARS", "4"))
    XAUUSD_TRAP_EVENT_WINDOW_MIN: int = int(os.getenv("XAUUSD_TRAP_EVENT_WINDOW_MIN", "30"))
    XAUUSD_TRAP_PENALTY_ROUND_RES: float = float(os.getenv("XAUUSD_TRAP_PENALTY_ROUND_RES", "8"))
    XAUUSD_TRAP_PENALTY_NO_CHASE: float = float(os.getenv("XAUUSD_TRAP_PENALTY_NO_CHASE", "10"))
    XAUUSD_TRAP_PENALTY_SWEEP: float = float(os.getenv("XAUUSD_TRAP_PENALTY_SWEEP", "18"))
    XAUUSD_TRAP_PENALTY_EVENT: float = float(os.getenv("XAUUSD_TRAP_PENALTY_EVENT", "12"))
    XAUUSD_TRAP_BLOCK_ON_SWEEP: bool = os.getenv("XAUUSD_TRAP_BLOCK_ON_SWEEP", "1").strip().lower() in ("1", "true", "yes", "on")
    XAUUSD_NEWS_FREEZE_ENABLED: bool = os.getenv("XAUUSD_NEWS_FREEZE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    XAUUSD_NEWS_FREEZE_WINDOW_MIN: int = int(os.getenv("XAUUSD_NEWS_FREEZE_WINDOW_MIN", "20"))
    XAUUSD_TRAP_BLOCK_ON_NEWS_FREEZE: bool = os.getenv("XAUUSD_TRAP_BLOCK_ON_NEWS_FREEZE", "1").strip().lower() in ("1", "true", "yes", "on")
    XAUUSD_LIQUIDITY_MAP_ENABLED: bool = os.getenv("XAUUSD_LIQUIDITY_MAP_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    XAUUSD_LIQUIDITY_VP_BINS: int = int(os.getenv("XAUUSD_LIQUIDITY_VP_BINS", "24"))
    XAUUSD_LIQUIDITY_VP_LOOKBACK_H1: int = int(os.getenv("XAUUSD_LIQUIDITY_VP_LOOKBACK_H1", "120"))
    XAUUSD_TRAP_DXY_SHOCK_PCT_15M: float = float(os.getenv("XAUUSD_TRAP_DXY_SHOCK_PCT_15M", "0.18"))
    XAUUSD_TRAP_TNX_SHOCK_BPS_15M: float = float(os.getenv("XAUUSD_TRAP_TNX_SHOCK_BPS_15M", "2.0"))
    XAUUSD_TRAP_PENALTY_MACRO_SHOCK: float = float(os.getenv("XAUUSD_TRAP_PENALTY_MACRO_SHOCK", "10"))
    XAUUSD_TRAP_PENALTY_SWEEP_PROB: float = float(os.getenv("XAUUSD_TRAP_PENALTY_SWEEP_PROB", "8"))
    XAUUSD_TRAP_SWEEP_PROB_BLOCK_SCORE: int = int(os.getenv("XAUUSD_TRAP_SWEEP_PROB_BLOCK_SCORE", "78"))

    # ── MT5 Execution Bridge (RPyC -> Windows MT5 host) ─────────────────────
    MT5_ENABLED: bool = os.getenv("MT5_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_DRY_RUN: bool = os.getenv("MT5_DRY_RUN", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_HOST: str = os.getenv("MT5_HOST", "127.0.0.1")
    MT5_PORT: int = int(os.getenv("MT5_PORT", "18812"))
    MT5_MAGIC: int = int(os.getenv("MT5_MAGIC", "770100"))
    MT5_DEVIATION: int = int(os.getenv("MT5_DEVIATION", "20"))
    MT5_LOT_SIZE: float = float(os.getenv("MT5_LOT_SIZE", "0.01"))
    MT5_MIN_SIGNAL_CONFIDENCE: int = int(os.getenv("MT5_MIN_SIGNAL_CONFIDENCE", "75"))
    MT5_MIN_SIGNAL_CONFIDENCE_FX: float = float(os.getenv("MT5_MIN_SIGNAL_CONFIDENCE_FX", os.getenv("MT5_MIN_SIGNAL_CONFIDENCE", "75")))
    MT5_MIN_SIGNAL_CONFIDENCE_SYMBOL_OVERRIDES: str = os.getenv("MT5_MIN_SIGNAL_CONFIDENCE_SYMBOL_OVERRIDES", "")
    MT5_MAX_SIGNALS_PER_SCAN: int = int(os.getenv("MT5_MAX_SIGNALS_PER_SCAN", "1"))
    MT5_MAX_ATTEMPTS_PER_SCAN: int = int(os.getenv("MT5_MAX_ATTEMPTS_PER_SCAN", "3"))
    MT5_MAX_OPEN_POSITIONS: int = int(os.getenv("MT5_MAX_OPEN_POSITIONS", "5"))
    MT5_MAX_POSITIONS_PER_SYMBOL: int = int(os.getenv("MT5_MAX_POSITIONS_PER_SYMBOL", "1"))
    MT5_MAX_MARGIN_USAGE_PCT: float = float(os.getenv("MT5_MAX_MARGIN_USAGE_PCT", "35"))
    MT5_MAX_MARGIN_USAGE_PCT_FX: float = float(os.getenv("MT5_MAX_MARGIN_USAGE_PCT_FX", os.getenv("MT5_MAX_MARGIN_USAGE_PCT", "35")))
    MT5_MAX_MARGIN_USAGE_PCT_SYMBOL_OVERRIDES: str = os.getenv("MT5_MAX_MARGIN_USAGE_PCT_SYMBOL_OVERRIDES", "")
    MT5_RISK_MULTIPLIER_SYMBOL_OVERRIDES: str = os.getenv("MT5_RISK_MULTIPLIER_SYMBOL_OVERRIDES", "")
    MT5_RISK_MULTIPLIER_MIN_SYMBOL_OVERRIDES: str = os.getenv("MT5_RISK_MULTIPLIER_MIN_SYMBOL_OVERRIDES", "")
    MT5_RISK_MULTIPLIER_MAX_SYMBOL_OVERRIDES: str = os.getenv("MT5_RISK_MULTIPLIER_MAX_SYMBOL_OVERRIDES", "")
    MT5_CANARY_FORCE_SYMBOL_OVERRIDES: str = os.getenv("MT5_CANARY_FORCE_SYMBOL_OVERRIDES", "")
    MT5_PENDING_ENTRY_ENABLED: bool = os.getenv("MT5_PENDING_ENTRY_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PENDING_ENTRY_DEFAULT_MODE: str = os.getenv("MT5_PENDING_ENTRY_DEFAULT_MODE", "auto")
    MT5_PENDING_ENTRY_MODE_SYMBOL_OVERRIDES: str = os.getenv("MT5_PENDING_ENTRY_MODE_SYMBOL_OVERRIDES", "")
    MT5_PENDING_ENTRY_MIN_ADV_ATR: float = float(os.getenv("MT5_PENDING_ENTRY_MIN_ADV_ATR", "0.08"))
    MT5_PENDING_ENTRY_MAX_DIST_ATR: float = float(os.getenv("MT5_PENDING_ENTRY_MAX_DIST_ATR", "1.25"))
    MT5_PENDING_ENTRY_MIN_ADV_ATR_SYMBOL_OVERRIDES: str = os.getenv("MT5_PENDING_ENTRY_MIN_ADV_ATR_SYMBOL_OVERRIDES", "")
    MT5_PENDING_ENTRY_MAX_DIST_ATR_SYMBOL_OVERRIDES: str = os.getenv("MT5_PENDING_ENTRY_MAX_DIST_ATR_SYMBOL_OVERRIDES", "")
    MT5_MIN_FREE_MARGIN_AFTER_TRADE: float = float(os.getenv("MT5_MIN_FREE_MARGIN_AFTER_TRADE", "1"))
    MT5_COMMENT_PREFIX: str = os.getenv("MT5_COMMENT_PREFIX", "DEXTER")
    MT5_NOTIFY_EXECUTED: bool = os.getenv("MT5_NOTIFY_EXECUTED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_NOTIFY_FAILED: bool = os.getenv("MT5_NOTIFY_FAILED", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_EXECUTE_XAUUSD: bool = os.getenv("MT5_EXECUTE_XAUUSD", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_EXECUTE_CRYPTO: bool = os.getenv("MT5_EXECUTE_CRYPTO", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_EXECUTE_FX: bool = os.getenv("MT5_EXECUTE_FX", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_EXECUTE_STOCKS: bool = os.getenv("MT5_EXECUTE_STOCKS", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_AVOID_DUPLICATE_DIRECTION: bool = os.getenv("MT5_AVOID_DUPLICATE_DIRECTION", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_MICRO_MODE_ENABLED: bool = os.getenv("MT5_MICRO_MODE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_MICRO_SINGLE_POSITION_ONLY: bool = os.getenv("MT5_MICRO_SINGLE_POSITION_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_POSITION_LIMITS_BOT_ONLY: bool = os.getenv("MT5_POSITION_LIMITS_BOT_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
    MT5_MICRO_MAX_SPREAD_PCT: float = float(os.getenv("MT5_MICRO_MAX_SPREAD_PCT", "0.15"))
    MT5_MICRO_WHITELIST_LEARNER_ENABLED: bool = os.getenv("MT5_MICRO_WHITELIST_LEARNER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_MICRO_WHITELIST_PATH: str = os.getenv("MT5_MICRO_WHITELIST_PATH", "")
    MT5_MICRO_WHITELIST_TTL_HOURS: int = int(os.getenv("MT5_MICRO_WHITELIST_TTL_HOURS", "24"))
    MT5_MICRO_BALANCE_BUCKET_USD: float = float(os.getenv("MT5_MICRO_BALANCE_BUCKET_USD", "2.0"))
    MT5_AUTOPILOT_ENABLED: bool = os.getenv("MT5_AUTOPILOT_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_AUTOPILOT_DB_PATH: str = os.getenv("MT5_AUTOPILOT_DB_PATH", "")
    MT5_AUTOPILOT_SYNC_INTERVAL_MIN: int = int(os.getenv("MT5_AUTOPILOT_SYNC_INTERVAL_MIN", "15"))
    MT5_AUTOPILOT_GATE_CACHE_SEC: int = int(os.getenv("MT5_AUTOPILOT_GATE_CACHE_SEC", "5"))
    MT5_RISK_GOV_DAILY_LOSS_LIMIT_USD: float = float(os.getenv("MT5_RISK_GOV_DAILY_LOSS_LIMIT_USD", "2.0"))
    MT5_RISK_GOV_DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("MT5_RISK_GOV_DAILY_LOSS_LIMIT_PCT", "15.0"))
    MT5_RISK_GOV_MAX_CONSECUTIVE_LOSSES: int = int(os.getenv("MT5_RISK_GOV_MAX_CONSECUTIVE_LOSSES", "2"))
    MT5_RISK_GOV_LOSS_COOLDOWN_MIN: int = int(os.getenv("MT5_RISK_GOV_LOSS_COOLDOWN_MIN", "30"))
    MT5_RISK_GOV_MAX_REJECTIONS_1H: int = int(os.getenv("MT5_RISK_GOV_MAX_REJECTIONS_1H", "5"))
    MT5_ORCHESTRATOR_DB_PATH: str = os.getenv("MT5_ORCHESTRATOR_DB_PATH", "")
    MT5_WF_TRAIN_DAYS: int = int(os.getenv("MT5_WF_TRAIN_DAYS", "30"))
    MT5_WF_FORWARD_DAYS: int = int(os.getenv("MT5_WF_FORWARD_DAYS", "7"))
    MT5_WF_MIN_TRAIN_TRADES: int = int(os.getenv("MT5_WF_MIN_TRAIN_TRADES", "8"))
    MT5_WF_MIN_FORWARD_TRADES: int = int(os.getenv("MT5_WF_MIN_FORWARD_TRADES", "5"))
    MT5_WF_MIN_FORWARD_WIN_RATE: float = float(os.getenv("MT5_WF_MIN_FORWARD_WIN_RATE", "0.45"))
    MT5_WF_MAX_FORWARD_MAE: float = float(os.getenv("MT5_WF_MAX_FORWARD_MAE", "0.45"))
    MT5_ADAPTIVE_SIZING_ENABLED: bool = os.getenv("MT5_ADAPTIVE_SIZING_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_ADAPTIVE_SIZING_MIN_MULT: float = float(os.getenv("MT5_ADAPTIVE_SIZING_MIN_MULT", "0.25"))
    MT5_ADAPTIVE_SIZING_MAX_MULT: float = float(os.getenv("MT5_ADAPTIVE_SIZING_MAX_MULT", "1.00"))
    MT5_ADAPTIVE_SIZING_CANARY_MULT: float = float(os.getenv("MT5_ADAPTIVE_SIZING_CANARY_MULT", "0.35"))
    MT5_ADAPTIVE_SIZING_TARGET_WIN_RATE: float = float(os.getenv("MT5_ADAPTIVE_SIZING_TARGET_WIN_RATE", "0.52"))
    MT5_ADAPTIVE_SIZING_TARGET_MAE: float = float(os.getenv("MT5_ADAPTIVE_SIZING_TARGET_MAE", "0.35"))
    MT5_ADAPTIVE_EXECUTION_ENABLED: bool = os.getenv("MT5_ADAPTIVE_EXECUTION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_ADAPTIVE_EXECUTION_LOOKBACK_DAYS: int = int(os.getenv("MT5_ADAPTIVE_EXECUTION_LOOKBACK_DAYS", "45"))
    MT5_ADAPTIVE_EXECUTION_MIN_SYMBOL_TRADES: int = int(os.getenv("MT5_ADAPTIVE_EXECUTION_MIN_SYMBOL_TRADES", "6"))
    MT5_ADAPTIVE_EXECUTION_RR_MIN: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_RR_MIN", "1.2"))
    MT5_ADAPTIVE_EXECUTION_RR_MAX: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_RR_MAX", "2.8"))
    MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MIN: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MIN", "0.85"))
    MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MAX: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_STOP_SCALE_MAX", "1.35"))
    MT5_ADAPTIVE_EXECUTION_SIZE_MIN: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_SIZE_MIN", "0.70"))
    MT5_ADAPTIVE_EXECUTION_SIZE_MAX: float = float(os.getenv("MT5_ADAPTIVE_EXECUTION_SIZE_MAX", "1.10"))
    MT5_POSITION_MANAGER_ENABLED: bool = os.getenv("MT5_POSITION_MANAGER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_POSITION_MANAGER_DB_PATH: str = os.getenv("MT5_POSITION_MANAGER_DB_PATH", "")
    MT5_POSITION_MANAGER_INTERVAL_MIN: int = int(os.getenv("MT5_POSITION_MANAGER_INTERVAL_MIN", "1"))
    MT5_PM_MANAGE_ENABLED: bool = os.getenv("MT5_PM_MANAGE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_MANAGE_MANUAL_POSITIONS: bool = os.getenv("MT5_PM_MANAGE_MANUAL_POSITIONS", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_BREAK_EVEN_R: float = float(os.getenv("MT5_PM_BREAK_EVEN_R", "0.8"))
    MT5_PM_TRAIL_START_R: float = float(os.getenv("MT5_PM_TRAIL_START_R", "1.2"))
    MT5_PM_TRAIL_GAP_R: float = float(os.getenv("MT5_PM_TRAIL_GAP_R", "0.6"))
    MT5_PM_PARTIAL_TP_R: float = float(os.getenv("MT5_PM_PARTIAL_TP_R", "1.0"))
    MT5_PM_PARTIAL_CLOSE_PCT: float = float(os.getenv("MT5_PM_PARTIAL_CLOSE_PCT", "0.5"))
    MT5_PM_MIN_PARTIAL_VOLUME: float = float(os.getenv("MT5_PM_MIN_PARTIAL_VOLUME", "0.01"))
    MT5_PM_TIME_STOP_MIN: int = int(os.getenv("MT5_PM_TIME_STOP_MIN", "120"))
    MT5_PM_TIME_STOP_FLAT_R: float = float(os.getenv("MT5_PM_TIME_STOP_FLAT_R", "0.25"))
    MT5_PM_MAX_ACTIONS_PER_CYCLE: int = int(os.getenv("MT5_PM_MAX_ACTIONS_PER_CYCLE", "3"))
    MT5_PM_NOTIFY_ACTIONS: bool = os.getenv("MT5_PM_NOTIFY_ACTIONS", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_EARLY_RISK_PROTECT_ENABLED: bool = os.getenv("MT5_PM_EARLY_RISK_PROTECT_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_EARLY_RISK_TRIGGER_R: float = float(os.getenv("MT5_PM_EARLY_RISK_TRIGGER_R", "-0.80"))
    MT5_PM_EARLY_RISK_SL_R: float = float(os.getenv("MT5_PM_EARLY_RISK_SL_R", "-0.92"))
    MT5_PM_EARLY_RISK_BUFFER_R: float = float(os.getenv("MT5_PM_EARLY_RISK_BUFFER_R", "0.05"))
    MT5_PM_SPREAD_SPIKE_PROTECT_ENABLED: bool = os.getenv("MT5_PM_SPREAD_SPIKE_PROTECT_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_SPREAD_SPIKE_PCT: float = float(os.getenv("MT5_PM_SPREAD_SPIKE_PCT", "0.18"))
    MT5_PM_ADAPTIVE_ENABLED: bool = os.getenv("MT5_PM_ADAPTIVE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_ADAPTIVE_LOOKBACK_DAYS: int = int(os.getenv("MT5_PM_ADAPTIVE_LOOKBACK_DAYS", "45"))
    MT5_PM_ADAPTIVE_MIN_SYMBOL_TRADES: int = int(os.getenv("MT5_PM_ADAPTIVE_MIN_SYMBOL_TRADES", "6"))
    MT5_PM_LEARNING_ENABLED: bool = os.getenv("MT5_PM_LEARNING_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_PM_LEARNING_LOOKBACK_DAYS: int = int(os.getenv("MT5_PM_LEARNING_LOOKBACK_DAYS", "60"))
    MT5_PM_LEARNING_MIN_ACTIONS: int = int(os.getenv("MT5_PM_LEARNING_MIN_ACTIONS", "8"))
    MT5_PM_LEARNING_SYNC_HOURS: int = int(os.getenv("MT5_PM_LEARNING_SYNC_HOURS", "168"))
    MT5_PM_LEARNING_MAX_CLOSED_ROWS: int = int(os.getenv("MT5_PM_LEARNING_MAX_CLOSED_ROWS", "400"))
    MT5_SYMBOL_MAP: str = os.getenv("MT5_SYMBOL_MAP", "")  # e.g. XAUUSD=XAUUSDm,BTC/USDT=BTCUSD
    MT5_ALLOW_SYMBOLS: str = os.getenv("MT5_ALLOW_SYMBOLS", "")  # comma-separated broker symbols
    MT5_BLOCK_SYMBOLS: str = os.getenv("MT5_BLOCK_SYMBOLS", "")  # comma-separated broker symbols

    # ── Signal Thresholds ──────────────────────────────────────────────────────
    MIN_SIGNAL_CONFIDENCE:  int = int(os.getenv("MIN_SIGNAL_CONFIDENCE", "70"))
    STOCK_MIN_CONFIDENCE:   int = int(os.getenv("STOCK_MIN_CONFIDENCE",  "70"))
    STOCK_MAX_RESULTS:      int = int(os.getenv("STOCK_MAX_RESULTS",     "5"))
    TOP_COINS_COUNT:        int = int(os.getenv("TOP_COINS_COUNT",       "50"))
    CRYPTO_AUTO_FOCUS_ONLY: bool = os.getenv("CRYPTO_AUTO_FOCUS_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    CRYPTO_AUTO_FOCUS_SYMBOLS: str = os.getenv("CRYPTO_AUTO_FOCUS_SYMBOLS", "BTC/USDT,ETH/USDT,BTCUSD,ETHUSD")
    CRYPTO_AUTO_FOCUS_NO_SIGNAL_REPORT: bool = os.getenv("CRYPTO_AUTO_FOCUS_NO_SIGNAL_REPORT", "1").strip().lower() in ("1", "true", "yes", "on")
    CRYPTO_AUTO_FOCUS_NO_SIGNAL_INTERVAL_MIN: int = int(os.getenv("CRYPTO_AUTO_FOCUS_NO_SIGNAL_INTERVAL_MIN", "5"))
    FX_TOP_N:             int = int(os.getenv("FX_TOP_N", "5"))
    FX_MIN_CONFIDENCE:    int = int(os.getenv("FX_MIN_CONFIDENCE", str(MIN_SIGNAL_CONFIDENCE)))
    STOCK_MIN_VOL_RATIO:    float = float(os.getenv("STOCK_MIN_VOL_RATIO", "0.9"))
    STOCK_MIN_EDGE:         float = float(os.getenv("STOCK_MIN_EDGE", "15"))
    STOCK_MIN_MOMENTUM_RSI: float = float(os.getenv("STOCK_MIN_MOMENTUM_RSI", "53"))
    US_OPEN_MIN_VOL_RATIO:  float = float(os.getenv("US_OPEN_MIN_VOL_RATIO", "1.0"))
    US_OPEN_MIN_DOLLAR_VOLUME: float = float(os.getenv("US_OPEN_MIN_DOLLAR_VOLUME", "30000000"))
    WATCHLIST_MIN_VOL_RATIO: float = float(os.getenv("WATCHLIST_MIN_VOL_RATIO", "0.5"))
    WATCHLIST_MAX_RESULTS: int = int(os.getenv("WATCHLIST_MAX_RESULTS", "5"))
    WATCHLIST_MIN_CONFIDENCE: int = int(
        os.getenv("WATCHLIST_MIN_CONFIDENCE", str(STOCK_MIN_CONFIDENCE))
    )
    VI_TOP_N: int = int(os.getenv("VI_TOP_N", "10"))
    VI_MAX_CANDIDATES: int = int(os.getenv("VI_MAX_CANDIDATES", "20"))
    VI_MIN_CONFIDENCE: int = int(os.getenv("VI_MIN_CONFIDENCE", "76"))
    VI_MIN_VOL_RATIO: float = float(os.getenv("VI_MIN_VOL_RATIO", "1.0"))
    VI_MIN_DOLLAR_VOLUME: float = float(os.getenv("VI_MIN_DOLLAR_VOLUME", "25000000"))
    VI_MAX_PE_RATIO: float = float(os.getenv("VI_MAX_PE_RATIO", "25"))
    VI_LONG_ONLY: bool = os.getenv("VI_LONG_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    VI_MIN_SETUP_WIN_RATE: float = float(os.getenv("VI_MIN_SETUP_WIN_RATE", "0.56"))
    VI_RSI_MIN: float = float(os.getenv("VI_RSI_MIN", "54"))
    VI_RSI_MAX: float = float(os.getenv("VI_RSI_MAX", "67"))
    VI_REQUIRE_QUALITY_SCORE: int = int(os.getenv("VI_REQUIRE_QUALITY_SCORE", "2"))
    TH_VI_MIN_CONFIDENCE: int = int(os.getenv("TH_VI_MIN_CONFIDENCE", "70"))
    TH_VI_MIN_VOL_RATIO: float = float(os.getenv("TH_VI_MIN_VOL_RATIO", "0.8"))
    TH_VI_MIN_DOLLAR_VOLUME: float = float(os.getenv("TH_VI_MIN_DOLLAR_VOLUME", "8000000"))
    TH_VI_MIN_SETUP_WIN_RATE: float = float(os.getenv("TH_VI_MIN_SETUP_WIN_RATE", "0.53"))
    TH_VI_RSI_MIN: float = float(os.getenv("TH_VI_RSI_MIN", "50"))
    TH_VI_RSI_MAX: float = float(os.getenv("TH_VI_RSI_MAX", "74"))
    TH_VI_REQUIRE_QUALITY_SCORE: int = int(os.getenv("TH_VI_REQUIRE_QUALITY_SCORE", "1"))
    TH_VI_LONG_ONLY: bool = os.getenv("TH_VI_LONG_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    STOCK_INFO_CACHE_TTL_SEC: int = int(os.getenv("STOCK_INFO_CACHE_TTL_SEC", "21600"))
    STOCK_MARKETS: list = ["US", "EU", "ASIA", "THAILAND"]

    # ── Economic Calendar Alerts ──────────────────────────────────────────────
    ECON_CALENDAR_ENABLED: bool = os.getenv("ECON_CALENDAR_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    ECON_CALENDAR_FEED_URL: str = os.getenv(
        "ECON_CALENDAR_FEED_URL",
        "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    )
    ECON_CALENDAR_CACHE_TTL_SEC: int = int(os.getenv("ECON_CALENDAR_CACHE_TTL_SEC", "300"))
    ECON_CALENDAR_CHECK_INTERVAL_MIN: int = int(os.getenv("ECON_CALENDAR_CHECK_INTERVAL_MIN", "5"))
    ECON_CALENDAR_LOOKAHEAD_HOURS: int = int(os.getenv("ECON_CALENDAR_LOOKAHEAD_HOURS", "24"))
    ECON_CALENDAR_MIN_IMPACT: str = os.getenv("ECON_CALENDAR_MIN_IMPACT", "high").strip().lower()
    ECON_ALERT_WINDOWS: str = os.getenv("ECON_ALERT_WINDOWS", "60,15")
    ECON_ALERT_TOLERANCE_MIN: int = int(os.getenv("ECON_ALERT_TOLERANCE_MIN", "3"))
    ECON_ALERT_CURRENCIES: str = os.getenv("ECON_ALERT_CURRENCIES", "USD,EUR,GBP,JPY,CAD,AUD,NZD,CHF")

    # ── Macro Headline Risk Watch ─────────────────────────────────────────────
    MACRO_NEWS_ENABLED: bool = os.getenv("MACRO_NEWS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MACRO_NEWS_FEED_URL: str = os.getenv(
        "MACRO_NEWS_FEED_URL",
        "https://news.google.com/rss/search?q=(Trump+OR+tariff+OR+Fed+OR+FOMC+OR+CPI+OR+NFP+OR+war+OR+missile+OR+oil+OR+crude+OR+OPEC+OR+sanctions+OR+geopolitical)+when:1d&hl=en-US&gl=US&ceid=US:en",
    )
    MACRO_NEWS_CACHE_TTL_SEC: int = int(os.getenv("MACRO_NEWS_CACHE_TTL_SEC", "300"))
    MACRO_NEWS_CHECK_INTERVAL_MIN: int = int(os.getenv("MACRO_NEWS_CHECK_INTERVAL_MIN", "30"))
    MACRO_NEWS_LOOKBACK_HOURS: int = int(os.getenv("MACRO_NEWS_LOOKBACK_HOURS", "24"))
    MACRO_NEWS_MIN_SCORE: int = int(os.getenv("MACRO_NEWS_MIN_SCORE", "8"))
    MACRO_NEWS_ALERT_MAX_AGE_MIN: int = int(os.getenv("MACRO_NEWS_ALERT_MAX_AGE_MIN", "240"))
    MACRO_NEWS_MAX_ALERTS_PER_RUN: int = int(os.getenv("MACRO_NEWS_MAX_ALERTS_PER_RUN", "2"))
    MACRO_NEWS_REQUIRE_PRIORITY_THEME: bool = os.getenv("MACRO_NEWS_REQUIRE_PRIORITY_THEME", "1").strip().lower() in ("1", "true", "yes", "on")
    MACRO_IMPACT_TRACKER_ENABLED: bool = os.getenv("MACRO_IMPACT_TRACKER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MACRO_IMPACT_TRACKER_DB_PATH: str = os.getenv("MACRO_IMPACT_TRACKER_DB_PATH", "")
    MACRO_IMPACT_TRACKER_SYNC_INTERVAL_MIN: int = int(os.getenv("MACRO_IMPACT_TRACKER_SYNC_INTERVAL_MIN", "15"))
    MACRO_IMPACT_TRACKER_LOOKBACK_HOURS: int = int(os.getenv("MACRO_IMPACT_TRACKER_LOOKBACK_HOURS", "72"))
    MACRO_IMPACT_TRACKER_MIN_SCORE: int = int(os.getenv("MACRO_IMPACT_TRACKER_MIN_SCORE", "5"))
    MACRO_IMPACT_TRACKER_MAX_HEADLINES_PER_SYNC: int = int(os.getenv("MACRO_IMPACT_TRACKER_MAX_HEADLINES_PER_SYNC", "20"))
    MACRO_REPORT_DEFAULT_HOURS: int = int(os.getenv("MACRO_REPORT_DEFAULT_HOURS", "24"))
    MACRO_REPORT_MAX_HEADLINES: int = int(os.getenv("MACRO_REPORT_MAX_HEADLINES", "5"))
    MACRO_ADAPTIVE_WEIGHTING_ENABLED: bool = os.getenv("MACRO_ADAPTIVE_WEIGHTING_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MACRO_ADAPTIVE_WEIGHT_MIN_SAMPLES: int = int(os.getenv("MACRO_ADAPTIVE_WEIGHT_MIN_SAMPLES", "3"))
    MACRO_ADAPTIVE_WEIGHT_MIN_MULT: float = float(os.getenv("MACRO_ADAPTIVE_WEIGHT_MIN_MULT", "0.80"))
    MACRO_ADAPTIVE_WEIGHT_MAX_MULT: float = float(os.getenv("MACRO_ADAPTIVE_WEIGHT_MAX_MULT", "1.25"))
    MACRO_ADAPTIVE_WEIGHT_UPDATE_HOURS: int = int(os.getenv("MACRO_ADAPTIVE_WEIGHT_UPDATE_HOURS", "168"))  # 7d
    MACRO_ALERT_ADAPTIVE_PRIORITY_ENABLED: bool = os.getenv("MACRO_ALERT_ADAPTIVE_PRIORITY_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MACRO_ALERT_ADAPTIVE_MIN_SAMPLES: int = int(os.getenv("MACRO_ALERT_ADAPTIVE_MIN_SAMPLES", "3"))
    MACRO_ALERT_ADAPTIVE_MIN_THEME_MULT: float = float(os.getenv("MACRO_ALERT_ADAPTIVE_MIN_THEME_MULT", "0.90"))
    MACRO_ALERT_ADAPTIVE_SKIP_NO_CLEAR_RATE: float = float(os.getenv("MACRO_ALERT_ADAPTIVE_SKIP_NO_CLEAR_RATE", "65"))
    MACRO_ALERT_ADAPTIVE_ULTRA_SCORE_FLOOR: int = int(os.getenv("MACRO_ALERT_ADAPTIVE_ULTRA_SCORE_FLOOR", "10"))
    MACRO_WEIGHTS_DEFAULT_TOP: int = int(os.getenv("MACRO_WEIGHTS_DEFAULT_TOP", "8"))

    # ── Risk Management ────────────────────────────────────────────────────────
    DEFAULT_RISK_PERCENT: float = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))
    DEFAULT_RR_RATIO:     float = float(os.getenv("DEFAULT_RR_RATIO",     "2.5"))

    # ── Timeframes — XAUUSD ───────────────────────────────────────────────────
    XAUUSD_TREND_TF:     str = os.getenv("XAUUSD_TREND_TF",     "1d")
    XAUUSD_STRUCTURE_TF: str = os.getenv("XAUUSD_STRUCTURE_TF", "4h")
    XAUUSD_ENTRY_TF:     str = os.getenv("XAUUSD_ENTRY_TF",     "1h")

    # ── Timeframes — Crypto ───────────────────────────────────────────────────
    CRYPTO_TREND_TF: str = os.getenv("CRYPTO_TREND_TF", "4h")
    CRYPTO_ENTRY_TF: str = os.getenv("CRYPTO_ENTRY_TF", "1h")
    FX_TREND_TF: str = os.getenv("FX_TREND_TF", "4h")
    FX_ENTRY_TF: str = os.getenv("FX_ENTRY_TF", "1h")
    FX_SCANNER_MT5_TRADABLE_ONLY: bool = os.getenv("FX_SCANNER_MT5_TRADABLE_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    FX_MAJOR_SYMBOLS: str = os.getenv(
        "FX_MAJOR_SYMBOLS",
        "EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCAD,USDCHF",
    )
    CRYPTO_SNIPER_EXCLUDE_FIAT_BASES: bool = os.getenv("CRYPTO_SNIPER_EXCLUDE_FIAT_BASES", "1").strip().lower() in ("1", "true", "yes", "on")
    CRYPTO_SNIPER_EXCLUDE_STABLE_BASES: bool = os.getenv("CRYPTO_SNIPER_EXCLUDE_STABLE_BASES", "1").strip().lower() in ("1", "true", "yes", "on")
    CRYPTO_SNIPER_MT5_TRADABLE_ONLY: bool = os.getenv("CRYPTO_SNIPER_MT5_TRADABLE_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")
    STOCK_SCANNER_MT5_TRADABLE_ONLY: bool = os.getenv("STOCK_SCANNER_MT5_TRADABLE_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
    CRYPTO_SNIPER_EXCLUDE_BASES: str = os.getenv(
        "CRYPTO_SNIPER_EXCLUDE_BASES",
        "USDT,USDC,FDUSD,TUSD,BUSD,USDP,DAI,USDD,PYUSD,EUR,GBP,JPY,AUD,TRY,BRL,RUB,NGN,UAH,ZAR",
    )

    # ── Stock Scanner Yahoo Cleanup ──────────────────────────────────────────
    STOCK_YF_BAD_SYMBOL_CACHE_TTL_SEC: int = int(os.getenv("STOCK_YF_BAD_SYMBOL_CACHE_TTL_SEC", "86400"))
    STOCK_YF_EMPTY_FAILS_TO_BLACKLIST: int = int(os.getenv("STOCK_YF_EMPTY_FAILS_TO_BLACKLIST", "2"))
    STOCK_YF_SYMBOL_ALIAS_MAP: str = os.getenv("STOCK_YF_SYMBOL_ALIAS_MAP", "")  # e.g. DTAC.BK=TRUE.BK

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    US_OPEN_SMART_MONITOR: bool = os.getenv("US_OPEN_SMART_MONITOR", "1").strip() in ("1", "true", "yes", "on")
    ADMIN_AI_INTENT_ENABLED: bool = os.getenv("ADMIN_AI_INTENT_ENABLED", "1").strip() in ("1", "true", "yes", "on")
    ACCESS_DB_PATH: str = os.getenv("ACCESS_DB_PATH", "")
    TRIAL_DAYS: int = int(os.getenv("TRIAL_DAYS", "7"))
    TRIAL_NO_AI_ALL: bool = os.getenv("TRIAL_NO_AI_ALL", "0").strip().lower() in ("1", "true", "yes", "on")
    PLAN_TRIAL_DAILY_LIMIT: int = int(os.getenv("PLAN_TRIAL_DAILY_LIMIT", "12"))
    TRIAL_CRYPTO_SYMBOLS: str = os.getenv("TRIAL_CRYPTO_SYMBOLS", "BTC/USDT,ETH/USDT,BTCUSD,ETHUSD")
    PLAN_A_DAILY_LIMIT: int = int(os.getenv("PLAN_A_DAILY_LIMIT", "30"))
    PLAN_B_DAILY_LIMIT: int = int(os.getenv("PLAN_B_DAILY_LIMIT", "120"))
    PLAN_C_DAILY_LIMIT: int = int(os.getenv("PLAN_C_DAILY_LIMIT", "500"))
    BILLING_ENABLED: bool = os.getenv("BILLING_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
    BILLING_AUTOSTART_IN_MONITOR: bool = os.getenv("BILLING_AUTOSTART_IN_MONITOR", "1").strip().lower() in ("1", "true", "yes", "on")
    BILLING_WEBHOOK_HOST: str = os.getenv("BILLING_WEBHOOK_HOST", "0.0.0.0")
    BILLING_WEBHOOK_PORT: int = int(os.getenv("BILLING_WEBHOOK_PORT", "8787"))
    BILLING_DEFAULT_PLAN: str = os.getenv("BILLING_DEFAULT_PLAN", "b").strip().lower()
    BILLING_DEFAULT_DAYS: int = int(os.getenv("BILLING_DEFAULT_DAYS", "30"))
    BILLING_UPGRADE_URL: str = os.getenv("BILLING_UPGRADE_URL", "")
    STRIPE_ENABLED: bool = os.getenv("STRIPE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_SIGNATURE_TOLERANCE_SEC: int = int(os.getenv("STRIPE_SIGNATURE_TOLERANCE_SEC", "300"))
    STRIPE_PRICE_PLAN_MAP: str = os.getenv("STRIPE_PRICE_PLAN_MAP", "")
    STRIPE_PRICE_ID_A: str = os.getenv("STRIPE_PRICE_ID_A", "")
    STRIPE_PRICE_ID_B: str = os.getenv("STRIPE_PRICE_ID_B", "")
    STRIPE_PRICE_ID_C: str = os.getenv("STRIPE_PRICE_ID_C", "")
    STRIPE_CHECKOUT_SUCCESS_URL: str = os.getenv("STRIPE_CHECKOUT_SUCCESS_URL", "")
    STRIPE_CHECKOUT_CANCEL_URL: str = os.getenv("STRIPE_CHECKOUT_CANCEL_URL", "")
    BILLING_CURRENCY: str = os.getenv("BILLING_CURRENCY", "usd").strip().lower()
    BILLING_PRICE_A_CENTS: int = int(os.getenv("BILLING_PRICE_A_CENTS", "1900"))
    BILLING_PRICE_B_CENTS: int = int(os.getenv("BILLING_PRICE_B_CENTS", "4900"))
    BILLING_PRICE_C_CENTS: int = int(os.getenv("BILLING_PRICE_C_CENTS", "12900"))
    BILLING_PLAN_DAYS_A: int = int(os.getenv("BILLING_PLAN_DAYS_A", "30"))
    BILLING_PLAN_DAYS_B: int = int(os.getenv("BILLING_PLAN_DAYS_B", "30"))
    BILLING_PLAN_DAYS_C: int = int(os.getenv("BILLING_PLAN_DAYS_C", "90"))
    PROMPTPAY_ENABLED: bool = os.getenv("PROMPTPAY_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    PROMPTPAY_WEBHOOK_SECRET: str = os.getenv("PROMPTPAY_WEBHOOK_SECRET", "")
    PROMPTPAY_SIGNATURE_HEADER: str = os.getenv("PROMPTPAY_SIGNATURE_HEADER", "X-PromptPay-Signature")
    PROMPTPAY_REQUIRE_SIGNATURE: bool = os.getenv("PROMPTPAY_REQUIRE_SIGNATURE", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_ENABLED: bool = os.getenv("NEURAL_BRAIN_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_SYNC_DAYS: int = int(os.getenv("NEURAL_BRAIN_SYNC_DAYS", "120"))
    NEURAL_BRAIN_SYNC_INTERVAL_MIN: int = int(os.getenv("NEURAL_BRAIN_SYNC_INTERVAL_MIN", "15"))
    NEURAL_BRAIN_AUTO_TRAIN: bool = os.getenv("NEURAL_BRAIN_AUTO_TRAIN", "0").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_MIN_SAMPLES: int = int(os.getenv("NEURAL_BRAIN_MIN_SAMPLES", "30"))
    NEURAL_BRAIN_BOOTSTRAP_MIN_SAMPLES: int = int(os.getenv("NEURAL_BRAIN_BOOTSTRAP_MIN_SAMPLES", "10"))
    NEURAL_BRAIN_EPOCHS: int = int(os.getenv("NEURAL_BRAIN_EPOCHS", "300"))
    NEURAL_BRAIN_HIDDEN_UNITS: int = int(os.getenv("NEURAL_BRAIN_HIDDEN_UNITS", "12"))
    NEURAL_BRAIN_LR: float = float(os.getenv("NEURAL_BRAIN_LR", "0.05"))
    NEURAL_BRAIN_EXECUTION_FILTER: bool = os.getenv("NEURAL_BRAIN_EXECUTION_FILTER", "0").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_MIN_PROB: float = float(os.getenv("NEURAL_BRAIN_MIN_PROB", "0.55"))
    NEURAL_BRAIN_MIN_PROB_FX: float = float(os.getenv("NEURAL_BRAIN_MIN_PROB_FX", os.getenv("NEURAL_BRAIN_MIN_PROB", "0.55")))
    NEURAL_BRAIN_MIN_PROB_SYMBOL_OVERRIDES: str = os.getenv("NEURAL_BRAIN_MIN_PROB_SYMBOL_OVERRIDES", "")
    NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW_SYMBOL_OVERRIDES: str = os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW_SYMBOL_OVERRIDES", "")
    NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH_SYMBOL_OVERRIDES: str = os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH_SYMBOL_OVERRIDES", "")
    NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY_SYMBOL_OVERRIDES: str = os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY_SYMBOL_OVERRIDES", "")
    NEURAL_BRAIN_FX_SOFT_FILTER_ENABLED: bool = os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW", "0.43"))
    NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH", "0.48"))
    NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY", "4.0"))
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BAND_ENABLED: bool = os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BAND_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_LOOKBACK_DAYS: int = int(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_LOOKBACK_DAYS", "60"))
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_RESOLVED: int = int(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_RESOLVED", "8"))
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BLEND: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BLEND", "0.50"))
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_LOW: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MIN_LOW", "0.35"))
    NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MAX_HIGH: float = float(os.getenv("NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_MAX_HIGH", "0.52"))
    MT5_FX_CONF_SOFT_FILTER_ENABLED: bool = os.getenv("MT5_FX_CONF_SOFT_FILTER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_FX_CONF_SOFT_FILTER_BAND_PTS: float = float(os.getenv("MT5_FX_CONF_SOFT_FILTER_BAND_PTS", "6.0"))
    MT5_FX_CONF_SOFT_FILTER_MAX_SIZE_PENALTY: float = float(os.getenv("MT5_FX_CONF_SOFT_FILTER_MAX_SIZE_PENALTY", "0.35"))
    MT5_FX_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES: str = os.getenv("MT5_FX_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES", "")
    MT5_FX_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES: str = os.getenv("MT5_FX_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES", "")
    MT5_CRYPTO_CONF_SOFT_FILTER_ENABLED: bool = os.getenv("MT5_CRYPTO_CONF_SOFT_FILTER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_CRYPTO_CONF_SOFT_FILTER_BAND_PTS: float = float(os.getenv("MT5_CRYPTO_CONF_SOFT_FILTER_BAND_PTS", "4.0"))
    MT5_CRYPTO_CONF_SOFT_FILTER_MAX_SIZE_PENALTY: float = float(os.getenv("MT5_CRYPTO_CONF_SOFT_FILTER_MAX_SIZE_PENALTY", "0.25"))
    MT5_CRYPTO_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES: str = os.getenv("MT5_CRYPTO_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES", "")
    MT5_CRYPTO_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES: str = os.getenv("MT5_CRYPTO_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES", "")
    MT5_FX_CONF_SOFT_FILTER_LEARNED_BAND_ENABLED: bool = os.getenv("MT5_FX_CONF_SOFT_FILTER_LEARNED_BAND_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    MT5_FX_CONF_SOFT_FILTER_LEARNED_LOOKBACK_DAYS: int = int(os.getenv("MT5_FX_CONF_SOFT_FILTER_LEARNED_LOOKBACK_DAYS", "60"))
    MT5_FX_CONF_SOFT_FILTER_LEARNED_MIN_RESOLVED: int = int(os.getenv("MT5_FX_CONF_SOFT_FILTER_LEARNED_MIN_RESOLVED", "8"))
    MT5_FX_CONF_SOFT_FILTER_LEARNED_BLEND: float = float(os.getenv("MT5_FX_CONF_SOFT_FILTER_LEARNED_BLEND", "0.50"))
    MT5_FX_CONF_SOFT_FILTER_MIN_LOW: float = float(os.getenv("MT5_FX_CONF_SOFT_FILTER_MIN_LOW", "55"))
    MT5_FX_CONF_SOFT_FILTER_MIN_GAP: float = float(os.getenv("MT5_FX_CONF_SOFT_FILTER_MIN_GAP", "1.0"))
    MT5_EXEC_REASONS_DELTA_MARKER_UTC: str = os.getenv("MT5_EXEC_REASONS_DELTA_MARKER_UTC", "")
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_ENABLED: bool = os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_LOOKBACK_DAYS: int = int(os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_LOOKBACK_DAYS", "60"))
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_RESOLVED: int = int(os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_RESOLVED", "8"))
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_PROB: float = float(os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MIN_PROB", "0.40"))
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MAX_PROB: float = float(os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_MAX_PROB", "0.55"))
    NEURAL_BRAIN_FX_LEARNED_THRESHOLD_BLEND: float = float(os.getenv("NEURAL_BRAIN_FX_LEARNED_THRESHOLD_BLEND", "0.50"))
    NEURAL_BRAIN_FILTER_MIN_SAMPLES: int = int(os.getenv("NEURAL_BRAIN_FILTER_MIN_SAMPLES", "60"))
    NEURAL_BRAIN_FILTER_MIN_VAL_ACC: float = float(os.getenv("NEURAL_BRAIN_FILTER_MIN_VAL_ACC", "0.52"))
    NEURAL_BRAIN_FILTER_MAX_MODEL_AGE_HOURS: int = int(os.getenv("NEURAL_BRAIN_FILTER_MAX_MODEL_AGE_HOURS", "720"))
    NEURAL_BRAIN_SOFT_ADJUST: bool = os.getenv("NEURAL_BRAIN_SOFT_ADJUST", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_SOFT_ADJUST_WEIGHT: float = float(os.getenv("NEURAL_BRAIN_SOFT_ADJUST_WEIGHT", "0.35"))
    NEURAL_BRAIN_SOFT_ADJUST_MAX_DELTA: float = float(os.getenv("NEURAL_BRAIN_SOFT_ADJUST_MAX_DELTA", "8.0"))
    SIGNAL_FEEDBACK_ENABLED: bool = os.getenv("SIGNAL_FEEDBACK_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_SIGNAL_FEEDBACK_MAX_RECORDS: int = int(os.getenv("NEURAL_BRAIN_SIGNAL_FEEDBACK_MAX_RECORDS", "400"))
    NEURAL_BRAIN_PSEUDO_LABEL_ENABLED: bool = os.getenv("NEURAL_BRAIN_PSEUDO_LABEL_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    NEURAL_BRAIN_PSEUDO_LABEL_MIN_HOURS: float = float(os.getenv("NEURAL_BRAIN_PSEUDO_LABEL_MIN_HOURS", "2"))
    NEURAL_BRAIN_PSEUDO_LABEL_MIN_ABS_R: float = float(os.getenv("NEURAL_BRAIN_PSEUDO_LABEL_MIN_ABS_R", "0.25"))

    # ── Internal Mappings ─────────────────────────────────────────────────────
    TF_TO_CCXT: dict = {
        "1m": "1m",  "5m": "5m",  "15m": "15m", "30m": "30m",
        "1h": "1h",  "4h": "4h",  "1d": "1d",   "1w": "1w",
    }
    TF_TO_YFINANCE: dict = {
        "1m": "1m",  "5m": "5m",  "15m": "15m", "30m": "30m",
        "1h": "1h",  "4h": "1h",  "1d": "1d",   "1w": "1wk",
    }

    # ── Priority Crypto Pairs (always included in scans) ──────────────────────
    PRIORITY_PAIRS: list = [
        "BTC/USDT", "ETH/USDT", "BNB/USDT",  "SOL/USDT",  "XRP/USDT",
        "AVAX/USDT","DOGE/USDT","ADA/USDT",  "DOT/USDT",  "LINK/USDT",
        "POL/USDT", "LTC/USDT","BCH/USDT",   "UNI/USDT",  "ATOM/USDT",
    ]

    # ── Trading Sessions (UTC) ────────────────────────────────────────────────
    SESSIONS: dict = {
        "asian":    {"start": "00:00", "end": "08:00"},
        "london":   {"start": "07:00", "end": "16:00"},
        "new_york": {"start": "12:00", "end": "21:00"},
        "overlap":  {"start": "12:00", "end": "16:00"},
    }

    @classmethod
    def validate(cls) -> list:
        """Return list of missing critical config keys."""
        missing = []
        if not cls.has_any_ai_key():
            missing.append("AI KEY MISSING  ← set GROQ_API_KEY or GEMINI_API_KEY or ANTHROPIC_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID  ← add your Telegram Chat ID")
        return missing

    @classmethod
    def summary(cls) -> str:
        """One-line config summary for startup log."""
        tg = "✅" if cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_ID else "⚠️ Chat ID missing"
        provider = cls.resolve_ai_provider()
        ai = f"✅ {provider}" if provider != "none" else "❌ Missing"
        return (
            f"AI={ai} | TG={tg} | "
            f"Exchange={cls.CRYPTO_EXCHANGE.upper()} | "
            f"Confidence≥{cls.MIN_SIGNAL_CONFIDENCE}%"
        )

    @classmethod
    def has_any_ai_key(cls) -> bool:
        return bool(cls.GROQ_API_KEY or cls.GEMINI_API_KEY or cls.ANTHROPIC_API_KEY)

    @classmethod
    def resolve_ai_provider(cls) -> str:
        """Resolve active AI provider using preferred order."""
        pref = (cls.AI_PROVIDER or "auto").strip().lower()
        if pref in ("groq", "gemini", "anthropic"):
            if pref == "groq" and cls.GROQ_API_KEY:
                return "groq"
            if pref == "gemini" and cls.GEMINI_API_KEY:
                return "gemini"
            if pref == "anthropic" and cls.ANTHROPIC_API_KEY:
                return "anthropic"
        if cls.GROQ_API_KEY:
            return "groq"
        if cls.GEMINI_API_KEY:
            return "gemini"
        if cls.ANTHROPIC_API_KEY:
            return "anthropic"
        return "none"

    @classmethod
    def model_for_provider(cls, provider: str) -> str:
        provider = (provider or "").lower()
        if provider == "groq":
            return cls.GROQ_MODEL
        if provider == "gemini":
            return cls.GEMINI_MODEL
        return cls.AI_MODEL

    @classmethod
    def get_admin_ids(cls) -> set[int]:
        """Return Telegram admin user IDs allowed to run bot commands."""
        ids: set[int] = set()
        raw = (cls.TELEGRAM_ADMIN_IDS or "").strip()
        if raw:
            for part in raw.split(","):
                part = part.strip()
                if part and part.lstrip("-").isdigit():
                    ids.add(int(part))
        # Fallback for private-chat deployments where chat ID equals user ID.
        if cls.TELEGRAM_CHAT_ID and cls.TELEGRAM_CHAT_ID.lstrip("-").isdigit():
            ids.add(int(cls.TELEGRAM_CHAT_ID))
        return ids

    @classmethod
    def _parse_symbol_set(cls, raw: str) -> set[str]:
        values: set[str] = set()
        for part in (raw or "").split(","):
            item = part.strip().upper()
            if item:
                values.add(item)
        return values

    @classmethod
    def _parse_upper_map(cls, raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in (raw or "").split(","):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            left, right = item.split("=", 1)
            k = left.strip().upper()
            v = right.strip().upper()
            if k and v:
                out[k] = v
        return out

    @classmethod
    def _parse_float_map(cls, raw: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for chunk in (raw or "").split(","):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            left, right = item.split("=", 1)
            k = left.strip().upper()
            if not k:
                continue
            try:
                v = float(right.strip())
            except Exception:
                continue
            out[k] = v
        return out

    @classmethod
    def _parse_bool_or_auto_map(cls, raw: str) -> dict[str, Optional[bool]]:
        out: dict[str, Optional[bool]] = {}
        for chunk in (raw or "").split(","):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            left, right = item.split("=", 1)
            k = left.strip().upper()
            if not k:
                continue
            v = right.strip().lower()
            if v in {"auto", "none", "default", ""}:
                out[k] = None
            elif v in {"1", "true", "yes", "on"}:
                out[k] = True
            elif v in {"0", "false", "no", "off"}:
                out[k] = False
        return out

    @classmethod
    def _parse_int_list(cls, raw: str) -> list[int]:
        out: list[int] = []
        for part in (raw or "").split(","):
            item = part.strip()
            if not item:
                continue
            try:
                out.append(int(item))
            except Exception:
                continue
        return out

    @classmethod
    def get_mt5_allow_symbols(cls) -> set[str]:
        return cls._parse_symbol_set(cls.MT5_ALLOW_SYMBOLS)

    @classmethod
    def get_trial_crypto_symbols(cls) -> set[str]:
        return cls._parse_symbol_set(cls.TRIAL_CRYPTO_SYMBOLS)

    @classmethod
    def get_fx_major_symbols(cls) -> list[str]:
        vals = sorted(cls._parse_symbol_set(cls.FX_MAJOR_SYMBOLS))
        return vals or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]

    @classmethod
    def get_crypto_auto_focus_symbols(cls) -> set[str]:
        return cls._parse_symbol_set(cls.CRYPTO_AUTO_FOCUS_SYMBOLS)

    @classmethod
    def get_crypto_sniper_exclude_bases(cls) -> set[str]:
        return cls._parse_symbol_set(cls.CRYPTO_SNIPER_EXCLUDE_BASES)

    @classmethod
    def get_econ_alert_windows(cls) -> list[int]:
        windows = [x for x in cls._parse_int_list(cls.ECON_ALERT_WINDOWS) if x > 0]
        return sorted(set(windows), reverse=True) or [60, 15]

    @classmethod
    def get_econ_alert_currencies(cls) -> set[str]:
        return cls._parse_symbol_set(cls.ECON_ALERT_CURRENCIES)

    @classmethod
    def get_mt5_block_symbols(cls) -> set[str]:
        return cls._parse_symbol_set(cls.MT5_BLOCK_SYMBOLS)

    @classmethod
    def get_mt5_min_conf_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_MIN_SIGNAL_CONFIDENCE_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_margin_usage_pct_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_MAX_MARGIN_USAGE_PCT_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_risk_multiplier_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_RISK_MULTIPLIER_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_risk_multiplier_min_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_RISK_MULTIPLIER_MIN_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_risk_multiplier_max_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_RISK_MULTIPLIER_MAX_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_canary_force_symbol_overrides(cls) -> dict[str, Optional[bool]]:
        return cls._parse_bool_or_auto_map(cls.MT5_CANARY_FORCE_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_symbol_map(cls) -> dict[str, str]:
        """
        Parse MT5 symbol map from env string:
          XAUUSD=XAUUSDm,BTC/USDT=BTCUSD,ETH/USDT=ETHUSD
        """
        return cls._parse_upper_map(cls.MT5_SYMBOL_MAP)

    @classmethod
    def get_stock_yf_symbol_alias_map(cls) -> dict[str, str]:
        return cls._parse_upper_map(cls.STOCK_YF_SYMBOL_ALIAS_MAP)

    @classmethod
    def get_neural_min_prob_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.NEURAL_BRAIN_MIN_PROB_SYMBOL_OVERRIDES)

    @classmethod
    def get_neural_fx_soft_filter_band_low_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW_SYMBOL_OVERRIDES)

    @classmethod
    def get_neural_fx_soft_filter_band_high_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH_SYMBOL_OVERRIDES)

    @classmethod
    def get_neural_fx_soft_filter_max_penalty_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_fx_conf_soft_filter_band_pts_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_FX_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_fx_conf_soft_filter_max_penalty_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_FX_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_crypto_conf_soft_filter_band_pts_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_CRYPTO_CONF_SOFT_FILTER_BAND_PTS_SYMBOL_OVERRIDES)

    @classmethod
    def get_mt5_crypto_conf_soft_filter_max_penalty_symbol_overrides(cls) -> dict[str, float]:
        return cls._parse_float_map(cls.MT5_CRYPTO_CONF_SOFT_FILTER_MAX_SIZE_PENALTY_SYMBOL_OVERRIDES)

    @classmethod
    def get_exec_reasons_delta_marker_utc(cls) -> str:
        return str(cls.MT5_EXEC_REASONS_DELTA_MARKER_UTC or "").strip()

    @classmethod
    def get_stripe_price_plan_map(cls) -> dict[str, tuple[str, int]]:
        """
        Parse STRIPE_PRICE_PLAN_MAP from env:
          price_abc=a:30,price_def=b:30,price_xyz=c:90
        """
        parsed: dict[str, tuple[str, int]] = {}
        raw = (cls.STRIPE_PRICE_PLAN_MAP or "").strip()
        if not raw:
            return parsed
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            price_id, plan_days = chunk.split("=", 1)
            pid = price_id.strip()
            rhs = plan_days.strip().lower()
            if not pid or ":" not in rhs:
                continue
            plan, days_txt = rhs.split(":", 1)
            plan = plan.strip().lower()
            days_txt = days_txt.strip()
            if plan not in {"trial", "a", "b", "c"}:
                continue
            if not days_txt.isdigit():
                continue
            parsed[pid] = (plan, max(1, int(days_txt)))
        return parsed

    @classmethod
    def get_stripe_plan_price_ids(cls) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if cls.STRIPE_PRICE_ID_A:
            mapping["a"] = cls.STRIPE_PRICE_ID_A.strip()
        if cls.STRIPE_PRICE_ID_B:
            mapping["b"] = cls.STRIPE_PRICE_ID_B.strip()
        if cls.STRIPE_PRICE_ID_C:
            mapping["c"] = cls.STRIPE_PRICE_ID_C.strip()
        return mapping

    @classmethod
    def get_plan_days_map(cls) -> dict[str, int]:
        return {
            "a": max(1, int(cls.BILLING_PLAN_DAYS_A)),
            "b": max(1, int(cls.BILLING_PLAN_DAYS_B)),
            "c": max(1, int(cls.BILLING_PLAN_DAYS_C)),
        }

    @classmethod
    def get_plan_price_cents_map(cls) -> dict[str, int]:
        return {
            "a": int(cls.BILLING_PRICE_A_CENTS),
            "b": int(cls.BILLING_PRICE_B_CENTS),
            "c": int(cls.BILLING_PRICE_C_CENTS),
        }


config = Config()

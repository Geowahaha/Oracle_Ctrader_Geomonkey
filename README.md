# 🦞 Dexter Pro — AI Trading Agent

**XAUUSD (Gold) + Crypto (Top 50) + Global Stocks (262 stocks, 11 markets)**

---

## ⚡ Quick Setup (3 Steps)

### Step 1 — Install
```bash
pip install -r requirements.txt
```

### Step 2 — Configure `.env.local`
Your `.env.local` already has the Telegram bot token pre-filled.
You only need to add **2 things**:

```bash
# Open .env.local and fill in:
# Pick at least one AI key:
# ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx              # Anthropic
# GEMINI_API_KEY=AIza...                             # Google AI Studio Gemini
# GEMINI_VERTEX_AI_API_KEY=AQ....                    # Vertex AI Gemini (low-cost option)
# Optional model overrides:
# GEMINI_MODEL=gemini-2.0-flash
# GEMINI_VERTEX_MODEL=gemini-2.5-flash-lite
TELEGRAM_CHAT_ID=123456789               # your Telegram chat ID
TELEGRAM_BROADCAST_SIGNALS=1             # push scheduler alerts to entitled subscribers
```

**How to get your Telegram Chat ID:**
1. Open Telegram → search `@userinfobot`
2. Send `/start` → it replies with your numeric ID
3. Paste that number as `TELEGRAM_CHAT_ID`

### Step 3 — Run
```bash
# Start 24/7 monitor (recommended for OpenClaw)
python main.py monitor

# Or run a one-time scan
python main.py scan all
```

### Run 24/7 on Oracle Always Free VM
Use the Oracle deployment guide and bootstrap script:

- `ops/ORACLE_ALWAYS_FREE_VM.md`
- `ops/oracle_always_free_setup.sh`

---

## 📋 All Commands

```bash
python main.py monitor            # 24/7 auto-scan + Telegram alerts
python main.py scan all           # Full scan (gold + crypto + stocks)
python main.py scan gold          # XAUUSD only
python main.py scan crypto        # Top 50 crypto only
python main.py scan stocks        # All open stock markets
python main.py scan calendar      # Economic calendar (medium/high impact)
python main.py scan macro         # Macro policy-risk headlines (Trump/Fed/tariff)
python main.py scan vi            # US value + trend candidates
python main.py scan us_open       # 🇺🇸 US open top-10 daytrade plan
python main.py scan us_open_monitor  # 🇺🇸 US open smart monitor snapshot
python main.py stocks vi          # 🇺🇸 VI-style value + trend list
python main.py stocks us          # 🇺🇸 US markets
python main.py stocks thai        # 🇹🇭 Thailand SET50
python main.py stocks uk          # 🇬🇧 UK FTSE100
python main.py stocks jp          # 🇯🇵 Japan Nikkei
python main.py markets            # Global market hours status
python main.py overview           # XAUUSD overview → Telegram
python main.py research "Is gold bullish today?"   # AI research
python main.py status             # System status
python main.py mt5 status         # MT5 bridge/execution status
python main.py mt5 symbols        # Preview broker tradable symbols
python main.py mt5 bootstrap      # Suggest MT5_SYMBOL_MAP from broker symbols
python main.py mt5 bootstrap all  # Include stock universe in suggestion scan
python main.py mt5 backtest --days 30      # MT5 outcome backtest report
python main.py mt5 train --days 120        # Train backprop neural model
python main.py mt5 brain                   # Neural model status
python main.py billing status     # Billing webhook status
python main.py billing start      # Start Stripe/PromptPay webhook listener
```

Telegram commands also support:
`/plan`, `/upgrade`, `/grant <user_id> <trial|a|b|c> <days>` (admin), `/revoke <user_id>` (admin).

Upgrade flow in Telegram:
- `/upgrade` → show plans
- `/upgrade a` or `/upgrade b` or `/upgrade c` → create Stripe Checkout link with auto metadata
- If `STRIPE_PRICE_ID_A/B/C` is empty, bot uses `BILLING_PRICE_*_CENTS` fallback pricing.

---

## 🌍 Market Coverage

| Market | Stocks | Auto-Triggered (UTC) |
|--------|--------|----------------------|
| 🇺🇸 US S&P500 + NASDAQ | 65 | 13:35 + 19:55 |
| 🇬🇧 UK FTSE100 | 20 | 08:05 |
| 🇩🇪 Germany DAX40 | 20 | 08:05 |
| 🇫🇷 France CAC40 | 15 | 08:05 |
| 🇯🇵 Japan Nikkei225 | 20 | 01:35 |
| 🇭🇰 Hong Kong Hang Seng | 20 | 01:35 |
| 🇨🇳 China ADRs | 10 | 13:35 |
| 🇹🇭 **Thailand SET50** | **30** | **03:35 (dedicated)** |
| 🇸🇬 Singapore STI | 15 | 01:35 |
| 🇮🇳 India Nifty50 | 20 | 03:45 |
| 🇦🇺 Australia ASX | 15 | 23:00 |
| **TOTAL** | **262** | **24/7 coverage** |

Plus **XAUUSD (Gold)** every 15 min, **Top 50 Crypto** every 5 min,
**Economic Calendar alerts** on rolling lead-time windows (default: 60m/15m),
and **Macro Risk headline watch** (default: every 15m).

---

## 🔌 MT5 Execution (Optional)

You can auto-route scanner signals to MT5 through the OpenClaw MT5 Python bridge.

1. Run bridge server on Windows MT5 host:
```bash
python mt5_server.py
```

2. Configure in `.env.local`:
```bash
MT5_ENABLED=1
MT5_DRY_RUN=1
MT5_HOST=127.0.0.1
MT5_PORT=18812
# Optional broker symbol map:
# MT5_SYMBOL_MAP=XAUUSD=XAUUSDm,BTC/USDT=BTCUSD,ETH/USDT=ETHUSD
```

3. Validate connection:
```bash
python main.py mt5 status
python main.py mt5 symbols
python main.py mt5 bootstrap
```

4. Switch to live execution only after validation:
```bash
MT5_DRY_RUN=0
```

By default, stock execution is disabled (`MT5_EXECUTE_STOCKS=0`).

Live MT5 execution notifications:
- `MT5_NOTIFY_EXECUTED=1` sends Telegram when a trade is actually placed.
- `MT5_NOTIFY_FAILED=1` optionally sends broker reject/error updates.

---

## 🧠 Neural Brain (Backprop + MT5 Outcomes)

Dexter now includes a lightweight neural learner:
- Logs every MT5 execution attempt with signal features
- Syncs closed trade outcomes from MT5 history
- Syncs Telegram-sent signal outcomes from market TP/SL path
- Trains a small backprop model (ReLU + sigmoid) on labeled outcomes

Useful commands:
```bash
python main.py mt5 backtest --days 30
python main.py mt5 train --days 120
python main.py mt5 brain
```

Key `.env.local` controls:
```bash
ECON_CALENDAR_ENABLED=1
ECON_CALENDAR_CHECK_INTERVAL_MIN=5
ECON_ALERT_WINDOWS=60,15
ECON_ALERT_CURRENCIES=USD,EUR,GBP,JPY,CAD,AUD,NZD,CHF
MACRO_NEWS_ENABLED=1
MACRO_NEWS_CHECK_INTERVAL_MIN=15
MACRO_NEWS_MIN_SCORE=6
VI_TOP_N=10
VI_MIN_CONFIDENCE=76
VI_MIN_VOL_RATIO=1.0
VI_MIN_DOLLAR_VOLUME=25000000
VI_MAX_PE_RATIO=25
VI_LONG_ONLY=1
VI_MIN_SETUP_WIN_RATE=0.58
VI_RSI_MIN=55
VI_RSI_MAX=66
NEURAL_BRAIN_ENABLED=1
NEURAL_BRAIN_AUTO_TRAIN=1
NEURAL_BRAIN_MIN_SAMPLES=30
NEURAL_BRAIN_BOOTSTRAP_MIN_SAMPLES=10
NEURAL_BRAIN_EXECUTION_FILTER=1
NEURAL_BRAIN_MIN_PROB=0.55
NEURAL_BRAIN_FILTER_MIN_SAMPLES=60
NEURAL_BRAIN_FILTER_MIN_VAL_ACC=0.52
NEURAL_BRAIN_FILTER_MAX_MODEL_AGE_HOURS=720
NEURAL_BRAIN_SOFT_ADJUST=1
NEURAL_BRAIN_SOFT_ADJUST_WEIGHT=0.35
NEURAL_BRAIN_SOFT_ADJUST_MAX_DELTA=8.0
SIGNAL_FEEDBACK_ENABLED=1
NEURAL_BRAIN_SIGNAL_FEEDBACK_MAX_RECORDS=400
NEURAL_BRAIN_PSEUDO_LABEL_ENABLED=1
NEURAL_BRAIN_PSEUDO_LABEL_MIN_HOURS=2
NEURAL_BRAIN_PSEUDO_LABEL_MIN_ABS_R=0.25
```

Execution behavior:
- Signal confidence is softly adjusted by neural win-probability before Telegram/MT5 flow (never hard-blocked by this step).
- MT5 execution filter (`NEURAL_BRAIN_EXECUTION_FILTER=1`) is applied only when the model is stable (`samples`, `val_accuracy`, and model age pass thresholds). Otherwise, execution continues without neural blocking.

---

## 💳 Paid Access Layer (Trial + A/B/C)

Bot access is now entitlement-controlled via SQLite:
- Auto trial on first user interaction
- Plan-based feature gating
- Daily command quotas
- Admin plan management commands (`/grant`, `/revoke`)

Default command tiers:
- `TRIAL`: gold + US-open assistance + BTC/ETH crypto alerts + economic/macro risk + VI scanner
- `A`: trial + crypto scan
- `B`: A + stock scans + US open monitor + research
- `C`: B + full scan + MT5 status

Tune limits in `.env.local`:
`TRIAL_DAYS`, `PLAN_TRIAL_DAILY_LIMIT`, `TRIAL_CRYPTO_SYMBOLS`, `PLAN_A_DAILY_LIMIT`, `PLAN_B_DAILY_LIMIT`, `PLAN_C_DAILY_LIMIT`.

---

## 🔔 Auto-Upgrade Webhooks (Stripe + PromptPay)

Enable in `.env.local`:
```bash
BILLING_ENABLED=1
BILLING_WEBHOOK_HOST=0.0.0.0
BILLING_WEBHOOK_PORT=8787
STRIPE_WEBHOOK_SECRET=whsec_...
PROMPTPAY_WEBHOOK_SECRET=your_hmac_secret
```

Run listener:
```bash
python main.py billing start
```

Webhook endpoints:
- `POST /webhook/stripe`
- `POST /webhook/promptpay`
- `GET /health`

Required payment metadata for auto-upgrade:
- `telegram_user_id` (or `user_id`)
- `plan` (`trial|a|b|c`)
- `days` (integer)

Stripe notes:
- Signature header: `Stripe-Signature` (verified)
- Handles `checkout.session.completed`, `invoice.paid`, `payment_intent.succeeded`
- Optional fallback map: `STRIPE_PRICE_PLAN_MAP=price_abc=a:30,...`
- Direct checkout creation from bot requires:
  - `STRIPE_SECRET_KEY`
  - `STRIPE_CHECKOUT_SUCCESS_URL`
  - `STRIPE_CHECKOUT_CANCEL_URL`
  - `STRIPE_PRICE_ID_A/B/C`
  - Or fallback: `BILLING_CURRENCY` + `BILLING_PRICE_A_CENTS/B/C`

PromptPay notes:
- Signature header default: `X-PromptPay-Signature` (HMAC-SHA256)
- Paid status accepted: `paid|success|succeeded|completed`

Webhook re-delivery is safe: events are idempotent in SQLite and won't double-upgrade.

---

## 🔐 Config File Priority

```
.env.local   ← YOUR FILE  (highest priority, loaded first)
.env         ← fallback if .env.local not found
system env   ← fallback if neither file exists
```

**Never commit `.env.local` to Git** — it's already in `.gitignore`.

---

## 🏗️ Architecture

```
dexter_pro/
├── .env.local              ← YOUR CONFIG (Telegram + API keys)
├── .gitignore              ← protects secrets
├── config.py               ← loads .env.local automatically
├── main.py                 ← CLI entry point
├── scheduler.py            ← 24/7 background scan engine
├── market/
│   ├── data_fetcher.py     ← XAUUSD (yfinance) + Crypto (CCXT) data
│   ├── economic_calendar.py ← Economic event feed + impact filters
│   └── stock_universe.py   ← 262 global stocks, market hours
├── analysis/
│   ├── technical.py        ← EMA, RSI, MACD, ATR, BB, Stoch, Pivots
│   ├── smc.py              ← Order Blocks, FVG, BOS/ChoCH (Smart Money)
│   └── signals.py          ← 10-factor signal scoring engine
├── scanners/
│   ├── xauusd.py           ← Gold multi-TF scanner
│   ├── crypto_sniper.py    ← Crypto top-50 parallel scanner
│   └── stock_scanner.py    ← Global stocks parallel scanner
├── agent/
│   └── brain.py            ← Claude AI autonomous research agent
├── learning/
│   ├── neural_brain.py     ← MT5 outcome store + backprop trainer
│   └── mt5_backtester.py   ← MT5 history backtest helper
└── notifier/
    └── telegram_bot.py     ← Professional signal Telegram delivery
```

---

## ⚠️ Disclaimer
For research and informational purposes only. Not financial advice.
Always manage your own risk. Trading involves significant risk of loss.

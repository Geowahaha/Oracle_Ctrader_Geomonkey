# CLAUDE.md — dexter\_pro\_v3\_fixed

# ============================================================

# SAVE TO: D:\\dexter\_pro\_v3\_fixed\\dexter\_pro\_v3\_fixed\\CLAUDE.md

# Auto-loaded by Claude Code every session. Keep updated.

# ============================================================

## What This System Is

Dexter Pro is a fully autonomous multi-strategy AI trading system.
It is NOT a simple signal bot. It has:

* Multiple concurrent strategy families running in parallel (swarm model)
* A neural reasoning brain that evaluates market context per signal
* A self-learning layer that adapts confidence thresholds from live performance
* A trading manager that orchestrates all families + guards simultaneously
* A position manager that actively defends open trades in real-time
* A canary system that probes experimental strategies with minimal risk
* Live execution on cTrader and MT5 with real money

Owner: mrgeo | Bangkok (UTC+7) | Windows dev machine

## Full Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     MARKET DATA LAYER                        │
│  data/ — candles, ticks, OHLCV feeds (Binance, cTrader)     │
│  market/ — session detection, regime classification          │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                    ANALYSIS LAYER                            │
│  analysis/ — behavioral, structural, regime, sentiment       │
│  agent/ — chart state router, signal context builder         │
│  data/reports/chart\_state\_memory\_report.json                 │
│         └── accumulated band/session/pattern memory          │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                  NEURAL BRAIN LAYER                          │
│  AI providers: Gemini 2.0 Flash (primary)                    │
│                OpenRouter (fallback, multi-model routing)    │
│                Ollama local (qwen3:1.5b / llama3.2:3b)       │
│  learning/live\_profile\_autopilot.py — confidence band logic  │
│  learning/ — neural scoring, probability estimation          │
│  Purpose: enrich raw signals with market reasoning context   │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                SELF-LEARNING LAYER                           │
│  learning/ — winner logic, performance scoring               │
│  • Winner logic: historical session/band/side/symbol perf    │
│    → applies confidence bonus (+2.0) or penalty (-2.0/-4.5) │
│  • Chart state memory: accumulates band samples over time    │
│    → gates first\_sample\_mode and high\_confidence\_bridge      │
│  • Live profile autopilot: tunes confidence bands per setup  │
│  • Family promotion/demotion by win rate + PnL               │
│  • Crypto weekend scorecard: weekly performance analysis     │
│  System improves itself continuously from live trade results │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                  SCHEDULER / SIGNAL ROUTER                   │
│  scheduler.py — THE core brain of the system                 │
│  • Runs all scan intervals (XAU 300s, stocks 1800s, etc.)   │
│  • Routes each signal through regime guard → pattern gate    │
│    → confidence gate → direction guard → family selector     │
│  • Manages: behavioral, scalp, scheduled, canary, swarm      │
│  • US open smart monitor, XAU shock mode, mood-stop logic    │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│               TRADING MANAGER LAYER                          │
│  data/runtime/trading\_manager\_state.json                     │
│  • xau\_execution\_directive — master XAU bias                 │
│  • xau\_order\_care — monitors pending orders for staleness    │
│  • xau\_micro\_regime — short-window regime refresh            │
│  • xau\_cluster\_loss\_guard — pauses after cluster losses      │
│  • xau\_opportunity\_sidecar — tracks follow-on opportunities  │
│  • Swarm sampling — multiple families evaluated per signal   │
└────────────────────────┬────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
┌────────▼────────┐             ┌────────▼────────┐
│  STANDARD       │             │  CANARY SYSTEM   │
│  STRATEGIES     │             │  (probe layer)   │
│                 │             │                  │
│ XAU behavioral  │             │ Low-risk parallel│
│ XAU scheduled   │             │ trades that test │
│ Stocks scanner  │             │ strategy families│
│                 │             │ before scaling up│
└────────┬────────┘             └────────┬────────┘
         │                               │
         └───────────────┬───────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│              STRATEGY FAMILIES (all run in parallel)         │
│                                                              │
│  PRIMARY CANARY FAMILIES (tested, running live):            │
│  xau\_scalp\_pullback\_limit    CTRADER\_RISK=2.5 USD           │
│  btc\_weekday\_lob\_momentum    CTRADER\_RISK=1.1 USD           │
│                                                              │
│  EXPERIMENTAL FAMILIES (canary probe, lower risk):          │
│  xau\_scalp\_tick\_depth\_filter (TDF)  RISK=0.75 USD           │
│  xau\_scalp\_microtrend\_follow\_up (MFU)  RISK=0.65 USD        │
│  xau\_scalp\_flow\_short\_sidecar (FSS)  RISK=0.45 USD          │
│  xau\_scalp\_failed\_fade\_follow\_stop (FFFS)  RISK=0.75 USD    │
│  xau\_scalp\_range\_repair (RR)  RISK=0.75 USD                 │
│  eth\_weekday\_overlap\_probe  RISK=0.35 USD                    │
│                                                              │
│  Each family has: allowed patterns, sessions, direction      │
│  guards, confidence thresholds, and its own variant limit   │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│               EXECUTION LAYER                                │
│  execution/ — order placement, position sizing, risk check   │
│  api/ — cTrader OpenAPI + MT5 + Binance/Bybit wrappers       │
│                                                              │
│  Guards (all must pass before any order fires):              │
│  • check\_risk() — max positions, max USD at risk             │
│  • Direction guard — CTRADER\_BLOCK\_OPPOSITE\_DIRECTION=1      │
│  • Per-family/symbol/direction limits                        │
│  • Pending order limits per symbol                           │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│               POSITION MANAGER                               │
│  CTRADER\_POSITION\_MANAGER\_ENABLED=1                          │
│  • close\_at\_planned\_target — honor TP at entry time          │
│  • invalid\_TP repair — fixes broken TP after entry           │
│  • breakout/pullback repair TP logic                         │
│  • XAU active defense — real-time adverse flow detection     │
│    Uses: bar\_volume\_proxy, delta\_proxy, adverse\_drift        │
│    Can: tighten stop, close early, lock profit               │
│  • BE (breakeven) trigger after TP1 partial close            │
│  • Scheduled canary no-follow logic                          │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│              NOTIFICATION + MONITORING                       │
│  notifier/ — Telegram bot @mrgeon8n\_bot                      │
│  • Per-signal alerts (async, non-blocking)                   │
│  • US open session kickoff + quality reports every 15min     │
│  • Mood-stop alerts when opportunity breadth weakens         │
│  • XAU shock mode alerts on major news events                │
│  • Crypto weekend scorecard (Sundays UTC 00:15)              │
│  moltworker/ — background workers, health checks             │
│  ops/ — watchdog (dexter\_monitor\_watchdog.ps1)               │
└─────────────────────────────────────────────────────────────┘
```

## Directory Map

```
dexter\_pro\_v3\_fixed/
  agent/          ← chart state router, signal context builder
  analysis/       ← behavioral, structural, regime, sentiment analysis
  api/            ← broker wrappers (cTrader OpenAPI, MT5, Binance, Bybit)
  data/
    ctrader\_openapi.db        ← execution journal + all live trade records
    runtime/                  ← live JSON state (trading\_manager\_state etc.)
    reports/                  ← chart\_state\_memory\_report, autopilot reports
  docs/           ← audit checklists, session logs, prompt library
  execution/      ← order placement, position sizing, risk check, PM
  learning/       ← winner logic, neural scoring, autopilot, band tracking
  logs/           ← runtime logs (read-only reference)
  market/         ← session detection (asian/london/ny), regime classifier
  moltworker/     ← background workers, scheduled jobs
  notifier/       ← Telegram formatting, alert dispatch
  openclaw\_skills/← autonomous agent skill modules
  ops/            ← watchdog, health check, deployment scripts
  runtime/        ← live canary state, monitor logs, watchdog state
  scanners/       ← XAU scalp scanner, BTC scanner, ETH scanner, stocks
  store/          ← trade journal, persistent state, performance history
  tests/          ← unit + integration tests (pytest)
  scheduler.py    ← CORE: all scan loops + complete signal routing logic
  config.py       ← CORE: typed config object parsing all .env.local keys
  main.py         ← entry point, process orchestration
```

## Parent Directory

```
D:\\dexter\_pro\_v3\_fixed\\
  dexter\_pro\_v3\_fixed\\        ← project root (you are here)
  \_audit\\                     ← audit logs, read-only
  \_refs\\                      ← reference documents
  learning\\                   ← ML artifacts (shared)
  docs\\
    dexter\_claude\_prompts.py  ← Claude Code prompt templates
    dexter\_file\_guide.py      ← /add cheat sheet per task type
```

## Critical Files

|File|Purpose|
|-|-|
|`scheduler.py`|Core — all routing, all scan loops, all family selectors|
|`config.py`|All env vars parsed to typed object — source of truth|
|`.env.local`|Raw thresholds 1200+ lines — never hardcode from here|
|`main.py`|Entry point, process start|
|`execution/`|Order placement + position manager|
|`api/`|All brok## Recent Changes

### 2026-03-24 — Deep Data Architecture phase preparation (Pre-Phase Backup)

Status: **BACKUP COMPLETE** (Safe Restore Point Created)
Branch: `backtester-xauusd`
Tag: `v3-fixed-profitable-20260324`

**Backup Locations:**
- **Git:** Pushed to `origin` and `dexter` remotes (Tag: `v3-fixed-profitable-20260324`)
- **Local DB/Config Backup:** `C:\dexter_backups\`
  - `ctrader_openapi__backup__20260324_before-deep-data.db` (2.3GB)
  - `candle_data__backup__20260324.db`
  - `.env.local__backup__20260324`
  - `config__backup__20260324.py`
- **Restore Docs:** `RESTORE_POINT_2026-03-24.txt` at project root.

**Next Phase (Deep Data):**
- Implementing `TickBarEngine` (Quote-level exact Timestamp bars).
- Microstructure Regime Detector (MRD) testing.
- Order-Flow Asymmetry Scanner (OFAS).

## Session Startup

```
/add scheduler.py
/add config.py
/add data/runtime/trading_manager_state.json
```

Say: "Continue from 2026-03-24 pre-deep-data backup. What is current system state?"

## Never Ask Me About

* Basic Python, installing packages, general coding concepts
* Anything not related to this trading system's live behavior
Focus: multi-family trading logic, AI reasoning, self-learning adaptation, position management, live-market safety, signal routing bugs.idental exposure)
* SSH keys moved to: `C:\\Users\\mrgeo\\.ssh\\`
* `.gitignore` must cover: `.env.local`, `\*.key`, `\*.pem`, `\*.pub`, `\*.zip`

## Recent Changes

### 2026-03-20 — FSS Pattern Gate + High-Confidence Bridge

Files: `scheduler.py`, `config.py`, `tests/test\_scheduler\_watchlist.py`
Status: LIVE — PID 20028 since 09:35:57 ICT

Root cause: `scalp\_xauusd:fss:canary` not firing on confidence=82.0 setup
despite delta\_proxy=0.1391, bar\_volume\_proxy=1.0 (flow was strong).

Fix 1: Pattern bridge — FSS now accepts
"Behavioral Sweep-Retest + Liquidity Continuation" on continuation desk
Fix 2: `high\_confidence\_bridge` — 80+ borrows 70-79.9 band context
New key: `XAU\_FLOW\_SHORT\_SIDECAR\_FIRST\_SAMPLE\_ALLOW\_HIGH\_CONFIDENCE\_BRIDGE`
Flag written to raw\_scores for audit trail.

Watch: Does `scalp\_xauusd:fss:canary` now appear as `sell\_stop`?
Check: `SELECT \* FROM execution\_journal WHERE source LIKE '%fss%' ORDER BY id DESC LIMIT 5`

DO NOT revert without new regression test.

## Session Startup

```
/add scheduler.py
/add config.py
/add data/runtime/trading\_manager\_state.json
```

Say: "Continue from 2026-03-20 FSS fix. What is current system state?"

## Never Ask Me About

* Basic Python, installing packages, general coding concepts
* Anything not related to this trading system's live behavior
Focus: multi-family trading logic, AI reasoning, self-learning adaptation,
position management, live-market safety, signal routing bugs.

## Current Branch State (update every session)

Active branches:

* main → production, stable, do not touch
* fss-fixes-only → Fix 1 + Fix 3 only, ready to merge
* fls-development → FLS new feature, in development

Last session: 2026-03-21

* Fix 2 cooldown REMOVED (redundant, blocks opportunity)
* Fix 1 + Fix 3 confirmed safe
* FLS unauthorized code separated to fls-development branch
* Unauthorized: CTRADER\_PM\_XAU\_EXTENSION\_MIN\_CONFIDENCE (not approved)

Next session todo:

* Verify fss-fixes-only is clean (2 lines only)
* Merge fss-fixes-only → main
* Build FLS test lane in fls-development

● Now let me check the scanner's datetime usage to know exactly what to patch.

  Searched for 3 patterns (ctrl+o to expand)

● Let me read the key sections in the scanner that use datetime.now().

  Read 1 file (ctrl+o to expand)

● Let me look at how session_info is used in the scanner's direction logic.

  Searched for 1 pattern, read 2 files (ctrl+o to expand)

● Now I see the full issue. The _backtest_session_info() calls _original_get_info() which uses real datetime.now() for session detection. All 829 bars
  see the same Saturday session instead of their actual historical session. Let me also check what now is used for at line 323.

  Read 1 file (ctrl+o to expand)

● Now I see all the issues. datetime.now() at lines 323, 339, 363 uses real Saturday time for PDH/PDL and session window calculations. This corrupts the
   liquidity map analysis. The fix needs three things:

  1. Patch datetime in scanner module to use cursor time
  2. Fix get_session_info() to use cursor time for session detection
  3. Suppress EcoCalendar to prevent 429 spam

  Read 1 file (ctrl+o to expand)
  ⎿  You're out of extra usage · resets 5am (Asia/Bangkok)

✻ Worked for 19m 58s

● Background command "Run 3-day XAUUSD backtest with session_info patch" completed (exit code 0)
  ⎿  You're out of extra usage · resets 5am (Asia/Bangkok)

❯ /rate-limit-options
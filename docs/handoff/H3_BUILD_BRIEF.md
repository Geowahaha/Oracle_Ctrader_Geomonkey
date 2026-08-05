# H3 M1-FADE LANE BUILD BRIEF (pre-registered 2026-08-05 — execute VERBATIM)

Owner order: "เปิด session ใหม่แล้วสร้าง H3 M1 ต่อเลย". This file is the complete,
self-contained mission for that session (no worktree needed — work in this
repo directly). Do NOT re-tune or re-interpret the spec: it survived a
28-cell pre-registered campaign and re-tuning voids it.

## Context to load first
1. `docs/AGENT_SYNC_BOARD.md` — entries 2026-08-05 ~04:45Z (the M1 mission +
   this spec's honesty box) and ~02:00Z (live-wins-only standard).
2. Auto-memory `feedback_live_wins_only` — replay/backtest = filter, NEVER
   proof; success claims cite broker deals only.
3. Evidence status: explore +$11.4 (N=33, WR 55%) / confirm +$18.8 (N=20) /
   days+ 6/10 on 14k real M1 bars. N=53 is SMALL; 1-2 false survivors were
   statistically expected across the 28 cells — H3 could be one. The live
   N>=30 trial is the only judge.

## THE SPEC (lock as env defaults)
- Symbol XAUUSD, M1 bars, UTC hours 13:00–16:59 ONLY.
- Trigger: 5 consecutive same-direction M1 CLOSES, total move >= 2.0 × ATR1
  (RMA-14 of M1 true range) → FADE (sell after up-streak / buy after
  down-streak), MARKET at that M1 close.
- SL = 2.0 × ATR1 (wick disaster stop; ≈$3.3 at the 1-oz floor). Broker TP:
  none or far 3R backstop only.
- Exits in priority: (1) first M1 CLOSE with PnL >= +0.5R → close at market
  ("bank" — MUST be close-based: touch-TP was measured to kill the edge,
  explore +11.4 → −5.5); (2) 4-minute time stop; (3) broker SL. One position.
- Tier: DAILY_LOSS_USD=8, DAILY_TARGET_USD=15, BASE_RISK_FRAC=0.004,
  MAX_ENTRIES_PER_DAY=20, DEXTER3_V16_HOUSE_MONEY_ENABLED=0,
  DEXTER3_SL_FLOOR_TR_MULT=0 (producer owns the stop),
  DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD=300, MARKET_STATE_GATE=1,
  WEEKEND_FLATTEN=1, CROSS_LANE_DEDUP=downsize, VP_NO_TRADE_UTC=20:45-22:15.

## Build steps
1. `dexter3/h3fade.py` — pure producer, mirror `dexter3/mscalp.py` structure.
   Label family `dexter3:h3fade`, label `dexter3:h3fade:canary`.
2. M1 decision cadence: new mode `h3fade` in `dexter3/shadow_runner.py` —
   mirror the "mscalp" wiring (all ~10 touch points: import, state/log/lock
   consts, `_h3fade_producer_enabled`, `_alt_producer_enabled`, label/family/
   state/log resolvers, lock acquire/release, producer dispatch, run_loop and
   --once label forcing). Dispatch on NEW M1 CLOSES: track last-seen M1 ts in
   the lane's own state; fetch ~40 M1 bars per poll (poll-sec 20 acceptable).
3. M1-close bank/time exit: mode-gated branch (env, default off) on the fast
   tick for mode h3fade with an open position — fetch last 2–3 M1 bars; if
   latest CLOSED bar shows >= +0.5R → executor close (reason `h3_bank`); if
   position age >= 4 min → close (reason `h3_time`). Other lanes must be
   byte-identical when the env is absent.
4. Tests: producer trigger/sidedness/hours/ATR; runner isolation; exit branch.
   Full dexter3 suite green (baseline 1509).
5. `ops/dexter3-h3fade.service` (tier above) + add `("dexter3:h3fade","h3fade")`
   to `ops/dexter3_lane_tally.py` `_LANE_FAMILIES`.
6. Deploy per the board's "VM deploy discipline" row: push → on VM
   (129.150.36.17, key `C:/Users/mrgeo/.ssh/ssh-key-2026-03-21_Oracle_Geomonkey.key`,
   `/opt/dexter_pro`) `git checkout <sha> -- <files>` → diff-vs-sha EMPTY →
   py_compile → install unit → start ONLY `dexter3-h3fade` → verify banner
   mode/label, envs in `/proc/<pid>/environ`, first journal cycles.

## Guardrails (absolute)
- NEVER restart/touch `dexter3-fable` / `dexter3-daytrend` (frozen SL-floor
  experiment at 32/40 and 31/40 — whoever sees closed=40 renders the verdict
  on broker EV vs baselines −0.174R / −0.201R, on the board).
- All other lanes untouched. Update board + auto-memory after work; push to
  the `dexter` remote, branch `deploy-xau-family-canary`.
- Judgment: N>=30 broker deals in DOLLARS; negative → kill without ceremony,
  no tuning. Label evidence rungs on every claim.
- Disk note: D: has <2GB free — do NOT create worktrees or large temp files
  on D:; use `C:\Users\mrgeo\AppData\Local\Temp` for any scratch.

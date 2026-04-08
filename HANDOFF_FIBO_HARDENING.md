# HANDOFF_FIBO_HARDENING.md — AI Session Handoff

> ไฟล์นี้คือ "สมอง" สำหรับ AI ตัวถัดไป อ่านไฟล์นี้ก่อนจะรู้ทุกอย่าง

## 🎯 สถานะปัจจุบัน (2026-04-08 13:05 UTC+8)

### Project: Oracle_Ctrader_Geomonkey (Dexter Pro v3)
- Repo: https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git
- Branch: `main` (merge commit `6ed9d13`)
- PR #1 merged ✅

### สิ่งที่ทำเสร็จแล้ว (DON'T REDO):
1. ✅ `_cfg()` bool bug — `is None` check แทน `or default`
2. ✅ Sharpness error default — fail-safe (return False)
3. ✅ Circuit breaker — `_check_circuit_breaker()` + `report_trade_result()`
4. ✅ Daily loss cap — $20 → pause until midnight
5. ✅ Trend alignment gate — `_check_trend_alignment()` D1+H4 EMA
6. ✅ Scout auto-disable — ≥2 consec losses disables Scout
7. ✅ Raised thresholds — conf 68/62, fibo_score 45/35, RR 1.5/1.2
8. ✅ Scheduler wiring — `_feed_fibo_trade_results()` in sync cycle
9. ✅ Backtest wrapper — `backtest/run_fibo_backtest.py`
10. ✅ Cross-checked — 0 lines changed in shared modules
11. ✅ Pushed + PR #1 merged to main

### ยังไม่ได้ทำ (DO NEXT):
1. ❌ **Deploy to VM** — `git pull origin main` + restart service
2. ❌ **Unit tests** — ตาม test plan:
   - 3 consecutive losses → circuit breaker triggers
   - $20 daily loss → circuit breaker triggers
   - D1 bearish + H4 bearish → block LONG, allow SHORT
   - D1 bullish + H4 bullish → block SHORT, allow LONG
   - D1/H4 conflict → allow both
   - Sharpness error → return False (block)
   - Scout auto-disable on 2+ consec losses
3. ❌ **Backtest with cTrader data** — ต้อง Windows dev machine
4. ⚠️ **Revoke PAT token** — `ghp_x7VVX...` ที่ใช้ push

### Files changed (2 files only):
- `scanners/fibo_advance.py` — 6 fixes + circuit breaker + trend gate
- `scheduler.py` — `_feed_fibo_trade_results()` hook

### Files NOT changed (important!):
- `config.py` — ไม่แก้ (thresholds อยู่ใน `_cfg()` defaults)
- `scanners/xauusd.py` — behavioral fallback ไม่แตะ
- `scanners/crypto_sniper.py` — ไม่แตะ
- `execution/ctrader_executor.py` — ไม่แตะ
- `learning/*` — ไม่แตะ
- `analysis/fibonacci.py` — ไม่แตะ

## 📚 Key References

### Architecture:
- `CLAUDE.md` — project instructions (READ FIRST after this file)
- `config.py` — all env vars, 260KB

### Fibonacci Advance Scanner:
- `scanners/fibo_advance.py` — the main file we changed
- Line 66: `_cfg()` — config helper (FIXED)
- Line 111-160: circuit breaker logic (NEW)
- Line 395-434: trend alignment gate (NEW)
- Line 896-904: circuit breaker check in scan() (NEW)

### Scheduler:
- `scheduler.py` line 9141-9184: `_feed_fibo_trade_results()` (NEW)
- `scheduler.py` line 9186+: `_run_fibo_advance_scan()` (unchanged)
- `scheduler.py` line 7995-7999: feed hook in sync cycle (NEW)

### Previous Analysis:
- `MIMO_analysis_dexter_1.txt` — full code review (in workspace root)
- `MIMO_analysis_dexter_2.txt` — fibo root cause analysis
- `MIMO_fix_fibo_git.txt` — fix plan with code
- `MIMO_test_fibo_git.txt` — test cases

## 🔧 Context for New AI

### กฎสำคัญ:
- อย่าแก้ไฟล์อื่นนอกจาก `scanners/fibo_advance.py` และ `scheduler.py`
- อย่าเปลี่ยน config.py (thresholds อยู่ใน `_cfg()` defaults)
- ทุก fix ต้อง self-contained ใน fibo family
- ก่อนแก้ — cross-check ว่าไม่กระทบ family อื่น

### Git:
- Remote: https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git
- ต้อง PAT token เพื่อ push
- Commit author: ตั้ง git config ก่อน commit

### Memory files:
- `MEMORY.md` — long-term memory
- `memory/2026-04-08.md` — today's session log

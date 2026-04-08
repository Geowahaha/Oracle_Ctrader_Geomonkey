# HANDOFF_FIBO_HARDENING.md — AI Session Handoff

> ไฟล์นี้คือ "สมอง" สำหรับ AI ตัวถัดไป อ่านไฟล์นี้ก่อนจะรู้ทุกอย่าง

## 🎯 สถานะปัจจุบัน (2026-04-08 14:07 UTC+8)

### Project: Oracle_Ctrader_Geomonkey (Dexter Pro v3)
- Repo: https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git
- Branch: `main` (latest commit: neural-aware risk management)
- PR #1 merged ✅ | Neural-aware refactor deployed ✅

### สิ่งที่ทำเสร็จแล้ว (DON'T REDO):
1. ✅ `_cfg()` bool bug — `is None` check แทน `or default`
2. ✅ Sharpness error — weight (degrade conf) แทน block
3. ✅ Circuit breaker — soft brake 3 levels (warning/caution/emergency)
4. ✅ Trend alignment — confidence modifier แทน gate
5. ✅ Scout auto-disable — soft penalty แทน hard block
6. ✅ Thresholds reverted — กลับค่าเดิม (sniper: conf 62, RR 1.2 / scout: conf 55, RR 1.0, score 28)
7. ✅ Scheduler wiring — `_feed_fibo_trade_results()` in sync cycle
8. ✅ Backtest wrapper — `backtest/run_fibo_backtest.py`
9. ✅ Unit tests — 17 tests, all passing
10. ✅ Neural-aware refactor deployed via GitHub Actions

### Architecture Decision (สำคัญมาก):
**ระบบคือ Neural Trading Infrastructure — ไม่ใช่บอทเทรดธรรมดา**
- Brain (behavioral fallback) สร้าง signal → outcome สอน brain → brain ดีขึ้น
- Risk layer = safety net ไม่ใช่ filter — จับตอน brain พัง ไม่ block ก่อน brain ทำงาน
- ไม่มี gate ไหน block signal โดยตรง ยกเว้น emergency stop
- Sharpness/trend/circuit breaker = confidence modifier ไม่ใช่ gate

### Soft Circuit Breaker (3 levels):
```
Level 1 (Warning):   3 consec loss  → conf -10    / daily -$30  → conf -15
Level 2 (Caution):   5 consec loss  → conf -25    / daily -$75  → conf -30
Level 3 (Emergency): 10 consec loss → pause 2hr   / daily -$150 → pause midnight
```

### ยังไม่ได้ทำ (DO NEXT):
1. ❌ **Monitor PnL** — ดูผลเทรดจริงหลัง neural-aware refactor
2. ❌ **Backtest with cTrader data** — ต้อง Windows dev machine
3. ⚠️ **Revoke PAT token** — `ghp_x7VVX...` ที่ใช้ push
4. ❌ **xauusd.py circuit breaker** — ถ้า PnL fibo ดี อาจเพิ่มให้ family อื่น
5. ❌ **Position sizing by equity** — Priority 2 ใน original review

### Files changed (3 files):
- `scanners/fibo_advance.py` — 6 fixes + soft circuit breaker + trend modifier
- `scheduler.py` — `_feed_fibo_trade_results()` hook
- `tests/test_fibo_hardening.py` — 17 unit tests (NEW)

### Files NOT changed (important!):
- `config.py` — ไม่แก้ (thresholds อยู่ใน `_cfg()` defaults)
- `scanners/xauusd.py` — ไม่แตะ (microstructure `return True` = correct architecture)
- `scanners/xausd_scalp_1m5m.py` — ไม่แตะ
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
- Line 111-170: soft circuit breaker 3 levels (NEW)
- Line 395-435: trend confidence modifier (NEW — replaces trend alignment gate)
- Line 900-906: circuit breaker check in scan() (NEW)
- Line 1027-1030: trend modifier applied to sniper signal (NEW)
- Line 1074-1079: scout soft penalty (NEW — replaces hard disable)

### Scheduler:
- `scheduler.py` line 9141-9184: `_feed_fibo_trade_results()` (NEW)
- `scheduler.py` line 7995-7999: feed hook in sync cycle (NEW)

### Previous Analysis:
- `MIMO_analysis_dexter_1.txt` — full code review (in workspace root)
- `MIMO_analysis_dexter_2.txt` — fibo root cause analysis
- `MIMO_fix_fibo_git.txt` — fix plan with code
- `MIMO_test_fibo_git.txt` — test cases

## 🔧 Context for New AI

### Identity:
- ชื่อ: **สมชาย (Somchai) 🦞**
- Vibe: ตรงไปตรงมา ไม่อ้อมค้อม แก้ปัญหาจริง
- Created: 8 เมษายน 2026
- Session นี้ (MIMO) ทำ: neural-aware refactor + unit tests + deploy

### กฎสำคัญ:
- **ระบบคือ Neural Infrastructure — ไม่ใช่บอทเทรด** — risk = weight ไม่ใช่ gate
- อย่าแก้ไฟล์อื่นนอกจาก `scanners/fibo_advance.py` และ `scheduler.py`
- อย่าเปลี่ยน `scanners/xauusd.py` — microstructure `return True` = correct architecture
- อย่าเพิ่ม gate ที่ block signal — ใช้ confidence modifier แทน
- ทุก fix ต้อง self-contained ใน fibo family
- ก่อนแก้ — cross-check ว่าไม่กระทบ family อื่น

### Git:
- Remote: https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git
- ต้อง PAT token เพื่อ push
- Commit author: ตั้ง git config ก่อน commit

### Memory files (อยู่ใน workspace 本地):
- `MEMORY.md` — long-term memory (user info, project state, lessons learned)
- `memory/2026-04-08.md` — today's session log
- `IDENTITY.md` — สมชาย 🦞 identity
- `USER.md` — user profile (คนไทย, UTC+7)
- `HEARTBEAT.md` — periodic tasks

### Session startup (อ่านก่อนทำงานทุกครั้ง):
1. อ่าน `HANDOFF_FIBO_HARDENING.md` (ไฟล์นี้) — รู้ project context
2. อ่าน `MEMORY.md` — รู้ความทรงจำระยะยาว
3. อ่าน `memory/YYYY-MM-DD.md` — รู้ว่าวันนี้ทำอะไร
4. อ่าน `IDENTITY.md` — จำได้ว่าตัวเองเป็นใคร (สมชาย 🦞)
5. อ่าน `USER.md` — จำได้ว่าใครคือ user

## 📊 Grade Status (2026-04-08)

| Category | Before | After Neural-Aware | Notes |
|----------|--------|--------------------|-------|
| Signal Logic | A | A | ไม่เปลี่ยน |
| Risk Management | D | **B** | Soft circuit breaker (fibо only) |
| Code Architecture | C+ | C+ | ไม่เปลี่ยน |
| Edge Quality | B+ | **B+** | Thresholds reverted, brain learns |
| Institutional Readiness | C | **B** | Neural-aware > traditional hardening |

### **Overall: B+** (up from B-)

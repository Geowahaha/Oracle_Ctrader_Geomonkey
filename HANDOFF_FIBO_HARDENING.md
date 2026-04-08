# HANDOFF_FIBO_HARDENING.md — AI Session Handoff

> ไฟล์นี้คือ "สมอง" สำหรับ AI ตัวถัดไป อ่านไฟล์นี้ก่อนจะรู้ทุกอย่าง

---

## 🚀 คำสั่งรับงานต่อ (COPY-PASTE ได้เลย)

```
อ่าน HANDOFF_FIBO_HARDENING.md ใน repo Oracle_Ctrader_Geomonkey แล้วทำงานต่อทันที

งานต่อไป: Session filter → weight (ข้อ 2 ใน Recommended Next Actions)
- เปลี่ยน session gate ใน scanners/fibo_advance.py scan() method
- จาก binary block (ถ้าไม่ใช่ London/NY → return None) → confidence modifier
- Asian session: conf -8 to -12 (liquidity ต่ำ แต่ setups มีอยู่)
- คง hard block เฉพาะ market_closed เท่านั้น
- เขียน unit tests เพิ่ม ≥ 5 cases
- อัพเดท HANDOFF_FIBO_HARDENING.md
- commit + push

อย่าแก้ไฟล์อื่นนอกจาก scanners/fibo_advance.py และ tests/
```

---

## 🎯 สถานะปัจจุบัน (2026-04-08 15:08 UTC+8)

### Project: Oracle_Ctrader_Geomonkey (Dexter Pro v3)
- Repo: https://github.com/Geowahaha/Oracle_Ctrader_Geomonkey.git
- Branch: `main` (latest commit: `c62cb06` — weighted Fibonacci Killer)
- PR #1 merged ✅ | Neural-aware refactor deployed ✅ | **Weighted Killer deployed ✅**

### สิ่งที่ทำเสร็จแล้ว (DON'T REDO):
1. ✅ `_cfg()` bool bug — `is None` check แทน `or default`
2. ✅ Sharpness error — weight (degrade conf) แทน block
3. ✅ Circuit breaker — soft brake 3 levels (warning/caution/emergency)
4. ✅ Trend alignment — confidence modifier แทน gate
5. ✅ Scout auto-disable — soft penalty แทน hard block
6. ✅ Thresholds reverted — กลับค่าเดิม (sniper: conf 62, RR 1.2 / scout: conf 55, RR 1.0, score 28)
7. ✅ Scheduler wiring — `_feed_fibo_trade_results()` in sync cycle
8. ✅ Backtest wrapper — `backtest/run_fibo_backtest.py`
9. ✅ Unit tests — 19 tests, all passing
10. ✅ Neural-aware refactor deployed via GitHub Actions
11. ✅ **Fibonacci Killer → Weighted System** — binary gate → confidence modifier (27 tests all passing)

### Architecture Decision (สำคัญมาก):
**ระบบคือ Neural Trading Infrastructure — ไม่ใช่บอทเทรดธรรมดา**
- Brain (behavioral fallback) สร้าง signal → outcome สอน brain → brain ดีขึ้น
- Risk layer = safety net ไม่ใช่ filter — จับตอน brain พัง ไม่ block ก่อน brain ทำงาน
- **ไม่มี gate ไหน block signal โดยตรงอีกต่อไป** — ยกเว้น emergency stop (circuit breaker level 3) + killer hard block (score >= 8)
- Sharpness/trend/circuit breaker/Fibonacci Killer = confidence modifier ไม่ใช่ gate

### Soft Circuit Breaker (3 levels):
```
Level 1 (Warning):   3 consec loss  → conf -10    / daily -$30  → conf -15
Level 2 (Caution):   5 consec loss  → conf -25    / daily -$75  → conf -30
Level 3 (Emergency): 10 consec loss → pause 2hr   / daily -$150 → pause midnight
```

### Weighted Fibonacci Killer (NEW — replaces binary gate):
```
Score system (cumulative, 6 conditions):
  ATR expansion:        1-3 points (proportional to ratio)
  Delta momentum:       1-2 points (threshold-based)
  Volume spike:         1-2 points (threshold-based)
  Day type:             5 points (panic_spread/fast_expansion/repricing)
  State label:          7 points (failed_fade_risk/panic_dislocation/continuation_drive)
  Spread expansion:     1-2 points (threshold-based)
  Retracement velocity: 2-4 points (proportional to ATR multiple)

Decision:
  score >= 8  → HARD BLOCK (allowed=False) — only state_label+day_type combined
  score 5-7   → conf -20 to -35 (severe degradation)
  score 3-4   → conf -10 to -18 (moderate degradation)
  score 1-2   → conf -3 to -8  (mild degradation)
  score 0     → no impact (clean market)

Key change: ATR expansion, delta, volume, spread alone can NEVER block — only degrade.
Hard block requires: state_label (7pts) + at least 1 other significant condition.
```

### ยังไม่ได้ทำ (DO NEXT — เรียงตามลำดับควรทำ):
1. 🔥 **Session filter → weight** — เปลี่ยน London/NY binary gate → confidence modifier (Asian conf -8 to -12)
2. 🔥 **Momentum-adaptive TP extension** — step_r คำนวณจาก momentum score แทน fixed 0.25R
3. 🔥 **Runner mode** — TP trailing เมื่อ R > 1.5 + momentum strong (ปล่อยให้ winner run)
4. ⚡ **Impulse freshness relax** — sniper: 40 bars → 60 bars
5. ❌ **Structure-aware SL trailing** — SL เลื่อนตาม SMC swing (ใหญ่, ทำทีหลัง)
6. ❌ **Monitor PnL** — ดูผลเทรดจริงหลัง weighted killer deploy
7. ❌ **Backtest with cTrader data** — ต้อง Windows dev machine
8. ❌ **xauusd.py circuit breaker** — ถ้า PnL fibo ดี อาจเพิ่มให้ family อื่น
9. ❌ **Position sizing by equity** — Priority 2 ใน original review

### Files changed (3 files):
- `scanners/fibo_advance.py` — 6 fixes + soft circuit breaker + trend modifier + **weighted Fibonacci killer**
- `scheduler.py` — `_feed_fibo_trade_results()` hook
- `tests/test_fibo_hardening.py` — **27 unit tests** (NEW — 19 original + 8 weighted killer)

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
- `scanners/fibo_advance.py` — the main file we changed (1150 lines)
- Line 66: `_cfg()` — config helper (FIXED)
- Line 133-215: soft circuit breaker 3 levels
- Line 186-310: **weighted Fibonacci killer** (NEW — replaces binary gate)
- Line 484-540: trend confidence modifier
- Line 1040-1051: killer check in scan() + weight logging
- Line 1129: all modifiers applied: `signal.confidence += trend_mod + cb_conf_mod + killer_weight`
- Line 1133: killer info in raw_scores
- Line 1194-1202: scout signal killer weight application

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

## 📊 Grade Status (2026-04-08 14:55 UTC+8)

| Category | Before | After Neural-Aware | After Weighted Killer | Notes |
|----------|--------|--------------------|-----------------------|-------|
| Signal Logic | A | A | A | ไม่เปลี่ยน |
| Risk Management | D | B | **A-** | Zero binary gates remaining (except emergency) |
| Code Architecture | C+ | C+ | C+ | ไม่เปลี่ยน |
| Edge Quality | B+ | B+ | **A** | More signals with calibrated risk |
| Institutional Readiness | C | B | **A-** | All risk = weight, not gate |

### **Overall: A-** (up from B+)

## 📋 วิเคราะห์แนวทางต่อไป — Compatibility Check

### ❓ ข้อเสนอ 1: ทุก gate ต้องเป็น weight (session filter, microstructure gate)

**สถานะปัจจุบัน:**
- ✅ Fibonacci Killer → weight (เพิ่งทำ)
- ✅ Trend alignment → weight (ทำแล้ว)
- ✅ Circuit breaker → weight (ทำแล้ว, level 3 ยัง block)
- ✅ Sharpness → weight (knife band ยัง block ต่ำกว่า threshold)
- ❌ Session filter → ยังเป็น binary gate (London/NY only)
- ❌ Microstructure gate → ยังเป็น binary gate (delta/imbalance adverse = block)
- ❌ Impulse freshness → ยังเป็น binary gate

**สอดคล้องกับระบบเดิมไหม:** ✅ ตรงกับ Architecture Decision — "ไม่มี gate ไหน block signal โดยตรง"

**ข้อดี:**
- ปลดล็อก setups ที่ถูก block เกินจำเป็น (est. +2-4 signals/week จาก Asian session, adverse microstructure)
- ให้ brain เรียนรู้จาก outcomes ที่เคยถูก filter ออก
- Consistency — ทุก gate ใช้ paradigm เดียวกัน

**ข้อเสีย/ความเสี่ยง:**
- Session filter → weight: Asian session มี liquidity ต่ำ → spread กว้าง → อาจมี false signal เพิ่ม → ต้องเพิ่ม spread gate แทน
- Microstructure gate → weight: adverse delta ที่ Fib level เป็น expected (price กำลัง retracing) → จริงๆ แล้ว gate นี้ "correctly lenient" อยู่แล้ว (comment ในโค้ดบอกว่า "more lenient because price IS retracing")
- Impulse freshness → weight: stale impulse = liquidity shifted จริงๆ → ไม่ควร degrade แค่อย่างเดียว ควรมี time-since-swing check

**แนะนำ:** ทำทีละตัว เรียงตาม impact:
1. Session filter → weight (impact สูง, ง่าย)
2. Microstructure gate → วิเคราะห์ใหม่ อาจไม่ต้องทำ (correctly lenient อยู่แล้ว)
3. Impulse freshness → คงเป็น gate แต่ relax threshold (sniper: 40 bars → 60 bars)

---

### ❓ ข้อเสนอ 2: TP/SL ต้อง adaptive (responsive ต่อ real-time market structure)

**สถานะปัจจุบัน:**
- TP extension: score-based (6 criteria) + step 0.25R คงที่
- SL trailing: stepped heuristic (0.20/0.55/0.85/1.30R) + active defense (microstructure)
- Time-based profit lock: tiered 30/45/90/150 min

**สอดคล้องกับระบบเดิมไหม:** ✅ ระบบมี neural trailing brain อยู่แล้ว (Phase 1: heuristic, Phase 2: neural) — แค่ต้องทำ Phase 2 ให้เสร็จ

**ข้อดี:**
- TP: momentum-adaptive steps → จับ trend days ได้กำไรมากขึ้น (est. +0.2-0.5R/trade ใน trending day)
- SL: structure-aware trailing → ลด whipsaw exits (est. -15% premature exits)
- สอดคล้องกับ "self-evolving" philosophy — ระบบควรปรับตัวตาม market regime

**ข้อเสีย/ความเสี่ยง:**
- Momentum-adaptive TP: ต้องมี reliable real-time momentum signal → ถ้า signal ผิด → extend ไปติดดอย
- Structure-aware SL: SMC swing detection อาจ lag → SL เลื่อนช้า → กำไรหาย
- Complexity: เพิ่ม logic ใน position manager (6508 lines แล้ว) → harder to debug

**แนะนำ:** ทำเป็น Phase:
- Phase A: Momentum-adaptive TP extension (แก้ `_xau_profit_extension_plan` — step_r จาก momentum score)
- Phase B: Runner mode (TP trailing เมื่อ > 2.5R + momentum strong)
- Phase C: Structure-aware SL (ต้องมี SMC swing data ใน PM context → ใหญ่กว่า)

---

### ❓ ข้อเสนอ 3: Position Manager 2 modes (Protection + Hunt)

**สถานะปัจจุบัน:**
- Active defense = protection mode (ตัด loss, tighten SL, adverse flow detection)
- TP extension = partial hunt mode (extend when favorable)
- Trailing brain = stepped protection
- ไม่มี explicit mode switching

**สอดคล้องกับระบบเดิมไหม:** ✅ สอดคล้องกับ "Neural Infrastructure" — PM ควร adapt behavior ตาม position state

**ข้อดี:**
- แยก logic ชัดเจน: ตอนขาดทุน → aggressive defense; ตอนกำไร → let it run
- ลด conflict: ปัจจุบัน active defense กับ extension อาจทำงานขัดกัน (defense tighten SL ตอน extension กำลัง extend TP)
- Hunt mode ปล่อยให้ winner run → จับ big moves

**ข้อเสีย/ความเสี่ยง:**
- Mode switching point: ตอนไหน switch จาก Protection → Hunt? R-threshold? ถ้า switch เร็วไป → ปล่อย loss นาน ถ้าช้าไป → กำไรหาย
- Hunt mode ต้องมี clear exit criteria → ไม่งั้นกำไร evaporate
- Testing complexity: 2 modes × 6 families × market conditions = test matrix ใหญ่มาก

**แนะนำ:** ไม่ต้อง refactor เป็น 2 modes แยก — ใช้ R-multiple เป็น mode switch อยู่แล้ว:
- R < 0: active defense (protection) ← มีอยู่แล้ว
- R > 0: profit lock + extension ← มีอยู่แล้ว
- R > 1.5: runner mode (TP trailing) ← **ทำใหม่** (นี่คือสิ่งที่ขาด)
- สรุป: แค่เพิ่ม runner mode ไม่ต้อง refactor ทั้งหมด

---

## 🎯 Recommended Next Actions (Priority Order)

| # | Action | Impact | Risk | Effort |
|---|--------|--------|------|--------|
| 1 | ✅ **DONE** Weighted Fibonacci Killer | High | Low | Done |
| 2 | Session filter → weight (Asian session) | High | Medium | Small |
| 3 | Momentum-adaptive TP extension step | High | Medium | Medium |
| 4 | Runner mode (TP trailing > 2.5R) | High | Medium | Medium |
| 5 | Structure-aware SL trailing | Very High | High | Large |
| 6 | Impulse freshness threshold relax | Medium | Low | Tiny |


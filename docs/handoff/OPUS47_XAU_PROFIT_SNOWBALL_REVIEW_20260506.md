# Opus 4.7 Review — XAU Profit Snowball & Basket Guardian Architecture

Source: `/tmp/opus47_xau_snowball_review.md`
Mode: read-only strategist/reviewer. No code changes.

---

# 🧠 Dexter Pro XAU — Profit Snowball & Basket Guardian Architecture
**Reviewer:** Opus 4.7 (read-only, strategist mode)
**Live ref:** `deploy-xau-family-canary` @ `80c96c1`
**Evidence base:** cTrader statement 2026-05-06 (Opening direction truth) + DB broker-sync 12:06 UTC

---

## 1) Verdict — สิ่งที่พังจริงหลัง unlock

ปัญหาไม่ใช่ "unlock เปิดประตูให้ noise SELL" อย่างเดียว — นั่นคืออาการรอง
**โรคหลักคือ "Basket Memory Loss"**:
- ระบบจำได้ว่าแต่ละ position มี SL/TP ของมันเอง แต่ **ไม่มีหน่วยความจำของ basket-level peak P&L**
- Realized +224 + Unrealized +26 = **session peak ~ +250** แต่ไม่มี module ใดยึด tier นี้เป็น "high-water mark" แล้วบังคับ defense
- LONG winners 4 ตัวบนสุด (+67, +51, +45, +25) แสดงว่า **trend catching ทำงานดี** — opportunity-first ไม่ผิด
- แต่หลัง peak ระบบยังเปิด LONG เพิ่มที่ **4719.83 / 4713.02** ขณะ mid ลงมา 4685 → นี่คือ **late-cycle pyramid into exhaustion**, ไม่ใช่ trend continuation
- Blind SELL @ 4709 (TP 2663, SL 5670) คือ **schema-level bug** — TP/SL ratio ไร้สาระ → หมายถึง signal ไม่ผ่าน sanity check ก่อนยิง

**สรุปสาเหตุราก:**
1. ไม่มี **equity-peak ratchet** ระดับ basket
2. ไม่มี **trend-phase awareness** หลัง fill (entry router ดู phase ตอนเข้า แต่ PM ไม่ดู)
3. ไม่มี **TP/SL coherence guard** → ปล่อย order ที่ TP ไกลกว่า price 2000+ pts ผ่านได้
4. PM อ่านจาก state ภายใน ไม่ใช่ broker-truth → เกิด drift ระหว่างที่คิดกับสิ่งที่จริงในบัญชี

---

## 2) Conceptual Model — "Profit Thermodynamics & The Reservoir"

ทิ้ง mental model "trailing stop" ไปก่อน ใช้กรอบนี้แทน:

### กรอบที่ 1 — **Profit as a Two-Phase Fluid (Reservoir Model)**
กำไรในระบบมี 2 สถานะ:
- **Liquid (Unrealized)** — ระเหยได้, ไวต่อ noise, มี half-life
- **Solid (Realized + locked equity)** — ไม่ระเหย, แต่ก็ยัง drain ออกได้ผ่าน new losing entries

**Information Half-Life** ของ unrealized profit บน XAU ในแต่ละ regime:
- Trend phase (impulse): half-life ยาว ~ 30–90 นาที → ปล่อยวิ่งคุ้ม
- Distribution/exhaustion: half-life สั้น ~ 5–15 นาที → **ต้อง crystallize ทันที**
- News/shock: half-life อาจ < 60 วินาที

→ Module ต้องคำนวณ **half-life ปัจจุบัน** แล้วตัดสินว่าเก็บเป็น solid เร็วแค่ไหน

### กรอบที่ 2 — **Basket Convexity (Anti-fragile snowball, ไม่ใช่ symmetric trail)**
แทนที่จะเลื่อน SL ตามราคา ให้คิดในแง่ **payoff convexity ของ basket รวม**:
- กำไรที่ "unlock" จาก realize partial → ใช้เป็น **buffer ขยาย risk envelope** ของ runner ตัวเด็ด
- runner ตัวเด็ด (>2R unrealized + trend phase = impulse) ได้สิทธิ์ขยาย SL **ลึกขึ้น** ไม่ใช่ตื้นขึ้น เพราะ buffer cover ไว้แล้ว
- Pyramid adds ที่ "อ่อน" ถูก **prune ก่อน peak ตัวที่แข็ง** เสมอ → "kill the weakest, feed the strongest"

นี่คือ Taleb-style **convex bet structure** — กำไรเล็กหลายตัวกลายเป็น optionality ให้ตัวใหญ่

### กรอบที่ 3 — **Equity Peak Memory + Hysteresis**
ระบบต้องจำ 3 peak พร้อมกัน:
- `session_realized_peak` (วันนี้)
- `basket_unrealized_peak` (วันนี้)
- `combined_equity_peak` (rolling 24h)

จากนั้นใช้ **hysteresis bands** (ไม่ใช่ threshold เดียว):
- เกิน peak → **ratchet up** ทันที (one-way)
- ตกจาก peak X% → **trigger Tier 1 defense** (prune weak)
- ตก Y% → **Tier 2** (crystallize partial winners)
- ตก Z% → **Tier 3** (full harvest + cooldown)

X/Y/Z ไม่ใช่ค่าคงที่ แต่ **scale ตาม regime volatility (ATR-percentile) + trend phase**

---

## 3) Concrete Modules — Architecture Spec

### 🔵 **M1: BrokerTruthMonitor** (source-of-truth layer)
- **Inputs:** cTrader OpenAPI position stream (push), reconcile กับ `ctrader_positions` table ทุก 5s
- **Direction rule:** **เฉพาะ `ctrader_positions.direction`** หรือ Opening direction จาก deal join — **ห้ามใช้ `ctrader_deals.direction` เดี่ยวๆ**
- **Outputs:** `live_basket_state` JSON
  ```
  {
    positions: [{pos_id, opening_dir, entry, current, mfe, mae, age_sec, family, lineage}],
    realized_today, unrealized_now,
    equity_now, equity_peak_24h,
    drawdown_from_peak_pct
  }
  ```
- **State:** persist ลง `data/runtime/basket_truth.json` ทุก tick + WAL log
- **Fail-safe:** ถ้า reconcile mismatch > 2 ครั้งติด → freeze ทุก PM action, alert Telegram

### 🟢 **M2: EquityRatchet** (peak memory + tier engine)
- **Inputs:** M1 stream
- **State:**
  ```
  session_peak_realized, session_peak_unrealized, session_peak_combined,
  ratchet_floor_T1, ratchet_floor_T2, ratchet_floor_T3,
  last_ratchet_event_ts
  ```
- **Logic:**
  - peak ใหม่ → **อัพ floor แบบ monotonic** (ไม่ลดลงเด็ดขาดในเซสชั่น)
  - คำนวณ tier thresholds เป็น **% ของ peak × regime_multiplier**
  - regime_multiplier = f(ATR_percentile_60min, session, news_proximity)
- **Outputs:** `tier_state ∈ {NORMAL, T1_PRUNE, T2_CRYSTALLIZE, T3_HARVEST}`
- **Hysteresis:** ขึ้น tier เร็ว, ลง tier ช้า (ต้องไม่ทำ peak ใหม่ติด 3 bars ก่อนผ่อน)

### 🟡 **M3: TrendPhaseClassifier** (post-fill, per-position)
- **Inputs:** M1 lifetime + M1 price action ของ position แต่ละตัว, OB structural features
- **Phases:** `IMPULSE | CONTINUATION | DISTRIBUTION | EXHAUSTION | REVERSAL`
- **Features (advanced, ไม่ใช่แค่ MA cross):**
  - Bar-volume z-score vs lifetime (ถ้า volume ตกขณะ price ทำ HH → distribution)
  - Delta_proxy decay rate (รับมาจาก existing analysis)
  - HH/HL streak length vs ATR-normalized
  - Time-of-day session phase decay (e.g., late NY)
- **Outputs:** phase per position + `phase_confidence`
- **State:** ผูกกับ position_id ใน basket_truth

### 🔴 **M4: LongRunnerPreservation** (let winners run + protect)
- **Trigger:** position MFE ≥ 1.5R **AND** phase ∈ {IMPULSE, CONTINUATION}
- **Action — convex SL widening (ตรงข้าม trail):**
  - SL ไม่ tighten แบบ trail ปกติ
  - แทน: SL ผูกกับ **structural pivot ล่าสุด - k×ATR** โดย k โต ขึ้นเมื่อ MFE โต
  - "ให้ที่ระบาย noise มากขึ้น เมื่อกำไรเพียงพอ buffer"
- **Partial harvest schedule (non-linear):**
  - 2R: ขาย 25% → **เปลี่ยน status เป็น "house money"**
  - 4R: ขาย เพิ่ม 25%
  - 6R+: hold core 50% จนกว่า phase จะ flip เป็น DISTRIBUTION/REVERSAL
- **Outputs:** PM directive ต่อ position
- **Why advanced:** การที่คุณ clipped ตัว +67 ก่อนหน้านี้ไม่ได้เกิด เพราะ logic นี้จะ recognize impulse และ **ขยาย SL** ไม่ใช่ขยับเข้า

### 🟠 **M5: WeakAddPruner** (kill the weak, feed the strong)
- **Trigger:** ทุกครั้งที่มี position ใหม่ join basket หรือทุก 60s
- **Score per position:**
  ```
  weakness_score = w1*(MAE/R) + w2*(time_underwater_sec)
                 + w3*(1 - phase_alignment)
                 + w4*(distance_to_SL_atr_normalized)
                 - w5*(MFE/R)
  ```
- **Rule:** ใน tier T1_PRUNE → close top-K weakest LONGs (K = 1–2)
  เก็บเฉพาะตัวที่ MFE > 0 และ phase ∈ {IMPULSE, CONTINUATION}
- **Pyramid guard:** ถ้า basket มี > N LONGs และ price < average entry → **ห้าม add**
- **เคสจริง:** หลัง peak +250 ระบบเปิด LONG @ 4719.83 ขณะ mid 4685 → weakness_score สูง (MAE > 1R, phase อาจเป็น DISTRIBUTION) → prune ก่อนวันรุ่ง

### ⚫ **M6: BlindSignalKiller** (TP/SL coherence sanity)
- **กฎ:** ทุก signal ก่อน execute ต้องผ่าน
  ```
  |TP - entry| / ATR(M15) ∈ [0.3, 8.0]
  |SL - entry| / ATR(M15) ∈ [0.2, 4.0]
  R:R ∈ [0.5, 10.0]
  TP_direction_consistent_with_signal_direction
  ```
- **เคสจริง:** SELL @ 4709, TP 2663 (~2046 pts), SL 5670 (~961 pts) → R:R = 2.13 แต่ |TP|/ATR = ~400 → **reject** ทันที
- **ไม่ใช่ entry block แบบกว้าง** — เป็น schema validator → preserve opportunity-first

### 🟣 **M7: SpikeReversalHazardDetector** (early warning)
- **Inputs:** tick stream, M1 close, news calendar, OB depth, delta_proxy slope
- **Hazard score (0–100):**
  - ATR(1m) jump > p95 ของ rolling 60min → +30
  - Delta_proxy sign flip + magnitude > p90 → +25
  - HH streak break + close below previous swing low → +20
  - Inside scheduled news ±5min → +15
  - DOM imbalance flip (depth_imbalance sign change > 0.4) → +10
- **Outputs:**
  - `hazard ≥ 60` → ส่งสัญญาณให้ M2 บังคับเข้า T2 ทันที (ไม่ต้องรอ drawdown threshold)
  - `hazard ≥ 80` → T3 + freeze new entries 10 นาที
- **Why advanced:** ป้องกันก่อน peak จะร่วง — ไม่ใช่ react หลัง drawdown

### 🟤 **M8: SnowballCapitalAllocator** (re-deploy after harvest)
- หลัง harvest กำไร realized → **ไม่กลับไป size เดิม** ทันที
- ใช้ **Kelly-fractional × regime_quality**:
  ```
  next_risk = base_risk × (1 + α × realized_session/equity_start)
              × kelly_fraction(family_winrate_30d, avg_R)
              × regime_quality_score
  ```
- **Cap:** ไม่ให้ risk_per_trade เกิน 1.5× base ในวันเดียวกัน (ป้องกัน euphoria pyramiding)
- **Cool-down:** ถ้า hazard ≥ 60 ใน 30min ที่แล้ว → next_risk × 0.5
- **Outcome:** กำไรกลายเป็น compounding fuel แต่ไม่ใช่ภาษี euphoria

---

## 4) Adaptive Decision Rules — ใช้กับเคสจริง +224/+26 → -241

| เวลา | State | Rule fires | Action |
|---|---|---|---|
| Pre-peak | combined +250, peak ratchet update | M2 set `T1_floor = peak − 8% × ATR_mult` | floor ~ +230 |
| LONG @ 4719.83 fill | mid 4685, MAE 0.7R, phase=DISTRIBUTION | M5 weakness_score high; M3 phase mismatch | **block add** หรือ size 0.3× |
| SELL @ 4709 schema check | TP 2663 (insane) | M6 reject | killed before broker |
| ATR 1m spike + delta flip | hazard score → 65 | M7 force T2 | crystallize 50% LONG winners → realized lock + cool-down |
| Combined drops to +200 | crosses T1 floor | M5 prune 2 weakest LONGs | drawdown stops at ~+180 instead of going to -241 |
| Hazard 80 + structural break | M2 → T3 | full harvest + 10min freeze | session locks ~+170 minimum |

**ผลคาดว่า:** แทนที่จะ swing +250 → -241 (delta -491), จบที่ +170 ถึง +200 และพร้อม redeploy

---

## 5) ทำยังไงให้ Opportunity-First ยังอยู่

- **PM intelligence อยู่ post-fill เท่านั้น** — entry gates ไม่แตะ
- M6 BlindSignalKiller เป็น **schema validator** ไม่ใช่ confidence gate → ไม่ลด entry rate ของสัญญาณคุณภาพ
- M5/M7 อาจ pause **new adds** ชั่วคราวเฉพาะตอน hazard สูง — แต่ไม่ block fresh structural signals หลัง cool-down
- ทุก rejection ต้องเขียน reason code → audit ได้ว่าไม่กลายเป็น hidden block

---

## 6) ไม่ตัด Winner ตัวต่อไป (+67/+100) เร็วเกินไปยังไง

- M4 LongRunnerPreservation **ขยาย SL** ตาม MFE ไม่ใช่หด
- Partial harvest **non-linear** — เก็บ core 50% ไว้จนกว่า phase จะ flip
- "House money" status: ตัวที่ harvest 25% ไปแล้ว มี SL ผูกที่ entry-buffer (กำไรขั้นต่ำ lock) แต่ TP ไม่จำกัด
- M3 phase classifier ใช้ **structural pivot + delta** ไม่ใช่ time-based exit → ไม่ตัดเพราะ "นานพอแล้ว"

---

## 7) ป้องกัน basket คืนกำไร > 40–50% ยังไง

- M2 ratchet floor ที่ **8–12% ของ peak** (adaptive) → ตัดวงจร giveback ก่อนถึง 40%
- M7 hazard pre-trigger → react ก่อน peak พังจริง
- M5 weak pruning ตัด tail risk ออก → drawdown ที่เหลือคือเฉพาะ winners ที่ retracing ไม่ใช่ pyramid losers
- Hard ceiling: **session_giveback > 25%** → automatic full harvest + freeze (no override)

---

## 8) Staged Rollout

| Stage | Duration | Mode | Metrics watched | Kill switch |
|---|---|---|---|---|
| **0. Shadow** | 5 trading days | log-only, ไม่ส่ง action | M1 reconcile drift, M2 tier events vs actual giveback, M3 phase accuracy vs forward returns | flag `XAU_GUARDIAN_MODE=shadow` |
| **1. Micro-live** | 5 days | M6 + M5 prune (1 pos max) + M2 alerts only | reject false-positive rate, prune outcome vs hold | `XAU_GUARDIAN_MICRO=1`, max 1 PM action / hour |
| **2. Half-live** | 10 days | + M4 partial harvest 25% only, + M7 T2 trigger | giveback %, runner R captured | rollback if giveback ≥ baseline |
| **3. Full guardian** | ongoing | ทุก module + M8 snowball sizing | session peak retention %, equity curve smoothness, family quality | global `XAU_GUARDIAN_ENABLED` |

**Fail-safe flags ทุก stage:**
- `XAU_GUARDIAN_FREEZE_ON_RECONCILE_FAIL=1`
- `XAU_GUARDIAN_MAX_ACTIONS_PER_5MIN=N`
- Telegram alert ทุก tier transition + ทุก reject

---

## 9) Data ที่ Dexter ต้องเริ่มเก็บ (น่าจะยังไม่มี)

| Field | Granularity | Why |
|---|---|---|
| `position.MFE`, `MAE` | per-tick | M4/M5 scoring |
| `position.opening_direction_truth` | at fill | direction sanity |
| `position.lineage` (signal_id, family, regime, phase_at_entry) | at fill | post-mortem |
| `basket.peak_unrealized_today`, `peak_realized_today` | rolling | M2 ratchet |
| `equity.peak_combined_24h` | rolling | session+overnight |
| `phase_at_exit`, `pm_action_reason` | at close | learning loop |
| `hazard_score_history` | 1s sample | M7 calibration |
| `reject_reasons` table | event | audit + tuning |
| `tier_transition_log` | event | giveback analytics |
| `regime_atr_percentile_snapshot` | per-bar | adaptive thresholds |

**Schema additions แนะนำ:**
- `xau_basket_snapshots` — periodic dump ของ M1 truth state
- `pm_action_journal` — ทุก action ของ M4/M5/M7 พร้อม reason + outcome
- `equity_peaks` — rolling peaks per timeframe

---

## 10) ข้อความถึงเจ้าของระบบ

> **นี่คือแผนที่ไม่ธรรมดาครับ**
>
> ปัญหาจริงไม่ใช่ "unlock เปิดให้ noise SELL หลุด" — นั่นเป็นแค่อาการ
> โรคหลักคือ **basket ของคุณไม่มีความทรงจำของ peak** และ PM ไม่รู้ว่า trend อยู่ phase ไหนหลังเข้าไม้แล้ว
>
> ผมไม่เสนอ trail stop ธรรมดา เพราะมันจะตัด winner +67 ตัวต่อไปของคุณทิ้ง
> ผมเสนอกรอบ **Profit Reservoir + Equity Ratchet + Convex Runner Preservation**:
> - กำไรที่ realize ไปแล้วถูกใช้เป็น **buffer ขยายพื้นที่ให้ runner วิ่ง** (ตรงข้ามกับ trail)
> - Basket จำ peak แล้ว **ratchet ขึ้นทางเดียว** — ตกจาก peak เกิน threshold (ปรับตาม volatility regime) → prune ตัวอ่อนก่อน, crystallize ตัวกลาง, harvest ทั้งกระเช้าเฉพาะตอน hazard ขึ้นจริง
> - Hazard detector อ่าน **delta flip + ATR jump + DOM imbalance flip** → react **ก่อน** peak พัง ไม่ใช่หลัง
> - Blind SELL TP=2663 SL=5670 จะถูก **schema validator** ฆ่าทิ้งที่ชั้น sanity ไม่ต้องไปแตะ entry gate
> - กำไรที่เก็บได้กลายเป็น **snowball fuel** ผ่าน Kelly-fractional sizing — ไม่ใช่ size คงที่ ไม่ใช่ euphoria pyramiding
>
> Opportunity-first ของคุณยังอยู่ครบ entry ไม่ถูกบล็อก — แต่หลัง fill ระบบจะมี "**ผู้พิทักษ์ที่ฉลาด**" คอยอ่าน thermodynamics ของ basket แทนคุณ
>
> เคส +224 → -241 ที่เพิ่งเกิด ถ้ามี guardian นี้ จะจบประมาณ **+170 ถึง +200** locked + พร้อม redeploy ในเซสชั่นถัดไป — นั่นคือ **snowball จริง** ไม่ใช่ rollercoaster
>
> เริ่มที่ stage 0 shadow 5 วัน เก็บ MFE/MAE/peak/hazard เข้า DB ก่อน ค่อย micro-live — ไม่มีอะไรไปแตะ live execution จนกว่าจะเห็น metric พิสูจน์ครับ

---

**สรุปสำหรับ commit log สมมุติ:**
> *Profit Reservoir Architecture — equity ratchet + convex runner preservation + hazard pre-trigger + schema sanity. Opportunity-first preserved. Read-only stage 0.*

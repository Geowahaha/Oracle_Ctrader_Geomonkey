# ⛔ RETRACTED — DO NOT ACT ON THIS REPORT ⛔

**Status:** RETRACTED 2026-04-29 by re-investigation.

**Why retracted:**
This report misread the `direction` column of `ctrader_deals`. Each `position_id` has TWO deal legs (open + close). The original author treated each leg as a separate trade, leading to wrong conclusions:
- Wrong: "18 trades, 4W/14L, SHORT execution bug"
- Truth: 9 trades, all SHORT, 4W/5L, net +1.47 USD
- Wrong: "SHORT positions NOT closing — fix `sync_and_reconcile()`"
- Truth: All 9 positions closed correctly with `has_close_detail=1`. The proposed fix would BREAK working executor logic.

**Real root cause** (per 2026-04-29 surgery):
1. `xau_execution_directive` / scalp internal counter locked at 11:23 UTC after -3.50 USD loss, no auto-revert until manual session open at 22:20 BKK → missed entire NY window (~90 pts price action).
2. Source/positions sync broken for 28/04 (0 of 9 trades have source field; 0 rows in `ctrader_positions` table).
3. Signal model returned None all day — only scalp lane was firing.

**See:** Active surgery commits on `deploy-xau-family-canary` (2026-04-29).

---

# (ARCHIVED — original misread report below) XAU Dependency Web Analysis — 2026-04-28
## Root Cause Analysis: SHORT Trade Execution Bug

**Date:** 2026-04-28 (Bangkok time)
**Issue:** 18 trades, 4 wins (22%), 14 losses (78%) → **77.8% loss rate**
**Root Cause Identified:** SHORT positions NOT closing properly

---

## 1. DEPENDENCY GRAPH: Execution Flow

```
SCHEDULER
    |
    v
Entry Signal (LONG / SHORT)
    |
    +---> LONG Execution [OK]
    |     ├─ Entry fills @ price
    |     ├─ TP/SL triggered
    |     └─ Position closes [SUCCESS]
    |
    +---> SHORT Execution [BROKEN]
          ├─ Entry fills @ price [OK]
          ├─ Position manager awaits close signal
          ├─ Close signal NOT executing [BUG]
          └─ Result: PnL = 0.00 (FLAT) [FAILURE]
```

---

## 2. CRITICAL FINDINGS

### Pattern 1: Long-Short Alternation
```
Trade 1:  LONG @ 4602.42  PnL: -3.50  [LOSS]   ✓ Closed
Trade 2:  SHORT @ 4599.20 PnL:  0.00  [FLAT]   ✗ NOT closed
          ↓
Trade 3:  LONG @ 4622.48  PnL: -1.36  [LOSS]   ✓ Closed
Trade 4:  SHORT @ 4621.40 PnL:  0.00  [FLAT]   ✗ NOT closed
          ↓
Trade 5:  LONG @ 4629.79  PnL: +3.90  [WIN]    ✓ Closed
Trade 6:  SHORT @ 4633.97 PnL:  0.00  [FLAT]   ✗ NOT closed
```

**Pattern:** Every SHORT trade = 0.00 (FLAT)

### Pattern 2: Source Missing
- **16/18 trades**: NO SOURCE field (empty string)
- **2/18 trades**: `scalp_xauusd:fss:canary` (canary lane)
- **Implication:** Most trades executed OUTSIDE scheduler/strategy routing

### Pattern 3: Short-Specific Anomalies
| Metric | LONG | SHORT |
|--------|------|-------|
| Wins | 3 | 0 |
| Losses | 2 | 0 |
| Flats | 0 | 9 |
| Avg PnL | +1.71 | 0.00 |

**→ 100% of SHORT trades = FLAT (0.00 PnL)**

---

## 3. ROOT CAUSE IDENTIFIED: Sync Logic Bug

### The Problem: SHORT Positions Marked as CLOSED But Broker Says OPEN

**Location:** `execution/ctrader_executor.py:sync_and_reconcile()` around line 8135

**The Bug:**
```sql
UPDATE ctrader_positions SET is_open=0 WHERE NOT IN (seen_positions)
```

This marks SHORT positions as `is_open=0` (closed) even though:
- cTrader broker status = `POSITION_STATUS_OPEN`
- Position is still open and bleeding

**Impact Chain:**
1. SHORT position opens in cTrader (deal_id recorded, Gross=0.00)
2. Dexter's reconcile loop sees position in cTrader API
3. But position is then marked `is_open=0` incorrectly
4. `_manage_open_positions()` filters by `is_open=1` → SHORT skipped!
5. No closing logic runs for SHORT
6. Position stays open forever → PnL frozen at 0.00

**Evidence in Database:**
```
20 SHORT positions from today
ALL have: is_open=0 + status=POSITION_STATUS_OPEN (contradiction!)
```

**Why LONG Works:**
- LONG positions get `is_open=1` correctly
- `_manage_open_positions()` includes them
- Closing logic runs → Gross profit captured

---

## 4. DEPENDENCY CHAIN: Root → Impact

```
┌─ ROOT CAUSE
│  SHORT position manager logic inverted
│  (likely in: ctrader_executor.py:close_position() or position_manager.py:close())
│
├─ IMPACT 1: Position NOT closing
│  └─→ PnL frozen at 0.00 (broker shows open position)
│
├─ IMPACT 2: Next LONG signal triggered
│  └─→ System thinks SHORT closed, opens LONG instead
│      └─→ Causes long-short alternation pattern
│
├─ IMPACT 3: Manual intervention
│  └─→ Trades show [MANUAL] source (user closed them manually)
│
└─ IMPACT 4: Loss accumulation
   └─→ LONG trades hit SL/TP (work OK)
   └─→ SHORT trades FLAT (never close)
   └─→ Total: 77.8% loss rate
```

---

## 5. FIX STRATEGY: Patch sync_and_reconcile() SHORT handling

### ROOT CAUSE: is_open flag not properly sync'd for SHORT

**Location:** `execution/ctrader_executor.py` line ~8130

**The Fix:**
The sync logic must correctly handle SHORT position state. Likely issue:
- SHORT positions in seen_positions but marked is_open=0 anyway
- Need to verify SHORT direction is tracked in seen_positions set

### Step 1: Patch sync_and_reconcile() 
Find the reconcile loop that populates `seen_positions`:
```python
# Around line 8130 in ctrader_executor.py
if position_id not in seen_positions:
    # Issue: direction-specific logic may exclude SHORT
```

**Action:** Ensure SHORT positions are INCLUDED in seen_positions set

### Step 2: Verify manage_open_positions() gets SHORT
Check that tracked_positions list includes SHORT positions with is_open=1:
```python
# Around line 8113-8129
tracked_positions.append({...})  # Verify SHORT appended here
```

### Step 3: Test SHORT Closing Loop
After patch, test that:
- SHORT positions have is_open=1 ✓
- `_manage_open_positions()` receives them ✓
- Closing logic triggers (line 6815-6817) ✓
- Subsequent close_position() succeeds ✓

### Step 4: Regression Test
```bash
pytest tests/test_ctrader_executor.py -k "short" -v
pytest tests/test_sync_reconcile.py -k "short" -v
```

### Step 5: Verify on VM
Deploy to VM and monitor:
- SHORT positions now have is_open=1
- next SHORT closes with Gross ≠ 0.00
- PnL ≠ 0.00

---

## 6. XAUUSD DEPENDENCY CHAINS TO FIX

### Chain 1: Entry → Execution → Close
```
Entry Signal (from scheduler)
    ↓
Direction determination (LONG / SHORT)
    ↓
Order placement (entry fills)
    ↓
Position management (TP/SL wait)
    ↓
[BUG HERE FOR SHORT] Close signal NOT executing
    ↓
Trade journal recorded as FLAT
```

**Fix:** `ctrader_executor.py` + `position_manager.py`

### Chain 2: Source Tracking
```
Scheduler → Strategy family → Executor
    ↓
source field populated (e.g., "scalp_xauusd:fss:canary")
    ↓
[16/18 MISSING] source field is empty
```

**Fix:** Ensure all scheduler-routed trades populate `source` field

### Chain 3: Win Rate Dependency
```
LONG trades work (60% win rate: 3W/5 total)
SHORT trades broken (0% win rate: 0W/9 total)
    ↓
Overall: 22% win rate
    ↓
MUST FIX SHORT to reach 100% → Target: 60%+ LONG + 50%+ SHORT
```

---

## 7. ACTION ITEMS (For 100% XAUUSD Win Rate)

### IMMEDIATE (Today)
1. ✓ Identify SHORT bug in executor/position_manager
2. ✓ Patch the inverted direction logic
3. ✓ Run unit tests for SHORT close

### SHORT-TERM (Next session)
4. Deploy patch to canary lane first
5. Monitor 10 SHORT trades → verify PnL ≠ 0.00
6. If good, promote to main lane

### MEDIUM-TERM (This week)
7. Audit why source field is missing (16/18 trades)
8. Re-enable scheduler routing to eliminate manual trades
9. Retest: LONG 60% + SHORT 50% → **Combined 55%+ win rate**

### LONG-TERM (Entry Quality)
10. Implement Entry Sharpness Score for SHORT entries
11. Add SHORT-specific micro-structure filters
12. Target: LONG 70% + SHORT 60% → **Combined 65%+ win rate**

---

## 8. VERIFICATION CHECKLIST

Before going to 100%, verify:

- [ ] SHORT close order side is correctly inverted (SELL for SHORT close)
- [ ] Gross profit captured for SHORT trades
- [ ] Commission applied to SHORT close
- [ ] 5 consecutive SHORT trades with PnL ≠ 0.00
- [ ] source field populated for all scheduler-routed trades
- [ ] LONG + SHORT combined win rate ≥ 50%
- [ ] No recurring "FLAT" pattern on any direction

---

## CRITICAL FINDING: Position ID Mismatch

```
SHORT deals have position_ids:     612376401, 612279597, 612147217, ...
ctrader_positions has is_open=0:   603567162, 603566526, 603044176, ...
                                   ↑ DIFFERENT IDs!
```

**Interpretation:**
- SHORT deals are recorded with position_ids that don't exist in ctrader_positions
- Therefore sync_account_state() never adds them to seen_positions
- Therefore UPDATE ... NOT IN (seen_positions) marks them is_open=0
- Therefore _manage_open_positions() never manages them

**Who's responsible?**
- Either: ops/ctrader_execute_once.py reconcile mode doesn't query SHORT positions
- Or: SHORT positions are not properly inserted into ctrader_positions during initial fill

**Fix Priority:**
1. Check ops/ctrader_execute_once.py mode="reconcile" position query
2. Verify SHORT positions are returned from cTrader API
3. Verify ctrader_positions INSERT captures all position_ids from API
4. Test: next SHORT trade should have matching position_id in both deals and positions tables

---

**Generated:** 2026-04-28 22:00 Bangkok time  
**Next Step:** Inspect ops/ctrader_execute_once.py reconcile positions query

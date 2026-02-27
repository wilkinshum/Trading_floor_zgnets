# Finance Analysis — Feb 27, 2026 (Day 1 Post-Filter)

## Executive Summary

**Zero trades executed across 7 scan cycles.** The system is severely over-filtered. Multiple independent filters each blocking different signals means the probability of ANY trade passing all gates is near zero. This is a classic "filter stacking" problem — each filter is individually reasonable but their conjunction is lethal.

---

## Root Cause Analysis

### 1. NFLX Rejected 6+ Times: ATR Floor Too High ✅ YES

**Finding:** `min_atr_pct: 0.005` (0.50%) in `workflow.yaml` → `risk.py` line 86 rejects anything with ATR < 0.50% of price. NFLX at 0.47% is just 3 basis points under.

**Verdict:** 0.50% is too high for mega-cap stocks. NFLX ($~900+) naturally has lower percentage ATR than small-caps. This filter was designed to block "dead" stocks with no movement, but it's catching liquid large-caps.

**Recommendation:** Lower to **0.35%**. This still blocks truly flat stocks but lets liquid large-caps through. Alternatively, tier it: 0.50% for stocks <$100, 0.35% for stocks >$100.

### 2. MARA at 0.78 — Why No PM Plan?

**Finding:** The PM's `create_plan()` (pm.py line 55) has a hard gate:

```python
if market_regime["is_downtrend"] and score > 0:
    continue
```

This checks if SPY is below its 20-period MA. If SPY dipped below even briefly on 5-min data, **ALL buy signals are killed**. At 9:45 the bull confidence dropped from 96.7%→66.3%, suggesting SPY was weakening. If SPY crossed below its intraday 20-bar MA, the PM blocked every single BUY.

**This is the #1 issue.** A binary downtrend flag on 5-min data is far too sensitive. SPY oscillates around its short-term MA constantly during the session.

**Additional gates MARA would have hit:**
- `min_momentum_score: 0.40` — MARA's momentum component needed to be ≥0.40
- Persistence filter — MARA was dropped at 9:30 (first cycle), but should have passed at 9:45 since the first signal was logged

**Recommendation:** Replace the binary downtrend block with a graduated approach:
- Remove `if market_regime["is_downtrend"] and score > 0: continue`
- Instead, apply a confidence haircut: if downtrend, require `score > threshold * 1.5` (i.e., 0.375 instead of 0.25)
- Or only block buys when HMM bear confidence > 70%

### 3. Persistence Filter — Too Aggressive?

**How it works** (workflow.py ~line 370): Checks if the previous signal's sign matches the current signal's sign. If they flip (e.g., last cycle was SELL, now BUY), the stock is dropped.

**The problem:** On the first scan of the day, if the LAST signal in the DB was from yesterday and was a different direction, the stock gets killed. Intraday signals naturally flip direction vs. prior-day signals.

MARA, IONQ, RDW, CRML were all killed by this. With a 15-min scan interval and 2-hour window, you only get ~8 chances. Losing stocks to persistence on cycles 1-3 means they're effectively blocked for half the session.

**Recommendation:** 
- Add a **time decay**: only check persistence against signals from the SAME session (today). Ignore yesterday's last signal.
- Or reduce to: only block if the signal flipped within the LAST 2 CYCLES (30 min), not "ever"

### 4. Is the 2-Hour Window Too Short?

**Yes, given the current filter density.** The pipeline has **9 independent filter layers**:

| # | Filter | Type |
|---|--------|------|
| 1 | Scout top-5 | Pre-signal |
| 2 | Signal persistence | Pre-PM |
| 3 | Market regime (downtrend block) | PM gate |
| 4 | Momentum gate (min 0.40) | PM gate |
| 5 | High-bar sector check | PM gate |
| 6 | ATR min/max | Risk agent |
| 7 | Challenge system | Post-plan |
| 8 | Pre-execution filters (regime, volume, time-of-day, crypto, Kalman, min price, last-entry cutoff) | Pre-exec |
| 9 | Approval.json | Administrative |

With ~8 scan cycles in 2 hours and the last-entry cutoff eating 30 minutes (leaving ~6 usable cycles), a stock needs to pass ALL 9 layers simultaneously. If each filter independently passes 70% of the time, the joint probability is 0.70^9 = **4%** per stock per cycle.

**With 5 stocks and 6 cycles: ~1-2 expected trades per day** if filters are well-calibrated. But several filters are far below 70% pass rate right now.

**Recommendation:** Keep the 2-hour window but **reduce filter count**. Specifically:
- The PM's downtrend block is redundant with the regime filter in pre-execution
- The morning time-of-day filter (require 0.60+ before 10:30) overlaps with the higher threshold (0.25)
- Kalman agreement is checked TWICE (pre-exec filter + morning filter requirement)

### 5. Overall Assessment: Over-Filtering

**The system is in "analysis paralysis" mode.** You went from a 25% win rate / PF 0.96 system that traded too loosely to a system that doesn't trade at all. The pendulum swung too far.

The core issue is **redundant layered filtering**:
- Regime is checked 3 times (PM downtrend, HMM in pre-exec, regime monitor file)
- Signal quality is gated 3 times (threshold 0.25, momentum gate 0.40, morning gate 0.60)
- Direction agreement is checked 2 times (persistence filter, Kalman agreement)

Each layer adds a "no" vote. Very few signals can survive the gauntlet.

### 6. The approval.json Gap

At 10:30, even if a trade had passed all filters, `approval.json` was missing. This is a process bug, not a filter bug. Ensure approval.json is auto-created on system boot with today's date.

---

## Specific Recommendations for Monday (March 2)

### Priority 1 — Must Fix (Filter Relaxation)

| Change | Current | Recommended | Reasoning |
|--------|---------|-------------|-----------|
| `min_atr_pct` | 0.005 (0.50%) | **0.0035 (0.35%)** | Unblocks NFLX and other large-caps |
| PM downtrend block | Binary block all buys | **Remove or change to `score > 0.375` when downtrend** | Biggest single cause of zero trades |
| Persistence filter | Check vs ANY previous signal | **Only check vs signals from today's session** | Stop yesterday's signals from killing today's |
| `morning_min_score` | 0.60 | **0.45** | 0.60 is too aggressive on top of 0.25 threshold |

### Priority 2 — Should Fix

| Change | Current | Recommended | Reasoning |
|--------|---------|-------------|-----------|
| `last_entry_minutes` | 30 | **20** | Recover 10 minutes of trading; 20 min is still conservative |
| `min_momentum_score` | 0.40 | **0.30** | 0.40 blocks too many; threshold + weights already gate quality |
| Auto-create approval.json | Manual | **Auto-create on boot** | Process reliability |

### Priority 3 — Monitor

| Item | Note |
|------|------|
| Kalman agreement | Working correctly but contributes to over-filtering. Consider making it a "warn" not "block" if other filters tighten |
| Challenge system | Correctly blocked RDW SELL (meanrev opposing). Keep as-is |
| Sector diversification | Not yet tested since no trades passed. Monitor |

---

## Expected Impact

With Priority 1 changes:
- NFLX would have passed ATR filter → eligible for trading
- MARA 0.78 BUY at 9:45 would have generated a PM plan (if not downtrend-blocked)
- Persistence wouldn't have killed MARA/IONQ/RDW on first cycles
- Estimated: **2-4 trade opportunities per day** instead of 0

With Priority 1+2:
- Estimated: **3-5 opportunities**, with **1-3 actual executions** after remaining filters

---

## Filter Pass-Through Probability (Estimated)

| Filter | Current Pass Rate | After Fix |
|--------|-------------------|-----------|
| Scout top-5 | 100% (by design) | 100% |
| Persistence | ~40% (cross-day kills) | ~80% |
| PM regime gate | ~30% (binary block) | ~70% |
| PM momentum gate | ~50% | ~65% |
| ATR min/max | ~60% (NFLX always blocked) | ~85% |
| Challenge system | ~80% | ~80% |
| Pre-exec (combined) | ~50% | ~55% |
| Approval | 0% (missing!) | ~95% |
| **Joint probability** | **~0%** | **~15%** |

15% × 5 stocks × 6 cycles = **~4.5 opportunities/day** → realistic for 1-3 trades.

---

## Files to Modify

1. **`configs/workflow.yaml`**: `min_atr_pct: 0.0035`
2. **`src/trading_floor/agents/pm.py`** line ~55: Replace binary downtrend block with graduated gate
3. **`src/trading_floor/workflow.py`** ~line 370: Add time filter to persistence check (same-day only)
4. **`src/trading_floor/pre_execution_filters.py`**: Change `morning_min_score` default from 0.6 to 0.45
5. **Boot script**: Auto-generate `approval.json` with `{"approved": true, "date": "<today>"}`

---

*Analysis by Finance Agent | Data: Feb 27, 2026 session logs | Next review: March 2 (Monday post-market)*

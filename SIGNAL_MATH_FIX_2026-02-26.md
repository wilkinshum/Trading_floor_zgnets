# Signal System Math Fix — 2026-02-26

## Problem
Nearly every candidate was blocked by the challenge system due to:
1. Zero-weight signals (meanrev=0.00) inflating spread calculations
2. Breakout signal pinned at ±1.0 (always at range extremes)

## Changes Made

### Fix 1: Challenge spread excludes zero-weight signals ✅ (Already implemented)
`challenger.py` already filtered `weights.get(key, 0) <= 0` in `_check_signal_disagreement()`.
**No code change needed** — this was already correct.

### Fix 2: Breakout signal range calculation (FIXED)
**File:** `src/trading_floor/agents/signal_breakout.py`
**Bug:** The breakout lookback window *included* the current bar. Since the current price is always within its own range, `position` was always 0 or 1, yielding scores of -1.0 or +1.0.
**Fix:** Calculate high/low from the *prior* bars (excluding current bar). Now:
- Current price at prior midpoint → score ≈ 0.0
- Current price at prior high → score = +1.0
- Current price at prior low → score = -1.0
- Actual breakouts (beyond prior range) get clamped to ±1.0

### Fix 3: Persistence filter review ✅ (No change, documented)
**File:** `src/trading_floor/workflow.py` lines 352-382
**What it does:** Rejects signals that flip direction between consecutive cycles (BUY→SELL or SELL→SELL).
**First-time signals pass through.** Same-direction signals pass through.
**Assessment:** This is working correctly. Today's data shows stocks like IONQ flipping BUY↔SELL across cycles — exactly the indecision the filter should catch. The filter is not too strict; it only blocks sign reversals, not magnitude changes.

### Fix 4: Challenge threshold alignment ✅ (Already correct at 1.2)
**Config:** `challenges.disagreement_threshold: 1.2`
With only 3 active signals (momentum [-1,1], breakout [-1,1], news [0,1]):
- Max theoretical spread = 2.0 (momentum=-1, news=+1)
- 1.2 = 60% of max → catches significant disagreement
- After fixes, today's last cycle: all spreads 0.44–0.88 (well under 1.2)
- Threshold is appropriate. No change needed.

## Before/After (Last cycle 2026-02-26T18:45)

| Stock | Old Spread | Old Result | New Spread | New Result |
|-------|-----------|------------|-----------|------------|
| RDW   | 1.912     | BLOCKED    | 0.875     | ok         |
| CRML  | 1.627     | BLOCKED    | 0.500     | ok         |
| IONQ  | 1.770     | BLOCKED    | 0.600     | ok         |
| TE    | 1.981     | BLOCKED    | 0.583     | ok         |
| RGTI  | 0.939     | ok         | 0.440     | ok         |

**Result: 4/5 false blocks eliminated.** The gridlock is resolved.

## Validation
- `pip install -e .` ✅
- Breakout unit tests: midpoint→0.02, high→1.0, low→-1.0, upper-mid→0.51 ✅
- Spread recalculation with real data confirms no over-blocking ✅

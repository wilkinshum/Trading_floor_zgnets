# V4 Swing Strategy Analysis — March 2, 2026

## 1. Root Cause Analysis: Swing -$273.31

**Three structural problems identified from trade-level data:**

### A. The Mean Reversion Signal Is Counterproductive
Splitting trades by meanrev signal direction:

| Meanrev Signal | Trades | Win Rate | Avg P&L |
|---|---|---|---|
| Negative (< 0) | 10 | **50%** | **+$4** |
| Positive (≥ 0) | 11 | **27%** | **-$28** |

Positive meanrev (supposed to indicate oversold = good entry) loses at 73%. These are falling knives, not mean-reverting. Winners are momentum-driven: the 5 wins from negative-meanrev trades all had momentum ≥ 0.86.

### B. 15% TP Target Is Unreachable
Only 2/21 trades (10%) reached 15% TP. The 6 trail exits averaged +7.7% gain. TP is decorative — all profit capture depends on the trailing mechanism.

### C. Re-Entry Into Recent Losers Without Cooldown
- RDW: -$80, re-entered next day → -$80 again
- SYM: -$76, re-entered next day → -$80 again
- Combined immediate re-entry cost: **-$316** (more than the entire swing loss)

## 2. Parameter Recommendations (max 2 changes)

### Change 1 (PRIORITY): Add 5-day symbol cooldown after SL exit
**Evidence:** 4 immediate re-entries after SL, 100% loss rate, total cost -$316. The one profitable re-entry (IDR) came after a 2-week gap.

### Change 2: Reduce swing TP: 15% → 9%
**Evidence:** 6 trail exits averaged +7.7% before trail caught them. A 9% TP would capture 4 of those as direct TP exits, locking in ~1-2% more per trade.

### Queued (needs 30+ trades): Reduce meanrev weight 0.35 → 0.15
Strong directional signal but only 21 trades — below the 30-trade minimum for weight changes.

## 3. Intraday: No Changes
-$24.71 across 63 trades (PF 0.94). Near break-even, pipeline works. Focus energy on swing (92% of losses).

## 4. Kill Switch: Keep at 12%
It correctly flagged a real problem. Loosening masks the issue. If v4.1 still hits 12%, that's a genuine signal to pause.

## 5. Priority-Ranked Action Items

| # | Action | Expected Impact | Evidence |
|---|---|---|---|
| 1 | 5-day symbol cooldown after SL | Avoid ~$316 re-entry losses | 4 cases, 100% immediate re-entry loss rate |
| 2 | Swing TP 15% → 9% | Better win capture, est. win rate 38%→~45% | 6 trail exits avg +7.7% |
| 3 | *Later:* Meanrev weight 0.35→0.15 | Filter falling knives | 50% vs 27% win rate split, needs 30 trades |
| 4 | Keep intraday unchanged | Preserve what works | 63 trades near break-even |
| 5 | Keep kill switch at 12% | Honest risk signal | Correctly flagged the problem |

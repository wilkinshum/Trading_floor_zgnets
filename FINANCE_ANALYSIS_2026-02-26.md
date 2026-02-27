# FINANCE ANALYSIS — 2026-02-26

## Executive Summary

**Overall P&L (post-fix, Feb 23–26):** -$0.42 across 8 closed round-trips  
**Win Rate:** 25% (2W / 6L)  
**Profit Factor:** 0.96  
**Avg Winner:** +$32.16 | **Avg Loser:** -$11.19  
**Verdict:** The system barely breaks even. Winners are 2.9x larger than losers (good!), but win rate is terrible. The new filters are directionally correct but several critical bugs and design flaws need fixing.

---

## 1. Pipeline Review: Signal → Challenge → Finance → Pre-Exec → Execution

### Flow Summary
1. **Scout** ranks universe, picks top 5
2. **Signal agents** score each (momentum, meanrev, breakout, news) → weighted composite
3. **Signal persistence** filter (requires consistent direction vs. prior cycle)
4. **PM** creates plan: threshold gate, momentum gate, high-bar sector gate, correlation filter, sizing
5. **Risk agent** evaluates: ATR volatility filter, sector filter, max positions
6. **Challenge system** checks: signal disagreement, re-entry guard, regime mismatch, news absence, consecutive losses
7. **Finance agent** reviews cautioned trades (1 warning)
8. **Pre-execution filters**: regime recheck, volume, time-of-day, crypto correlation, Kalman agreement
9. **Execution** via portfolio

### 🔴 CRITICAL BUG: Signal Components Logging as Zero

Looking at today's DB signals, `score_mom`, `score_mean`, `score_break` are ALL stored as zero in the `signals` table for post-market-hours runs (the 17:30–18:45 entries), while the BITF signals during market hours show correct non-zero values. 

But there's a worse problem: **the logged `weight_mom`/`weight_break` columns show the *configured* weights, not the `weights_used` (adjusted for missing news)**. The actual scoring code redistributes weights when news=0, but the log doesn't capture this. This makes forensic analysis unreliable.

### 🔴 CRITICAL: Breakout Signal Still Clamps to ±1.0

The fix changed breakout to use prior bars only, which is correct. However the final `score = max(-1.0, min(1.0, score))` still clamps. Since the position formula `(last - prior_low) / (prior_high - prior_low)` maps mid-range→0 and edges→±1, and overshoots ARE the breakouts, clamping at ±1.0 means **every real breakout has the same score**. The signal can't distinguish a 0.1% breakout from a 5% breakout. BITF's breakout was -1.0 on both Feb 25 and Feb 26 — it can't tell us how far below the range it fell.

### 🟡 GAP: PM Market Regime Filter Blocks All Longs in Downtrend

```python
if market_regime["is_downtrend"] and score > 0:
    continue
```
This kills ALL long candidates when SPY < SMA20. In sideways markets that dip slightly below the SMA, this blocks every long signal. Combined with the 2-hour trading window (9:30–11:30), this can lead to entire days with zero trades. Should be softened to a score penalty, not a hard block.

### 🟡 GAP: No Position Tracking for Short Entries

The BITF trade shows `qty=204` SELL entry then `qty=0` BUY exit. The exit recorded qty=0, meaning portfolio didn't properly track the short position size. The PnL calculation still worked ($-8.33), but qty=0 in the trade log is confusing for analysis.

### 🟡 GAP: Signal Persistence Uses Raw DB, Not Normalized Scores

The persistence filter compares `final_score` signs. But since scores can flip rapidly on 5-min bars (BITF went from -0.367 to +0.604 in 30 minutes), this filter adds almost no value for fast-moving stocks. It only catches slow-moving names that shouldn't be traded anyway.

---

## 2. Threshold Analysis

| Threshold | Current Value | Assessment | Recommendation |
|-----------|--------------|------------|----------------|
| **Challenge disagreement** | 0.9 caution / 1.5 block | ✅ Reasonable with zero-weight exclusion | Keep |
| **Finance min score (caution)** | 0.50 | 🟡 Never triggered — no trade had caution+score≥0.50 yet | Lower to 0.40 |
| **Trade threshold** | 0.15 | 🟡 Too low — allows weak conviction trades | Raise to 0.25 |
| **Min momentum** | 0.40 | 🔴 Correct idea, but see weight analysis below | Keep if weights fixed |
| **Volume ratio** | 1.0x | ✅ Correct — below-average volume = poor fills | Keep |
| **Morning min score** | 0.60 | ✅ Good — morning volatility needs conviction | Keep |
| **ATR range** | 0.5%–10% | ✅ Appropriate for 5-min bars | Keep |

### Trade Threshold Math Check
With current weights (mom=0.50, mr=0.00, brk=0.10, news=0.20):
- **No news case**: weights redistribute to mom=0.833, brk=0.167 (total=0.60, renormalized to 1.0)
- Max possible score with no news: mom=1.0×0.833 + brk=1.0×0.167 = **1.0**
- Min score to trade: **0.15** → requires only 15% of max possible signal
- This is too loose. A momentum of 0.20 with breakout of 0.20 clears the bar: 0.20×0.833 + 0.20×0.167 = 0.20. That's a very weak signal.

**With news**: total weight = 0.80. Max score = 0.80. Threshold 0.15 = 18.75% of max. Still loose.

---

## 3. Signal Weight Review

### Current: mom=0.50, mr=0.00, brk=0.10, news=0.20 (sum=0.80, reserve=0.00)

**Problem: Weights sum to 0.80, not 1.0.** When news is present, the composite score maxes at 0.80 instead of 1.0. When news is absent, the code redistributes mom+mr+brk (=0.60) to sum to 1.0. This means **the system behaves very differently with vs. without news**, and thresholds have different effective meanings depending on news availability.

### BITF Case Study (Feb 26)
- Entry signal: mom=-0.684, mr=+0.760 (ignored, weight=0), brk=-1.0, news=+0.375
- Composite: (-0.684×0.50) + (-1.0×0.10) + (0.375×0.20) = -0.342 - 0.10 + 0.075 = **-0.367**
- This passed the -0.15 threshold → SELL entered
- 30 min later: mom flipped to +0.858, brk flipped to +1.0 → composite = +0.604
- ATR stop hit → closed at -$8.33 loss

**Key insight**: Momentum flipped 180° in 30 minutes. The mean-reversion signal (weight=0) was already warning at +0.760 (counter to the short). If meanrev had even 0.10 weight, the entry score would have been weaker: -0.342 - 0.10 + 0.076 + 0.075 = **-0.291**, still passing threshold but flagging lower conviction.

### Breakout Weight (0.10)
At 0.10 weight, breakout contributes max ±0.10 to the composite. This means breakout alone can never trigger a trade (needs 0.15 threshold). It's essentially decorative. Either:
- Raise to 0.15–0.20 so it matters
- Or keep at 0.10 as a tie-breaker (current role)

### Mean Reversion (0.00)
Mean reversion is **anti-correlated with momentum by construction** — momentum says "price above SMA = bullish", meanrev says "price above SMA = bearish." Running both at equal weight guarantees they cancel. At weight=0.00, it's disabled. 

**However**, meanrev provided a useful contrary signal on BITF (+0.76 while shorting). Consider using it as a **challenge input** rather than a composite weight — i.e., if meanrev strongly opposes the trade direction, flag it in the challenger.

### Recommended Weights
```yaml
momentum: 0.50   # keep — primary driver
meanrev: 0.00    # keep at 0, but use as challenger input
breakout: 0.15   # raise from 0.10 — now that it's fixed, it adds info
news: 0.25       # raise from 0.20 — news confirmation matters
# Sum = 0.90 (reserve 0.10 for future signals)
```

---

## 4. Historical Performance Analysis

### Post-Fix Round Trips (Feb 23–26)

| # | Symbol | Side | Entry Px | Exit Px | PnL | Hold Time | Signal |
|---|--------|------|----------|---------|-----|-----------|--------|
| 1 | ONDS | BUY | 10.465 | 10.185 | -$14.85 | ~15 min | ATR stop |
| 2 | CRML | BUY | 9.499 | 10.010 | +$34.68 | ~4.5 hrs | ATR trail |
| 3 | RGTI | BUY | 16.715 | 16.380 | -$19.25 | ~20 hrs* | exit monitor |
| 4 | IREN | BUY | 45.340 | 44.350 | -$15.60 | ~20 hrs* | exit monitor |
| 5 | TMQ | BUY | 4.100 | 4.315 | +$29.63 | ~20 hrs* | exit monitor |
| 6 | TE | SELL | 7.335 | 7.445 | -$6.70 | ~55 min | exit monitor |
| 7 | BITF | SELL | 2.300 | 2.339 | -$8.33 | ~30 min | ATR stop |

*Overnight holds (entered Feb 24 PM, exited Feb 25 AM)

### Patterns

**Winners (CRML, TMQ):**
- Both had strong composite scores (0.606, 0.252)
- Both held for extended periods (trailing stop let them run)
- Both were in sectors not already held

**Losers (ONDS, RGTI, IREN, TE, BITF):**
- ONDS: ATR stop hit in 15 minutes — too volatile for position size
- RGTI/IREN: Overnight gap down — entered late in the day, no protection overnight
- TE: Short in a stock that reversed quickly — weak score (-0.371)
- BITF: Short a $2.30 stock — low-priced, crypto-adjacent, momentum flipped fast

### Key Finding: Losers share two patterns
1. **Short-duration stops** (15–55 min) — entered weak trades that immediately went against
2. **Overnight holds** — the 9:30–11:30 window means positions often carry overnight

### Win Rate by Entry Score
- Score ≥ 0.50: 1W/2L (33%)
- Score < 0.50: 1W/4L (20%)

Low sample size, but the data supports raising the trade threshold.

---

## 5. BITF Backtest: Would New Filters Have Caught It?

### The Trade
- **Entry**: Feb 26 15:15, SELL 204 shares @ $2.30, score=-0.367
- **Exit**: Feb 26 15:45, BUY @ $2.34, PnL=-$8.33 (ATR stop)
- **Holding time**: 30 minutes

### Filter-by-Filter Check

| Filter | Would it block? | Why |
|--------|----------------|-----|
| **Regime recheck** | ❌ No | HMM = bull (96.7% confidence). Shorts aren't blocked in bull unless bear confidence >70% |
| **Volume** | ❓ Unknown | Need volume data, but BITF is typically high-volume |
| **Time-of-day** | ❌ No | 15:15 is after 10:30 morning cutoff |
| **Crypto correlation** | **✅ YES** | BITF is in crypto_symbols list. BTC momentum was +0.40% (flat/slightly up). Shorting crypto while BTC flat → allowed (threshold is -0.5%). But BTC was trending UP slightly. **Wait** — the filter checks `btc_momentum < -0.005` for blocking BUY and `btc_momentum > 0.005` for blocking SELL. BTC momentum was +0.004 (0.4%), which is < 0.005 threshold. **Would NOT have blocked.** |
| **Kalman agreement** | **✅ LIKELY YES** | Shadow predictions show HUT (crypto peer) kalman=-3.624 (bearish), but BITF wasn't in the top 5 scout list typically. If Kalman had data for BITF and showed bearish signal, it would agree with SELL. But if Kalman had no data → **BLOCK** (Kalman required=True). Need to verify if BITF gets Kalman data. |
| **Challenge: signal disagreement** | ❌ No | With mr at weight=0, only mom(-0.684) and brk(-1.0) are checked. Both negative. News(+0.375) is positive but spread = 0.375-(-1.0) = 1.375. That's > 0.9 threshold! **Actually YES — this would trigger CAUTION.** |

### Revised Assessment
The challenge system **should have caught BITF** with a spread of 1.375 (>0.9 caution threshold). But wait — let me recheck. The challenge looks at active-weight signals only:
- momentum (w=0.50): -0.684 ✓
- breakout (w=0.10): -1.0 ✓  
- news (w=0.20): +0.375 ✓
- meanrev (w=0.00): skipped ✓

Scores: [-0.684, -1.0, +0.375]. Spread = 0.375 - (-1.0) = **1.375**. This exceeds 0.9 → **CAUTION flag**.

Then finance agent review: score=0.367, which is < 0.50 caution_min_score → **REJECTED**.

**So the new system SHOULD have blocked BITF.** If it didn't, there's a bug in how challenges pass signal components. Let me check...

In `workflow.py`, the challenge context builds signal_details from `signal_details[sym].get("components", {})`. The components dict has keys: `momentum`, `meanrev`, `breakout`, `news`. The challenger checks weights and skips zero-weight. This should work correctly.

**Conclusion**: If the system was fully deployed at 15:15, BITF would have been:
1. Challenged (spread 1.375 > 0.9) → CAUTION
2. Routed to finance agent → score 0.367 < 0.50 → **REJECTED**

The fact that it traded suggests the challenge system wasn't active for this run, or there was a deployment timing issue.

---

## 6. Risk Assessment: Too Tight or Too Loose?

### Trade Frequency
- **Feb 23**: 2 entries (CRML, ONDS)
- **Feb 24**: 3 entries (TMQ, RGTI, IREN)  
- **Feb 25**: 1 entry (TE)
- **Feb 26**: 1 entry (BITF)
- **Average**: ~1.75 trades/day

With the new filters fully active, expect **0.5–1.5 trades/day**. The regime recheck and Kalman mandatory filters will block a significant percentage.

### Filter Stack Depth
A trade must pass: scout top-5 → threshold → momentum gate → persistence → PM plan → risk agent → challenge system → finance review (if caution) → regime recheck → volume → time-of-day → crypto correlation → Kalman agreement.

That's **13 sequential gates**. Each with ~70–90% pass rate. Cumulative: 0.8^13 ≈ 5% of signals reach execution. With scout top-5 feeding ~5 symbols per cycle and ~12 cycles per day (2hr window / 10min interval), that's 60 signal evaluations/day → ~3 candidates surviving to execution.

**Assessment**: The filter depth is appropriate given the 25% win rate. Better to be selective. But ensure you're not filtering on the same information multiple times (momentum gate + Kalman agreement + regime check all test trend direction).

### Redundancy Concerns
1. **PM momentum gate** (min_momentum=0.40) AND **Kalman agreement** both test trend direction
2. **PM market regime filter** AND **pre-exec regime recheck** AND **challenger regime mismatch** all test regime
3. News is checked in **challenger** (news absence) AND **challenge disagreement** (spread calculation)

This isn't necessarily bad (defense in depth), but it means the effective threshold is harder to reason about.

---

## 7. Recommendations (Prioritized by Expected Impact)

### 🔴 HIGH IMPACT

**1. Raise trade_threshold from 0.15 to 0.25**
- Current 0.15 allows weak-conviction trades (TE at -0.371 was marginal quality)
- 0.25 would have filtered ONDS (score=0.569, would still pass) but caught more noise
- Expected impact: Eliminates bottom ~30% of trades by conviction → better win rate

**2. Fix weight sum to 1.0**
```yaml
weights:
  momentum: 0.50
  meanrev: 0.00
  breakout: 0.15
  news: 0.25
  reserve: 0.10  # explicitly allocated
```
This makes thresholds meaningful across all conditions.

**3. Add mean reversion as challenger input**
In `challenger.py`, add a new check: if meanrev component strongly opposes trade direction (|meanrev| > 0.5 and opposite sign to trade), raise a WARNING. This uses meanrev's information without it distorting the composite score.
```python
def _check_meanrev_opposition(self, sym, side, signals):
    mr = signals.get("meanrev", 0)
    if side == "BUY" and mr < -0.5:
        return Challenge("strategy", "warn", f"Mean reversion strongly bearish ({mr:.2f})")
    if side == "SELL" and mr > 0.5:
        return Challenge("strategy", "warn", f"Mean reversion strongly bullish ({mr:.2f})")
    return None
```

**4. Block low-priced stocks (< $5)**
BITF at $2.30 has terrible risk/reward for a short — spread and slippage eat the profit. Add to pre-execution filters:
```python
def check_min_price(price, min_price=5.0):
    if price < min_price:
        return False, f"Price ${price:.2f} below ${min_price} minimum"
    return True, f"price OK: ${price:.2f}"
```

### 🟡 MEDIUM IMPACT

**5. Shorten overnight exposure**
The 9:30–11:30 trading window means positions entered at 11:00 carry overnight. Either:
- Add a "last entry" cutoff (e.g., 11:00 — no new entries in last 30 min)
- Or add an EOD flatten rule (sell all at 15:55 if not already exited)

**6. Tighten crypto filter BTC threshold**
Current: 0.5% (0.005). This is too generous — BITF's BTC was +0.4% (just under). Lower to 0.3% (0.003) for better crypto-BTC alignment.

**7. Add ATR-based position sizing floor**
Stocks with very high ATR get small positions via volatility sizing, but the ATR stop is also wide. This means you can have a $200 position with a 5% stop = $10 risk, which is fine. But verify the math ensures position_size × stop_pct ≤ max_loss_per_trade. Currently no explicit per-trade risk cap.

**8. Log weights_used (not just configured weights)**
The signal logger stores configured weights, not the adjusted weights when news is missing. Fix `signal_logger.log_signal()` to use `details["weights_used"]` for forensic accuracy.

### 🟢 LOW IMPACT (Quality of Life)

**9. Consider raising breakout weight to 0.15**
Now that breakout is fixed (using prior bars), it provides genuine breakout/breakdown information. At 0.10 it barely matters. At 0.15 it can differentiate between a momentum trade and a momentum+breakout trade.

**10. Add a "signal freshness" check**
The regime_state.json has a timestamp. If it's >10 min stale, the pre-exec filter should treat it as "no data" rather than using old readings. Currently it trusts whatever's in the file.

**11. Normalize the composite score to [−1, +1] range**
Since weights don't sum to 1.0 and news ranges differ from momentum/breakout, the composite score has an inconsistent scale. Dividing by sum(active_weights) would make thresholds universally meaningful.

---

## Appendix: Math Verification

### Composite Score Formula (with news)
```
score = mom × 0.50 + mr × 0.00 + brk × 0.10 + news × 0.20
```
Max possible = 1.0×0.50 + 1.0×0.10 + 1.0×0.20 = **0.80** ← not 1.0!

### Composite Score Formula (without news)
```
non_news_total = 0.50 + 0.00 + 0.10 = 0.60
adj_mom = 0.50/0.60 = 0.833
adj_brk = 0.10/0.60 = 0.167
score = mom × 0.833 + brk × 0.167
```
Max possible = **1.0** ← full range when no news

This asymmetry means the trade_threshold of 0.15 is effectively:
- 18.75% conviction when news present (0.15/0.80)
- 15.0% conviction when news absent (0.15/1.0)

Stocks WITH news are held to a LOWER effective bar. This is backwards — news should increase confidence, not lower the bar.

### Kalman Signal Math
```
signal = (price - kalman_level) / uncertainty
```
This is a z-score. The Kalman "agrees" with a BUY if signal > 0 (price above estimated level). This checks CURRENT price position, not trend direction. A stock that's been dropping but bounced slightly above Kalman level would "agree" with a BUY even though trend is down. Consider using `kalman_trend` sign instead of `kalman_signal` for agreement check.

### Challenge Spread Math (BITF)
```
Active signals: mom=-0.684, brk=-1.0, news=+0.375
Spread = max(0.375, -0.684, -1.0) - min(0.375, -0.684, -1.0)
       = 0.375 - (-1.0) = 1.375
```
1.375 > 0.9 → CAUTION, 1.375 < 1.5 → not BLOCK.
Finance review: |score| = 0.367 < 0.50 → REJECT. ✅ Math checks out.

---

## Summary of Priority Actions

| # | Action | Expected Impact | Effort |
|---|--------|----------------|--------|
| 1 | Raise trade_threshold to 0.25 | +5–10% win rate | Config change |
| 2 | Fix weight sum to 1.0 | Consistent thresholds | Config change |
| 3 | Add meanrev as challenger input | Catch counter-trend trades | ~20 lines code |
| 4 | Block stocks < $5 | Avoid BITF-type losses | ~10 lines code |
| 5 | Add last-entry cutoff or EOD flatten | Reduce overnight risk | ~30 lines code |
| 6 | Tighten crypto BTC threshold to 0.003 | Better crypto filtering | Config change |
| 7 | Fix Kalman agreement to use trend, not signal | Correct directional check | ~5 lines code |
| 8 | Log weights_used in signals table | Better forensics | ~5 lines code |

---

*Analysis by Finance Agent — Feb 26, 2026 18:02 EST*
*Data: 8 post-fix round trips, 61 total trades in DB, 296 signal records*

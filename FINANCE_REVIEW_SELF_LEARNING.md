# Finance Agent Review: Self-Learning System (MW + Regime Conditioning)
## 2026-03-02 | Quantitative Analysis

---

## Summary Verdict

**The approach is sound. MW + Regime Conditioning is the right framework.** But several implementation details need adjustment to protect a $5K account. Below are specific concerns with numbers.

---

## 1. Is Multiplicative Weights + Regime Conditioning Right for Our Scale?

**Yes, with caveats.**

MW is designed for exactly this: small expert pools (we have 4 signals), online updates, no batch data requirements. The provable regret bound of O(sqrt(T ln N)) means with N=4 experts and T=255 trades (our 180-day intraday count), the regret bound is ~sqrt(255 x ln4) = sqrt(353) = 18.8 utility units. Reasonable.

**The concern is regime conditioning splitting an already small sample.** If we maintain 3 regime profiles and our 255 trades split roughly 40/40/20 across trending/ranging/volatile, each profile only sees ~100/100/50 trades over 180 days. The volatile bucket at ~50 trades is marginal for learning 4 weights.

**Recommendation:** Start with 2 regimes, not 3. Merge "ranging" and "volatile" into "non-trending." This gives ~155 trades for non-trending vs ~100 for trending over 180 days. Split into 3 only after accumulating 200+ trades per bucket.

---

## 2. Utility-Based Scoring Formula

The proposed formula:
`
utility_i = signal_score_i x sign(pnl) x |pnl| / entry_price
`

**Problems:**

### A. No position size normalization
If we buy 50 shares of a $20 stock ($1,000 position) and 10 shares of a $150 stock ($1,500 position), the $20 stock's PnL gets divided by 20 while the $150 stock's PnL gets divided by 150. A $30 profit on the $20 stock gives utility = score x 30/20 = score x 1.5. A $30 profit on the $150 stock gives utility = score x 30/150 = score x 0.2. Same dollar profit, 7.5x different utility. **This biases MW toward low-priced stocks.**

**Fix:** Normalize by position value, not entry price:
`
utility_i = signal_score_i x sign(pnl) x |pnl| / position_value
`
This makes utility a return-based metric regardless of price or position size.

### B. No holding period normalization
An intraday trade held 2 hours vs one held 6 hours -- same PnL means very different signal quality. The 2-hour trade was more capital-efficient.

**Recommendation:** For intraday, this is minor (all held <7 hours). For swing, normalize by sqrt(holding_days) to account for time exposure:
`
swing_utility_i = signal_score_i x sign(pnl) x |pnl| / (position_value x sqrt(holding_days))
`

### C. Signal score magnitude creates odd incentive
If momentum scores 0.8 and news scores 0.3, and the trade wins $50 on a $1,000 position, momentum gets utility = 0.8 x 0.05 = 0.04, news gets 0.3 x 0.05 = 0.015. **Higher-scoring signals get more credit for wins AND more blame for losses.** This is actually correct -- a confident wrong signal should be penalized more. Keep as-is.

---

## 3. Learning Rates

### Intraday eta=0.10
Standard MW theory uses eta = sqrt(ln(N)/T). For N=4, T=255: eta_optimal = sqrt(1.39/255) = 0.074. So eta=0.10 is **~35% above theoretical optimum**. Not dangerous, but slightly aggressive.

With eta=0.10 and max utility ~0.05 (typical for a $50 win on $1,000 position with score 1.0):
- Single trade weight update: w *= (1 + 0.10 x 0.05) = w x 1.005, i.e., **0.5% per trade**
- After 10 consecutive winners where momentum scored high: momentum weight shifts ~5%
- Well within the 10% drift cap, so **the cap is the real safety net, not eta**

**Dollar risk of bad convergence:** If MW converges all weight to momentum (worst case within 10% drift: 0.60 instead of 0.50), and momentum enters a losing streak, the excess loss vs baseline is roughly: 10% more weight x avg losing trade PnL. Our backtest avg loss is about -$8.50 per trade. Over 10 trades: 0.10 x 10 x $8.50 = **$8.50 excess loss.** Negligible.

**Verdict: eta=0.10 is fine.** The drift cap does the real work.

### Swing eta=0.05
For swing with ~139 test trades/year: eta_optimal = sqrt(1.39/139) = 0.10. So eta=0.05 is **half the theoretical optimum** -- too conservative. MW will learn very slowly.

With swing trades averaging ~1-2/week, at eta=0.05 it would take months to see meaningful weight movement.

**Recommendation:** Use eta=0.08 for swing.

---

## 4. The 10% Max Drift Bound

**Worst-case scenario analysis:**

With our intraday backtest: 255 trades, $381 total PnL, profit factor 1.32, win rate 49.4%.
- Avg PnL/trade = $1.49
- If bad weights reduce win rate by 3 percentage points (reasonable worst case for 10% weight shift): expectancy drops ~$0.50/trade
- Over 14-day review window (~20 trades): **$10 excess loss**
- Over full month: **$30 excess loss**

**For a $5K account, $30 is 0.6% -- totally acceptable.** Kill switches ($250/day, $400/week) are the real protection.

**Verdict: 10% drift is adequately conservative.** Could even go to 15% without meaningful risk.

---

## 5. Shadow Mode Duration: 2 Weeks

At 1.4 trades/day (backtest average), 2 weeks x 10 trading days = **14 intraday trades.** The research doc sets min_trades_for_update at 20. **You won't even hit the minimum threshold in shadow mode.**

For swing: 2 weeks might yield 2-4 trades. Statistically meaningless.

**Recommendation:**
- Shadow mode for **code validation**: 1 week is enough
- Shadow mode for **MW validation**: need 4 weeks minimum (to get ~28 trades)
- **Better approach: Run MW shadow against the existing 255-trade backtest first.** Simulate MW updates sequentially through all 255 trades and compare final PnL under MW weights vs static weights. This gives statistical significance before going live.

---

## 6. Sector-Adjusted Attribution

NVDA dropping 5% while semis drop 4%, leaving -1% excess -- the math is correct. This **undercorrects** compared to naive (which would blame momentum for the full -5%). That's the point.

**Real concern: sector ETF data quality.**
- Which ETFs? XLK for tech is too broad (NVDA is 20% of XLK). Need SMH for semis, XBI for biotech, etc.
- For intraday, need intraday sector ETF returns for the exact holding period, not daily.

**Recommendation:**
- Use sub-sector ETFs (SMH, XBI, ARKK, etc.) where available
- For intraday, **skip sector adjustment and use raw PnL.** Holding period is short enough that sector beta is minimal (correlation < 0.5 over 2-6 hours)
- For swing (4.7-day average hold), sector adjustment is valuable -- keep it

---

## 7. Auto-Revert After 3 Consecutive Losing Days

**This will fire constantly under normal conditions.**

With ~1.4 trades/day and 49.4% win rate:
- P(losing day) ~ 0.50-0.55
- P(3 consecutive losing days) = 0.50^3 to 0.55^3 = **12.5% to 16.6%**
- Expected false-trigger: **roughly every 3-4 weeks**

You'd never let MW learn because it keeps getting reverted by normal variance.

**Recommendation:** Change to one of:
- **5 consecutive losing days** -- P ~ 3-5%, triggers ~2x/year
- **Drawdown-based revert**: if post-adjustment drawdown exceeds 2x the improvement seen in shadow mode, revert
- **PnL-based revert**: if cumulative PnL since last MW adjustment is worse than -$50 (1% of account), revert

---

## Additional Concerns

### 8. Weight Normalization After Clipping
Clip then normalize can push other weights outside their drift bounds. Example: if momentum clips at 0.60 and normalization scales everything down, news might drop below its lower bound. **Need to clip-normalize-clip iteratively until convergence.**

### 9. meanrev Weight = 0.00 for Intraday
MW update w *= (1 + eta x utility) applied to w=0 always gives 0. If you ever want to discover mean reversion works for intraday, you need a small floor weight (e.g., 0.02) or a separate exploration mechanism.

### 10. Apply Nightly vs Per-Trade
Research says "update per-trade to MW state, APPLY nightly." Smart. But ensure MW state updates use the weights **active at trade time**, not updated weights. Otherwise: look-ahead bias.

---

## Recommended Changes Summary

| Item | Current | Recommended | Risk Impact |
|------|---------|-------------|-------------|
| Regime buckets | 3 | 2 (trending / non-trending) | Doubles sample size per bucket |
| Utility formula | pnl/entry_price | pnl/position_value | Removes price bias |
| Swing utility | No time adjustment | Divide by sqrt(holding_days) | Better capital efficiency signal |
| eta swing | 0.05 | 0.08 | Faster learning, still within caps |
| Shadow mode | 2 weeks live | 1 week code test + backtest simulation on 255 trades | Statistical significance |
| Sector adj (intraday) | Full sector-adjusted | Raw PnL (skip sector adj) | Simpler, avoids bad intraday ETF data |
| Auto-revert trigger | 3 consecutive losing days | 5 losing days OR -$50 cumulative | Reduces false triggers from ~monthly to ~2x/year |
| meanrev floor (intraday) | 0.00 | 0.02 | Allows exploration |
| Drift bound | 10% | 10% (keep) | Adequate for $5K |
| eta intraday | 0.10 | 0.10 (keep) | Close to theoretical optimum |

---

## Bottom Line

The MW + Regime Conditioning approach is **well-chosen and appropriate for our scale.** The theoretical foundations are solid and the safety mechanisms (drift caps, kill switches, shadow mode) are layered correctly. The main risks are implementation details -- formula bias, premature reversion, and insufficient shadow validation -- not architectural. Fix the 10 items above and this is ready for Phase 3 implementation.

**Expected impact if implemented correctly:** 2-5% win rate improvement is realistic. On $5K at ~500 trades/year, that's roughly **$75-$200 additional annual PnL** -- modest but meaningful for a system that's already marginally profitable ($381 over 180 days intraday).

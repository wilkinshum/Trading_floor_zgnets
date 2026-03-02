# Finance Review: ARCHITECTURE_V4 Risk Parameters
## 2026-03-02 — Finance Agent

---

## 1. Budget Split: $3K Intraday / $2K Swing

**Recommendation: Flip it. $2K intraday / $3K swing.**

The numbers don't support the current allocation:

| Metric | Intraday (180d) | Swing (365d, test) |
|--------|------------------|--------------------|
| Test PnL | +$228.86 | +$3,096.42 |
| Annualized PnL | ~$465 | ~$3,096 |
| Win Rate | 48.1% | 58.3% |
| Profit Factor | 1.46 | 1.79 |
| Sharpe | 0.142 | 0.215 |
| Starting Equity | not specified | $3,651 |
| Return | ~12.7% ann. | 84.8% |

Intraday's current production weights are even worse: -$79.93 PnL, 40.9% WR, 0.93 PF over 180 days. The optimized weights improve this but still produce modest returns.

Swing generates **6.7x more PnL** than intraday on roughly comparable capital. Giving 60% of capital to the weaker strategy is backwards.

**Specific recommendation:**
- Swing: $3,000 (3 positions x $1,000)
- Intraday: $2,000 (max 3 positions x $667, or 2 x $1,000)
- Revisit after 30 live trading days with the self-learner data

---

## 2. Intraday Risk: TP 5%, SL 2x ATR, Max 4 Positions, $750 Each

**TP 5% is too wide for intraday. SL 2x ATR is fine. Position count is high for $3K (and even higher concern at $2K).**

Evidence from intraday backtest exit breakdown (test set, 131 trades):
- Stop loss: 30 (22.9%)
- Take profit: **0** (0.0%)
- Trailing stop: 9 (6.9%)
- Time exit: 92 (70.2%)

**Zero trades hit the 5% TP target in 131 test trades.** The TP is decorative. Most trades exit at close (time exit) with small gains or losses. The trailing stop at least captured 9 wins.

**Recommendations:**
- Lower TP to 2.5-3% or remove it entirely and rely on trailing stops
- Keep SL at 2x ATR -- it's catching 22.9% of trades which is a reasonable stop rate
- Reduce max positions to 3 if budget stays at $3K ($1,000 each), or 2 if budget drops to $2K ($1,000 each)
- $750/position on a $5K account is fine at 15% per position; don't go below $500 (commissions eat you alive on small positions with $0.005/share)

---

## 3. Swing Risk: TP 15%, SL 8%, Max Drawdown vs SL

**8% SL per position is appropriate but needs context on portfolio-level drawdown.**

Swing backtest exit breakdown (test set, 139 trades):
- Stop loss: 41 (29.5%)
- Take profit: 18 (12.9%)
- Trailing stop: 54 (38.8%)
- Time exit: 26 (18.7%)

Unlike intraday, the 15% TP actually gets hit (18 trades). The trailing stop is the workhorse at 38.8%, which is good -- it means the trailing logic at +8% trigger / 4% trail is capturing gains.

**On the 13.2% max drawdown vs 8% SL:**

The 13.2% max drawdown is a *portfolio-level* peak-to-trough metric across all concurrent positions, not a single-position loss. With 3 max positions, worst case if all 3 hit 8% SL simultaneously = 24% of swing allocation lost. On $2K swing budget that's $480; on $3K it's $720.

At the portfolio level ($5K total), a $720 loss = 14.4%. That exceeds the backtest's 13.2% max drawdown, which means the SL parameters are actually *slightly more permissive* than what the backtest experienced.

**Recommendations:**
- Keep 8% SL -- it's consistent with the backtest's stop rate (29.5%) and the strategy's win profile
- Keep 15% TP -- it's actually getting hit unlike intraday's 5%
- The trailing trigger at +8% / 4% trail is working well (captures 38.8% of exits)
- Add a **correlated position check**: don't hold 3 swing positions in the same sector. The worst symbols (IONQ -$281, RGTI -$223) are both quantum computing -- sector correlation amplifies drawdown
- Time exits lose money: -$1,342.63 PnL from 26 time exits. Consider tightening max hold from 10 to 7 days, or adding a time-decay trailing stop that tightens after day 5

---

## 4. Self-Learning Bounds

**The bounds are reasonable. One tweak needed.**

| Parameter | Value | Assessment |
|-----------|-------|------------|
| Max weight change per review | 5% | Conservative, good |
| Min trades to adjust | 20 | Matches our evidence threshold |
| Max total drift | 15% | Possibly too loose |
| Review window | 14 days | Good balance |

**On the 15% max drift:** Starting weight for momentum is 0.50 (intraday) and 0.55 (swing). A 15% drift means momentum could go to 0.35 or 0.65. That's a huge range -- the difference between the best and worst backtest configs was often less than 15% in any single weight.

**Recommendations:**
- Tighten max_total_drift to **10%** -- still gives meaningful room to adapt but prevents the self-learner from accidentally recreating a losing config
- Keep 5% per review and 20-trade minimum -- these are solid guardrails
- The `auto_apply: false` start is smart. Keep it manual for at least 60 trading days before enabling auto
- Add a **revert trigger**: if any auto-applied change leads to 3 consecutive losing days, automatically revert to the last known good config

---

## 5. $100/Day Target Reality Check

The architecture already acknowledges this is aggressive. Let's quantify:

**What the backtests actually produce:**
- Intraday: $228.86 / ~60 test days = **$3.81/day** (optimized) or -$0.44/day (current weights)
- Swing: $3,096.42 / 365 days = **$8.48/day** on $3,651 equity

**Combined realistic daily P&L: ~$12/day on $5K** (optimized intraday + swing)

**To reach $100/day:**
- At current edge ($12/day rate), you need **$5K x ($100/$12) = ~$42K equity**
- At swing-only edge ($8.48/day on $3,651), that's a 0.23% daily return -> $100/day needs **~$43K**
- With compounding at $12/day, reaching $42K takes: ln(42000/5000) / ln(1.0024) = **884 trading days (~3.5 years)**

**The self-learning loop won't close a 8x gap.** It can maybe improve edge by 20-50% over months, getting you to $15-18/day. The path to $100/day is primarily through capital growth, not edge improvement.

**Recommendation:** Set Stage 1 target to **$25/day** (realistic with optimized parameters). Define milestones:
- Stage 1: $25/day on $5K (0.5% daily -- aggressive but in backtest range)
- Stage 2: $50/day at ~$12K equity (through compounding + deposits)
- Stage 3: $100/day at ~$25K equity

---

## 6. Kill Switch: 5% Portfolio Loss

**5% = $250 on a $5K account. This is appropriate but needs nuance.**

On a $5K account, $250 is meaningful but survivable. The question is whether it triggers too often or not often enough.

**From the backtest data:**
- Swing max drawdown: 13.2% on $3,651 = ~$482
- Intraday: with 4 positions x $750 x 2x ATR stops, worst case is roughly 4 x $75 = $300 in a single day (if ATR is ~5% of price)

A 5% daily kill switch ($250) would have triggered during the swing backtest's worst drawdown period if it occurred in a single day. But swing drawdowns typically accumulate over multiple days, not intraday.

**Recommendations:**
- Keep 5% **daily** kill switch -- it primarily protects against intraday blowups
- Add a separate **weekly** kill switch at 8% ($400) -- catches multi-day swing drawdowns
- Add a **monthly** drawdown cap at 12% ($600) -- approaching backtest max DD territory means something is broken
- At $5K, every dollar matters. 5% is the right level. Don't raise it.

---

## 7. Position Sizing with Compounding on Small Capital

**Three concerns:**

### A. Minimum viable position size
With $0.005/share commission, a 100-share trade costs $0.50 each way ($1 round trip). On a $500 position, that's 0.2% drag. On a $200 position, it's 0.5%. Below $500/position, commissions become a meaningful headwind.

**With $2K intraday / 3 positions = $667/position** -> commission drag ~0.15%. Acceptable.
**With $3K swing / 3 positions = $1,000/position** -> commission drag ~0.10%. Good.

### B. Compounding granularity
Compounding on $5K means gains are tiny in absolute terms. A 1% gain on $1,000 position = $10. After a good week ($50 gain), your positions grow to ~$683 each. The compounding effect is negligible for months.

**Recommendation:** Don't compound position sizes until equity reaches $7,500. Below that, use fixed position sizes. Compounding tiny amounts adds complexity without meaningful benefit and creates rounding issues.

### C. Odd lot risk
At $667/position buying a $150 stock = 4 shares. At 4 shares, each $1 move = $4 P&L. You're at the mercy of bid-ask spread. Stick to stocks under $50 for intraday (gives 13+ shares minimum) or increase position sizes.

**Recommendation:** Add a minimum shares filter: skip any trade where position size / price < 10 shares. This avoids getting killed by spreads on high-priced, low-share positions.

---

## Summary of Specific Recommendations

| # | Change | From | To | Rationale |
|---|--------|------|----|-----------|
| 1 | Budget split | $3K intra / $2K swing | $2K intra / $3K swing | Swing returns 6.7x more |
| 2 | Intraday TP | 5% | 2.5% or trailing-only | 0 out of 131 trades hit 5% TP |
| 3 | Intraday max positions | 4 | 2-3 | Budget reduction + concentration |
| 4 | Max total drift | 15% | 10% | Prevent drifting into losing configs |
| 5 | Daily target | $100 | $25 | Backtest supports ~$12/day; $25 is stretch goal |
| 6 | Kill switch | 5% daily only | 5% daily + 8% weekly + 12% monthly | Layered protection |
| 7 | Compounding | From day 1 | After $7,500 equity | Negligible benefit below that |
| 8 | Min shares | None | 10 shares minimum | Spread protection |
| 9 | Swing max hold | 10 days | 7 days (or tighten trail after day 5) | Time exits lose -$1,342.63 |
| 10 | Swing sector limit | None | Max 1 position per sector | IONQ+RGTI correlation risk |

---

*Review based on: backtest_results.json (v3-alpaca, 180d, 2026-03-01), swing_backtest_results.json (swing-v1, 365d, 2026-03-02). All numbers from out-of-sample test sets.*

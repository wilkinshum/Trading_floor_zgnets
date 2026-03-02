# Strategy Review — Architecture V4
## 2026-03-02 | Strategy Agent

---

## 1. Swing Weights Validity (mom=0.55, mean=0.35, brk=0.00, news=0.10, thr=0.20, TP=15%)

**Verdict: Conditionally valid, but fragile.**

The weights came from a 365-day backtest with hold=10. Reasonable sample window, but concerns:

- **Overfitting risk with hold=10 fixed**: Real swing trades exit at TP (15%), SL (8%), trail, or max-hold — not fixed 10 days. The weight profile optimized for always hold 10 days may not be optimal for dynamic exits. The backtest should have used actual exit logic (TP/SL/trail) rather than fixed hold.
- **brk=0.00 is suspicious**: Zeroing out breakout entirely means the optimizer found it was noise over that window. But breakout signals are regime-dependent (work in trends, fail in chop). A blanket zero loses breakout edge during strong trends. **Alternative**: Set brk=0.05 as a floor, or make it regime-conditional (brk=0.15 in trending regime, 0.00 in mean-reverting).
- **news=0.10 is low but reasonable** for swing. News impact decays fast; by day 3-5 of a hold, initial news is irrelevant.
- **thr=0.20 is loose for swing**. With ~/position and a 15% TP target, I want higher conviction. **Recommendation**: Start at 0.25 and let the self-learner loosen it if win rate supports it.
- **TP=15% with SL=8%** gives 1.875:1 R:R. Need ~43% WR to break even. Monitor closely.

**Action items:**
1. Re-run backtest with actual exit logic (TP/SL/trail) instead of fixed hold=10
2. Start threshold at 0.25, not 0.20
3. Track brk signal accuracy separately — revisit after 30 swing trades

---

## 2. Self-Learning Signal Accuracy Decomposition

**Verdict: Right idea, wrong attribution method.**

Current design: momentum was +0.8 and price went up -> momentum was RIGHT. This is **naive attribution** that will mislead the learner.

### The Macro Problem
If momentum is +0.8 but price drops due to Fed announcement, marking momentum as WRONG is incorrect — the signal was reading the stock correctly; an exogenous shock overrode it. Penalizing momentum for macro events systematically underweights your best signal.

### Better Approaches

**Option A — Relative attribution**: Score accuracy against sector/market-adjusted returns, not raw returns. If SPY dropped 3% and your stock dropped 2%, momentum was arguably right.

**Option B — Signal agreement weighting**: When 3/4 signals agree and the trade loses, attribute to market regime error. Only penalize signals that were the sole bullish contributor in a losing trade.

**Option C (recommended) — Conditional accuracy**: Track signal accuracy by regime. Momentum accuracy in trending vs. range-bound markets. Learn momentum works in trends but not in chop rather than momentum is 52% accurate overall.

### Implementation
Add to signal_accuracy table:
- market_regime TEXT (trending/ranging/volatile at time of trade)
- sector_return REAL (sector ETF return over same period)
- adjusted_correct BOOL (correct vs sector-adjusted return)

Use adjusted_correct for weight recommendations, not raw was_correct.

---

## 3. Independent Self-Learning for Intraday vs. Swing

**Verdict: Absolutely yes — they must be independent.**

The weight profiles reflect fundamentally different dynamics (minutes vs. days). A signal accurate for intraday tells you nothing about swing accuracy. The self-learner needs:

- Separate accuracy tables per strategy
- Separate weight adjustment tracks
- Separate min-trade thresholds (intraday hits 20 trades much faster than swing)
- Separate drift bounds

The current self_learner.py design doesn't explicitly separate these. signal_accuracy() and recommend_weight_adjustments() need a strategy parameter throughout.

**Risk if combined**: Intraday generates ~4 trades/day, swing ~1-2/week. Intraday data dominates and swing weights get dragged by intraday patterns.

---

## 4. Evidence Framework Compliance (20+ to loosen, 30+ to change weights)

**Verdict: Partially respected, needs tightening.**

Config says min_trades_to_adjust: 20, but doesn't distinguish between:
- 20 trades to loosen a filter (lower threshold, remove exclusion)
- 30 trades to change weights

Fix:
- min_trades_to_loosen_filter: 20
- min_trades_to_change_weights: 30 (MISSING)
- min_trades_to_add_exclusion: 10 (lower bar for risk-off actions)

Also: review_window_days=14 is too short for swing. At 1-2 trades/week, you get 2-4 swing trades in 14 days — nowhere near thresholds. **Swing review window should be 60-90 days**, or use trade-count window (last N trades) with a 90-day max age cap.

---

## 5. Swing Entry Timing — 9:35 AM Only vs. EOD

**Verdict: Add end-of-day entries. Single 9:35 AM entry is a significant weakness.**

### Problems with 9:35-only
- **Opening noise**: 9:30-10:00 is highest volatility. Spreads wider, gap fills in progress. For a multi-day hold, entering in the noisiest window is counterintuitive.
- **Gap-up trap**: Stock gaps up on news, momentum reads +0.8, gap fills by 10:30. Entered at worst price of the day.
- **Single sample**: One missed day = one missed opportunity.

### Recommended: Dual entry windows
- 9:35 AM — gap continuation setups (stock gaps up, holds above VWAP through first 5 min)
- 3:50 PM — trend confirmation setups (trended all day, closing near highs)

Why 3:50 PM works for swing:
- Daily candle nearly formed, full-day regime known
- Spreads tighter than open
- No gap risk — known price
- Overnight risk accepted anyway (it is a swing trade)

Different signal emphasis per window:
- AM: weight news higher (gap catalyst), require momentum confirmation
- PM: weight momentum + meanrev (full-day data), news less relevant

---

## 6. Swing Exclusion List — Apply to Intraday?

**Verdict: Partial overlap, not blanket copy.**

| Symbol | Swing problem | Intraday impact |
|--------|--------------|-----------------|
| IONQ | Quantum hype, massive gaps reverse over days | High intraday vol = tradeable with tight stops |
| RGTI | Same — momentum fakeouts multi-day | Could work intraday |
| AVAV | Moves on contract news then flatlines | Low intraday edge — exclude |
| MP | Thin, commodity-driven, gaps unpredictably | Thin = bad for both — exclude |
| POWL | Low vol most days, spikes on earnings | Low intraday vol — exclude |

**Recommendation:**
- **Exclude from both**: AVAV, MP, POWL
- **Keep for intraday only**: IONQ, RGTI (high vol is tradeable intraday; multi-day reversals irrelevant when closing by 3:45)
- **Monitor**: If intraday WR on IONQ/RGTI < 35% after 10 trades, add to exclusion

---

## Summary of Recommended Changes

| # | Item | Current | Recommended |
|---|------|---------|-------------|
| 1 | Swing threshold | 0.20 | 0.25 (loosen via self-learner after evidence) |
| 2 | Signal accuracy | Raw direction match | Sector-adjusted + regime-conditional |
| 3 | Self-learner scope | Appears unified | Explicitly separate per strategy |
| 4 | Weight change threshold | 20 trades | 30 trades for weights, 20 for filters |
| 5 | Swing review window | 14 days | 60-90 days (or trade-count gated) |
| 6 | Swing entry | 9:35 AM only | Add 3:50 PM entry window |
| 7 | Exclusion list | Swing-only extras | AVAV/MP/POWL both; IONQ/RGTI swing only |
| 8 | Breakout weight | 0.00 | 0.00 but regime-conditional override to 0.15 in trending |
| 9 | Backtest validation | Fixed hold=10 | Re-run with actual exit logic |

---

## Open Questions for Snake / Main Agent
1. The /day target needs scale beyond  or much higher edge. Plan for capital increase milestone? (e.g., at .5K equity, increase swing budget to )
2. auto_apply: false is correct for launch. Trust criteria to flip true? Suggest: 30 consecutive days of self-learner recommendations with >60% accuracy on its own predictions.
3. Kill switch is portfolio-level (-5%). Add per-strategy kill switches? (intraday -3% daily, swing -4% weekly)

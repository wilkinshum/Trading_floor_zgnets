# Strategy Review: Self-Learning System (MW + Regime Conditioning)
## 2026-03-02 | Strategy Agent Deep Review

---

## 1. MW Treating Signals as "Experts" — Wrong Abstraction

**Verdict: Partially flawed, but salvageable with reframing.**

Classical MW assumes each expert makes an independent prediction, and you combine them by weighted majority vote. That's not what we do. Our signals are *features* combined into a single composite score:

```
score = w_mom * s_mom + w_brk * s_brk + w_news * s_news
```

The signals don't independently predict "buy" or "sell" — they contribute continuous scores to a weighted sum. This matters because:

- MW's regret bound assumes you could have followed *one* expert exclusively and done well. But `s_mom = 0.6` alone doesn't produce a trade decision — it needs the threshold and the other signals for context.
- Attribution is entangled. If momentum = 0.8 and news = -0.3, you enter with composite 0.50, and the trade wins — did momentum "win"? News was negative but the trade still worked. Under MW's utility formula (`signal_score * sign(pnl) * |pnl|`), news gets *penalized* for a winning trade because its score was negative. That's actually correct behavior for our purpose — we want to downweight signals that disagreed with winning trades. But it's not MW in the textbook sense.

**What this really is**: You're doing online gradient descent on linear combination weights, using a multiplicative update rule. That's fine — it's actually a well-studied variant (exponentiated gradient). Just don't lean on MW's "best single expert" regret bound as justification, because we're not picking one expert — we're optimizing a mixture.

**Recommendation**: Keep the multiplicative update rule. It works for feature weight optimization. But rename it internally — call it "exponentiated gradient signal tuning" or just "adaptive weighting." The math is the same; the framing is more honest.

---

## 2. Regime Classification: HMM Bull/Bear ≠ Trending/Ranging/Volatile

**Verdict: Current regime_state.json is insufficient for 3 categories.**

We have HMM-based bull/bear confidence (a single axis). The research proposes 3 regimes:
- **Trending**: bull/bear confidence > 60%
- **Ranging**: "low directional conviction" (undefined)
- **Volatile**: VIX > 25 or regime confidence < 40%

Problems:
1. **Trending and Volatile overlap.** A strong bear trend with VIX at 30 is both trending AND volatile. Which profile wins?
2. **Ranging is the leftover bucket.** Confidence between 40-60% AND VIX < 25 → ranging? That's a narrow band. Most days will classify as trending or volatile, leaving ranging under-trained.
3. **VIX > 25 as "volatile" is crude.** VIX was >25 for most of 2022 — that's not a special regime, it was just the market. Realized vol vs implied vol matters more.

**Proposed fix — simplify to 2 regimes:**

- **Directional**: HMM confidence > 65% (bull OR bear — we don't care which)
- **Non-directional**: HMM confidence ≤ 65%

Two profiles means each gets 2× the training data. Add a VIX override: if VIX > 30, switch to non-directional regardless (high vol kills trend-following).

**Concrete thresholds:**
- `regime_state.json` → `bull_confidence` or `bear_confidence` > 0.65 → Directional
- Otherwise → Non-directional
- VIX > 30 override → Non-directional

---

## 3. Sector-Adjusted Attribution: Circular for Sector Dominants

**Verdict: Real problem. Needs a fix.**

NVDA is ~30% of SOXX. TSLA is ~15% of XLY. Adjusting NVDA's return by SOXX means you're subtracting a return that NVDA itself heavily influenced. If NVDA goes up 5% and SOXX goes up 2% (largely because NVDA dragged it up), the "excess return" of 3% understates NVDA's idiosyncratic move.

**Fix options:**

Option A: For mega-cap sector dominants (>10% of sector ETF), use a **peer basket** instead (e.g., NVDA → average of AMD, AVGO, MRVL, QCOM).

Option B: Use **SPY as the adjustment** for all stocks. It's never circular (no single stock is >7% of SPY), and it removes the macro component which was the original goal.

**Recommendation**: Use SPY for all sector adjustment universally. It's simpler, never circular, and the goal was just "remove the macro tide." Per-sector adjustment is over-engineering for our trade volume.

---

## 4. MW Update Frequency: Per-Trade vs Batch

**Verdict: Per-trade compute, weekly batch apply is better than nightly.**

With 2-4 intraday trades/day, per-trade MW updates will oscillate. The nightly aggregation smooths this, but even nightly apply means weights can shift based on just 2-4 trades.

**One concern**: η=0.10 with per-trade updates means each trade moves weights by up to ~1%. After a 4-trade day, cumulative drift could be 2-4% before normalization. A single bad day (4 losses) could burn through half the 10% drift budget.

**Recommendation**:
- Keep per-trade MW state updates (mathematically correct for online learning)
- Apply weights **weekly** for both strategies, not nightly for intraday
- Nightly: generate the report, label weight changes as "projected" not "recommended"

---

## 5. Starting Weights: All Profiles Identical is Wasteful

**Verdict: Yes, differentiate initial weights based on what we already know.**

Starting all profiles at `{mom:0.50, brk:0.15, news:0.25}` throws away our backtest knowledge.

**Proposed initial weights:**

For **Directional** profile:
```
intraday: {mom: 0.55, mean: 0.00, brk: 0.10, news: 0.30}  # momentum dominates in trends
swing:    {mom: 0.60, mean: 0.20, brk: 0.00, news: 0.15}   # strong trend capture
```

For **Non-directional** profile:
```
intraday: {mom: 0.35, mean: 0.10, brk: 0.15, news: 0.30}  # less momentum reliance, seed meanrev
swing:    {mom: 0.40, mean: 0.40, brk: 0.00, news: 0.15}   # mean reversion gets equal billing
```

This gives MW a head start without starting from a deliberately ignorant position.

---

## 6. Breakout Weight = 0 Is a Dead Weight Under MW

**Verdict: Yes, this is a bug. Zero stays zero forever.**

`w *= (1 + η * utility)` — if w=0, result is always 0. MW literally cannot learn whether breakout is useful.

**Recommendation**: Seed intraday breakout at 0.05. For swing, keep at 0.00 — no evidence breakout works for multi-day holds, and swing trades are too precious for exploration. Set a **minimum weight floor of 0.02** for any signal that starts non-zero to prevent MW from permanently killing a signal.

---

## 7. Swing Learning: 8 Weeks to First Adjustment

**Verdict: Too slow in absolute terms, but correct given constraints.**

~2 trades/week × 8 weeks = 16 trades (passes 15-trade min). With 4 signals and 15 trades, you have ~3-4 trades per signal where that signal was dominant. That's anecdote, not statistics.

**Recommendation**:
- Keep 15-trade minimum — it's the floor of defensibility
- Extend review window from 90 to 120 days for more context
- Add confidence tiers: "15 trades = LOW confidence, manual review required" vs "45+ trades = MEDIUM, auto-apply eligible"
- Accept that swing self-learning is a **logging and insight tool** for months 1-3

---

## Summary of Recommended Changes

| # | Issue | Recommendation | Priority |
|---|-------|---------------|----------|
| 1 | MW abstraction mismatch | Keep multiplicative updates, reframe as exponentiated gradient on feature weights | Low (cosmetic) |
| 2 | 3 regimes from 1 axis | Collapse to 2 regimes: Directional (>65%) / Non-directional. VIX>30 override. | **High** |
| 3 | Circular sector adjustment | Use SPY for all sector adjustment instead of sector ETFs | **High** |
| 4 | Update frequency | Weekly apply for both strategies, not nightly | Medium |
| 5 | Identical starting weights | Differentiate by regime using backtest priors | Medium |
| 6 | Breakout weight = 0 bug | Seed intraday breakout at 0.05. Add 0.02 min floor for non-zero signals | **High** |
| 7 | Swing 8-week delay | Keep 15-trade min, extend window to 120 days, add confidence tiers | Low |

---

## Does MW Have a Fundamental Flaw Here?

**No.** The multiplicative update rule works for online optimization of linear combination weights. The "flaw" is in framing — calling signals "experts" when they're features. The math doesn't care.

The real risk is **data volume**. With 4-10 intraday trades/day and 2 swing trades/week, any learning system will be slow and noisy. MW is one of the better choices because it's conservative by design, has provable convergence, and requires no batch retraining.

The simpler alternative: **30-day rolling Sharpe ratio per signal as the weight** (each signal's weight = its trailing Sharpe contribution / sum). No MW, no learning rates. But MW gives nice safety properties (bounded drift, multiplicative conservatism), so it's reasonable if we want guardrails.

**Bottom line**: Ship MW with the fixes above. It's not wrong, it just needs the rough edges filed down.

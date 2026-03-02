# Self-Learning System — Research & Recommendation
## 2026-03-02 | Phase 3 Architecture Decision

---

## Approaches Evaluated

### 1. Naive EWMA Accuracy Tracking (Original Plan)
- Track per-signal accuracy: "momentum said +0.8, price went up → correct"
- EWMA-weighted recent accuracy → adjust signal weights
- **Problem**: Naive attribution. If macro crashes the market, momentum signal gets blamed even though it read the technicals correctly. Noise >> signal with only 4-10 trades/day

### 2. Multiplicative Weights / Hedge Algorithm (Academic Gold Standard)
- From online learning theory (Freund & Schapire, Littlestone & Warmuth)
- Treat each signal (momentum, meanrev, breakout, news) as an "expert"
- After each trade outcome, multiply winning expert's weight by (1+η), losing by (1-η)
- Provable regret bounds: converges to best expert in hindsight
- **Perfect fit for our problem**: 4 experts, online updates, no batch retraining needed
- Used successfully in production: Numerai hedge fund uses weighted-majority for signal aggregation (Numin paper, ICAIF 2024)

### 3. Bayesian Thompson Sampling
- Maintain Beta distribution per signal: Beta(α=wins, β=losses)
- Sample from each distribution to generate stochastic weights
- Naturally balances exploration (try underweighted signals) vs exploitation (use proven ones)
- **Good for small samples** — handles uncertainty gracefully with few trades
- **Problem**: Harder to implement correctly, distributions can be unstable early

### 4. Full RL / Deep Q-Learning
- Train an agent to optimize weight selection as actions
- **Already ruled out**: ~4,500 signals/180 days is orders of magnitude too small for RL

### 5. Regime-Conditional Expert Switching (Adaptive Regret)
- From "Adaptive Regret for Regime-Shifting Markets" (QuantBeckman)
- Instead of one set of weights, maintain per-regime weight profiles
- Bull market → weights A, Bear → weights B, Choppy → weights C
- Switch profiles when regime monitor detects shift
- **Key insight**: A model optimized for trending markets will bleed in mean-reverting ones. Static weights are implicitly optimized for their training regime

---

## Recommendation: Hybrid Multiplicative Weights + Regime Conditioning

**Why this combination wins for our system:**

1. **Multiplicative Weights (MW)** is mathematically proven to converge with few samples. We have 4 experts (signals) — MW works beautifully with small expert pools. No hyperparameter tuning nightmare.

2. **Regime conditioning** solves the biggest weakness of MW: regime shifts that invalidate learned weights. By maintaining separate weight profiles per regime, we adapt faster when markets change character.

3. **Utility-based scoring** (from Numin paper) instead of raw accuracy. Score signals by their contribution to PnL, not just direction. A momentum signal that was "right" but only made $0.02 is less valuable than news signal that was "wrong" 40% of the time but made $5 on the correct calls.

4. **Conservative bounds** fit our evidence framework: MW naturally makes small adjustments (multiplicative factor < 2x per update), and we can cap total drift.

---

## Detailed Algorithm Design

### Core: Multiplicative Weights Update (MW)

```
For each trade t:
  1. Record outcome: PnL, entry signals {s_mom, s_mean, s_brk, s_news}
  2. For each signal i:
     - utility_i = signal_score_i * sign(pnl) * |pnl| / entry_price
     - If utility_i > 0:  w_i *= (1 + η * utility_i)    # reward
     - If utility_i <= 0: w_i *= (1 - η * |utility_i|)   # penalize
  3. Normalize: w_i = w_i / sum(w_all)
  4. Clip to bounds: w_i = clip(w_i, baseline_i - max_drift, baseline_i + max_drift)
  5. Store updated weights + audit trail
```

**Parameters:**
- η (learning rate) = 0.1 (conservative — standard MW uses 0.5, we halve it)
- max_drift = 0.10 from baseline (per strategy agent recommendation)
- Min weight = 0.0 (signals can be zeroed, as meanrev already is for intraday)
- Update frequency: per-trade (real-time learning)
- But APPLY only nightly after review (safety gate)

### Regime-Conditional Profiles

```
Maintain 3 weight profiles:
  - TRENDING (Bull/Bear with confidence > 60%)
  - RANGING  (low directional conviction)
  - VOLATILE (VIX > 25 or regime confidence < 40%)

Each profile has independent MW state:
  regime_weights = {
    'trending':  {mom: 0.50, mean: 0.00, brk: 0.15, news: 0.25},
    'ranging':   {mom: 0.50, mean: 0.00, brk: 0.15, news: 0.25},
    'volatile':  {mom: 0.50, mean: 0.00, brk: 0.15, news: 0.25},
  }

Active profile selected by regime_state.json at scan time.
MW updates only apply to the profile that was active during the trade.
```

### Signal Attribution (Sector-Adjusted)

Strategy agent correctly identified that naive attribution is broken. Fix:

```
For each closed trade:
  1. Get the sector ETF return over the same holding period
  2. excess_return = stock_return - sector_return
  3. For each signal:
     - If signal predicted direction of EXCESS return → correct
     - If signal predicted direction of total return but not excess → neutral (no update)
     - If signal predicted wrong on excess return → incorrect
```

This prevents macro events from corrupting signal accuracy.

### Separate Learning Tracks (per Strategy Agent)

```
Intraday SelfLearner:
  - review_window: 14 days
  - min_trades_for_update: 20
  - MW learning rate η: 0.10
  - Update: per-trade to MW state, APPLY nightly
  
Swing SelfLearner:
  - review_window: 90 days
  - min_trades_for_update: 15 (fewer trades, lower bar)
  - MW learning rate η: 0.05 (more conservative — fewer samples)
  - Update: per-trade to MW state, APPLY weekly (Fridays)
```

### Safety & Reversion System

```
Stability gates:
  1. min_trades_since_last_adjustment: 10
  2. If 3 consecutive losing days after adjustment → auto-revert to baseline
  3. If total drift from baseline > 10% on any weight → hard cap
  4. Auto-apply = FALSE initially (recommendations only)
  5. After 50 trades with auto_apply=false, if recommendations would have improved PnL → suggest enabling
  
Shadow mode:
  - First 2 weeks: MW computes weights but doesn't apply them
  - Tracks "what would have happened" with MW weights vs baseline
  - If MW weights underperform baseline → something is wrong, don't enable
```

### Nightly Review Report

```
memory/reviews/YYYY-MM-DD.md:
  - Trades today: N intraday, M swing
  - PnL: intraday $X, swing $Y
  - Signal accuracy (sector-adjusted):
    momentum: 62% (14/23 correct)
    meanrev: N/A (weight=0)
    breakout: 45% (9/20 correct)  
    news: 58% (11/19 correct)
  - MW recommended weights: {mom: 0.52, brk: 0.13, news: 0.27}
  - Current baseline: {mom: 0.50, brk: 0.15, news: 0.25}
  - Drift: mom +0.02, brk -0.02, news +0.02
  - Action: RECOMMEND (auto_apply=false)
  - Symbol performance: MARA 3/5 wins, NVDA 1/4 (watchlist)
  - Kill switch status: daily PnL -$45 (within limits)
```

---

## Implementation Files

```
src/trading_floor/review/
├── __init__.py
├── self_learner.py          # Main orchestrator
├── multiplicative_weights.py # MW algorithm + regime profiles  
├── signal_attribution.py     # Sector-adjusted accuracy
├── safety.py                 # Bounds, reversion, shadow mode
└── reporter.py               # Nightly report generator

configs/
├── overrides.yaml            # Written by self_learner (never workflow.yaml)
└── mw_state.json             # MW weight state (persisted between runs)
```

---

## Why NOT the Other Approaches

| Approach | Why Not |
|----------|---------|
| Naive EWMA | Macro noise corrupts attribution. No theoretical guarantees. |
| Thompson Sampling | Elegant but harder to debug/explain. MW is equally good with our 4-expert problem and more transparent |
| Full RL | ~5,000 trades/year is not enough data. Need millions of episodes |
| Static grid search | Already did this (v3.1). One-time optimization doesn't adapt to changing markets |
| LLM-based review | Too expensive for nightly runs. No mathematical guarantees. Good as supplement (finance agent does this already) |

---

## Expected Impact

With MW + regime conditioning:
- **Week 1-2**: Shadow mode only, collecting baseline comparison
- **Week 3-4**: First weight adjustments (if shadow shows improvement)
- **Month 2+**: System continuously adapts, weights shift toward more accurate signals per regime
- **Conservative estimate**: 2-5% improvement in win rate from adaptive weighting
- **Key value**: Automatic regime adaptation — no manual intervention when markets shift

This approach is used by real hedge funds (Numerai), has mathematical convergence proofs, and works with exactly the trade volume we generate (~4-10 trades/day intraday + 1-2/week swing).

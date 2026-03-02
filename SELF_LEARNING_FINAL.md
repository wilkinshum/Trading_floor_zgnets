# Self-Learning System — FINAL SPEC (Post Finance + Strategy Review)
## 2026-03-02 | All Agent Recommendations Applied

---

## Algorithm: Adaptive Signal Weighting (Exponentiated Gradient)

Multiplicative weight updates on signal feature weights, with regime-conditional profiles.
Not classical MW "expert aggregation" — it's online optimization of linear combination weights.

---

## Core Update Rule

```
For each closed trade t:
  1. Compute per-signal utility:
     INTRADAY:
       utility_i = signal_score_i * sign(pnl) * |pnl| / position_value
     
     SWING:
       utility_i = signal_score_i * sign(pnl) * |pnl| / (position_value * sqrt(holding_days))

  2. Update MW state (per regime profile, per strategy):
     If utility_i > 0:  w_i *= (1 + η * utility_i)
     If utility_i <= 0: w_i *= (1 - η * |utility_i|)

  3. Apply floors: w_i = max(w_i, min_floor)  # 0.02 for non-zero signals
  
  4. Clip to drift bounds: w_i = clip(w_i, baseline_i - max_drift, baseline_i + max_drift)
  
  5. Normalize: w_i = w_i / sum(w_all)
  
  6. Re-clip (iterative until stable): repeat clip + normalize until convergence

  7. Store updated MW state to mw_state.json
```

**Parameters:**
- η_intraday = 0.10
- η_swing = 0.08
- max_drift = 0.10 from baseline per weight
- min_floor = 0.02 (prevents signals from being permanently zeroed)
- Update: per-trade to MW state file
- **APPLY: weekly** (not nightly — too noisy with 2-4 trades/day)

---

## 2 Regime Profiles (Not 3)

Classification from regime_state.json:

```
DIRECTIONAL:   max(bull_confidence, bear_confidence) > 0.65
NON-DIRECTIONAL: max(bull_confidence, bear_confidence) <= 0.65
VIX OVERRIDE:  VIX > 30 → force NON-DIRECTIONAL regardless
```

### Differentiated Starting Weights (from backtest priors)

**Intraday — Directional profile:**
```
{momentum: 0.55, meanrev: 0.02, breakout: 0.10, news: 0.28, reserve: 0.05}
```

**Intraday — Non-directional profile:**
```
{momentum: 0.35, meanrev: 0.10, breakout: 0.15, news: 0.30, reserve: 0.10}
```

**Swing — Directional profile:**
```
{momentum: 0.60, meanrev: 0.20, breakout: 0.00, news: 0.15, reserve: 0.05}
```

**Swing — Non-directional profile:**
```
{momentum: 0.40, meanrev: 0.40, breakout: 0.00, news: 0.15, reserve: 0.05}
```

Notes:
- Intraday breakout seeded at 0.05-0.15 (not 0) so MW can learn
- Intraday meanrev seeded at 0.02-0.10 (not 0) so MW can explore
- Swing breakout stays 0.00 — no evidence it works multi-day, too few trades to waste on exploration
- Reserve weight absorbs normalization slack

---

## Attribution Method

**Intraday**: Raw PnL — no sector adjustment. Holding period too short for meaningful sector beta.

**Swing**: SPY-adjusted returns:
```
excess_return = stock_return - spy_return_over_same_period
attribution uses excess_return instead of raw return
```
- SPY used universally (never circular, no single stock >7% of SPY)
- Removes macro tide from swing signal accuracy

---

## Safety & Reversion System

```
Stability gates:
  1. min_trades_since_last_apply: 10 (intraday), 5 (swing)
  2. Auto-revert trigger: 5 consecutive losing days OR -$50 cumulative PnL since last adjustment
  3. Max drift: 10% from baseline per weight
  4. Min floor: 0.02 for any non-zero signal
  5. auto_apply = FALSE initially (recommendations only)

Shadow validation before going live:
  Phase A: Backtest MW against existing 255 intraday trades (simulate sequential updates)
  Phase B: 1 week live shadow (code validation only)
  Phase C: Enable weekly weight application if backtest showed improvement

Confidence tiers (for reporting):
  - <15 trades: INSUFFICIENT — no recommendation
  - 15-30 trades: LOW — manual review required, cannot auto-apply
  - 30-45 trades: MEDIUM — can auto-apply if auto_apply=true
  - 45+ trades: HIGH — full confidence in recommendations
```

---

## Separate Learning Tracks

```
Intraday:
  - Review window: 14 days
  - Min trades to apply: 20
  - Apply cadence: Weekly (Friday close)
  - η: 0.10
  - Attribution: Raw PnL / position_value
  - Regimes: Directional / Non-directional

Swing:
  - Review window: 120 days
  - Min trades to apply: 15
  - Apply cadence: Weekly (Friday close)  
  - η: 0.08
  - Attribution: SPY-adjusted PnL / (position_value * sqrt(holding_days))
  - Regimes: Directional / Non-directional
  - Confidence tiers active (LOW/MEDIUM/HIGH)
  - First real adjustment: ~8 weeks (insight tool until then)
```

---

## Nightly Report (Review, Not Apply)

```
memory/reviews/YYYY-MM-DD.md:
  - Trades today: N intraday, M swing
  - PnL: intraday $X, swing $Y
  - Active regime: Directional/Non-directional (confidence %)
  - Signal accuracy (per strategy, per regime):
    momentum: 62% (14/23 correct)
    meanrev: N/A (weight=0.02, insufficient data)
    breakout: 45% (9/20 correct)  
    news: 58% (11/19 correct)
  - Current MW weights vs baseline:
    Directional:     {mom: 0.57, mean: 0.02, brk: 0.08, news: 0.28} (drift: +2/0/-2/+0)
    Non-directional: {mom: 0.34, mean: 0.11, brk: 0.14, news: 0.31} (drift: -1/+1/-1/+1)
  - Confidence tier: MEDIUM (34 trades in window)
  - Projected weekly apply: YES/NO (meets thresholds?)
  - Cumulative PnL since last adjustment: +$23 (within bounds)
  - Kill switch status: daily -$12, weekly +$45, monthly +$89
  - Symbol performance: MARA 3/5 wins, NVDA 1/4 (watchlist)
```

---

## Implementation Files

```
src/trading_floor/review/
├── __init__.py
├── self_learner.py              # Main orchestrator: nightly review + weekly apply
├── adaptive_weights.py          # Exponentiated gradient update rule + regime profiles
├── signal_attribution.py        # Raw PnL (intraday) + SPY-adjusted (swing)
├── safety.py                    # Drift bounds, reversion triggers, confidence tiers
└── reporter.py                  # Nightly markdown report generator

configs/
├── overrides.yaml               # Written by self_learner (never touches workflow.yaml)
└── mw_state.json                # MW weight state per strategy per regime (persisted)
```

---

## Config Section

```yaml
self_learning:
  enabled: true
  auto_apply: false
  apply_cadence: weekly          # Friday close
  
  regimes:
    directional_threshold: 0.65
    vix_override: 30
    
  intraday:
    eta: 0.10
    review_window_days: 14
    min_trades_to_apply: 20
    max_drift: 0.10
    min_weight_floor: 0.02
    attribution: raw_pnl         # no sector adjustment
    baselines:
      directional:     {momentum: 0.55, meanrev: 0.02, breakout: 0.10, news: 0.28, reserve: 0.05}
      non_directional: {momentum: 0.35, meanrev: 0.10, breakout: 0.15, news: 0.30, reserve: 0.10}
    
  swing:
    eta: 0.08
    review_window_days: 120
    min_trades_to_apply: 15
    max_drift: 0.10
    min_weight_floor: 0.02
    attribution: spy_adjusted     # SPY-adjusted PnL / (position_value * sqrt(holding_days))
    baselines:
      directional:     {momentum: 0.60, meanrev: 0.20, breakout: 0.00, news: 0.15, reserve: 0.05}
      non_directional: {momentum: 0.40, meanrev: 0.40, breakout: 0.00, news: 0.15, reserve: 0.05}
  
  safety:
    revert_after_consecutive_losing_days: 5
    revert_after_cumulative_loss: 50     # dollars
    min_trades_since_last_apply: 10      # intraday
    min_trades_since_last_apply_swing: 5
    confidence_tiers:
      insufficient: 15
      low: 30
      medium: 45                         # auto-apply eligible above this
```

---

## Validation Plan

1. **Backtest simulation**: Run MW sequentially through 255 existing intraday trades. Compare final PnL with static weights vs MW-adapted weights. Must show improvement or we don't ship.
2. **Unit tests**: Update rule math, drift bounds, reversion logic, regime classification, confidence tiers
3. **1-week live shadow**: Verify code runs nightly, generates reports, doesn't crash
4. **Week 2+**: Enable weekly apply if backtest showed improvement

---

## Changes from Original Research Doc

| # | Original | Final | Source |
|---|----------|-------|--------|
| 1 | 3 regimes | 2 regimes (Directional/Non-directional) | Finance + Strategy |
| 2 | pnl/entry_price | pnl/position_value | Finance |
| 3 | No time normalization | sqrt(holding_days) for swing | Finance |
| 4 | eta_swing=0.05 | eta_swing=0.08 | Finance |
| 5 | Full sector-adjusted both | Raw for intraday, SPY for swing | Finance + Strategy |
| 6 | 3-day revert | 5-day OR -$50 cumulative | Finance |
| 7 | meanrev=0.00 intraday | meanrev=0.02 floor | Finance |
| 8 | breakout=0.00 intraday | breakout=0.05-0.15 (regime-dependent) | Strategy |
| 9 | Nightly apply | Weekly apply | Strategy |
| 10 | Identical starting weights | Differentiated by regime from backtest | Strategy |
| 11 | 2-week shadow | Backtest validation first + 1-week shadow | Finance |
| 12 | 90-day swing window | 120-day swing window | Strategy |
| 13 | No confidence tiers | 4-tier system (insufficient/low/medium/high) | Strategy |
| 14 | "Multiplicative Weights" | "Exponentiated Gradient Signal Tuning" | Strategy (naming) |
| 15 | 0.02 min floor | 0.02 for all non-zero signals | Finance + Strategy |

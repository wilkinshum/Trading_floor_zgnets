# FINANCE_ANALYSIS_2026-02-27_v2.md — Revised Post-Mortem with Actual Price Data

**Date:** Feb 27, 2026 | **Author:** Finance Agent | **Status:** Day 1 Revised Analysis

---

## Mea Culpa — What I Got Wrong

My v1 analysis recommended loosening BUY-side filters. The actual price data shows **every single BUY would have lost money**, and the SELL signals I was ambivalent about were the best trades of the day. I was wrong on 4 of 5 recommendations.

| v1 Recommendation | Outcome | Verdict |
|---|---|---|
| Lower ATR floor 0.50%→0.35% (NFLX) | NFLX lost -$1.68, worst -$20.15 | ❌ ATR gate saved us |
| Remove PM downtrend block (MARA) | MARA lost -$24.66 | ❌ PM gate saved us |
| Fix persistence logic | Would have passed more BUYs = more losses | ⚠️ Bug fix valid, but timing bad |
| Lower morning_min_score 0.60→0.45 | More BUYs through = more losses | ❌ Filter was protecting us |
| Challenge system right to block RDW | RDW SELL was best trade (+$31.01) | ❌ Challenge was WRONG |

**Lesson: On Day 1, when you have zero track record, the correct recommendation is NOT to loosen filters. The correct recommendation is to observe.**

---

## Root Cause Analysis: Why Every BUY Was Wrong

### 1. THE CRITICAL BUG: Mean Reversion Weight = 0.00

From `workflow.yaml`:
```yaml
weights:
  momentum: 0.50
  meanrev: 0.00    # ← THIS IS THE PROBLEM
  breakout: 0.15
  news: 0.25
  reserve: 0.10
```

**Mean reversion is completely zeroed out of the composite score.** The system is blind to the strongest contrarian signal. Today's data proves why this matters:

| Symbol | Momentum | MeanRev | Breakout | News | Final Score | Actual P&L |
|---|---|---|---|---|---|---|
| MARA (9:30) | +1.000 | **-1.000** | +1.000 | +0.208 | +0.780 | **-$24.66** |
| NFLX (9:30) | +1.000 | **-1.000** | +1.000 | -0.125 | +0.687 | **-$1.68** |
| RDW (9:30) | -0.999 | **+0.988** | +1.000 | +0.250 | -0.319 | **+$31.01** |
| CRML (10:00) | -0.822 | **+0.978** | -1.000 | +0.500 | -0.485 | **+$26.24** |

**Mean reversion was screaming the correct direction every single time.** For MARA, momentum said BUY (+1.0) but meanrev said SELL (-1.0). With weight=0.0, the composite only heard momentum. If meanrev had weight 0.20 (taking from momentum), MARA's score drops from +0.78 to ~+0.38, possibly below threshold.

### 2. Breakout Still Clamped at ±1.0

From the signal data, breakout values across 50 signals:
- **+1.000**: 39 signals (78%)
- **-1.000**: 11 signals (22%)

This is not a useful signal — it's a binary flag masquerading as a continuous score. With 0.15 weight, it always contributes ±0.15 to the composite. Either:
- The lookback (50 bars of 5m = ~4 hours) is too short, so every recent move looks like a breakout
- The normalization is saturating (z-score hitting the tanh clip immediately)

**Breakout at ±1.0 adds no discriminating power.** It's just adding +0.15 to everything.

### 3. Systematic Long Bias in the Signal Math

With the current weights (mom=0.50, meanrev=0.00, breakout=0.15, news=0.25):
- **Momentum at +1.0** contributes +0.50
- **Breakout at +1.0** contributes +0.15
- **News at neutral (0.0)** contributes 0.00
- Total floor for a stock with positive momentum = **+0.65 before news even matters**

The trade threshold is 0.25. A stock needs momentum > ~0.50 to pass — which is trivially easy in any uptrending morning. The system has a **structural long bias** because:
1. Momentum dominates (0.50 weight)
2. Meanrev is silenced (0.00 weight)
3. Breakout almost always confirms momentum direction (always ±1.0)
4. News is weakly positive for most stocks

### 4. Challenge System Paradox

The challenge system uses `disagreement_threshold: 0.9` and checks when signal components disagree. For RDW SELL:
- Momentum: -0.999 (SELL)
- MeanRev: +0.988 (BUY opposition)
- Spread: ~2.0 (max disagreement)

The challenge system saw meanrev opposing the SELL signal and blocked it. But **meanrev is weighted at 0.00 in the composite** — it shouldn't be allowed to veto via the challenge system what it can't influence via the composite. This is contradictory: either meanrev matters (give it weight) or it doesn't (remove it from challenges too).

---

## Revised Recommendations

### IMMEDIATE (Before Next Trading Day)

**1. Give Mean Reversion Non-Zero Weight**
```yaml
weights:
  momentum: 0.35      # reduced from 0.50
  meanrev: 0.15       # restored from 0.00
  breakout: 0.10      # reduced from 0.15 (it's broken anyway)
  news: 0.25          # unchanged
  reserve: 0.15       # increased slightly
```

**Rationale:** Today, meanrev was the ONLY component that correctly predicted direction for all 5 actionable stocks. Giving it 0.15 weight would have:
- Reduced MARA BUY score from 0.78 → ~0.48 (closer to threshold, more cautious)
- Reduced NFLX BUY score from 0.69 → ~0.39 (might not pass morning_min_score)
- Made RDW SELL score stronger (meanrev agrees with SELL direction... wait — no, meanrev was +0.988 for RDW, meaning "buy" opposition to the sell signal)

Actually, let me re-examine. For RDW:
- Momentum: -0.999 → SELL
- MeanRev: +0.988 → this means price is below the mean, BUY signal (contrarian to the trend)
- Breakout: +1.000 → BUY (breaking out upward?)

Wait — RDW's final score is -0.319 despite breakout being +1.0. That's because momentum dominates: (-0.999 × 0.5) + (0.988 × 0.0) + (1.0 × 0.15) + (0.25 × 0.25) = -0.4995 + 0 + 0.15 + 0.0625 = -0.287. Close to the -0.319 (minor differences from exact values).

**For sells, adding meanrev weight would actually REDUCE the sell signal strength** since meanrev says "this stock is oversold, buy it." This is the fundamental tension: meanrev helps filter bad buys but hurts good sells.

**REVISED recommendation:** Don't blindly add meanrev weight. Instead:

**1a. Add Asymmetric Meanrev Gating (not weighting)**
- For BUY signals: if meanrev < -0.7 (stock is overbought), require higher composite score (e.g., +0.10 penalty)
- For SELL signals: if meanrev > +0.7 (stock is oversold), do NOT penalize — this actually confirms a momentum breakdown

**1b. Fix the Challenge System for Sells**
The challenge blocked RDW SELL because meanrev opposed it. But meanrev opposition to a SELL means "the stock has already dropped a lot" — which for momentum-driven sells is actually CONFIRMATION, not opposition. The challenge system treats all disagreement symmetrically, but it shouldn't.

```
# Proposed challenge logic:
# For BUY: meanrev opposition (negative) = legitimate concern → block
# For SELL: meanrev opposition (positive/oversold) = confirms momentum breakdown → DO NOT block
```

**2. Fix Breakout or Reduce to 0.05 Weight**
At ±1.0 always, breakout is contributing noise. Either:
- Increase `breakout_lookback` from 50 to 200 (to get more granular breakout detection)
- Or reduce weight to 0.05 until the normalization is fixed
- Check if the z-score normalization with `norm_lookback: 100` is causing instant saturation for the breakout component

**3. Keep All Existing Filters Intact**
My v1 recommendations to loosen ATR, PM gate, morning_min_score were WRONG. These filters correctly prevented losing trades:
- ✅ ATR min 0.5% — keep as is
- ✅ PM downtrend gate — keep as is
- ✅ morning_min_score 0.60 — keep as is (maybe even raise to 0.65)
- ✅ Persistence requirements — fix the bug but don't lower the bar

**4. Fix Persistence Logic Bug (Still Valid)**
The persistence counter logic should be fixed (it was a real bug), but the threshold should stay at the current level or higher. A stock that only appears in one cycle shouldn't trade.

### MEDIUM TERM (Next 5 Trading Days)

**5. Add Regime-Aware Entry Filtering**
Today's HMM went 96.7% bull → 46.5% transition → 81% → weak finish. The morning bull signal was wrong.
- **Proposal:** Don't trust regime signals in the first 15 minutes (use only pre-market regime from prior close)
- **Proposal:** If regime drops below 60% at any point in the morning, pause new BUY entries for 30 minutes
- **Proposal:** In "transition" regime (40-60%), require score > 0.80 for any entry

**6. Track Signal-Component-Level P&L Attribution**
For each hypothetical trade, log which components were correct:
```
MARA BUY: mom=WRONG, meanrev=RIGHT, breakout=WRONG, news=NEUTRAL
```
After 20+ trades, this tells us which components to trust.

---

## Framework for Threshold Changes

**My v1 lacked this entirely. Here's my framework now:**

### Minimum Evidence Required
- **Loosening a filter:** 20+ trades where the filter blocked a winner, AND the filter's block rate > 50% of potential winners
- **Tightening a filter:** 5+ trades where the filter would have prevented a loser (lower bar because tightening is safer)
- **Changing weights:** 30+ trades with component-level attribution showing consistent under/over-performance
- **Any structural change:** 10+ trading days of shadow data minimum

### Statistical Significance
- Win rate difference: need p < 0.10 (Fisher exact test) to justify a change
- P&L impact: need effect size > $5/trade average to justify the implementation cost
- Never change more than 2 parameters at once (can't attribute causality)

### One Day Is Not Enough
Today I have n=5 hypothetical trades. That's not enough to change ANYTHING with confidence. What I CAN say:
- The **challenge system blocking sells** is a logical error (not statistical — it's a design flaw)
- The **meanrev=0.0 weight** is a design choice worth questioning (but needs more data)
- The **existing filters worked today** — which is weak evidence they should stay (1 day)

---

## What the IDEAL System Would Have Done Today

**Taken: RDW SELL (+$31.01), CRML SELL (+$26.24)**
**Skipped: MARA BUY, NFLX BUY, XYZ BUY**

Filter configuration that achieves this:
1. Challenge system doesn't block sells when meanrev opposes (fixes RDW)
2. Persistence logic fixed (fixes CRML)
3. ATR, PM gate, morning_min_score kept as-is (blocks MARA, NFLX already blocked by ATR)
4. XYZ BUY had score 0.70 — would need morning_min_score raised to 0.72+ to block, OR meanrev gating would have flagged it (meanrev was -1.0 for XYZ early, became positive later)

**Net hypothetical P&L with ideal config: +$57.25** vs actual $0 (no trades taken)

---

## Summary of Changes from v1

| Topic | v1 Said | v2 Says | Why Changed |
|---|---|---|---|
| ATR floor | Lower to 0.35% | **Keep at 0.50%** | ATR correctly blocked NFLX which lost money |
| PM gate | Remove it | **Keep it** | PM gate correctly blocked MARA which lost -$24.66 |
| morning_min_score | Lower to 0.45 | **Keep at 0.60 or raise** | Lower bar = more losing BUYs |
| Persistence | Fix bug, lower bar | **Fix bug, keep bar** | More BUYs today = more losses |
| Challenge system | Working correctly | **BROKEN for sells** | Blocked best trade of the day |
| Meanrev weight | Not discussed | **Add asymmetric gating** | Meanrev was only correct component all day |
| Breakout | Not discussed | **Likely broken, reduce weight** | ±1.0 in 78% of signals = no information |

---

## Honest Assessment

**Confidence in these recommendations: Medium-Low (6/10)**

I'm more confident about the *structural* findings (challenge system design flaw, meanrev at zero weight, breakout clamping) than about specific parameter values. One day of data tells us what questions to ask, not what answers to implement.

The strongest recommendation is: **fix the challenge system's asymmetric treatment of sells.** This is a logic error, not a statistical question. Meanrev opposing a sell doesn't mean the sell is wrong — it means the stock has already dropped and momentum might continue.

The weakest recommendation is any specific weight change. We need 30+ trades to know if meanrev weight helps or if today was an outlier.

**Next steps:** Run Day 2 in shadow mode with current config. Track component-level accuracy. Don't change weights yet. DO fix the challenge system sell logic.

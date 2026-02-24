# Post-Mortem: RGTI & IREN Losses — Feb 24, 2026

## Trade Summary
| Symbol | Entry | Exit | Hold Time | PnL | Exit Reason |
|--------|-------|------|-----------|-----|-------------|
| RGTI | 54 @ $16.72 (10:30) | $16.38 (10:40) | **~10 min** | **-$19.25** | ATR stop (-2.1%) |
| IREN | 15 @ $45.34 (10:30) | $44.35 (10:45) | **~15 min** | **-$15.60** | ATR stop (-2.2%) |
| TMQ | 144 @ $4.11 (9:30) | $4.32 (11:10) | **~100 min** | **+$29.63** | Take profit (+5.1%) |

## Signal Analysis

### What triggered the entries?
Both RGTI and IREN entered at the **10:30 AM cycle** when the HMM flipped from 84% bear → 81% bull.

| Signal | RGTI (10:30) | IREN (10:30) | TMQ (9:30) |
|--------|-------------|-------------|------------|
| **Momentum** | 0.793 | 0.905 | -0.056 |
| **Breakout** | 1.000 | 1.000 | 1.000 |
| **Mean Rev** | -0.996 | -1.000 | -0.758 |
| **News** | **0.250** | **0.000** | 0.250 |
| **Final** | 0.541 | 0.517 | 0.267 |

### Key Finding: News scores were weak or zero
- **IREN news score = 0.000** — The news agent found NOTHING relevant. Zero signal.
- **RGTI news score = 0.250** — Barely above noise.
- Both were carried entirely by **momentum + breakout** (which are lagging/reactive signals).
- **TMQ** survived because it entered earlier (9:30, before the choppy regime flip) and had time to build a cushion.

## Macro Context (what our system missed)

### Monday Feb 23 — Market Selloff
- **Wall Street finished sharply lower** on tariff fog + AI disruption fears
- Tech stocks hit hardest — software names slumped after Anthropic announcements
- This was the OVERNIGHT backdrop going into Tuesday morning

### Tuesday Feb 24 — The Rebound... Sort Of
- Market **rebounded** led by tech recovery, but it was uneven
- **RGTI headwinds**: Pulled back to $15-18 range after January delay of 108-qubit system. Net loss of $350M on $7.5M revenue. Quantum stocks broadly weak (IONQ, D-Wave down 5%+ previous week)
- **IREN headwinds**: Wobbling on MSCI rebalance, bitcoin drop, AND new U.S. tariffs on AI chips hitting data center sentiment. "Half bitcoin miner, half AI infrastructure play" = double exposure to negative catalysts

### What sector-level news would have caught:
1. **"U.S. tariffs on advanced AI chips"** → Would flag IREN (data center/AI infra) as sector risk
2. **"Quantum computing stocks broadly weak"** → Would flag RGTI as sector drag
3. **"Bitcoin dropping, crypto-linked names volatile"** → Would flag IREN's crypto mining exposure
4. **"Wall Street tech selloff Monday"** → Broad caution signal for all high-beta tech

## Root Causes

### 1. No Sector/Industry News Filter
Our news agent checks **individual stock headlines** only. It doesn't check:
- Sector ETFs (XLK, ARKQ, BITQ)
- Industry-wide themes (AI tariffs, quantum selloffs, crypto drops)
- Macro sentiment (Monday's broad selloff)

### 2. HMM Regime Whipsaw
- 9:30: Bear 84% → 10:00: Bear 44% → 10:30: **Bull 81%** → 11:00: Bull 61%
- The system entered RGTI/IREN on a **false bull flip** that lasted < 30 minutes
- No regime stability filter exists

### 3. High-Beta Names in Choppy Market
- RGTI (quantum) and IREN (crypto/AI) are inherently high-volatility
- In a regime-uncertain market, these are the WORST entries
- TMQ (small cap, lower beta) survived precisely because it's less correlated to tech/AI sentiment

### 4. Breakout Signal Dominated
- Both had breakout = 1.000 (max) — but breakout in a whipsaw market = false signal
- Mean reversion was screaming SELL (both near -1.000) — system overrode it with momentum + breakout weights

## Recommendations

### Quick Win: Sector News Filter (Phase 1)
Add a **sector sentiment check** before entry:
1. Map each stock to its sector/industry (RGTI→Quantum Computing, IREN→Crypto Mining/AI Infra)
2. Before entering, check sector-level news via web search (e.g., "quantum computing stocks today", "crypto mining stocks today")
3. Assign a **sector_news_score** (-1 to +1)
4. If sector_news_score < -0.3, **block the entry** regardless of individual signals

### Medium Win: Regime Stability Filter (Phase 1.5)
- Require HMM regime to hold for **2 consecutive scans** before trusting a flip
- A bear→bull flip at 10:30 shouldn't trigger entries until confirmed at 11:00
- This alone would have prevented both RGTI and IREN entries

### Medium Win: Beta/Volatility Scoring (Phase 2)
- Penalize high-beta names when regime confidence < 70%
- In uncertain regimes, prefer lower-beta, lower-correlation stocks
- TMQ would have scored higher than RGTI/IREN under this rule

### Advanced: Ensemble Disagreement (Phase 2)
- Mean reversion was at -1.000 for both stocks (strong SELL signal)
- When signals **strongly disagree** (breakout says BUY, mean rev says SELL), that's a red flag
- Add disagreement penalty: if max_signal - min_signal > 1.5, reduce final score by 20-30%

## Conclusion
**Sector-level news would have likely prevented both losses.** The quantum computing selloff and AI chip tariff news were widely reported but invisible to our stock-specific news agent. Adding a sector filter is the highest-impact, lowest-effort improvement we can make.

Net impact if sector filter existed today: **saved $34.85** (both trades blocked), making the day **+$29.63** instead of **-$5.22**.

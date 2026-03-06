# 🏛️ Trading Floor Board
*Auto-updated by floor agents. Read-only for humans.*
*Last updated: 2026-03-06 08:01 by floor-manager*

---

## 📊 Positions
| Symbol | Side | Qty | Entry | Current | P&L % | OCO TP | OCO SL | Days Held |
|--------|------|-----|-------|---------|--------|--------|--------|-----------|
| CVX | LONG | 5 | $188.67 | $189.33 | +0.35% | $203.76 | $172.63 | ?d |
| NFLX | LONG | 10 | $98.06 | $99.07 | +1.03% | $105.90 | $89.72 | ?d |
| PLTR | LONG | 6 | $147.44 | $152.25 | +3.26% | $159.24 | $134.91 | ?d |

## 💰 Account
| Metric | Value |
|--------|-------|
| Equity | $26,042.27 |
| Cash | $23,191.41 |
| Buying Power | $49,233.68 |
| Slots | 3/3 |
| PDT Status | EXEMPT |

## ⚠️ Risk
| Factor | Level | Impact |
|--------|-------|--------|
| Macro | EXTREME (US-Iran) | 50% position sizing |
| VIX | — | — |
| Max DD Today | — | Kill switch at -5% equity |

## 🎯 Today's Plan
*Set by Morning Strategy briefing*
- Holding all 3 positions. No entry slots available.
- Trailing trigger at +4%. PLTR closest (~4.37% yesterday close).
- Watch CVX weakness (-1.84%).

## ⚙️ Active Config
| Parameter | Value |
|-----------|-------|
| Strategy | Swing only (intraday DISABLED) |
| Weights | mom=0.55 / mr=0.35 / brk=0.00 / news=0.10 |
| Threshold | 0.25 |
| Signal Alignment | 0.60 min |
| TP | 8% |
| SL | 8.5% |
| Max Hold | 10 days |
| Trail Trigger | 4% → 4% trail, Day 5+ → 2.5% trail |
| Max Slots | 3 |

## 📡 Squawk Box
*Latest first. Agents append here after every action.*

| Time | Agent | Event |
|------|-------|-------|
| 2026-03-06 08:01 | floor-manager | Config synced to best backtest: swing weights mom=0.55/mr=0.35/brk=0.00/news=0.10, threshold=0.25, TP=9%%, SL=8%% |
| 2026-03-05 20:00 | finance | Nightly review: Intraday/Swing no trades; PnL ; Weights no drift; Confidence INSUFFICIENT; Kill switch auto_apply=False |
| 2026-03-05 15:51 | trading-ops | PM scan: 0 signals, 0 trades entered |
| 2026-03-05 14:00 | trading-ops | Exit check: 0 actions. Swing exit scan completed with no triggered exits. |
| 2026-03-05 12:01 | trading-ops | Exit check: 0 actions. Swing exit scan completed with no triggered exits. |
| 2026-03-05 10:55 | trading-ops | Exit actions: PLTR trail tightened to $147.43 |
| 2026-03-05 10:45 | trading-ops | Exit actions: PLTR trail tightened to $147.48 |
| 2026-03-05 10:40 | trading-ops | Exit actions: PLTR trail tightened to $147.49 |
| 2026-03-05 10:35 | trading-ops | Exit actions: PLTR trail tightened to $147.42 |
| 2026-03-05 10:31 | trading-ops | Exit actions: PLTR trail tightened to $147.44 |
| 2026-03-05 10:20 | trading-ops | Exit actions: PLTR trail tightened to $148.21 |
| 2026-03-05 10:15 | trading-ops | Exit actions: PLTR trail tightened to $148.48 |
| 2026-03-05 10:10 | trading-ops | Exit actions: PLTR trail tightened to $148.90 |
| 2026-03-05 10:05 | trading-ops | Exit actions: PLTR trail tightened to $149.09 |
| 2026-03-05 10:01 | trading-ops | Exit check: 1 actions. PLTR hit trail; trail moved to stop at .77; exiting on next fill/market condition. |
| 2026-03-05 10:00 | trading-ops | Exit actions: PLTR trail tightened to $149.77 |
| 2026-03-05 09:55 | trading-ops | Exit actions: PLTR trail tightened to $149.69 |
| 2026-03-05 09:50 | trading-ops | Exit actions: PLTR trail tightened to $148.56 |
| 2026-03-05 09:47 | trading-ops | Exit actions: PLTR trail tightened to $148.35 |
| 2026-03-05 09:41 | trading-ops | AM scan: 0 signals, 0 trades entered |

## 📋 Standing Orders
- **DO NOT DEPLOY TO LIVE** — paper only until Snake approves
- **Alpaca is source of truth** — always sync FROM Alpaca
- **OCO on every entry** — broker-level TP+SL, no exceptions
- **No intraday** — disabled until further notice
- **Compound date**: May 3 (first cycle)

## 🔬 Research Queue
*Strategy/backtest picks up items here*
- [ ] Test lower trailing trigger (3% vs 4%) impact on win rate
- [ ] Evaluate adding defense stocks (LMT, NOC) to universe given macro
- [ ] Backtest time-decay trail (2.5% at day 5) vs fixed trail

## 🚨 Incidents
*Log any bugs, failures, unexpected behavior*
- 2026-03-04: Exit cron gpt-5-mini checked wrong DB files — fixed with explicit commands
- 2026-03-04: 5-min exit monitor was reading stale portfolio.json — rewritten to use live Alpaca

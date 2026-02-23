# Volatility Filter Enhancement
Date: 2026-02-23
Reporter: Main Agent

## Problem
System is trading ultra-volatile small-caps (ONDS: 22% daily ATR, CRML: 29%) on a $3,655 account. ATR-based stops become meaninglessly wide on these names, and trailing stops trigger too fast on normal volatility.

## Proposed Fix
Add a volatility filter in the **Risk Agent** (`src/trading_floor/agents/risk.py`) that rejects trades where:
1. `ATR(14) / price > 0.10` (daily volatility > 10% of price) — too volatile
2. Optionally: `ATR(14) / price < 0.005` (daily volatility < 0.5%) — too flat, not worth trading

This should be checked BEFORE position sizing, in the risk assessment step.

## Implementation
In `risk.py`, after fetching ATR data:
```python
atr_pct = atr_value / current_price
MAX_VOLATILITY = 0.10  # 10% daily — configurable in workflow.yaml
MIN_VOLATILITY = 0.005  # 0.5% daily

if atr_pct > MAX_VOLATILITY:
    reject trade — "Too volatile: {symbol} ATR={atr_pct:.1%}"
if atr_pct < MIN_VOLATILITY:
    reject trade — "Too flat: {symbol} ATR={atr_pct:.1%}"
```

## Config Addition (workflow.yaml, under risk:)
```yaml
max_atr_pct: 0.10    # reject if daily ATR > 10% of price
min_atr_pct: 0.005   # reject if daily ATR < 0.5% of price
```

## Impact
- Filters out penny stocks and ultra-volatile small-caps
- Keeps mid-volatility names (1-10% daily) which suit our account size
- With 55-stock universe, some names will be filtered — that's fine
- Won't affect exits (only entry filtering)

## Files to Modify
1. `src/trading_floor/agents/risk.py` — add volatility check
2. `configs/workflow.yaml` — add max/min_atr_pct config

from __future__ import annotations

from typing import Dict, List

import pandas as pd


class ScoutAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer

    def rank(self, market_data: Dict[str, pd.DataFrame]) -> List[Dict]:
        self.tracer.emit_span("scout.rank", {"symbols": list(market_data.keys())})
        ranked = []
        for sym, df in market_data.items():
            if df.empty or len(df) < 2:
                continue
            
            # Vectorized calculations are faster, but here we work on single series.
            # Avoid full pct_change series if we just need vol of the whole window.
            # Keeping pct_change() is fine as it's optimized in pandas.
            
            closes = df["close"]
            
            # Only calc vol if needed for ranking logic.
            # Optim: Use iloc for ends to avoid index lookup overhead
            start_price = closes.iloc[0]
            end_price = closes.iloc[-1]
            
            if start_price == 0:
                trend = 0.0
            else:
                trend = (end_price - start_price) / start_price

            returns = closes.pct_change().dropna()
            if returns.empty:
                vol = 0.0
            else:
                vol = returns.std() * (252 ** 0.5)
            
            ranked.append({"symbol": sym, "trend": float(trend), "vol": float(vol)})
            
        ranked.sort(key=lambda x: (x["trend"], -x["vol"]), reverse=True)
        return ranked

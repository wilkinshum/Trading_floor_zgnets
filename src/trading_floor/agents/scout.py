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
            if df.empty:
                continue
            closes = df["close"]
            returns = closes.pct_change().dropna()
            vol = returns.std() * (252 ** 0.5)
            trend = (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0]
            ranked.append({"symbol": sym, "trend": float(trend), "vol": float(vol)})
        ranked.sort(key=lambda x: (x["trend"], -x["vol"]), reverse=True)
        return ranked

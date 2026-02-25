from __future__ import annotations

import pandas as pd


class BreakoutSignalAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.lookback = cfg.get("signals", {}).get("breakout_lookback", 10)

    def score(self, df: pd.DataFrame) -> float:
        self.tracer.emit_span("signal.breakout", {"rows": len(df)})
        if df.empty or len(df) < self.lookback:
            return 0.0
        
        closes = df["close"]
        recent = closes.iloc[-self.lookback:]
        recent_high = recent.max()
        recent_low = recent.min()
        last = closes.iloc[-1]
        
        if recent_high == recent_low or recent_high == 0:
            return 0.0
        
        # Position within range: 0 = at low, 1 = at high
        position = (last - recent_low) / (recent_high - recent_low)

        # Smooth score across range, normalized to [-1, +1]
        # 0.0 at midpoint, +1.0 at range high, -1.0 at range low
        score = (position * 2.0) - 1.0
        return float(score)

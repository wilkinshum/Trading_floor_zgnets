from __future__ import annotations

import pandas as pd


class MomentumSignalAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.short = cfg.get("signals", {}).get("momentum_short", 10)

    def score(self, df: pd.DataFrame) -> float:
        self.tracer.emit_span("signal.momentum", {"rows": len(df)})
        if df.empty or len(df) < self.short:
            return 0.0
        
        # Optimize: Only calculate rolling mean for the last window needed
        # We need the last `short` elements to compute the rolling mean of the *last* point.
        # But rolling().mean() computes it for everyone.
        # Just taking the last `short` values and computing their mean is faster/simpler 
        # for a single point scalar return.
        
        closes = df["close"]
        last_closes = closes.iloc[-self.short:]
        sma = last_closes.mean()
        
        if sma == 0: return 0.0
        return float((closes.iloc[-1] - sma) / sma)

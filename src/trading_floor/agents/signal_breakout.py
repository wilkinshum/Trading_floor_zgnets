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
        recent_high = closes.tail(self.lookback).max()
        return float((closes.iloc[-1] - recent_high) / recent_high)

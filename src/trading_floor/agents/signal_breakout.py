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
        last = closes.iloc[-1]

        # Use lookback period EXCLUDING the current bar for the range.
        # This way a breakout (price beyond prior range) gives |score| > 0
        # and the signal isn't pinned to ±1.0 when the current bar IS the
        # high or low of the window.
        if len(closes) < self.lookback + 1:
            # Not enough history to exclude current bar; use all available
            prior = closes.iloc[-self.lookback:]
        else:
            prior = closes.iloc[-self.lookback - 1:-1]

        prior_high = prior.max()
        prior_low = prior.min()

        if prior_high == prior_low or prior_high == 0:
            return 0.0

        # Position relative to prior range: 0.5 = mid, >1 = breakout up, <0 = breakout down
        position = (last - prior_low) / (prior_high - prior_low)

        # Map to [-1, +1]: midpoint → 0, at prior high → +1, at prior low → -1
        # Allow overshoot for actual breakouts but clamp to [-1, 1]
        score = (position * 2.0) - 1.0
        score = max(-1.0, min(1.0, score))
        return float(score)

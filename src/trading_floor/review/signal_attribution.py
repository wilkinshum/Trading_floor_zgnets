"""Compute per-signal utility for MW updates."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, Optional

SIGNAL_NAMES = ["momentum", "meanrev", "breakout", "news"]


class SignalAttribution:
    """Raw PnL attribution for intraday, SPY-adjusted for swing."""

    def __init__(self, cfg: dict):
        sl = cfg["self_learning"]
        self.intraday_method = sl["intraday"]["attribution"]
        self.swing_method = sl["swing"]["attribution"]
        self._spy_cache: Dict[str, float] = {}

    def compute_utility(self, strategy: str, trade_data: dict) -> dict:
        """Compute per-signal utility dict.

        Args:
            strategy: 'intraday' or 'swing'.
            trade_data: dict with signal_scores, pnl, position_value,
                        holding_days, entry_time, exit_time, symbol.
        """
        scores = trade_data["signal_scores"]
        pnl = trade_data["pnl"]
        pos_val = trade_data["position_value"]
        holding_days = trade_data.get("holding_days", 1.0)

        if strategy == "intraday":
            result = {}
            for sig in SIGNAL_NAMES:
                s = scores.get(sig, 0.0)
                if pos_val == 0:
                    result[sig] = 0.0
                else:
                    result[sig] = s * math.copysign(1, pnl) * abs(pnl) / pos_val
            return result
        else:
            # Swing: SPY-adjusted
            entry_time = trade_data.get("entry_time")
            exit_time = trade_data.get("exit_time")
            entry_price = trade_data.get("entry_price", 0.0)

            spy_ret = 0.0
            if entry_time and exit_time:
                spy_ret = self._get_spy_return(entry_time, exit_time)

            stock_ret = pnl / pos_val if pos_val else 0.0
            excess_ret = stock_ret - spy_ret
            excess_pnl = excess_ret * pos_val

            denom = pos_val * math.sqrt(max(holding_days, 1.0)) if pos_val else 1.0
            result = {}
            for sig in SIGNAL_NAMES:
                s = scores.get(sig, 0.0)
                result[sig] = s * math.copysign(1, excess_pnl) * abs(excess_pnl) / denom
            return result

    def _get_spy_return(self, start: datetime, end: datetime) -> float:
        """Fetch SPY return over period. Returns 0.0 on failure.

        Caches results keyed by date range string.
        """
        cache_key = f"{start.date()}_{end.date()}"
        if cache_key in self._spy_cache:
            return self._spy_cache[cache_key]

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            import os

            client = StockHistoricalDataClient(
                os.environ.get("ALPACA_API_KEY"),
                os.environ.get("ALPACA_API_SECRET"),
            )
            req = StockBarsRequest(
                symbol_or_symbols="SPY",
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            bars = client.get_stock_bars(req)
            spy_bars = bars["SPY"]
            if len(spy_bars) >= 2:
                ret = (spy_bars[-1].close - spy_bars[0].open) / spy_bars[0].open
            elif len(spy_bars) == 1:
                ret = 0.0
            else:
                ret = 0.0
            self._spy_cache[cache_key] = ret
            return ret
        except Exception:
            self._spy_cache[cache_key] = 0.0
            return 0.0

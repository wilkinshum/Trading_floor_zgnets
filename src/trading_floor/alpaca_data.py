"""Alpaca-based data provider (IEX feed) — drop-in replacement for YahooDataProvider."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

logger = logging.getLogger(__name__)


class AlpacaDataProvider:
    """Fetch historical bars from Alpaca IEX free feed.

    Drop-in replacement for YahooDataProvider — same .fetch() signature,
    returns Dict[str, MarketData] with lowercase columns.
    """

    _cache: Dict[str, Dict] = {}
    CACHE_TTL = 60  # seconds

    # Map interval strings to Alpaca TimeFrame
    _TF_MAP = {
        "1m":  TimeFrame.Minute,
        "5m":  TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "1h":  TimeFrame.Hour,
        "1d":  TimeFrame.Day,
    }

    # Map lookback strings to timedelta
    _LOOKBACK_MAP = {
        "1d":  timedelta(days=1),
        "2d":  timedelta(days=2),
        "5d":  timedelta(days=7),   # 5 trading days ≈ 7 calendar days
        "10d": timedelta(days=14),
        "1mo": timedelta(days=30),
    }

    def __init__(self, interval: str = "5m", lookback: str = "5d",
                 api_key: str = None, api_secret: str = None):
        self.interval = interval
        self.lookback = lookback
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._api_secret = api_secret or os.environ.get("ALPACA_API_SECRET", "")
        self._client = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._api_secret,
        )

    def fetch(self, symbols: List[str]) -> Dict[str, "MarketData"]:
        from trading_floor.data import MarketData

        # Filter out index symbols (^VIX, ^GSPC) — Alpaca doesn't serve them
        tradeable = [s for s in symbols if not s.startswith("^")]
        skipped = [s for s in symbols if s.startswith("^")]
        if skipped:
            logger.debug("AlpacaDataProvider: skipping index symbols %s", skipped)

        if not tradeable:
            return {}

        cache_key = f"{','.join(sorted(tradeable))}|{self.interval}|{self.lookback}"
        cached = AlpacaDataProvider._cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < self.CACHE_TTL:
            return cached["data"]

        result = self._fetch_bars(tradeable)
        AlpacaDataProvider._cache[cache_key] = {"ts": time.time(), "data": result}
        return result

    def _fetch_bars(self, symbols: List[str]) -> Dict[str, "MarketData"]:
        from trading_floor.data import MarketData

        tf = self._TF_MAP.get(self.interval, TimeFrame(5, "Min"))
        delta = self._LOOKBACK_MAP.get(self.lookback, timedelta(days=7))
        end = datetime.now(timezone.utc)
        start = end - delta

        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=tf,
                start=start,
                end=end,
                feed=DataFeed.IEX,  # Free feed — no SIP entitlement needed
            )
            bars = self._client.get_stock_bars(req)
        except Exception as e:
            logger.error("AlpacaDataProvider: fetch failed: %s", e)
            return {}

        data: Dict[str, MarketData] = {}
        for sym in symbols:
            sym_bars = bars.get(sym, []) if hasattr(bars, 'get') else getattr(bars, 'data', {}).get(sym, [])
            if not sym_bars:
                continue

            rows = []
            for b in sym_bars:
                rows.append({
                    "datetime": b.timestamp,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                })

            if not rows:
                continue

            df = pd.DataFrame(rows)
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
            data[sym] = MarketData(symbol=sym, df=df)

        logger.info("AlpacaDataProvider: fetched %d/%d symbols (%s bars, %s lookback, IEX feed)",
                     len(data), len(symbols), self.interval, self.lookback)
        return data

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import time

import pandas as pd
import yfinance as yf


@dataclass
class MarketData:
    symbol: str
    df: pd.DataFrame


class YahooDataProvider:
    _cache: Dict[str, Dict] = {}  # {cache_key: {"ts": float, "data": Dict[str, MarketData]}}
    CACHE_TTL = 60  # seconds

    def __init__(self, interval: str = "5m", lookback: str = "5d"):
        self.interval = interval
        self.lookback = lookback

    def fetch(self, symbols: List[str]) -> Dict[str, MarketData]:
        cache_key = f"{','.join(sorted(symbols))}|{self.interval}|{self.lookback}"
        cached = YahooDataProvider._cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < self.CACHE_TTL:
            return cached["data"]

        result = self._fetch_from_yahoo(symbols)
        YahooDataProvider._cache[cache_key] = {"ts": time.time(), "data": result}
        return result

    def _fetch_from_yahoo(self, symbols: List[str]) -> Dict[str, MarketData]:
        if not symbols:
            return {}

        # Bulk download is much faster than sequential
        raw_data = yf.download(
            symbols,
            period=self.lookback,
            interval=self.interval,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )

        data: Dict[str, MarketData] = {}

        if len(symbols) == 1:
            sym = symbols[0]
            if raw_data is not None and not raw_data.empty:
                data[sym] = MarketData(symbol=sym, df=self._process_df(raw_data))
        else:
            # With group_by="ticker", top level columns are the symbols
            for sym in symbols:
                try:
                    df = raw_data[sym].copy()
                    # Skip if empty or all NaNs (failed download)
                    if df.empty or df.isna().all().all():
                        continue
                    data[sym] = MarketData(symbol=sym, df=self._process_df(df))
                except KeyError:
                    continue

        return data

    def _process_df(self, df: pd.DataFrame) -> pd.DataFrame:
        # yfinance can return MultiIndex columns
        if hasattr(df.columns, "levels"):
            df.columns = ["_".join([str(x) for x in col if x]) for col in df.columns]
        
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        
        # Normalize close column if yfinance adds suffixes like close_spy
        if "close" not in df.columns:
            for c in df.columns:
                if c.startswith("close"):
                    df["close"] = df[c]
                    break
        
        df = df.reset_index().rename(columns={"Datetime": "datetime", "Date": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df


def filter_trading_window(df: pd.DataFrame, tz: str, start: str, end: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(tz)
    df = df.set_index("datetime")
    return df.between_time(start_time=start, end_time=end).reset_index()


def latest_timestamp() -> str:
    return datetime.utcnow().isoformat()

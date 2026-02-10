from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import pandas as pd
import yfinance as yf


@dataclass
class MarketData:
    symbol: str
    df: pd.DataFrame


class YahooDataProvider:
    def __init__(self, interval: str = "5m", lookback: str = "5d"):
        self.interval = interval
        self.lookback = lookback

    def fetch(self, symbols: List[str]) -> Dict[str, MarketData]:
        data: Dict[str, MarketData] = {}
        for sym in symbols:
            df = yf.download(
                sym,
                period=self.lookback,
                interval=self.interval,
                progress=False,
                auto_adjust=True,
            )
            if df is None or df.empty:
                continue
            # yfinance can return MultiIndex columns
            if hasattr(df.columns, "levels"):
                df.columns = ["_".join([str(x) for x in col if x]) for col in df.columns]
            df = df.rename(columns={c: str(c).lower() for c in df.columns})
            df = df.reset_index().rename(columns={"Datetime": "datetime", "Date": "datetime"})
            df["datetime"] = pd.to_datetime(df["datetime"])
            data[sym] = MarketData(symbol=sym, df=df)
        return data


def filter_trading_window(df: pd.DataFrame, tz: str, start: str, end: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(tz)
    df = df.set_index("datetime")
    return df.between_time(start_time=start, end_time=end).reset_index()


def latest_timestamp() -> str:
    return datetime.utcnow().isoformat()

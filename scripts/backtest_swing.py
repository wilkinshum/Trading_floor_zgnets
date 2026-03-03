"""
Swing Trading Backtester v1 — Alpaca Daily Bars
================================================
Adapts the intraday v3.1 infrastructure for multi-day swing trades.

Key differences from intraday:
  - Daily bars (not 5m)
  - Hold period: 5-20 trading days (configurable grid)
  - TP targets: 5%, 8%, 10%, 15% (vs intraday 1.5-5%)
  - ATR on daily = wider stops, proper breathing room
  - Overnight gaps naturally modeled (daily OHLC)
  - News sentiment aggregated over trailing 3 days (not same-day)
  - Earnings calendar awareness (TODO: future enhancement)

Usage:
    python scripts/backtest_swing.py --days 365 --step 0.05
    python scripts/backtest_swing.py --quick   # 180 days, step=0.10
"""

from __future__ import annotations
import argparse
import os
import itertools
import json
import sys
import io
import time
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_floor.signal_normalizer import SignalNormalizer

# Persistent losers from v3.1 analysis
DEFAULT_EXCLUSIONS = {"RKLB", "ONDS", "HUT"}

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def load_config():
    cfg_path = Path(__file__).resolve().parent.parent / "configs" / "workflow.yaml"
    import yaml
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Data types ──────────────────────────────────

@dataclass
class DailyBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SwingSignal:
    date: str
    symbol: str
    momentum: float
    meanrev: float
    breakout: float
    news: float
    has_news: bool
    price_now: float
    atr: float
    avg_volume: float
    forward_bars: list[DailyBar]  # daily bars for hold period


@dataclass
class TradeOutcome:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    pnl_pct: float
    days_held: int
    date: str


@dataclass
class BacktestResult:
    weights: dict
    threshold: float
    tp_pct: float = 0.10
    max_hold_days: int = 10
    total_signals: int = 0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    stops_hit: int = 0
    tp_hit: int = 0
    trailing_hit: int = 0
    time_exits: int = 0
    time_exit_pnl: float = 0.0
    trade_pnls: list = field(default_factory=list)
    trade_pnl_pcts: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    starting_equity: float = 3651.0
    final_equity: float = 3651.0
    max_drawdown: float = 0.0

    @property
    def total_return(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.final_equity - self.starting_equity) / self.starting_equity

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.trades_taken, 1)

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / max(abs(self.gross_loss), 0.01)

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / max(self.trades_taken, 1)

    @property
    def avg_pnl_pct(self) -> float:
        if not self.trade_pnl_pcts:
            return 0.0
        return float(np.mean(self.trade_pnl_pcts))

    @property
    def sharpe(self) -> float:
        if len(self.trade_pnls) < 5:
            return 0.0
        arr = np.array(self.trade_pnls)
        std = np.std(arr)
        if std < 0.01:
            return 0.0
        return float(np.mean(arr) / std)

    @property
    def composite_score(self) -> float:
        wr = self.win_rate
        pf = min(self.profit_factor, 3.0) / 3.0
        ret = max(min(self.total_return, 1.0), -1.0)  # cap at ±100%
        ret_norm = (ret + 1.0) / 2.0
        tc = min(self.trades_taken, 200) / 200.0
        sh = max(min(self.sharpe, 2.0), -2.0) / 2.0
        sh = (sh + 1.0) / 2.0
        dd_penalty = max(0, 1.0 - self.max_drawdown * 2)  # penalize >50% drawdown heavily
        return wr * 0.20 + pf * 0.15 + ret_norm * 0.25 + tc * 0.10 + sh * 0.15 + dd_penalty * 0.15


# ── News Sentiment (reuse existing cache) ───────

def _news_cache_path(date_str: str) -> Path:
    d = CACHE_DIR / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date_str}.json"


class SwingNewsSentiment:
    """
    For swing trading, aggregate news over trailing N days (not just same-day).
    Reuses the existing per-day news cache from v3/v3.1.
    """

    POSITIVE_WORDS = {
        'surge', 'soar', 'jump', 'rally', 'gain', 'beat', 'exceed', 'upgrade',
        'bullish', 'record', 'high', 'strong', 'growth', 'profit', 'buy',
        'outperform', 'positive', 'boom', 'breakout', 'up', 'rise', 'rising',
        'better', 'above', 'momentum', 'confident', 'optimistic', 'milestone',
    }
    NEGATIVE_WORDS = {
        'drop', 'fall', 'crash', 'plunge', 'decline', 'miss', 'below', 'downgrade',
        'bearish', 'low', 'weak', 'loss', 'sell', 'underperform', 'negative',
        'risk', 'warning', 'fear', 'cut', 'layoff', 'down', 'falling', 'worse',
        'slump', 'tumble', 'concern', 'trouble', 'lawsuit', 'investigation',
    }

    def __init__(self, api_key: str, api_secret: str, lookback_days: int = 3):
        self.client = NewsClient(api_key, api_secret)
        self.day_cache: dict[str, dict[str, float]] = {}  # date -> {symbol: score}
        self._fetched_days: set = set()
        self.lookback_days = lookback_days

    def _score_headline(self, headline: str) -> float:
        words = set(re.findall(r'[a-z]+', headline.lower()))
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    def _fetch_day(self, date_str: str, symbols: list[str]):
        if date_str in self._fetched_days:
            return

        # Check disk cache first
        cache_path = _news_cache_path(date_str)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    self.day_cache[date_str] = json.load(f)
                self._fetched_days.add(date_str)
                return
            except Exception:
                pass

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        start = dt.replace(hour=0, minute=0, second=0)
        end = dt.replace(hour=23, minute=59, second=59)

        sym_scores: dict[str, list[float]] = defaultdict(list)
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            try:
                req = NewsRequest(symbols=",".join(batch), start=start, end=end, limit=50)
                result = self.client.get_news(req)
                articles = result.data.get("news", []) if hasattr(result, 'data') else []
                for article in articles:
                    score = self._score_headline(article.headline)
                    if article.symbols:
                        for s in article.symbols:
                            if s in batch:
                                sym_scores[s].append(score)
                time.sleep(0.15)
            except Exception:
                continue

        day_data = {}
        for sym in symbols:
            scores = sym_scores.get(sym, [])
            day_data[sym] = float(np.clip(np.mean(scores), -1.0, 1.0)) if scores else 0.0

        self.day_cache[date_str] = day_data
        self._fetched_days.add(date_str)

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(day_data, f)
        except Exception:
            pass

    def get_sentiment(self, symbol: str, date_str: str, symbols: list[str]) -> tuple[float, bool]:
        """
        Get aggregated sentiment over trailing lookback_days.
        Returns (score, has_news).
        """
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        scores = []
        for offset in range(self.lookback_days):
            d = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
            self._fetch_day(d, symbols)
            day_data = self.day_cache.get(d, {})
            val = day_data.get(symbol, 0.0)
            if abs(val) > 0.001:
                scores.append(val)

        if scores:
            return float(np.mean(scores)), True
        return 0.0, False


# ── Daily Bar Caching ───────────────────────────

def _daily_cache_path(sym: str, start: datetime, end: datetime) -> Path:
    d = CACHE_DIR / "daily_bars"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sym}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet"


def download_daily_bars(client, symbols, start, end):
    all_data = {}
    to_fetch = []
    for sym in symbols:
        cp = _daily_cache_path(sym, start, end)
        if cp.exists():
            try:
                df = pd.read_parquet(cp)
                if len(df) > 0:
                    all_data[sym] = df
                    continue
            except Exception:
                pass
        to_fetch.append(sym)

    if all_data:
        print(f"  Loaded {len(all_data)} symbols from daily cache")
    if not to_fetch:
        return all_data

    print(f"  Fetching {len(to_fetch)} symbols from Alpaca (daily bars)...")
    batch_size = 10
    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i+batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(1, TimeFrameUnit.Day),
                start=start, end=end,
            )
            bars = client.get_stock_bars(req)
            bar_data = bars.data if hasattr(bars, 'data') and isinstance(bars.data, dict) else {}
            for sym in batch:
                sym_bars = bar_data.get(sym, [])
                if not sym_bars:
                    continue
                records = [{"timestamp": b.timestamp, "open": float(b.open), "high": float(b.high),
                            "low": float(b.low), "close": float(b.close), "volume": float(b.volume)}
                           for b in sym_bars]
                df = pd.DataFrame(records)
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.set_index("timestamp").sort_index()
                all_data[sym] = df
                df.to_parquet(_daily_cache_path(sym, start, end))
            print(f"  Batch {i//batch_size+1}/{(len(to_fetch)-1)//batch_size+1} done ({len(all_data)} total)", end="\r")
            time.sleep(0.3)
        except Exception as e:
            print(f"\n  Warning: batch error: {e}")
            for sym in batch:
                try:
                    req = StockBarsRequest(symbol_or_symbols=[sym], timeframe=TimeFrame(1, TimeFrameUnit.Day),
                                           start=start, end=end)
                    bars = client.get_stock_bars(req)
                    bar_data = bars.data if hasattr(bars, 'data') else {}
                    sym_bars = bar_data.get(sym, [])
                    if sym_bars:
                        records = [{"timestamp": b.timestamp, "open": float(b.open), "high": float(b.high),
                                    "low": float(b.low), "close": float(b.close), "volume": float(b.volume)}
                                   for b in sym_bars]
                        df = pd.DataFrame(records)
                        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                        df = df.set_index("timestamp").sort_index()
                        all_data[sym] = df
                        df.to_parquet(_daily_cache_path(sym, start, end))
                    time.sleep(0.2)
                except Exception:
                    continue
    return all_data


# ── Daily ATR ───────────────────────────────────

def compute_daily_atr(df, period=14):
    if len(df) < period + 1:
        return 0.0
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    return float(np.mean(tr[-period:]))


# ── Signal Generation (Daily) ──────────────────

def compute_momentum(df, short=10, long=20):
    """Daily momentum: short MA vs long MA + ROC."""
    if len(df) < long + 1:
        return 0.0
    close = df["close"].values
    sma_short = np.mean(close[-short:])
    sma_long = np.mean(close[-long:])

    # MA crossover component
    ma_signal = (sma_short - sma_long) / sma_long

    # Rate of change (10-day)
    roc = (close[-1] - close[-short]) / close[-short] if close[-short] != 0 else 0

    # RSI component
    deltas = np.diff(close[-(short+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains) if len(gains) > 0 else 0.0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0.0
    # avoid divide-by-zero (flat or monotonic up periods)
    if avg_loss < 1e-9:
        rs = 999.0
    else:
        rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi_signal = (rsi - 50) / 50  # normalize to -1 to 1

    # Combine
    score = ma_signal * 5 + roc * 3 + rsi_signal * 0.3
    return float(np.clip(score, -1.0, 1.0))


def compute_meanrev(df, period=20):
    """Daily mean reversion: Bollinger Band position + z-score."""
    if len(df) < period + 1:
        return 0.0
    close = df["close"].values
    sma = np.mean(close[-period:])
    std = np.std(close[-period:])
    if std < 0.001:
        return 0.0

    z_score = (close[-1] - sma) / std
    # Inverted: oversold (z < -2) = positive signal, overbought (z > 2) = negative
    return float(np.clip(-z_score / 3.0, -1.0, 1.0))


def compute_breakout(df, lookback=50):
    """Daily breakout: proximity to N-day high/low with volume confirmation."""
    if len(df) < lookback:
        return 0.0
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values

    period_high = np.max(high[-lookback:])
    period_low = np.min(low[-lookback:])
    price_range = period_high - period_low
    if price_range < 0.01:
        return 0.0

    # Position in range (0 = at low, 1 = at high)
    position = (close[-1] - period_low) / price_range

    # Volume surge (current vs 20-day avg)
    avg_vol = np.mean(volume[-20:]) if len(volume) >= 20 else np.mean(volume)
    vol_ratio = volume[-1] / avg_vol if avg_vol > 0 else 1.0

    # Breakout signal: near highs + volume surge = bullish breakout
    if position > 0.8 and vol_ratio > 1.3:
        return float(np.clip((position - 0.8) * 5 * min(vol_ratio, 3.0) / 3.0, 0, 1.0))
    elif position < 0.2 and vol_ratio > 1.3:
        return float(np.clip((0.2 - position) * -5 * min(vol_ratio, 3.0) / 3.0, -1.0, 0))
    else:
        return float(np.clip((position - 0.5) * 0.5, -0.3, 0.3))


def generate_swing_signals(cfg, universe, lookback_days=365, exclusions=None,
                           max_hold_days=20, min_price=5.0):
    alpaca_cfg = cfg.get("alpaca", {})
    api_key = os.environ.get("ALPACA_API_KEY") or alpaca_cfg.get("api_key")
    api_secret = os.environ.get("ALPACA_API_SECRET") or alpaca_cfg.get("api_secret")
    if not api_key or not api_secret or "${" in str(api_key):
        raise RuntimeError("Missing Alpaca credentials.")

    if exclusions:
        universe = [s for s in universe if s not in exclusions]
        print(f"  Excluded {len(exclusions)} symbols: {', '.join(sorted(exclusions))}")

    bar_client = StockHistoricalDataClient(api_key, api_secret)
    news = SwingNewsSentiment(api_key, api_secret, lookback_days=3)
    normalizer = SignalNormalizer(lookback=100)

    min_volume = cfg.get("min_avg_volume", 100000)
    atr_period = 14

    end_dt = datetime.now(timezone.utc) - timedelta(hours=1)
    start_dt = end_dt - timedelta(days=lookback_days + 60)  # extra for warmup

    print(f"\n-- Downloading {lookback_days}+ days of daily bars for {len(universe)} symbols...")
    print(f"   Period: {start_dt.date()} to {end_dt.date()}")
    print(f"   Max hold: {max_hold_days} trading days")

    all_bars = download_daily_bars(bar_client, universe, start_dt, end_dt)
    print(f"\n   Got data for {len(all_bars)}/{len(universe)} symbols")

    events = []
    vol_filtered = 0
    price_filtered = 0
    failed = []

    # We need at least 50 bars for signals + max_hold_days forward
    min_bars_needed = 50 + max_hold_days

    for sym_idx, sym in enumerate(universe):
        df = all_bars.get(sym)
        if df is None or len(df) < min_bars_needed:
            failed.append(sym)
            continue

        # Only use bars within our lookback period for signal generation
        cutoff = pd.Timestamp(end_dt - timedelta(days=lookback_days))
        dates = df.index.tolist()

        for i in range(50, len(df) - max_hold_days):
            signal_df = df.iloc[:i+1]  # all data up to this point
            current_date = dates[i]
            date_str = str(current_date.date()) if hasattr(current_date, 'date') else str(current_date)[:10]

            # Only generate signals within our test period
            if signal_df.index[-1] < cutoff:
                continue

            current_price = signal_df["close"].iloc[-1]
            if current_price < min_price:
                price_filtered += 1
                continue

            avg_vol = signal_df["volume"].rolling(20).mean().iloc[-1] if len(signal_df) >= 20 else signal_df["volume"].mean()
            if avg_vol < min_volume:
                vol_filtered += 1
                continue

            atr = compute_daily_atr(signal_df, atr_period)
            if atr <= 0:
                continue

            # Forward bars (daily, up to max_hold_days)
            forward_bars = []
            for j in range(i + 1, min(i + 1 + max_hold_days, len(df))):
                row = df.iloc[j]
                fdate = dates[j]
                forward_bars.append(DailyBar(
                    date=str(fdate.date()) if hasattr(fdate, 'date') else str(fdate)[:10],
                    open=row["open"], high=row["high"],
                    low=row["low"], close=row["close"],
                    volume=row["volume"],
                ))
            if len(forward_bars) < 3:
                continue

            # Compute signals on daily data
            mom = normalizer.normalize(sym, "momentum", compute_momentum(signal_df))
            mean = normalizer.normalize(sym, "meanrev", compute_meanrev(signal_df))
            brk = normalizer.normalize(sym, "breakout", compute_breakout(signal_df))

            # News: trailing 3-day aggregate
            news_score, has_news = news.get_sentiment(sym, date_str, universe)

            events.append(SwingSignal(
                date=date_str, symbol=sym,
                momentum=mom, meanrev=mean, breakout=brk, news=news_score,
                has_news=has_news,
                price_now=current_price, atr=atr, avg_volume=avg_vol,
                forward_bars=forward_bars,
            ))

        sym_count = len([e for e in events if e.symbol == sym])
        print(f"  [{sym_idx+1}/{len(universe)}] {sym}: {sym_count} signals", end="\r")

    news_coverage = sum(1 for e in events if e.has_news)
    print(f"\n\nSwing Signal Generation Complete:")
    print(f"  Total events: {len(events)}")
    print(f"  Filtered: {vol_filtered} low-volume, {price_filtered} under-$5")
    print(f"  News coverage: {news_coverage}/{len(events)} ({news_coverage/max(len(events),1)*100:.1f}%)")
    print(f"  Max hold: {max_hold_days} days")
    if exclusions:
        print(f"  Excluded: {', '.join(sorted(exclusions))}")
    if failed:
        print(f"  Failed/empty: {', '.join(failed[:10])}")
    return events


# ── Challenge System ────────────────────────────

def passes_challenge(event, weights, threshold=0.9):
    active = []
    if weights.get("momentum", 0) > 0: active.append(event.momentum)
    if weights.get("meanrev", 0) > 0: active.append(event.meanrev)
    if weights.get("breakout", 0) > 0: active.append(event.breakout)
    if weights.get("news", 0) > 0 and event.has_news: active.append(event.news)
    if len(active) < 2:
        return True
    return (max(active) - min(active)) <= threshold


# ── Swing Trade Simulator ───────────────────────

def simulate_swing_trade(event, side, position_size, atr_stop_mult=2.0,
                         tp_pct=0.10, slippage_bps=10.0, commission=0.005):
    """
    Simulate a swing trade using daily bars.
    Entry at next day's open (realistic — signal generated at close, execute next morning).
    """
    # Entry at first forward bar's open (next day open)
    if not event.forward_bars:
        return None

    entry_price = event.forward_bars[0].open
    slip = entry_price * slippage_bps / 10000.0
    entry_price = entry_price + slip if side == "BUY" else entry_price - slip

    atr = event.atr
    stop_distance = atr * atr_stop_mult

    # Trailing stop params (wider for swing)
    be_trigger = 0.03       # breakeven at +3%
    trail_trigger = 0.05    # start trailing at +5%
    trail_pct = 0.025       # trail 2.5% below HWM
    wide_trigger = 0.08     # at +8%, wider trail
    wide_pct = 0.04         # 4% wide trail

    if side == "BUY":
        stop_price = entry_price - stop_distance
        tp_price = entry_price * (1.0 + tp_pct)
    else:
        stop_price = entry_price + stop_distance
        tp_price = entry_price * (1.0 - tp_pct)

    hwm, lwm = entry_price, entry_price
    breakeven_moved = False
    trailing_active = False
    exit_price = None
    exit_reason = "time_exit"
    days_held = len(event.forward_bars)

    # Start from bar index 1 (bar 0 is entry day)
    for i, bar in enumerate(event.forward_bars[1:], 1):
        if side == "BUY":
            hwm = max(hwm, bar.high)
            gain_pct = (hwm - entry_price) / entry_price

            # Check stop (intraday low hits stop)
            if bar.low <= stop_price:
                exit_price = max(stop_price, bar.open * 0.95)  # gap protection
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                days_held = i
                break

            # Check TP (intraday high hits target)
            if bar.high >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                days_held = i
                break

            # Trailing stop logic
            if gain_pct >= wide_trigger:
                trailing_active = True
                stop_price = max(stop_price, hwm * (1.0 - wide_pct))
            elif gain_pct >= trail_trigger:
                trailing_active = True
                stop_price = max(stop_price, hwm * (1.0 - trail_pct))
            elif gain_pct >= be_trigger and not breakeven_moved:
                stop_price = max(stop_price, entry_price)
                breakeven_moved = True
        else:
            lwm = min(lwm, bar.low)
            gain_pct = (entry_price - lwm) / entry_price

            if bar.high >= stop_price:
                exit_price = min(stop_price, bar.open * 1.05)
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                days_held = i
                break

            if bar.low <= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                days_held = i
                break

            if gain_pct >= wide_trigger:
                trailing_active = True
                stop_price = min(stop_price, lwm * (1.0 + wide_pct))
            elif gain_pct >= trail_trigger:
                trailing_active = True
                stop_price = min(stop_price, lwm * (1.0 + trail_pct))
            elif gain_pct >= be_trigger and not breakeven_moved:
                stop_price = min(stop_price, entry_price)
                breakeven_moved = True

    if exit_price is None:
        exit_price = event.forward_bars[-1].close if event.forward_bars else entry_price

    # Exit slippage
    slip_exit = exit_price * slippage_bps / 10000.0
    exit_price = exit_price - slip_exit if side == "BUY" else exit_price + slip_exit

    shares = position_size / entry_price
    raw_pnl = (exit_price - entry_price) * shares if side == "BUY" else (entry_price - exit_price) * shares
    pnl = raw_pnl - (commission * 2)
    pnl_pct = pnl / position_size

    return TradeOutcome(
        symbol=event.symbol, side=side,
        entry_price=round(entry_price, 4), exit_price=round(exit_price, 4),
        exit_reason=exit_reason, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
        days_held=days_held, date=event.date
    )


# ── Backtest Engine ─────────────────────────────

def run_backtest(events, weights, threshold, tp_pct=0.10, max_hold_days=10,
                 atr_stop_mult=2.0, slippage_bps=10.0, commission=0.005,
                 starting_equity=3651.0, max_positions=4, challenge_threshold=0.9,
                 detailed=False):
    """Compounding mode: position size = current equity / max_positions."""

    result = BacktestResult(weights=weights, threshold=threshold,
                            tp_pct=tp_pct, max_hold_days=max_hold_days)
    result.total_signals = len(events)

    mom_w = weights["momentum"]
    mean_w = weights["meanrev"]
    brk_w = weights["breakout"]
    news_w = weights["news"]

    # Compounding equity tracker
    equity = starting_equity
    peak_equity = starting_equity
    max_drawdown = 0.0

    # Track open positions (can't overlap same symbol)
    open_positions: dict[str, str] = {}  # symbol -> exit_date
    positions_by_date = defaultdict(int)

    for idx, ev in enumerate(events):
        # Skip if equity blown (< 10% of start)
        if equity < starting_equity * 0.10:
            break

        # Dynamic news weighting
        if ev.has_news or news_w == 0:
            eff_mom_w, eff_mean_w, eff_brk_w, eff_news_w = mom_w, mean_w, brk_w, news_w
        else:
            other_sum = mom_w + mean_w + brk_w
            if other_sum > 0:
                scale = (other_sum + news_w) / other_sum
                eff_mom_w = mom_w * scale
                eff_mean_w = mean_w * scale
                eff_brk_w = brk_w * scale
            else:
                eff_mom_w, eff_mean_w, eff_brk_w = mom_w, mean_w, brk_w
            eff_news_w = 0.0

        active_weight = eff_mom_w + eff_mean_w + eff_brk_w + eff_news_w
        if active_weight < 0.01:
            continue

        raw = (ev.momentum * eff_mom_w + ev.meanrev * eff_mean_w +
               ev.breakout * eff_brk_w + ev.news * eff_news_w)
        score = raw / active_weight

        if abs(score) < threshold:
            continue

        side = "BUY" if score > 0 else "SELL"

        # Check if already holding this symbol
        if ev.symbol in open_positions and ev.date <= open_positions[ev.symbol]:
            continue

        # Max concurrent positions
        if positions_by_date[ev.date] >= max_positions:
            continue

        if not passes_challenge(ev, weights, challenge_threshold):
            continue

        # Compounding: position size = equity / max_positions
        position_size = equity / max(max_positions, 1)
        if position_size < 50:  # minimum viable trade
            continue

        outcome = simulate_swing_trade(ev, side, position_size,
                                       atr_stop_mult=atr_stop_mult,
                                       tp_pct=tp_pct,
                                       slippage_bps=slippage_bps,
                                       commission=commission)
        if outcome is None:
            continue

        # Update equity with trade PnL (compounding)
        equity += outcome.pnl
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)

        result.trades_taken += 1
        result.trade_pnls.append(outcome.pnl)
        result.trade_pnl_pcts.append(outcome.pnl_pct)
        positions_by_date[ev.date] += 1

        # Track position holding period
        if ev.forward_bars and len(ev.forward_bars) > outcome.days_held:
            exit_date = ev.forward_bars[outcome.days_held - 1].date if outcome.days_held > 0 else ev.date
        else:
            exit_date = ev.forward_bars[-1].date if ev.forward_bars else ev.date
        open_positions[ev.symbol] = exit_date

        if outcome.pnl > 0:
            result.wins += 1
            result.gross_profit += outcome.pnl
        else:
            result.losses += 1
            result.gross_loss += outcome.pnl
        result.total_pnl += outcome.pnl

        if outcome.exit_reason == "stop_loss": result.stops_hit += 1
        elif outcome.exit_reason == "take_profit": result.tp_hit += 1
        elif outcome.exit_reason == "trailing_stop": result.trailing_hit += 1
        else:
            result.time_exits += 1
            result.time_exit_pnl += outcome.pnl

        if detailed:
            result.trades.append(outcome)

    # Finalize compounding stats
    result.starting_equity = starting_equity
    result.final_equity = round(equity, 2)
    result.max_drawdown = round(max_drawdown, 4)

    return result


# ── Grid Search ─────────────────────────────────

def generate_weight_combos(step=0.05):
    combos = []
    vals = np.arange(0.0, 1.01, step)
    for mom in vals:
        for brk in vals:
            for news in vals:
                for mean in vals:
                    total = mom + brk + news + mean
                    if total < 0.01 or total > 1.001:
                        continue
                    reserve = round(1.0 - total, 2)
                    if reserve < -0.01:
                        continue
                    combos.append({
                        "momentum": round(mom, 2),
                        "meanrev": round(mean, 2),
                        "breakout": round(brk, 2),
                        "news": round(news, 2),
                        "reserve": round(max(reserve, 0), 2),
                    })
    return combos


def grid_search(events, step=0.05, thresholds=None, tp_pcts=None,
                hold_days_list=None, atr_stop_mult=2.0,
                slippage_bps=10.0, commission=0.005, max_positions=4,
                challenge_threshold=0.9, min_trades=20, starting_equity=3651.0):

    combos = generate_weight_combos(step)
    if thresholds is None:
        thresholds = [0.10, 0.15, 0.20, 0.25, 0.30]
    if tp_pcts is None:
        tp_pcts = [0.05, 0.08, 0.10, 0.15]
    if hold_days_list is None:
        hold_days_list = [10]  # hold days baked into signal generation

    total = len(combos) * len(thresholds) * len(tp_pcts)
    print(f"\n  Grid: {len(combos)} combos x {len(thresholds)} thresholds x {len(tp_pcts)} TPs = {total} backtests")

    results = []
    count = 0
    for combo in combos:
        for thresh in thresholds:
            for tp in tp_pcts:
                r = run_backtest(events, combo, thresh, tp_pct=tp,
                                 atr_stop_mult=atr_stop_mult,
                                 slippage_bps=slippage_bps, commission=commission,
                                 starting_equity=starting_equity,
                                 max_positions=max_positions,
                                 challenge_threshold=challenge_threshold)
                if r.trades_taken >= min_trades:
                    results.append(r)
                count += 1
                if count % 50000 == 0:
                    print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)", end="\r")

    print(f"\n  {len(results)} valid results (>={min_trades} trades)")
    return results


# ── Walk-Forward ────────────────────────────────

def walk_forward(events, step=0.05, train_pct=0.6,
                 atr_stop_mult=2.0, slippage_bps=10.0, commission=0.005,
                 max_positions=4, challenge_threshold=0.9, min_test_trades=15,
                 starting_equity=3651.0):

    dates = sorted(set(e.date for e in events))
    split = int(len(dates) * train_pct)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])

    train_events = [e for e in events if e.date in train_dates]
    test_events = [e for e in events if e.date in test_dates]

    print(f"\n  Walk-forward split:")
    print(f"    Train: {dates[0]} to {dates[split-1]} ({len(train_events)} events, {split} days)")
    print(f"    Test:  {dates[split]} to {dates[-1]} ({len(test_events)} events, {len(dates)-split} days)")
    print(f"    Starting equity: ${starting_equity:,.2f} (compounding mode)")

    print("\n  -- TRAIN SET --")
    train_results = grid_search(train_events, step, atr_stop_mult=atr_stop_mult,
                                slippage_bps=slippage_bps, commission=commission,
                                max_positions=max_positions,
                                challenge_threshold=challenge_threshold, min_trades=20,
                                starting_equity=starting_equity)
    train_results.sort(key=lambda r: r.composite_score, reverse=True)

    print("\n  -- VALIDATING TOP 50 ON TEST SET --")
    validated = []

    for tr in train_results[:50]:
        test_r = run_backtest(test_events, tr.weights, tr.threshold,
                              tp_pct=tr.tp_pct, atr_stop_mult=atr_stop_mult,
                              slippage_bps=slippage_bps, commission=commission,
                              starting_equity=starting_equity,
                              max_positions=max_positions,
                              challenge_threshold=challenge_threshold, detailed=True)

        if test_r.trades_taken < min_test_trades:
            continue

        sym_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "avg_days": 0, "count": 0})
        for t in test_r.trades:
            s = sym_stats[t.symbol]
            if t.pnl > 0: s["wins"] += 1
            else: s["losses"] += 1
            s["pnl"] += t.pnl
            s["avg_days"] += t.days_held
            s["count"] += 1

        for s in sym_stats.values():
            if s["count"] > 0:
                s["avg_days"] = round(s["avg_days"] / s["count"], 1)

        exit_bd = {
            "stop_loss": test_r.stops_hit, "take_profit": test_r.tp_hit,
            "trailing_stop": test_r.trailing_hit, "time_exit": test_r.time_exits,
        }

        avg_hold = np.mean([t.days_held for t in test_r.trades]) if test_r.trades else 0

        gap = abs(tr.win_rate - test_r.win_rate)
        consistency = max(0, 1.0 - gap * 2)

        validated.append({
            "weights": tr.weights,
            "threshold": tr.threshold,
            "tp_pct": tr.tp_pct,
            "train": {
                "trades": tr.trades_taken, "win_rate": round(tr.win_rate, 3),
                "pf": round(tr.profit_factor, 2), "pnl": round(tr.total_pnl, 2),
                "composite": round(tr.composite_score, 3), "sharpe": round(tr.sharpe, 3),
                "avg_pnl_pct": round(tr.avg_pnl_pct * 100, 2),
            },
            "test": {
                "trades": test_r.trades_taken, "win_rate": round(test_r.win_rate, 3),
                "pf": round(test_r.profit_factor, 2), "pnl": round(test_r.total_pnl, 2),
                "composite": round(test_r.composite_score, 3), "sharpe": round(test_r.sharpe, 3),
                "avg_pnl_pct": round(test_r.avg_pnl_pct * 100, 2),
                "avg_hold_days": round(avg_hold, 1),
                "exit_breakdown": exit_bd,
                "time_exit_pnl": round(test_r.time_exit_pnl, 2),
                "final_equity": test_r.final_equity,
                "total_return": round(test_r.total_return * 100, 2),
                "max_drawdown": round(test_r.max_drawdown * 100, 2),
            },
            "train_test_gap": round(gap, 3),
            "consistency": round(consistency, 3),
            "top_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)[:5]),
            "worst_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])[:5]),
        })

    validated.sort(
        key=lambda v: v["test"]["composite"] * (0.7 + 0.3 * v["consistency"]),
        reverse=True
    )
    return validated


# ── Main ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Swing Trading Backtester v1 (Alpaca Daily)")
    parser.add_argument("--days", type=int, default=365, help="Days of data")
    parser.add_argument("--step", type=float, default=0.05, help="Weight grid step")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument("--min-test-trades", type=int, default=15)
    parser.add_argument("--no-exclusions", action="store_true")
    parser.add_argument("--max-hold", type=int, default=10, help="Max hold days")
    parser.add_argument("--atr-mult", type=float, default=2.0, help="ATR stop multiplier")
    parser.add_argument("--equity", type=float, default=3651.0, help="Starting equity (compounding)")
    args = parser.parse_args()

    if args.quick:
        args.days = 180
        args.step = 0.10
        args.min_test_trades = min(args.min_test_trades, 10)

    cfg = load_config()
    universe = cfg["universe"]
    slippage_bps = 10.0  # wider for swing (overnight gaps)
    commission = 0.005
    max_positions = 4
    challenge_threshold = cfg.get("challenges", {}).get("disagreement_threshold", 0.9)
    exclusions = set() if args.no_exclusions else DEFAULT_EXCLUSIONS

    print("=" * 70)
    print("  SWING TRADING BACKTESTER v1 -- ALPACA DAILY BARS")
    print("=" * 70)
    print(f"  Universe: {len(universe)} symbols ({len(universe)-len(exclusions)} after exclusions)")
    print(f"  Period: {args.days} days (daily bars)")
    print(f"  Max hold: {args.max_hold} trading days")
    print(f"  Grid step: {args.step}")
    print(f"  TP grid: [5%, 8%, 10%, 15%]")
    print(f"  Stop: ATR x {args.atr_mult} (daily)")
    print(f"  Slippage: {slippage_bps}bps | Commission: ${commission}")
    print(f"  Trailing: BE@3%, trail@5%(2.5%), wide@8%(4%)")
    print(f"  Max positions: {max_positions}")
    print(f"  Entry: next-day open (realistic)")
    print(f"  News: 3-day trailing aggregate")
    print(f"  Mode: COMPOUNDING (equity reinvested)")
    print(f"  Starting equity: ${args.equity:,.2f}")

    start_time = time.time()

    events = generate_swing_signals(cfg, universe, args.days,
                                    exclusions=exclusions,
                                    max_hold_days=args.max_hold)
    if len(events) < 50:
        print(f"\nNot enough events ({len(events)}). Need >= 50.")
        return

    validated = walk_forward(events, args.step, slippage_bps=slippage_bps,
                             commission=commission, max_positions=max_positions,
                             challenge_threshold=challenge_threshold,
                             min_test_trades=args.min_test_trades,
                             starting_equity=args.equity)

    elapsed = time.time() - start_time

    # Print results
    print("\n" + "=" * 70)
    print("  TOP 10 SWING TRADING COMBOS")
    print("=" * 70)

    for i, v in enumerate(validated[:10]):
        w = v["weights"]
        t = v["test"]
        tr = v["train"]
        print(f"\n  #{i+1} (consistency={v['consistency']:.2f})")
        print(f"    Weights: mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']}")
        print(f"    Threshold: {v['threshold']} | TP: {v['tp_pct']:.0%}")
        print(f"    Train: {tr['trades']}t WR={tr['win_rate']:.1%} PF={tr['pf']} PnL=${tr['pnl']:.2f} avg={tr['avg_pnl_pct']:.2f}%/trade")
        print(f"    Test:  {t['trades']}t WR={t['win_rate']:.1%} PF={t['pf']} PnL=${t['pnl']:.2f} avg={t['avg_pnl_pct']:.2f}%/trade")
        print(f"    Equity: ${t.get('final_equity',0):,.2f} | Return: {t.get('total_return',0):.1f}% | Max DD: {t.get('max_drawdown',0):.1f}%")
        print(f"    Gap: {v['train_test_gap']:.1%} | Avg hold: {t['avg_hold_days']:.1f} days")
        exits = t.get('exit_breakdown', {})
        print(f"    Exits: stop={exits.get('stop_loss',0)} tp={exits.get('take_profit',0)} trail={exits.get('trailing_stop',0)} time={exits.get('time_exit',0)} (time PnL: ${t.get('time_exit_pnl',0):.2f})")
        if v.get("top_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["top_symbols"].items())[:3]]
            print(f"    Best: {', '.join(parts)}")
        if v.get("worst_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["worst_symbols"].items())[:3]]
            print(f"    Worst: {', '.join(parts)}")

    print(f"\n  Completed in {elapsed:.1f}s")

    # Save results
    output_path = args.output or str(Path(__file__).resolve().parent.parent / "swing_backtest_results.json")
    output = {
        "version": "swing-v1",
        "timestamp": pd.Timestamp.now().isoformat(),
        "config": {
            "days": args.days, "step": args.step,
            "universe_size": len(universe),
            "effective_universe": len(universe) - len(exclusions),
            "signal_events": len(events),
            "excluded_symbols": sorted(exclusions),
            "max_hold_days": args.max_hold,
            "atr_stop_mult": args.atr_mult,
            "tp_grid": [0.05, 0.08, 0.10, 0.15],
            "slippage_bps": slippage_bps,
            "commission": commission,
            "trailing": {"be": 0.03, "trail": 0.05, "trail_pct": 0.025, "wide": 0.08, "wide_pct": 0.04},
            "news_lookback_days": 3,
            "entry": "next-day open",
            "data_source": "Alpaca daily bars + Benzinga news (3-day trailing)",
            "compounding": True,
            "starting_equity": args.equity,
        },
        "top_10": validated[:10],
        "all_validated": validated,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"  Results saved to {output_path}")


if __name__ == "__main__":
    main()

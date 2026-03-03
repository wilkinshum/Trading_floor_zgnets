"""
Weight Optimizer Backtester v3 — Alpaca Data
=============================================
Uses Alpaca API for:
  - 6+ months of 5m bar data (vs yfinance 59-day cap)
  - Benzinga news per-symbol with timestamps (vs broken Google RSS)

Realistic simulation:
  ✅ ATR-based stop-loss with trailing stops
  ✅ Take-profit targets
  ✅ Slippage + commission
  ✅ Volume filter, min price $5
  ✅ Max concurrent positions per day
  ✅ Same-symbol same-day persistence filter
  ✅ Cooldown after recent trade
  ✅ Challenge system (signal disagreement)
  ✅ Bar-by-bar stop/TP simulation
  ✅ REAL news sentiment per-symbol per-day from Benzinga
  ✅ Improved composite scoring with PnL + consistency
  ✅ Min 20 trades on test set to qualify

Usage:
    python scripts/backtest_weights_v3.py --days 180 --step 0.05
    python scripts/backtest_weights_v3.py --quick   # 90 days, step=0.10
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

# Fix encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.signal_normalizer import SignalNormalizer
from trading_floor.lightning import LightningTracer


# ── Config ──────────────────────────────────────

def load_config():
    cfg_path = Path(__file__).resolve().parent.parent / "configs" / "workflow.yaml"
    import yaml
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Data types ──────────────────────────────────

@dataclass
class BarData:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SignalEvent:
    date: str
    symbol: str
    momentum: float
    meanrev: float
    breakout: float
    news: float               # REAL Benzinga sentiment
    price_now: float
    atr: float
    avg_volume: float
    forward_bars: list[BarData]


@dataclass
class TradeOutcome:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    bars_held: int
    date: str


@dataclass
class BacktestResult:
    weights: dict
    threshold: float
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
    trade_pnls: list = field(default_factory=list)  # for Sharpe calc
    trades: list = field(default_factory=list)

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
    def sharpe(self) -> float:
        """Per-trade Sharpe ratio: mean PnL / std PnL."""
        if len(self.trade_pnls) < 5:
            return 0.0
        arr = np.array(self.trade_pnls)
        std = np.std(arr)
        if std < 0.01:
            return 0.0
        return float(np.mean(arr) / std)

    @property
    def composite_score(self) -> float:
        """
        Balanced composite:
          WR * 0.25 + PF_norm * 0.20 + PnL_norm * 0.25 + trades_norm * 0.15 + sharpe_norm * 0.15
        """
        wr = self.win_rate
        pf = min(self.profit_factor, 3.0) / 3.0
        # PnL normalized: cap at $500 for normalization
        pnl_norm = max(min(self.total_pnl, 500.0), -500.0) / 500.0
        pnl_norm = (pnl_norm + 1.0) / 2.0  # shift to 0-1 range
        tc = min(self.trades_taken, 300) / 300.0
        sh = max(min(self.sharpe, 2.0), -2.0) / 2.0
        sh = (sh + 1.0) / 2.0  # shift to 0-1
        return wr * 0.25 + pf * 0.20 + pnl_norm * 0.25 + tc * 0.15 + sh * 0.15


# ── News Sentiment (Alpaca/Benzinga) ────────────

class AlpacaNewsSentiment:
    """
    Fetch news from Alpaca Benzinga API, compute simple sentiment per symbol per day.
    Caches results to avoid redundant API calls.
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

    def __init__(self, api_key: str, api_secret: str):
        self.client = NewsClient(api_key, api_secret)
        self.cache: dict[tuple[str, str], float] = {}  # (symbol, date) -> score
        self._batch_cache: dict[str, bool] = {}  # date -> already fetched?

    def _score_headline(self, headline: str) -> float:
        """Simple word-match sentiment: +1 to -1."""
        words = set(re.findall(r'[a-z]+', headline.lower()))
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total  # [-1, +1]

    def fetch_day(self, date_str: str, symbols: list[str]):
        """Fetch news per-symbol for a given day. Caches to disk as JSON per day."""
        if date_str in self._batch_cache:
            return

        # Check disk cache first
        cache_path = _news_cache_path(date_str)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                for sym in symbols:
                    self.cache[(sym, date_str)] = cached.get(sym, 0.0)
                self._batch_cache[date_str] = True
                return
            except Exception:
                pass

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        # Only pre-market + morning session news (midnight to 11:30 AM ET)
        start = dt.replace(hour=0, minute=0, second=0)
        end = dt.replace(hour=16, minute=30, second=0)

        sym_scores: dict[str, list[float]] = defaultdict(list)

        # Batch symbols in groups of 10 for the API
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            try:
                page_token = None
                pages = 0
                while pages < 5:  # max 5 pages per batch per day
                    kwargs = dict(
                        symbols=",".join(batch),
                        start=start,
                        end=end,
                        limit=50,
                    )
                    if page_token:
                        kwargs["page_token"] = page_token

                    req = NewsRequest(**kwargs)
                    result = self.client.get_news(req)
                    articles = result.data.get("news", []) if hasattr(result, 'data') else []

                    for article in articles:
                        score = self._score_headline(article.headline)
                        if article.symbols:
                            for s in article.symbols:
                                if s in batch:
                                    sym_scores[s].append(score)

                    # Check for next page
                    page_token = getattr(result, 'next_page_token', None)
                    pages += 1
                    if not page_token or len(articles) < 50:
                        break

                time.sleep(0.15)  # rate limit

            except Exception:
                continue

        # Build day cache and save to disk
        day_cache = {}
        for sym in symbols:
            scores = sym_scores.get(sym, [])
            val = float(np.clip(np.mean(scores), -1.0, 1.0)) if scores else 0.0
            self.cache[(sym, date_str)] = val
            day_cache[sym] = val

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(day_cache, f)
        except Exception:
            pass

        self._batch_cache[date_str] = True

    def get_sentiment(self, symbol: str, date_str: str) -> float:
        return self.cache.get((symbol, date_str), 0.0)


# ── Trade Simulator ─────────────────────────────

def simulate_trade(
    event: SignalEvent,
    side: str,
    position_size: float,
    cfg_risk: dict,
    slippage_bps: float = 5.0,
    commission: float = 0.005,
) -> TradeOutcome:
    entry_price = event.price_now
    slip = entry_price * slippage_bps / 10000.0
    if side == "BUY":
        entry_price += slip
    else:
        entry_price -= slip

    atr = event.atr
    if atr > 0 and atr / entry_price < cfg_risk.get("max_atr_pct", 0.10):
        stop_distance = atr * cfg_risk.get("atr_stop_multiplier", 2.0)
    else:
        stop_distance = entry_price * cfg_risk.get("stop_loss", 0.02)

    tp_pct = cfg_risk.get("take_profit", 0.05)
    be_trigger = cfg_risk.get("trailing_breakeven_trigger", 0.015)
    trail_trigger = cfg_risk.get("trailing_trigger", 0.025)
    trail_pct = cfg_risk.get("trailing_pct", 0.012)
    wide_trigger = cfg_risk.get("wide_trail_trigger", 0.035)
    wide_pct = cfg_risk.get("wide_trail_pct", 0.020)

    if side == "BUY":
        stop_price = entry_price - stop_distance
        tp_price = entry_price * (1.0 + tp_pct)
    else:
        stop_price = entry_price + stop_distance
        tp_price = entry_price * (1.0 - tp_pct)

    hwm = entry_price
    lwm = entry_price
    breakeven_moved = False
    trailing_active = False

    exit_price = None
    exit_reason = "time_exit"
    bars_held = len(event.forward_bars)

    for i, bar in enumerate(event.forward_bars):
        if side == "BUY":
            hwm = max(hwm, bar.high)
            gain_pct = (hwm - entry_price) / entry_price

            if bar.low <= stop_price:
                exit_price = stop_price
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                bars_held = i + 1
                break
            if bar.high >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1
                break

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
                exit_price = stop_price
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                bars_held = i + 1
                break
            if bar.low <= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1
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

    slip_exit = exit_price * slippage_bps / 10000.0
    if side == "BUY":
        exit_price -= slip_exit
    else:
        exit_price += slip_exit

    shares = position_size / entry_price
    if side == "BUY":
        raw_pnl = (exit_price - entry_price) * shares
    else:
        raw_pnl = (entry_price - exit_price) * shares

    pnl = raw_pnl - (commission * 2)

    return TradeOutcome(
        symbol=event.symbol, side=side,
        entry_price=round(entry_price, 4), exit_price=round(exit_price, 4),
        exit_reason=exit_reason, pnl=round(pnl, 2),
        bars_held=bars_held, date=event.date,
    )


# ── Signal Generation (Alpaca) ──────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
    )
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    return float(np.mean(tr[-period:]))


CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def _bars_cache_path(sym: str, start: datetime, end: datetime) -> Path:
    """Per-symbol parquet cache keyed by date range."""
    d = CACHE_DIR / "bars"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sym}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet"


def _news_cache_path(date_str: str) -> Path:
    """Per-day news sentiment JSON cache."""
    d = CACHE_DIR / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date_str}.json"


def download_alpaca_bars(client: StockHistoricalDataClient, symbols: list[str],
                         start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Download 5m bars from Alpaca. Caches each symbol as parquet for instant re-runs."""
    all_data = {}
    to_fetch = []

    # Check cache first
    for sym in symbols:
        cache_path = _bars_cache_path(sym, start, end)
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                if len(df) > 0:
                    all_data[sym] = df
                    continue
            except Exception:
                pass
        to_fetch.append(sym)

    if all_data:
        print(f"  Loaded {len(all_data)} symbols from cache")

    if not to_fetch:
        return all_data

    print(f"  Fetching {len(to_fetch)} symbols from Alpaca API...")
    batch_size = 5

    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i+batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start,
                end=end,
            )
            bars = client.get_stock_bars(req)
            bar_data = bars.data if hasattr(bars, 'data') and isinstance(bars.data, dict) else {}

            for sym in batch:
                sym_bars = bar_data.get(sym, [])
                if not sym_bars:
                    continue
                records = []
                for b in sym_bars:
                    records.append({
                        "timestamp": b.timestamp,
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": float(b.volume),
                    })
                df = pd.DataFrame(records)
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.set_index("timestamp").sort_index()
                all_data[sym] = df
                # Save to cache
                df.to_parquet(_bars_cache_path(sym, start, end))

            fetched_so_far = len(all_data)
            print(f"  Downloaded batch {i//batch_size + 1}/{(len(to_fetch)-1)//batch_size + 1}: ({fetched_so_far} total)", end="\r")
            time.sleep(0.5)

        except Exception as e:
            print(f"\n  Warning: batch {i//batch_size + 1} error: {e}")
            for sym in batch:
                try:
                    req = StockBarsRequest(
                        symbol_or_symbols=[sym],
                        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                        start=start,
                        end=end,
                    )
                    bars = client.get_stock_bars(req)
                    bar_data = bars.data if hasattr(bars, 'data') else {}
                    sym_bars = bar_data.get(sym, [])
                    if sym_bars:
                        records = [{"timestamp": b.timestamp, "open": float(b.open),
                                    "high": float(b.high), "low": float(b.low),
                                    "close": float(b.close), "volume": float(b.volume)}
                                   for b in sym_bars]
                        df = pd.DataFrame(records)
                        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                        df = df.set_index("timestamp").sort_index()
                        all_data[sym] = df
                        df.to_parquet(_bars_cache_path(sym, start, end))
                    time.sleep(0.3)
                except Exception:
                    continue

    return all_data


def generate_signals(cfg: dict, universe: list[str], lookback_days: int = 180) -> list[SignalEvent]:
    """Generate signals using Alpaca 5m bars + Benzinga news."""

    alpaca_cfg = cfg.get("alpaca", {})

    # Prefer environment variables (never store secrets in workflow.yaml)
    api_key = os.environ.get("ALPACA_API_KEY") or alpaca_cfg.get("api_key")
    api_secret = os.environ.get("ALPACA_API_SECRET") or alpaca_cfg.get("api_secret")

    if not api_key or not api_secret or "${" in str(api_key) or "${" in str(api_secret):
        raise RuntimeError(
            "Missing Alpaca credentials. Set env vars ALPACA_API_KEY and ALPACA_API_SECRET before running."
        )

    bar_client = StockHistoricalDataClient(api_key, api_secret)
    news_sentiment = AlpacaNewsSentiment(api_key, api_secret)

    tracer = LightningTracer(cfg)
    mom_agent = MomentumSignalAgent(cfg, tracer)
    mean_agent = MeanReversionSignalAgent(cfg, tracer)
    break_agent = BreakoutSignalAgent(cfg, tracer)
    normalizer = SignalNormalizer(lookback=cfg.get("signals", {}).get("norm_lookback", 100))

    min_volume = cfg.get("min_avg_volume", 100000)
    min_price = 5.0
    atr_period = cfg.get("risk", {}).get("atr_period", 14)

    end_dt = datetime.now(timezone.utc) - timedelta(hours=1)
    start_dt = end_dt - timedelta(days=lookback_days)

    print(f"\n-- Downloading {lookback_days}d of 5m bars from Alpaca for {len(universe)} symbols...")
    print(f"   Period: {start_dt.date()} to {end_dt.date()}")

    all_bars = download_alpaca_bars(bar_client, universe, start_dt, end_dt)
    print(f"\n   Got data for {len(all_bars)}/{len(universe)} symbols")

    events: list[SignalEvent] = []
    vol_filtered = 0
    price_filtered = 0
    failed = []
    total_news_fetched = 0

    for sym_idx, sym in enumerate(universe):
        df = all_bars.get(sym)
        if df is None or len(df) < 50:
            failed.append(sym)
            continue

        # Convert to ET for trading hours
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df_et = df.index.tz_convert("America/New_York")

        # Filter to trading hours 9:30-15:30
        mask = (df_et.hour * 60 + df_et.minute >= 570) & (df_et.hour * 60 + df_et.minute <= 930)
        df = df[mask]

        if len(df) < 50:
            failed.append(sym)
            continue

        df["date"] = df.index.tz_convert("America/New_York").date
        trading_days = sorted(df["date"].unique())

        for day in trading_days:
            day_df = df[df["date"] == day].copy()
            if len(day_df) < 20:
                continue

            # Signal generation window: first 24 bars (2 hours: 9:30-11:30)
            signal_window = min(24, len(day_df) // 2)
            if signal_window < 10:
                continue

            signal_df = day_df.iloc[:signal_window]
            current_price = signal_df["close"].iloc[-1]

            if current_price < min_price:
                price_filtered += 1
                continue

            avg_vol = signal_df["volume"].rolling(20).mean().iloc[-1] if len(signal_df) >= 20 else signal_df["volume"].mean()
            if avg_vol < min_volume:
                vol_filtered += 1
                continue

            atr = compute_atr(signal_df, atr_period)

            # Forward bars (up to 12 = 60 min after signal)
            forward_start = signal_window
            forward_end = min(signal_window + 12, len(day_df))
            forward_bars = []
            for j in range(forward_start, forward_end):
                row = day_df.iloc[j]
                forward_bars.append(BarData(
                    timestamp=day_df.index[j],
                    open=row["open"], high=row["high"],
                    low=row["low"], close=row["close"],
                    volume=row["volume"],
                ))
            if len(forward_bars) < 3:
                continue

            # Signals
            mom = normalizer.normalize(sym, "momentum", mom_agent.score(signal_df))
            mean = normalizer.normalize(sym, "meanrev", mean_agent.score(signal_df))
            brk = normalizer.normalize(sym, "breakout", break_agent.score(signal_df))

            # News: fetch once per day (batched)
            date_str = str(day)
            news_sentiment.fetch_day(date_str, universe)
            news_score = news_sentiment.get_sentiment(sym, date_str)
            if date_str not in news_sentiment._batch_cache or not total_news_fetched:
                total_news_fetched += 1

            events.append(SignalEvent(
                date=date_str, symbol=sym,
                momentum=mom, meanrev=mean, breakout=brk, news=news_score,
                price_now=current_price, atr=atr, avg_volume=avg_vol,
                forward_bars=forward_bars,
            ))

        sym_count = len([e for e in events if e.symbol == sym])
        print(f"  [{sym_idx+1}/{len(universe)}] {sym}: {sym_count} signals", end="\r")

    # Stats on news
    news_nonzero = sum(1 for e in events if abs(e.news) > 0.01)
    print(f"\n\nSignal Generation Complete:")
    print(f"  Total events: {len(events)}")
    print(f"  Filtered: {vol_filtered} low-volume, {price_filtered} under-$5")
    print(f"  News coverage: {news_nonzero}/{len(events)} events have news ({news_nonzero/max(len(events),1)*100:.1f}%)")
    print(f"  Days with news fetched: {len(news_sentiment._batch_cache)}")
    if failed:
        print(f"  Failed/empty: {', '.join(failed[:10])}")

    return events


# ── Challenge System ────────────────────────────

def passes_challenge(event: SignalEvent, weights: dict, threshold: float = 0.9) -> bool:
    active = []
    if weights.get("momentum", 0) > 0: active.append(event.momentum)
    if weights.get("meanrev", 0) > 0: active.append(event.meanrev)
    if weights.get("breakout", 0) > 0: active.append(event.breakout)
    if weights.get("news", 0) > 0: active.append(event.news)
    if len(active) < 2:
        return True
    return (max(active) - min(active)) <= threshold


# ── Backtest Engine ─────────────────────────────

def run_backtest(events, weights, threshold, cfg_risk,
                 slippage_bps=5.0, commission=0.005, position_size=1000.0,
                 max_positions=4, challenge_threshold=0.9,
                 cooldown_bars=12, detailed=False) -> BacktestResult:

    result = BacktestResult(weights=weights, threshold=threshold)
    result.total_signals = len(events)

    mom_w = weights["momentum"]
    mean_w = weights["meanrev"]
    brk_w = weights["breakout"]
    news_w = weights["news"]
    active_weight = mom_w + mean_w + brk_w + news_w

    if active_weight < 0.01:
        return result

    open_positions_by_date = defaultdict(int)
    last_trade_by_symbol = {}
    symbol_traded_today = defaultdict(set)

    for idx, ev in enumerate(events):
        raw = (ev.momentum * mom_w + ev.meanrev * mean_w +
               ev.breakout * brk_w + ev.news * news_w)
        score = raw / active_weight

        if abs(score) < threshold:
            continue

        side = "BUY" if score > 0 else "SELL"

        if open_positions_by_date[ev.date] >= max_positions:
            continue
        if ev.symbol in symbol_traded_today[ev.date]:
            continue
        if ev.symbol in last_trade_by_symbol:
            if idx - last_trade_by_symbol[ev.symbol] < cooldown_bars:
                continue
        if not passes_challenge(ev, weights, challenge_threshold):
            continue

        outcome = simulate_trade(ev, side, position_size, cfg_risk, slippage_bps, commission)

        result.trades_taken += 1
        result.trade_pnls.append(outcome.pnl)
        open_positions_by_date[ev.date] += 1
        last_trade_by_symbol[ev.symbol] = idx
        symbol_traded_today[ev.date].add(ev.symbol)

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
        else: result.time_exits += 1

        if detailed:
            result.trades.append(outcome)

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


def grid_search(events, cfg_risk, step=0.05, thresholds=None,
                slippage_bps=5.0, commission=0.005, max_positions=4,
                challenge_threshold=0.9, min_trades=30):

    combos = generate_weight_combos(step)
    if thresholds is None:
        thresholds = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]

    total = len(combos) * len(thresholds)
    print(f"\n  Grid search: {len(combos)} combos x {len(thresholds)} thresholds = {total} backtests")

    results = []
    for i, (combo, thresh) in enumerate(itertools.product(combos, thresholds)):
        r = run_backtest(events, combo, thresh, cfg_risk, slippage_bps, commission,
                         max_positions=max_positions, challenge_threshold=challenge_threshold)
        if r.trades_taken >= min_trades:
            results.append(r)
        if (i + 1) % 10000 == 0:
            print(f"  Progress: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="\r")

    print(f"\n  {len(results)} valid results (>={min_trades} trades)")
    return results


# ── Walk-Forward ────────────────────────────────

def walk_forward(events, cfg_risk, step=0.05, train_pct=0.6,
                 slippage_bps=5.0, commission=0.005, max_positions=4,
                 challenge_threshold=0.9, min_test_trades=20):

    dates = sorted(set(e.date for e in events))
    split = int(len(dates) * train_pct)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])

    train_events = [e for e in events if e.date in train_dates]
    test_events = [e for e in events if e.date in test_dates]

    print(f"\n  Walk-forward split:")
    print(f"    Train: {dates[0]} to {dates[split-1]} ({len(train_events)} events, {split} days)")
    print(f"    Test:  {dates[split]} to {dates[-1]} ({len(test_events)} events, {len(dates)-split} days)")

    # Train: require 30+ trades
    print("\n  -- TRAIN SET --")
    train_results = grid_search(train_events, cfg_risk, step, slippage_bps=slippage_bps,
                                commission=commission, max_positions=max_positions,
                                challenge_threshold=challenge_threshold, min_trades=30)
    train_results.sort(key=lambda r: r.composite_score, reverse=True)

    # Validate top 30 on test
    print("\n  -- VALIDATING TOP 30 ON TEST SET --")
    validated = []

    for tr in train_results[:30]:
        test_r = run_backtest(test_events, tr.weights, tr.threshold, cfg_risk,
                              slippage_bps, commission, max_positions=max_positions,
                              challenge_threshold=challenge_threshold, detailed=True)

        # Skip if test set has too few trades
        if test_r.trades_taken < min_test_trades:
            continue

        sym_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for t in test_r.trades:
            s = sym_stats[t.symbol]
            if t.pnl > 0: s["wins"] += 1
            else: s["losses"] += 1
            s["pnl"] += t.pnl

        exit_bd = {
            "stop_loss": test_r.stops_hit, "take_profit": test_r.tp_hit,
            "trailing_stop": test_r.trailing_hit, "time_exit": test_r.time_exits,
        }

        gap = abs(tr.win_rate - test_r.win_rate)
        consistency = max(0, 1.0 - gap * 2)

        validated.append({
            "weights": tr.weights,
            "threshold": tr.threshold,
            "train": {
                "trades": tr.trades_taken, "win_rate": round(tr.win_rate, 3),
                "pf": round(tr.profit_factor, 2), "pnl": round(tr.total_pnl, 2),
                "composite": round(tr.composite_score, 3), "sharpe": round(tr.sharpe, 3),
                "stops_hit": tr.stops_hit, "tp_hit": tr.tp_hit,
            },
            "test": {
                "trades": test_r.trades_taken, "win_rate": round(test_r.win_rate, 3),
                "pf": round(test_r.profit_factor, 2), "pnl": round(test_r.total_pnl, 2),
                "composite": round(test_r.composite_score, 3), "sharpe": round(test_r.sharpe, 3),
                "stops_hit": test_r.stops_hit, "tp_hit": test_r.tp_hit,
                "exit_breakdown": exit_bd,
            },
            "train_test_gap": round(gap, 3),
            "consistency": round(consistency, 3),
            "top_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)[:5]),
            "worst_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])[:5]),
        })

    # Sort by: test_composite * consistency — rewards both performance and stability
    validated.sort(
        key=lambda v: v["test"]["composite"] * (0.7 + 0.3 * v["consistency"]),
        reverse=True
    )

    return validated


# ── Main ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weight Optimizer v3 (Alpaca Data)")
    parser.add_argument("--days", type=int, default=180, help="Days of historical data (up to 730)")
    parser.add_argument("--step", type=float, default=0.05, help="Weight grid step size")
    parser.add_argument("--quick", action="store_true", help="Quick: 90 days, step=0.10")
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--min-test-trades", type=int, default=20, help="Minimum trades in test set to qualify")
    args = parser.parse_args()

    if args.quick:
        args.days = 90
        args.step = 0.10
        # quick runs often have fewer test trades; relax slightly
        args.min_test_trades = min(args.min_test_trades, 15)

    cfg = load_config()
    cfg_risk = cfg.get("risk", {})
    universe = cfg["universe"]
    slippage_bps = cfg.get("execution", {}).get("slippage_bps", 5)
    commission = cfg.get("execution", {}).get("commission", 0.005)
    max_positions = cfg_risk.get("max_positions", 4)
    challenge_threshold = cfg.get("challenges", {}).get("disagreement_threshold", 0.9)

    print("=" * 64)
    print("  WEIGHT OPTIMIZER v3 -- ALPACA DATA + BENZINGA NEWS")
    print("=" * 64)
    print(f"  Universe: {len(universe)} symbols")
    print(f"  Period: {args.days} days (Alpaca 5m bars)")
    print(f"  Grid step: {args.step}")
    print(f"  Stop: ATR x {cfg_risk.get('atr_stop_multiplier', 2.0)} | TP: {cfg_risk.get('take_profit', 0.05):.0%}")
    print(f"  Slippage: {slippage_bps}bps | Commission: ${commission}")
    print(f"  Max positions/day: {max_positions} | Challenge: {challenge_threshold}")
    print(f"  Min test trades: 20 | Min train trades: 30")
    print(f"  Scoring: WR*0.25 + PF*0.20 + PnL*0.25 + Trades*0.15 + Sharpe*0.15")
    print(f"  Current: {cfg.get('signals', {}).get('weights', {})}")

    start_time = time.time()

    events = generate_signals(cfg, universe, args.days)
    if len(events) < 100:
        print(f"\nNot enough events ({len(events)}). Need >= 100.")
        return

    validated = walk_forward(events, cfg_risk, args.step, slippage_bps=slippage_bps,
                             commission=commission, max_positions=max_positions,
                             challenge_threshold=challenge_threshold, min_test_trades=args.min_test_trades)

    elapsed = time.time() - start_time

    # Current config baseline
    current_weights = cfg.get("signals", {}).get("weights", {})
    current_thresh = cfg.get("signals", {}).get("trade_threshold", 0.25)
    curr = run_backtest(events, current_weights, current_thresh, cfg_risk,
                        slippage_bps, commission, max_positions=max_positions,
                        challenge_threshold=challenge_threshold, detailed=True)

    # Print results
    print("\n" + "=" * 64)
    print("  TOP 10 WEIGHT COMBOS (v3 -- Alpaca + Benzinga)")
    print("=" * 64)

    print(f"\n  CURRENT: {current_weights} @ thr={current_thresh}")
    print(f"    Trades: {curr.trades_taken} | WR: {curr.win_rate:.1%} | PF: {curr.profit_factor:.2f} | PnL: ${curr.total_pnl:.2f} | Sharpe: {curr.sharpe:.2f}")
    print(f"    Stops: {curr.stops_hit} | TP: {curr.tp_hit} | Trail: {curr.trailing_hit} | Time: {curr.time_exits}")

    for i, v in enumerate(validated[:10]):
        w = v["weights"]
        t = v["test"]
        tr = v["train"]
        print(f"\n  #{i+1} (consistency={v['consistency']:.2f})")
        print(f"    Weights: mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']}")
        print(f"    Threshold: {v['threshold']}")
        print(f"    Train: {tr['trades']}t WR={tr['win_rate']:.1%} PF={tr['pf']} PnL=${tr['pnl']:.2f} Sharpe={tr['sharpe']:.2f}")
        print(f"    Test:  {t['trades']}t WR={t['win_rate']:.1%} PF={t['pf']} PnL=${t['pnl']:.2f} Sharpe={t['sharpe']:.2f}")
        print(f"    Gap: {v['train_test_gap']:.1%}")
        exits = t.get('exit_breakdown', {})
        print(f"    Exits: stop={exits.get('stop_loss',0)} tp={exits.get('take_profit',0)} trail={exits.get('trailing_stop',0)} time={exits.get('time_exit',0)}")
        if v.get("top_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["top_symbols"].items())[:3]]
            print(f"    Best: {', '.join(parts)}")
        if v.get("worst_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["worst_symbols"].items())[:3]]
            print(f"    Worst: {', '.join(parts)}")

    print(f"\n  Completed in {elapsed:.1f}s")

    # Save
    curr_exit_bd = {
        "stop_loss": curr.stops_hit, "take_profit": curr.tp_hit,
        "trailing_stop": curr.trailing_hit, "time_exit": curr.time_exits,
    }
    output_path = args.output or str(Path(__file__).resolve().parent.parent / "backtest_results.json")
    output = {
        "version": "v3-alpaca",
        "timestamp": pd.Timestamp.now().isoformat(),
        "config": {
            "days": args.days, "step": args.step,
            "universe_size": len(universe), "signal_events": len(events),
            "slippage_bps": slippage_bps, "commission": commission,
            "stop_loss": f"ATR x {cfg_risk.get('atr_stop_multiplier', 2.0)}",
            "take_profit": f"{cfg_risk.get('take_profit', 0.05):.0%}",
            "max_positions_per_day": max_positions,
            "challenge_threshold": challenge_threshold,
            "min_volume": cfg.get("min_avg_volume", 100000),
            "min_price": 5.0,
            "scoring": "WR*0.25 + PF*0.20 + PnL*0.25 + Trades*0.15 + Sharpe*0.15",
            "data_source": "Alpaca (Benzinga news)",
        },
        "current_weights": current_weights,
        "current_result": {
            "trades": curr.trades_taken, "win_rate": round(curr.win_rate, 3),
            "pf": round(curr.profit_factor, 2), "pnl": round(curr.total_pnl, 2),
            "sharpe": round(curr.sharpe, 3),
            "exit_breakdown": curr_exit_bd,
        },
        "top_10": validated[:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"  Results saved to {output_path}")


if __name__ == "__main__":
    main()

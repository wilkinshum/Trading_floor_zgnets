"""
Weight Optimizer Backtester v3.1 — Finance Agent Recommendations
================================================================
Changes from v3:
  + TP grid search (1.5%, 2%, 2.5%, 3%, 5%) — was fixed at 5%
  + Symbol exclusion list (RKLB, ONDS, HUT — persistent losers)
  + Extended holding window: 18 bars (90 min) — was 12 bars (60 min)
  + Dynamic news weighting: redistribute to mom/brk when no news
  + Score normalization fix: divide by active_weight sum (reserve irrelevant)
  + Time-exit PnL tracking for analysis
  + All v3 features: Alpaca bars, Benzinga news, disk caching, walk-forward

Usage:
    python scripts/backtest_weights_v3_1.py --days 180 --step 0.05
    python scripts/backtest_weights_v3_1.py --quick
    python scripts/backtest_weights_v3_1.py --days 180 --step 0.05 --no-exclusions
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

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.signal_normalizer import SignalNormalizer
from trading_floor.lightning import LightningTracer

# ── Persistent losers identified by finance agent analysis ──
DEFAULT_EXCLUSIONS = {"RKLB", "ONDS", "HUT"}

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


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
    news: float
    has_news: bool            # NEW: whether real news data exists
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
    tp_pct: float = 0.05      # NEW: track which TP was used
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
    time_exit_pnl: float = 0.0   # NEW: track time-exit PnL separately
    trade_pnls: list = field(default_factory=list)
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
        pnl_norm = max(min(self.total_pnl, 500.0), -500.0) / 500.0
        pnl_norm = (pnl_norm + 1.0) / 2.0
        tc = min(self.trades_taken, 300) / 300.0
        sh = max(min(self.sharpe, 2.0), -2.0) / 2.0
        sh = (sh + 1.0) / 2.0
        return wr * 0.25 + pf * 0.20 + pnl_norm * 0.25 + tc * 0.15 + sh * 0.15


# ── News Sentiment (Alpaca/Benzinga) — with disk cache ──────

def _news_cache_path(date_str: str) -> Path:
    d = CACHE_DIR / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date_str}.json"


class AlpacaNewsSentiment:
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
        self.cache: dict[tuple[str, str], float] = {}
        self.has_news: dict[tuple[str, str], bool] = {}  # NEW: track coverage
        self._batch_cache: dict[str, bool] = {}

    def _score_headline(self, headline: str) -> float:
        words = set(re.findall(r'[a-z]+', headline.lower()))
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    def fetch_day(self, date_str: str, symbols: list[str]):
        if date_str in self._batch_cache:
            return

        # Check disk cache
        cache_path = _news_cache_path(date_str)
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                for sym in symbols:
                    val = cached.get(sym, 0.0)
                    self.cache[(sym, date_str)] = val
                    self.has_news[(sym, date_str)] = abs(val) > 0.001
                self._batch_cache[date_str] = True
                return
            except Exception:
                pass

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        start = dt.replace(hour=0, minute=0, second=0)
        end = dt.replace(hour=16, minute=30, second=0)

        sym_scores: dict[str, list[float]] = defaultdict(list)
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            try:
                page_token = None
                pages = 0
                while pages < 5:
                    kwargs = dict(symbols=",".join(batch), start=start, end=end, limit=50)
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
                    page_token = getattr(result, 'next_page_token', None)
                    pages += 1
                    if not page_token or len(articles) < 50:
                        break
                time.sleep(0.15)
            except Exception:
                continue

        day_cache = {}
        for sym in symbols:
            scores = sym_scores.get(sym, [])
            val = float(np.clip(np.mean(scores), -1.0, 1.0)) if scores else 0.0
            self.cache[(sym, date_str)] = val
            self.has_news[(sym, date_str)] = len(scores) > 0
            day_cache[sym] = val

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(day_cache, f)
        except Exception:
            pass
        self._batch_cache[date_str] = True

    def get_sentiment(self, symbol: str, date_str: str) -> float:
        return self.cache.get((symbol, date_str), 0.0)

    def symbol_has_news(self, symbol: str, date_str: str) -> bool:
        return self.has_news.get((symbol, date_str), False)


# ── Bar Caching ─────────────────────────────────

def _bars_cache_path(sym: str, start: datetime, end: datetime) -> Path:
    d = CACHE_DIR / "bars"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sym}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet"


def download_alpaca_bars(client, symbols, start, end):
    all_data = {}
    to_fetch = []
    for sym in symbols:
        cp = _bars_cache_path(sym, start, end)
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
                df.to_parquet(_bars_cache_path(sym, start, end))
            print(f"  Downloaded batch {i//batch_size+1}/{(len(to_fetch)-1)//batch_size+1}: ({len(all_data)} total)", end="\r")
            time.sleep(0.5)
        except Exception as e:
            print(f"\n  Warning: batch error: {e}")
            for sym in batch:
                try:
                    req = StockBarsRequest(symbol_or_symbols=[sym], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
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
                        df.to_parquet(_bars_cache_path(sym, start, end))
                    time.sleep(0.3)
                except Exception:
                    continue
    return all_data


# ── ATR ─────────────────────────────────────────

def compute_atr(df, period=14):
    if len(df) < period + 1:
        return 0.0
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    return float(np.mean(tr[-period:]))


# ── Trade Simulator (with variable TP) ──────────

def simulate_trade(event, side, position_size, cfg_risk, tp_pct=0.05,
                   slippage_bps=5.0, commission=0.005):
    entry_price = event.price_now
    slip = entry_price * slippage_bps / 10000.0
    entry_price = entry_price + slip if side == "BUY" else entry_price - slip

    atr = event.atr
    if atr > 0 and atr / entry_price < cfg_risk.get("max_atr_pct", 0.10):
        stop_distance = atr * cfg_risk.get("atr_stop_multiplier", 2.0)
    else:
        stop_distance = entry_price * cfg_risk.get("stop_loss", 0.02)

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

    hwm, lwm = entry_price, entry_price
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
                bars_held = i + 1; break
            if bar.high >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1; break
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
                bars_held = i + 1; break
            if bar.low <= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1; break
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
    exit_price = exit_price - slip_exit if side == "BUY" else exit_price + slip_exit

    shares = position_size / entry_price
    raw_pnl = (exit_price - entry_price) * shares if side == "BUY" else (entry_price - exit_price) * shares
    pnl = raw_pnl - (commission * 2)

    return TradeOutcome(symbol=event.symbol, side=side,
                        entry_price=round(entry_price, 4), exit_price=round(exit_price, 4),
                        exit_reason=exit_reason, pnl=round(pnl, 2),
                        bars_held=bars_held, date=event.date)


# ── Signal Generation ───────────────────────────

def generate_signals(cfg, universe, lookback_days=180, exclusions=None,
                     forward_bars_count=18):
    """Generate signals. forward_bars_count=18 (90 min) vs old 12 (60 min)."""

    alpaca_cfg = cfg.get("alpaca", {})
    api_key = os.environ.get("ALPACA_API_KEY") or alpaca_cfg.get("api_key")
    api_secret = os.environ.get("ALPACA_API_SECRET") or alpaca_cfg.get("api_secret")
    if not api_key or not api_secret or "${" in str(api_key) or "${" in str(api_secret):
        raise RuntimeError("Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET.")

    # Apply exclusions
    if exclusions:
        universe = [s for s in universe if s not in exclusions]
        print(f"  Excluded {len(exclusions)} symbols: {', '.join(sorted(exclusions))}")

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

    print(f"\n-- Downloading {lookback_days}d of 5m bars for {len(universe)} symbols...")
    print(f"   Period: {start_dt.date()} to {end_dt.date()}")
    print(f"   Forward window: {forward_bars_count} bars ({forward_bars_count * 5} min)")

    all_bars = download_alpaca_bars(bar_client, universe, start_dt, end_dt)
    print(f"\n   Got data for {len(all_bars)}/{len(universe)} symbols")

    events = []
    vol_filtered = 0
    price_filtered = 0
    failed = []

    for sym_idx, sym in enumerate(universe):
        df = all_bars.get(sym)
        if df is None or len(df) < 50:
            failed.append(sym)
            continue

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df_et = df.index.tz_convert("America/New_York")
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

            # EXTENDED forward window: 18 bars (90 min) vs old 12 (60 min)
            forward_start = signal_window
            forward_end = min(signal_window + forward_bars_count, len(day_df))
            forward_bars = []
            for j in range(forward_start, forward_end):
                row = day_df.iloc[j]
                forward_bars.append(BarData(
                    timestamp=day_df.index[j], open=row["open"], high=row["high"],
                    low=row["low"], close=row["close"], volume=row["volume"],
                ))
            if len(forward_bars) < 3:
                continue

            mom = normalizer.normalize(sym, "momentum", mom_agent.score(signal_df))
            mean = normalizer.normalize(sym, "meanrev", mean_agent.score(signal_df))
            brk = normalizer.normalize(sym, "breakout", break_agent.score(signal_df))

            date_str = str(day)
            news_sentiment.fetch_day(date_str, universe)
            news_score = news_sentiment.get_sentiment(sym, date_str)
            has_news = news_sentiment.symbol_has_news(sym, date_str)

            events.append(SignalEvent(
                date=date_str, symbol=sym,
                momentum=mom, meanrev=mean, breakout=brk, news=news_score,
                has_news=has_news,
                price_now=current_price, atr=atr, avg_volume=avg_vol,
                forward_bars=forward_bars,
            ))

        sym_count = len([e for e in events if e.symbol == sym])
        print(f"  [{sym_idx+1}/{len(universe)}] {sym}: {sym_count} signals", end="\r")

    news_nonzero = sum(1 for e in events if e.has_news)
    print(f"\n\nSignal Generation Complete:")
    print(f"  Total events: {len(events)}")
    print(f"  Filtered: {vol_filtered} low-volume, {price_filtered} under-$5")
    print(f"  News coverage: {news_nonzero}/{len(events)} ({news_nonzero/max(len(events),1)*100:.1f}%)")
    print(f"  Forward bars: {forward_bars_count} ({forward_bars_count*5} min)")
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


# ── Backtest Engine (with dynamic news + variable TP) ───────

def run_backtest(events, weights, threshold, cfg_risk, tp_pct=0.05,
                 slippage_bps=5.0, commission=0.005, position_size=1000.0,
                 max_positions=4, challenge_threshold=0.9,
                 cooldown_bars=12, detailed=False):

    result = BacktestResult(weights=weights, threshold=threshold, tp_pct=tp_pct)
    result.total_signals = len(events)

    mom_w = weights["momentum"]
    mean_w = weights["meanrev"]
    brk_w = weights["breakout"]
    news_w = weights["news"]

    open_positions_by_date = defaultdict(int)
    last_trade_by_symbol = {}
    symbol_traded_today = defaultdict(set)

    for idx, ev in enumerate(events):
        # DYNAMIC NEWS WEIGHTING: if no news, redistribute news_w proportionally
        if ev.has_news or news_w == 0:
            eff_mom_w, eff_mean_w, eff_brk_w, eff_news_w = mom_w, mean_w, brk_w, news_w
        else:
            # No news for this symbol/day — redistribute news weight to other signals
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
        # NORMALIZATION FIX: always divide by active_weight (not total with reserve)
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

        outcome = simulate_trade(ev, side, position_size, cfg_risk, tp_pct=tp_pct,
                                 slippage_bps=slippage_bps, commission=commission)

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
        else:
            result.time_exits += 1
            result.time_exit_pnl += outcome.pnl  # NEW

        if detailed:
            result.trades.append(outcome)

    return result


# ── Grid Search (with TP grid) ──────────────────

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


def grid_search(events, cfg_risk, step=0.05, thresholds=None, tp_pcts=None,
                slippage_bps=5.0, commission=0.005, max_positions=4,
                challenge_threshold=0.9, min_trades=30):

    combos = generate_weight_combos(step)
    if thresholds is None:
        thresholds = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    if tp_pcts is None:
        tp_pcts = [0.015, 0.02, 0.025, 0.03, 0.05]  # NEW: TP grid

    total = len(combos) * len(thresholds) * len(tp_pcts)
    print(f"\n  Grid: {len(combos)} combos x {len(thresholds)} thresholds x {len(tp_pcts)} TPs = {total} backtests")

    results = []
    count = 0
    for combo in combos:
        for thresh in thresholds:
            for tp in tp_pcts:
                r = run_backtest(events, combo, thresh, cfg_risk, tp_pct=tp,
                                 slippage_bps=slippage_bps, commission=commission,
                                 max_positions=max_positions, challenge_threshold=challenge_threshold)
                if r.trades_taken >= min_trades:
                    results.append(r)
                count += 1
                if count % 50000 == 0:
                    print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)", end="\r")

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

    print("\n  -- TRAIN SET --")
    train_results = grid_search(train_events, cfg_risk, step, slippage_bps=slippage_bps,
                                commission=commission, max_positions=max_positions,
                                challenge_threshold=challenge_threshold, min_trades=30)
    train_results.sort(key=lambda r: r.composite_score, reverse=True)

    print("\n  -- VALIDATING TOP 50 ON TEST SET --")
    validated = []

    for tr in train_results[:50]:  # validate more (was 30)
        test_r = run_backtest(test_events, tr.weights, tr.threshold, cfg_risk,
                              tp_pct=tr.tp_pct,
                              slippage_bps=slippage_bps, commission=commission,
                              max_positions=max_positions,
                              challenge_threshold=challenge_threshold, detailed=True)

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
            "tp_pct": tr.tp_pct,
            "train": {
                "trades": tr.trades_taken, "win_rate": round(tr.win_rate, 3),
                "pf": round(tr.profit_factor, 2), "pnl": round(tr.total_pnl, 2),
                "composite": round(tr.composite_score, 3), "sharpe": round(tr.sharpe, 3),
                "stops_hit": tr.stops_hit, "tp_hit": tr.tp_hit,
                "time_exit_pnl": round(tr.time_exit_pnl, 2),
            },
            "test": {
                "trades": test_r.trades_taken, "win_rate": round(test_r.win_rate, 3),
                "pf": round(test_r.profit_factor, 2), "pnl": round(test_r.total_pnl, 2),
                "composite": round(test_r.composite_score, 3), "sharpe": round(test_r.sharpe, 3),
                "stops_hit": test_r.stops_hit, "tp_hit": test_r.tp_hit,
                "trailing_hit": test_r.trailing_hit,
                "time_exits": test_r.time_exits,
                "time_exit_pnl": round(test_r.time_exit_pnl, 2),
                "exit_breakdown": exit_bd,
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
    parser = argparse.ArgumentParser(description="Weight Optimizer v3.1 (Finance Agent Recs)")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--step", type=float, default=0.05)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument("--min-test-trades", type=int, default=20)
    parser.add_argument("--no-exclusions", action="store_true", help="Disable symbol exclusions")
    parser.add_argument("--forward-bars", type=int, default=18, help="Forward bars (default 18 = 90 min)")
    args = parser.parse_args()

    if args.quick:
        args.days = 90
        args.step = 0.10
        args.min_test_trades = min(args.min_test_trades, 15)

    cfg = load_config()
    cfg_risk = cfg.get("risk", {})
    universe = cfg["universe"]
    slippage_bps = cfg.get("execution", {}).get("slippage_bps", 5)
    commission = cfg.get("execution", {}).get("commission", 0.005)
    max_positions = cfg_risk.get("max_positions", 4)
    challenge_threshold = cfg.get("challenges", {}).get("disagreement_threshold", 0.9)
    exclusions = set() if args.no_exclusions else DEFAULT_EXCLUSIONS

    print("=" * 70)
    print("  WEIGHT OPTIMIZER v3.1 -- FINANCE AGENT RECOMMENDATIONS")
    print("=" * 70)
    print(f"  NEW: TP grid [1.5%, 2%, 2.5%, 3%, 5%]")
    print(f"  NEW: Symbol exclusions: {sorted(exclusions) if exclusions else 'DISABLED'}")
    print(f"  NEW: Forward window: {args.forward_bars} bars ({args.forward_bars*5} min)")
    print(f"  NEW: Dynamic news weighting (redistribute when no news)")
    print(f"  NEW: Score normalization fix (reserve weight irrelevant)")
    print(f"  Universe: {len(universe)} symbols ({len(universe)-len(exclusions)} after exclusions)")
    print(f"  Period: {args.days} days | Grid step: {args.step}")
    print(f"  Stop: ATR x {cfg_risk.get('atr_stop_multiplier', 2.0)}")
    print(f"  Slippage: {slippage_bps}bps | Commission: ${commission}")
    print(f"  Max positions/day: {max_positions} | Challenge: {challenge_threshold}")
    print(f"  Scoring: WR*0.25 + PF*0.20 + PnL*0.25 + Trades*0.15 + Sharpe*0.15")
    current_weights = cfg.get("signals", {}).get("weights", {})
    print(f"  Current: {current_weights}")

    start_time = time.time()

    events = generate_signals(cfg, universe, args.days, exclusions=exclusions,
                              forward_bars_count=args.forward_bars)
    if len(events) < 100:
        print(f"\nNot enough events ({len(events)}). Need >= 100.")
        return

    validated = walk_forward(events, cfg_risk, args.step, slippage_bps=slippage_bps,
                             commission=commission, max_positions=max_positions,
                             challenge_threshold=challenge_threshold,
                             min_test_trades=args.min_test_trades)

    elapsed = time.time() - start_time

    # Current config baseline
    current_thresh = cfg.get("signals", {}).get("trade_threshold", 0.25)
    curr = run_backtest(events, current_weights, current_thresh, cfg_risk,
                        tp_pct=cfg_risk.get("take_profit", 0.05),
                        slippage_bps=slippage_bps, commission=commission,
                        max_positions=max_positions,
                        challenge_threshold=challenge_threshold, detailed=True)

    # Print results
    print("\n" + "=" * 70)
    print("  TOP 10 WEIGHT COMBOS (v3.1 -- Finance Agent Optimized)")
    print("=" * 70)

    print(f"\n  CURRENT: {current_weights} @ thr={current_thresh} tp=5%")
    print(f"    Trades: {curr.trades_taken} | WR: {curr.win_rate:.1%} | PF: {curr.profit_factor:.2f} | PnL: ${curr.total_pnl:.2f} | Sharpe: {curr.sharpe:.2f}")
    print(f"    Stops: {curr.stops_hit} | TP: {curr.tp_hit} | Trail: {curr.trailing_hit} | Time: {curr.time_exits} (${curr.time_exit_pnl:.2f})")

    for i, v in enumerate(validated[:10]):
        w = v["weights"]
        t = v["test"]
        tr = v["train"]
        print(f"\n  #{i+1} (consistency={v['consistency']:.2f})")
        print(f"    Weights: mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']}")
        print(f"    Threshold: {v['threshold']} | TP: {v['tp_pct']:.1%}")
        print(f"    Train: {tr['trades']}t WR={tr['win_rate']:.1%} PF={tr['pf']} PnL=${tr['pnl']:.2f} Sharpe={tr['sharpe']:.2f}")
        print(f"    Test:  {t['trades']}t WR={t['win_rate']:.1%} PF={t['pf']} PnL=${t['pnl']:.2f} Sharpe={t['sharpe']:.2f}")
        print(f"    Gap: {v['train_test_gap']:.1%}")
        exits = t.get('exit_breakdown', {})
        print(f"    Exits: stop={exits.get('stop_loss',0)} tp={exits.get('take_profit',0)} trail={exits.get('trailing_stop',0)} time={exits.get('time_exit',0)} (time PnL: ${t.get('time_exit_pnl',0):.2f})")
        if v.get("top_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["top_symbols"].items())[:3]]
            print(f"    Best: {', '.join(parts)}")
        if v.get("worst_symbols"):
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(v["worst_symbols"].items())[:3]]
            print(f"    Worst: {', '.join(parts)}")

    print(f"\n  Completed in {elapsed:.1f}s")

    # Save
    output_path = args.output or str(Path(__file__).resolve().parent.parent / "backtest_results_v3_1.json")
    output = {
        "version": "v3.1-finance-optimized",
        "timestamp": pd.Timestamp.now().isoformat(),
        "changes": [
            "TP grid search (1.5-5%)",
            "Symbol exclusions (RKLB, ONDS, HUT)",
            "Extended forward window (18 bars / 90 min)",
            "Dynamic news weighting",
            "Score normalization fix",
            "Validate top 50 (was 30)",
            "Time-exit PnL tracking",
        ],
        "config": {
            "days": args.days, "step": args.step,
            "universe_size": len(universe), "effective_universe": len(universe) - len(exclusions),
            "signal_events": len(events),
            "excluded_symbols": sorted(exclusions),
            "forward_bars": args.forward_bars,
            "tp_grid": [0.015, 0.02, 0.025, 0.03, 0.05],
            "slippage_bps": slippage_bps, "commission": commission,
            "scoring": "WR*0.25 + PF*0.20 + PnL*0.25 + Trades*0.15 + Sharpe*0.15",
            "data_source": "Alpaca (Benzinga news)",
        },
        "current_weights": current_weights,
        "current_result": {
            "trades": curr.trades_taken, "win_rate": round(curr.win_rate, 3),
            "pf": round(curr.profit_factor, 2), "pnl": round(curr.total_pnl, 2),
            "sharpe": round(curr.sharpe, 3),
            "exit_breakdown": {"stop_loss": curr.stops_hit, "take_profit": curr.tp_hit,
                               "trailing_stop": curr.trailing_hit, "time_exit": curr.time_exits},
            "time_exit_pnl": round(curr.time_exit_pnl, 2),
        },
        "top_10": validated[:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"  Results saved to {output_path}")


if __name__ == "__main__":
    main()

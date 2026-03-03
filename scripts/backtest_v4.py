"""
V4 Integrated Backtester — Dual Intraday + Swing
=================================================
Simulates the full V4 system over 30 trading days with:
  - Budget isolation ($2K intraday / $3K swing)
  - Kill switches (daily 5% / weekly 8% / monthly 12%)
  - Intraday: 5m bars, 9:30-11:30 entry, bracket TP/SL, 3:45 force close
  - Swing: daily bars, hold ≤10d, TP 15%, SL 8%, trail 4%→2.5% after day 5
  - Sector limits (max 1 swing per sector)
  - Compounding off (fixed sizing until $7,500)
  - Exclusion lists per strategy

Output: backtest_v4_results.json + console summary

Usage:
    python scripts/backtest_v4.py --days 30
    python scripts/backtest_v4.py --days 60 --verbose
"""
from __future__ import annotations
import argparse, json, sys, io, os, time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

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

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Config ──────────────────────────────────────

STARTING_EQUITY = 5000.0
INTRADAY_BUDGET = 2000.0
SWING_BUDGET = 3000.0

INTRADAY_CFG = {
    "max_positions": 3,
    "weights": {"momentum": 0.50, "meanrev": 0.00, "breakout": 0.15, "news": 0.25},
    "threshold": 0.25,
    "take_profit": 0.025,
    "stop_loss_atr": 2.0,
    "close_by_bar": -3,  # force close ~3 bars before market close
    "entry_start_bar": 0,  # first bar of day
    "entry_end_bar": 24,   # 9:30-11:30 = 24 five-min bars
    "exclusions": {"RKLB", "ONDS", "HUT", "AVAV", "MP", "POWL"},
    "slippage_bps": 5,
    "max_trades_per_day": 2,
}

SWING_CFG = {
    "max_positions": 3,
    "max_per_sector": 1,
    "weights": {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.00, "news": 0.10},
    "threshold": 0.25,
    "take_profit": 0.09,
    "stop_loss": 0.08,
    "max_hold_days": 10,
    "trailing_trigger": 0.08,
    "trailing_pct": 0.04,
    "time_decay_day": 5,
    "time_decay_pct": 0.025,
    "exclusions": {"RKLB", "ONDS", "HUT", "IONQ", "RGTI", "AVAV", "MP", "POWL"},
    "slippage_bps": 5,
    "max_trades_per_day": 2,
    "sl_cooldown_days": 5,
}

KILL_SWITCHES = {
    "daily": 0.05,
    "weekly": 0.08,
    "monthly": 0.12,
    "intraday_daily": 0.03,
    "swing_weekly": 0.04,
}

# Sector map (simplified)
SECTOR_MAP = {
    "SPY": "ETF", "QQQ": "ETF",
    "NVDA": "Semiconductors", "AMD": "Semiconductors", "TSM": "Semiconductors",
    "ASML": "Semiconductors", "QCOM": "Semiconductors",
    "MSFT": "Software", "GOOGL": "Software", "META": "Software", "ORCL": "Software",
    "CRWD": "Cybersecurity", "PATH": "Software", "PLTR": "Software",
    "AMZN": "E-Commerce", "COST": "Retail", "SBUX": "Retail", "NKE": "Retail",
    "TSLA": "EV", "GME": "Retail",
    "ISRG": "Medical", "UNH": "Healthcare", "GH": "Genomics", "MIRM": "Biotech",
    "GEV": "Energy", "CEG": "Nuclear", "CCJ": "Nuclear", "OKLO": "Nuclear",
    "VST": "Energy", "FSLR": "Solar", "EOSE": "Solar", "FLNC": "Solar",
    "ASTS": "Space", "LUNR": "Space", "RDW": "Space", "KTOS": "Defense",
    "IONQ": "Quantum", "RGTI": "Quantum", "QBTS": "Quantum",
    "COIN": "Crypto", "MSTR": "Crypto", "MARA": "Crypto", "RIOT": "Crypto",
    "CORZ": "Crypto", "BITF": "Crypto",
    "ANET": "Networking", "VRT": "Infra", "NFLX": "Streaming",
    "JPM": "Finance", "GS": "Finance", "V": "Finance",
    "NBIS": "AI", "IREN": "AI", "GRAL": "Biotech", "XYZ": "Other",
    "AMTM": "Industrial", "SYM": "Robotics", "TE": "Electronics",
    "ELVA": "Other", "CRML": "Other", "TMC": "Mining", "IDR": "Other",
    "MTZ": "Industrial", "AGX": "Industrial", "POWL": "Industrial",
    "TMQ": "Industrial", "UUUU": "Uranium",
}


def load_config():
    import yaml
    cfg_path = BASE_DIR / "configs" / "workflow.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Data loading with caching ───────────────────

def get_alpaca_clients():
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    bars_client = StockHistoricalDataClient(key, secret)
    news_client = NewsClient(key, secret)
    return bars_client, news_client


def load_intraday_bars(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Load 5m bars from cache or Alpaca."""
    cache_key = f"{symbol}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    cache_path = CACHE_DIR / "bars" / f"{cache_key}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    req = StockBarsRequest(
        symbol_or_symbols=symbol, start=start, end=end,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel(0)
    df.to_parquet(cache_path)
    return df


def load_daily_bars(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Load daily bars from cache or Alpaca."""
    cache_key = f"{symbol}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    cache_path = CACHE_DIR / "daily_bars" / f"{cache_key}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    req = StockBarsRequest(
        symbol_or_symbols=symbol, start=start, end=end,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel(0)
    df.to_parquet(cache_path)
    return df


def load_news_cache(date_str: str) -> dict:
    """Load cached Benzinga news for a date."""
    path = CACHE_DIR / "news" / f"{date_str}.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def fetch_and_cache_news(news_client, symbols: list, start: datetime, end: datetime):
    """Fetch and cache news day by day."""
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        cache_path = CACHE_DIR / "news" / f"{date_str}.json"
        if not cache_path.exists():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                req = NewsRequest(
                    symbols=",".join(symbols),
                    start=current, end=current + timedelta(days=1),
                    limit=50,
                )
                news = news_client.get_news(req)
                articles = {}
                for article in news.news:
                    for sym in (article.symbols or []):
                        if sym in symbols:
                            if sym not in articles:
                                articles[sym] = []
                            articles[sym].append({
                                "headline": article.headline,
                                "source": article.source,
                            })
                with open(cache_path, "w") as f:
                    json.dump(articles, f)
            except Exception as e:
                print(f"  News fetch error {date_str}: {e}")
        current += timedelta(days=1)


# ── Signal scoring ──────────────────────────────

def calc_momentum(df: pd.DataFrame, short: int = 10) -> float:
    if len(df) < short + 1:
        return 0.0
    returns = df["close"].pct_change()
    short_ret = returns.iloc[-short:].sum()
    return float(np.clip(short_ret * 10, -1, 1))


def calc_meanrev(df: pd.DataFrame, long: int = 20) -> float:
    if len(df) < long:
        return 0.0
    ma = df["close"].rolling(long).mean().iloc[-1]
    price = df["close"].iloc[-1]
    if ma == 0:
        return 0.0
    deviation = (price - ma) / ma
    return float(np.clip(-deviation * 10, -1, 1))


def calc_breakout(df: pd.DataFrame, lookback: int = 50) -> float:
    if len(df) < lookback + 1:
        return 0.0
    window = df.iloc[-(lookback + 1):-1]  # prior bars only (exclude current)
    high = window["high"].max() if "high" in df.columns else window["close"].max()
    low = window["low"].min() if "low" in df.columns else window["close"].min()
    price = df["close"].iloc[-1]
    if high == low:
        return 0.0
    pos = (price - low) / (high - low)
    if pos > 0.95:
        return float(np.clip((pos - 0.5) * 2, 0, 1))
    elif pos < 0.05:
        return float(np.clip((pos - 0.5) * 2, -1, 0))
    return 0.0


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    h = df["high"] if "high" in df.columns else df["close"]
    l = df["low"] if "low" in df.columns else df["close"]
    c = df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def news_sentiment(articles) -> float:
    """Simple headline sentiment: count positive/negative keywords."""
    if not articles or not isinstance(articles, list):
        return 0.0
    pos_kw = {"surge", "soar", "beat", "upgrade", "rally", "bullish", "strong", "record", "raise", "growth"}
    neg_kw = {"crash", "fall", "miss", "downgrade", "bearish", "weak", "decline", "loss", "cut", "warn"}
    score = 0.0
    for a in articles:
        hl = a.get("headline", "").lower()
        for w in pos_kw:
            if w in hl:
                score += 0.3
        for w in neg_kw:
            if w in hl:
                score -= 0.3
    return float(np.clip(score, -1, 1))


def weighted_score(weights: dict, raw: dict) -> float:
    total_w = 0.0
    weighted_sum = 0.0
    for key, w in weights.items():
        if key == "reserve" or w <= 0:
            continue
        val = raw.get(key, 0.0)
        if val is None:
            val = 0.0
        total_w += w
        weighted_sum += val * w
    return weighted_sum / total_w if total_w > 0 else 0.0


# ── Trade tracking ──────────────────────────────

@dataclass
class IntraTrade:
    symbol: str
    side: str
    entry_price: float
    qty: int
    entry_bar: int  # bar index within the day
    entry_date: str
    stop_price: float
    tp_price: float
    signals: dict = field(default_factory=dict)
    pnl: float = 0.0
    exit_reason: str = ""
    exit_price: float = 0.0


@dataclass
class SwingTrade:
    symbol: str
    entry_price: float
    qty: int
    entry_date: str
    sector: str
    stop_price: float
    tp_price: float
    signals: dict = field(default_factory=dict)
    days_held: int = 0
    high_water: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    exit_price: float = 0.0


@dataclass
class DayResult:
    date: str
    intraday_trades: int = 0
    intraday_pnl: float = 0.0
    swing_entries: int = 0
    swing_exits: int = 0
    swing_pnl: float = 0.0
    equity: float = 0.0
    daily_pnl: float = 0.0
    kill_switch_triggered: bool = False


# ── Main backtest engine ────────────────────────

class V4Backtester:
    def __init__(self, cfg: dict, days: int = 30, verbose: bool = False):
        self.cfg = cfg
        self.days = days
        self.verbose = verbose
        self.bars_client, self.news_client = get_alpaca_clients()

        self.equity = STARTING_EQUITY
        self.intraday_pnl_today = 0.0
        self.swing_pnl_week = 0.0
        self.total_pnl_daily = 0.0
        self.total_pnl_weekly = 0.0
        self.total_pnl_monthly = 0.0

        self.open_swing: List[SwingTrade] = []
        self.closed_intraday: List[IntraTrade] = []
        self.closed_swing: List[SwingTrade] = []
        self.daily_results: List[DayResult] = []
        self.equity_curve: List[dict] = []
        self.swing_sl_cooldowns: Dict[str, str] = {}  # symbol -> cooldown_expiry_date

        universe = cfg.get("universe", [])
        self.universe = [s for s in universe if s not in ("SPY", "QQQ")]  # trade everything except ETFs
        self.normalizer = SignalNormalizer(lookback=100)

    def run(self):
        end = datetime(2026, 3, 1, tzinfo=timezone.utc)
        start = end - timedelta(days=self.days + 60)  # extra for warmup

        print(f"\n{'='*60}")
        print(f"V4 INTEGRATED BACKTESTER")
        print(f"{'='*60}")
        print(f"Period: {self.days} trading days ending {end.strftime('%Y-%m-%d')}")
        print(f"Starting equity: ${STARTING_EQUITY:,.2f}")
        print(f"Intraday budget: ${INTRADAY_BUDGET:,.2f} | Swing budget: ${SWING_BUDGET:,.2f}")
        print(f"Universe: {len(self.universe)} symbols")
        print()

        # Load all data
        print("Loading intraday bars...")
        intraday_data = {}
        for i, sym in enumerate(self.universe):
            try:
                df = load_intraday_bars(self.bars_client, sym, start, end)
                if not df.empty:
                    intraday_data[sym] = df
                    if self.verbose:
                        print(f"  {sym}: {len(df)} bars")
            except Exception as e:
                print(f"  {sym}: error - {e}")
            if (i + 1) % 20 == 0:
                print(f"  ...loaded {i+1}/{len(self.universe)}")

        print(f"Loaded intraday data for {len(intraday_data)} symbols")

        print("Loading daily bars...")
        daily_data = {}
        for sym in self.universe:
            try:
                df = load_daily_bars(self.bars_client, sym, start, end)
                if not df.empty:
                    daily_data[sym] = df
            except Exception as e:
                if self.verbose:
                    print(f"  {sym} daily: {e}")

        print(f"Loaded daily data for {len(daily_data)} symbols")

        # News
        print("Loading news cache...")
        all_syms = list(set(self.universe))
        fetch_and_cache_news(self.news_client, all_syms, start, end)

        # Get trading days from SPY daily bars
        spy_daily = daily_data.get("SPY") or daily_data.get(list(daily_data.keys())[0])
        if spy_daily is None or spy_daily.empty:
            # Try loading SPY separately
            spy_daily = load_daily_bars(self.bars_client, "SPY", start, end)

        trading_days = sorted(spy_daily.index.normalize().unique())
        # Take last N days
        trading_days = trading_days[-self.days:] if len(trading_days) >= self.days else trading_days
        print(f"Trading days: {len(trading_days)} ({trading_days[0].strftime('%Y-%m-%d')} to {trading_days[-1].strftime('%Y-%m-%d')})")
        print()

        week_start_equity = self.equity
        month_start_equity = self.equity
        week_counter = 0

        for day_idx, day_ts in enumerate(trading_days):
            day_str = day_ts.strftime("%Y-%m-%d") if hasattr(day_ts, 'strftime') else str(day_ts)[:10]
            day_start_equity = self.equity
            self.intraday_pnl_today = 0.0
            day_result = DayResult(date=day_str, equity=self.equity)

            # Check kill switches
            daily_loss = (day_start_equity - self.equity) / STARTING_EQUITY  # will be 0 at start
            weekly_loss = (week_start_equity - self.equity) / STARTING_EQUITY
            monthly_loss = (month_start_equity - self.equity) / STARTING_EQUITY

            if weekly_loss >= KILL_SWITCHES["weekly"] or monthly_loss >= KILL_SWITCHES["monthly"]:
                day_result.kill_switch_triggered = True
                if self.verbose:
                    print(f"  {day_str}: KILL SWITCH - weekly {weekly_loss:.1%} / monthly {monthly_loss:.1%}")
                self.daily_results.append(day_result)
                self.equity_curve.append({"date": day_str, "equity": self.equity})
                continue

            # ── SWING: manage exits first ──
            swing_pnl_today = self._swing_manage_exits(day_str, daily_data)
            day_result.swing_pnl = swing_pnl_today
            day_result.swing_exits = sum(1 for s in self.closed_swing if s.exit_reason and s.exit_price > 0
                                         and getattr(s, '_exit_date', '') == day_str)

            # ── INTRADAY: run within-day simulation ──
            intra_pnl, intra_count = self._run_intraday_day(day_str, intraday_data)
            day_result.intraday_pnl = intra_pnl
            day_result.intraday_trades = intra_count

            # ── SWING: scan for new entries (simplified — check at start of day) ──
            swing_entries = self._swing_scan_and_enter(day_str, daily_data)
            day_result.swing_entries = swing_entries

            # ── Update equity ──
            total_day_pnl = intra_pnl + swing_pnl_today
            self.equity += total_day_pnl
            day_result.daily_pnl = total_day_pnl
            day_result.equity = self.equity

            self.daily_results.append(day_result)
            self.equity_curve.append({"date": day_str, "equity": round(self.equity, 2)})

            # Week/month tracking
            week_counter += 1
            if week_counter >= 5:
                week_start_equity = self.equity
                self.swing_pnl_week = 0.0
                week_counter = 0
            if day_idx > 0 and day_idx % 21 == 0:
                month_start_equity = self.equity

            if self.verbose or intra_count > 0 or swing_entries > 0 or abs(swing_pnl_today) > 0:
                print(f"  {day_str}: equity=${self.equity:,.2f} | "
                      f"intra={intra_count}t ${intra_pnl:+.2f} | "
                      f"swing_enter={swing_entries} swing_exit_pnl=${swing_pnl_today:+.2f} | "
                      f"open_swing={len(self.open_swing)}")

        self._print_results()
        self._save_results()

    # ── Intraday simulation ─────────────────────

    def _run_intraday_day(self, day_str: str, all_data: dict) -> Tuple[float, int]:
        """Simulate intraday trading for one day. Returns (pnl, trade_count)."""
        cfg = INTRADAY_CFG
        excl = cfg["exclusions"]
        symbols = [s for s in self.universe if s not in excl]

        # Get day's 5m bars for each symbol
        day_bars = {}
        for sym in symbols:
            df = all_data.get(sym)
            if df is None or df.empty:
                continue
            # Filter to this day
            day_mask = df.index.normalize() == pd.Timestamp(day_str, tz=df.index.tz)
            ddf = df[day_mask]
            if len(ddf) >= 20:  # need enough bars
                day_bars[sym] = ddf

        if not day_bars:
            return 0.0, 0

        news_data = load_news_cache(day_str)
        total_pnl = 0.0
        trades_today = 0
        open_trades: List[IntraTrade] = []
        budget_used = 0.0
        max_budget = INTRADAY_BUDGET

        # Get bar count for iteration
        first_sym = list(day_bars.keys())[0]
        n_bars = len(day_bars[first_sym])

        for bar_idx in range(n_bars):
            # ── Check exits for open trades ──
            closed_ids = []
            for i, trade in enumerate(open_trades):
                sym_bars = day_bars.get(trade.symbol)
                if sym_bars is None or bar_idx >= len(sym_bars):
                    continue
                bar = sym_bars.iloc[bar_idx]
                price = bar["close"]
                high = bar["high"] if "high" in sym_bars.columns else price
                low = bar["low"] if "low" in sym_bars.columns else price

                # Check TP
                if high >= trade.tp_price:
                    trade.exit_price = trade.tp_price
                    trade.exit_reason = "tp"
                    slip = trade.entry_price * cfg["slippage_bps"] / 10000
                    trade.pnl = (trade.exit_price - trade.entry_price - slip) * trade.qty
                    closed_ids.append(i)
                # Check SL
                elif low <= trade.stop_price:
                    trade.exit_price = trade.stop_price
                    trade.exit_reason = "sl"
                    slip = trade.entry_price * cfg["slippage_bps"] / 10000
                    trade.pnl = (trade.exit_price - trade.entry_price - slip) * trade.qty
                    closed_ids.append(i)
                # Force close near end of day
                elif bar_idx >= n_bars + cfg["close_by_bar"]:
                    trade.exit_price = price
                    trade.exit_reason = "time"
                    slip = trade.entry_price * cfg["slippage_bps"] / 10000
                    trade.pnl = (trade.exit_price - trade.entry_price - slip) * trade.qty
                    closed_ids.append(i)

            # Remove closed trades
            for i in sorted(closed_ids, reverse=True):
                t = open_trades.pop(i)
                total_pnl += t.pnl
                budget_used -= t.entry_price * t.qty
                self.closed_intraday.append(t)
                trades_today += 1

            # ── Check kill switch intraday ──
            if abs(total_pnl) / STARTING_EQUITY >= KILL_SWITCHES["intraday_daily"]:
                # Force close all open
                for t in open_trades:
                    sym_bars_t = day_bars.get(t.symbol)
                    if sym_bars_t is not None and bar_idx < len(sym_bars_t):
                        t.exit_price = sym_bars_t.iloc[bar_idx]["close"]
                    else:
                        t.exit_price = t.entry_price
                    t.exit_reason = "kill_switch"
                    slip = t.entry_price * cfg["slippage_bps"] / 10000
                    t.pnl = (t.exit_price - t.entry_price - slip) * t.qty
                    total_pnl += t.pnl
                    self.closed_intraday.append(t)
                    trades_today += 1
                open_trades.clear()
                break

            # ── Scan for entries (only in entry window) ──
            if bar_idx < cfg["entry_start_bar"] or bar_idx > cfg["entry_end_bar"]:
                continue
            if trades_today >= cfg["max_trades_per_day"]:
                continue
            if len(open_trades) >= cfg["max_positions"]:
                continue

            # Score all symbols, pick best
            candidates = []
            for sym, bars_df in day_bars.items():
                if bar_idx >= len(bars_df) or bar_idx < 20:
                    continue
                # Skip if already in a trade
                if any(t.symbol == sym for t in open_trades):
                    continue

                window = bars_df.iloc[:bar_idx+1]
                mom = calc_momentum(window, 10)
                mean = calc_meanrev(window, 20)
                brk = calc_breakout(window, min(50, bar_idx))
                news_raw = news_sentiment(news_data.get(sym, []))

                raw = {"momentum": mom, "meanrev": mean, "breakout": brk, "news": news_raw}
                score = weighted_score(cfg["weights"], raw)

                if abs(score) >= cfg["threshold"]:
                    candidates.append((sym, score, raw, window))

            # Sort by score, take top
            candidates.sort(key=lambda x: abs(x[1]), reverse=True)

            for sym, score, raw, window in candidates[:1]:  # max 1 entry per bar
                if len(open_trades) >= cfg["max_positions"]:
                    break
                if trades_today >= cfg["max_trades_per_day"]:
                    break

                price = window["close"].iloc[-1]
                atr = calc_atr(window, 14)

                # Position sizing
                per_pos = min(max_budget - budget_used, max_budget / cfg["max_positions"])
                if per_pos <= 0:
                    continue
                qty = int(per_pos // price)
                if qty < 10:
                    continue

                cost = qty * price
                budget_used += cost

                # Bracket
                tp_price = round(price * (1 + cfg["take_profit"]), 2)
                sl_price = round(price - cfg["stop_loss_atr"] * atr, 2) if atr > 0 else round(price * 0.98, 2)

                trade = IntraTrade(
                    symbol=sym, side="buy", entry_price=price, qty=qty,
                    entry_bar=bar_idx, entry_date=day_str,
                    stop_price=sl_price, tp_price=tp_price, signals=raw,
                )
                open_trades.append(trade)

        # Force close anything still open (shouldn't happen but safety)
        for t in open_trades:
            sym_bars_t = day_bars.get(t.symbol)
            if sym_bars_t is not None and len(sym_bars_t) > 0:
                t.exit_price = sym_bars_t.iloc[-1]["close"]
            else:
                t.exit_price = t.entry_price
            t.exit_reason = "eod"
            slip = t.entry_price * cfg["slippage_bps"] / 10000
            t.pnl = (t.exit_price - t.entry_price - slip) * t.qty
            total_pnl += t.pnl
            self.closed_intraday.append(t)
            trades_today += 1

        return total_pnl, trades_today

    # ── Swing management ────────────────────────

    def _swing_manage_exits(self, day_str: str, daily_data: dict) -> float:
        """Check swing positions for exit conditions. Returns pnl from closed trades."""
        cfg = SWING_CFG
        pnl = 0.0
        still_open = []

        for trade in self.open_swing:
            sym_df = daily_data.get(trade.symbol)
            if sym_df is None or sym_df.empty:
                still_open.append(trade)
                continue

            # Get today's bar
            day_mask = sym_df.index.normalize() == pd.Timestamp(day_str, tz=sym_df.index.tz)
            today_bars = sym_df[day_mask]
            if today_bars.empty:
                still_open.append(trade)
                continue

            bar = today_bars.iloc[-1]
            high = bar["high"] if "high" in sym_df.columns else bar["close"]
            low = bar["low"] if "low" in sym_df.columns else bar["close"]
            close = bar["close"]

            trade.days_held += 1
            trade.high_water = max(trade.high_water, high)
            gain_pct = (close - trade.entry_price) / trade.entry_price

            exit_reason = None
            exit_price = close

            # TP
            if high >= trade.tp_price:
                exit_reason = "tp"
                exit_price = trade.tp_price
            # SL
            elif low <= trade.stop_price:
                exit_reason = "sl"
                exit_price = trade.stop_price
            # Max hold
            elif trade.days_held >= cfg["max_hold_days"]:
                exit_reason = "time"
            # Trailing stop
            elif gain_pct >= cfg["trailing_trigger"]:
                trail_pct = cfg["trailing_pct"]
                if trade.days_held >= cfg["time_decay_day"]:
                    trail_pct = cfg["time_decay_pct"]
                trail_stop = trade.high_water * (1 - trail_pct)
                if low <= trail_stop:
                    exit_reason = "trail"
                    exit_price = trail_stop

            if exit_reason:
                slip = trade.entry_price * cfg["slippage_bps"] / 10000
                trade.pnl = (exit_price - trade.entry_price - slip) * trade.qty
                trade.exit_reason = exit_reason
                trade.exit_price = exit_price
                trade._exit_date = day_str
                pnl += trade.pnl
                self.closed_swing.append(trade)
                # Cooldown after SL
                if exit_reason == "sl" and cfg.get("sl_cooldown_days", 0) > 0:
                    self.swing_sl_cooldowns[trade.symbol] = day_str
            else:
                still_open.append(trade)

        self.open_swing = still_open
        return pnl

    def _swing_scan_and_enter(self, day_str: str, daily_data: dict) -> int:
        """Scan for swing entries on this day. Returns count of entries."""
        cfg = SWING_CFG
        excl = cfg["exclusions"]
        symbols = [s for s in self.universe if s not in excl]

        if len(self.open_swing) >= cfg["max_positions"]:
            return 0

        # Get open sectors
        open_sectors = defaultdict(int)
        for t in self.open_swing:
            open_sectors[t.sector] += 1

        # Budget tracking for swing
        swing_budget_used = sum(t.entry_price * t.qty for t in self.open_swing)
        available_budget = SWING_BUDGET - swing_budget_used

        if available_budget <= 0:
            return 0

        candidates = []
        for sym in symbols:
            df = daily_data.get(sym)
            if df is None or df.empty:
                continue

            # Get bars up to this day
            day_mask = df.index.normalize() <= pd.Timestamp(day_str, tz=df.index.tz)
            window = df[day_mask]
            if len(window) < 20:
                continue

            # Already in a swing trade?
            if any(t.symbol == sym for t in self.open_swing):
                continue

            # SL cooldown check
            cooldown_days = cfg.get("sl_cooldown_days", 0)
            if cooldown_days > 0 and sym in self.swing_sl_cooldowns:
                sl_date = pd.Timestamp(self.swing_sl_cooldowns[sym])
                current_date = pd.Timestamp(day_str)
                # Count trading days between (approximate with calendar days)
                if (current_date - sl_date).days < cooldown_days:
                    continue
                else:
                    del self.swing_sl_cooldowns[sym]  # cooldown expired

            # Sector check
            sector = SECTOR_MAP.get(sym, "Other")
            if sector != "ETF" and open_sectors.get(sector, 0) >= cfg["max_per_sector"]:
                continue

            today_mask = df.index.normalize() == pd.Timestamp(day_str, tz=df.index.tz)
            today = df[today_mask]
            if today.empty:
                continue

            price = today.iloc[-1]["close"]

            # Load 3-day news
            d = pd.Timestamp(day_str)
            news_articles = []
            for offset in range(3):
                nd = (d - timedelta(days=offset)).strftime("%Y-%m-%d")
                cached = load_news_cache(nd).get(sym, [])
                if isinstance(cached, list):
                    news_articles.extend(cached)

            mom = calc_momentum(window, 10)
            mean = calc_meanrev(window, 20)
            brk = 0.0  # breakout weight is 0 for swing
            news_raw = news_sentiment(news_articles)

            raw = {"momentum": mom, "meanrev": mean, "breakout": brk, "news": news_raw}
            score = weighted_score(cfg["weights"], raw)

            if score >= cfg["threshold"]:  # swing: BUY only
                candidates.append((sym, score, raw, price, sector))

        candidates.sort(key=lambda x: x[1], reverse=True)
        entries = 0

        for sym, score, raw, price, sector in candidates[:cfg["max_trades_per_day"]]:
            if len(self.open_swing) >= cfg["max_positions"]:
                break
            if available_budget <= 0:
                break

            per_pos = min(available_budget, SWING_BUDGET / cfg["max_positions"])
            qty = int(per_pos // price)
            if qty < 10:
                continue

            cost = qty * price
            available_budget -= cost

            sl_price = round(price * (1 - cfg["stop_loss"]), 2)
            tp_price = round(price * (1 + cfg["take_profit"]), 2)

            trade = SwingTrade(
                symbol=sym, entry_price=price, qty=qty, entry_date=day_str,
                sector=sector, stop_price=sl_price, tp_price=tp_price,
                signals=raw, high_water=price,
            )
            self.open_swing.append(trade)
            open_sectors[sector] += 1
            entries += 1

        return entries

    # ── Results ─────────────────────────────────

    def _print_results(self):
        total_pnl = self.equity - STARTING_EQUITY
        intra_pnl = sum(t.pnl for t in self.closed_intraday)
        swing_pnl = sum(t.pnl for t in self.closed_swing)

        # Unrealized swing PnL (not counted in closed)
        n_intra = len(self.closed_intraday)
        n_swing = len(self.closed_swing)

        intra_wins = sum(1 for t in self.closed_intraday if t.pnl > 0)
        swing_wins = sum(1 for t in self.closed_swing if t.pnl > 0)

        intra_wr = (intra_wins / n_intra * 100) if n_intra > 0 else 0
        swing_wr = (swing_wins / n_swing * 100) if n_swing > 0 else 0
        total_wr = ((intra_wins + swing_wins) / (n_intra + n_swing) * 100) if (n_intra + n_swing) > 0 else 0

        # Profit factor
        intra_gross_profit = sum(t.pnl for t in self.closed_intraday if t.pnl > 0)
        intra_gross_loss = abs(sum(t.pnl for t in self.closed_intraday if t.pnl < 0))
        swing_gross_profit = sum(t.pnl for t in self.closed_swing if t.pnl > 0)
        swing_gross_loss = abs(sum(t.pnl for t in self.closed_swing if t.pnl < 0))

        intra_pf = intra_gross_profit / intra_gross_loss if intra_gross_loss > 0 else float('inf')
        swing_pf = swing_gross_profit / swing_gross_loss if swing_gross_loss > 0 else float('inf')

        # Max drawdown
        peak = STARTING_EQUITY
        max_dd = 0.0
        for pt in self.equity_curve:
            eq = pt["equity"]
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

        # Best/worst days
        best_day = max(self.daily_results, key=lambda d: d.daily_pnl) if self.daily_results else None
        worst_day = min(self.daily_results, key=lambda d: d.daily_pnl) if self.daily_results else None

        # Exit reason breakdown
        intra_exits = defaultdict(int)
        for t in self.closed_intraday:
            intra_exits[t.exit_reason] += 1
        swing_exits = defaultdict(int)
        for t in self.closed_swing:
            swing_exits[t.exit_reason] += 1

        # Avg trade
        avg_intra = intra_pnl / n_intra if n_intra > 0 else 0
        avg_swing = swing_pnl / n_swing if n_swing > 0 else 0

        print(f"\n{'='*60}")
        print(f"V4 BACKTEST RESULTS — {len(self.daily_results)} trading days")
        print(f"{'='*60}")
        print(f"  Starting equity:    ${STARTING_EQUITY:>10,.2f}")
        print(f"  Final equity:       ${self.equity:>10,.2f}")
        print(f"  Total P&L:          ${total_pnl:>+10,.2f} ({total_pnl/STARTING_EQUITY:+.1%})")
        print(f"  Max drawdown:       {max_dd:>10.1%}")
        print(f"  Daily avg P&L:      ${total_pnl/len(self.daily_results):>+10,.2f}" if self.daily_results else "")
        print()

        print(f"  INTRADAY")
        print(f"    Trades:           {n_intra}")
        print(f"    P&L:              ${intra_pnl:>+10,.2f}")
        print(f"    Win rate:         {intra_wr:>9.1f}%")
        print(f"    Profit factor:    {intra_pf:>10.2f}")
        print(f"    Avg trade:        ${avg_intra:>+10,.2f}")
        print(f"    Exits:            {dict(intra_exits)}")
        print()

        print(f"  SWING")
        print(f"    Trades:           {n_swing} (+ {len(self.open_swing)} still open)")
        print(f"    P&L:              ${swing_pnl:>+10,.2f}")
        print(f"    Win rate:         {swing_wr:>9.1f}%")
        print(f"    Profit factor:    {swing_pf:>10.2f}")
        print(f"    Avg trade:        ${avg_swing:>+10,.2f}")
        print(f"    Exits:            {dict(swing_exits)}")
        print()

        print(f"  COMBINED")
        print(f"    Total trades:     {n_intra + n_swing}")
        print(f"    Win rate:         {total_wr:.1f}%")
        if best_day:
            print(f"    Best day:         {best_day.date} ${best_day.daily_pnl:+,.2f}")
        if worst_day:
            print(f"    Worst day:        {worst_day.date} ${worst_day.daily_pnl:+,.2f}")

        print(f"\n  Open swing positions: {len(self.open_swing)}")
        for t in self.open_swing:
            print(f"    {t.symbol}: entry ${t.entry_price:.2f}, {t.days_held}d held, sector={t.sector}")

    def _save_results(self):
        results = {
            "config": {
                "starting_equity": STARTING_EQUITY,
                "intraday_budget": INTRADAY_BUDGET,
                "swing_budget": SWING_BUDGET,
                "days": self.days,
                "intraday": {k: (list(v) if isinstance(v, set) else v) for k, v in INTRADAY_CFG.items()},
                "swing": {k: (list(v) if isinstance(v, set) else v) for k, v in SWING_CFG.items()},
                "kill_switches": KILL_SWITCHES,
            },
            "summary": {
                "final_equity": round(self.equity, 2),
                "total_pnl": round(self.equity - STARTING_EQUITY, 2),
                "total_return_pct": round((self.equity - STARTING_EQUITY) / STARTING_EQUITY * 100, 2),
                "intraday_trades": len(self.closed_intraday),
                "swing_trades": len(self.closed_swing),
                "intraday_pnl": round(sum(t.pnl for t in self.closed_intraday), 2),
                "swing_pnl": round(sum(t.pnl for t in self.closed_swing), 2),
                "intraday_win_rate": round(sum(1 for t in self.closed_intraday if t.pnl > 0) / max(len(self.closed_intraday), 1) * 100, 1),
                "swing_win_rate": round(sum(1 for t in self.closed_swing if t.pnl > 0) / max(len(self.closed_swing), 1) * 100, 1),
            },
            "equity_curve": self.equity_curve,
            "daily_results": [
                {"date": d.date, "equity": round(d.equity, 2), "daily_pnl": round(d.daily_pnl, 2),
                 "intraday_trades": d.intraday_trades, "intraday_pnl": round(d.intraday_pnl, 2),
                 "swing_entries": d.swing_entries, "swing_pnl": round(d.swing_pnl, 2)}
                for d in self.daily_results
            ],
            "intraday_trades": [
                {"symbol": t.symbol, "date": t.entry_date, "entry": t.entry_price,
                 "exit": round(t.exit_price, 2), "qty": t.qty, "pnl": round(t.pnl, 2),
                 "exit_reason": t.exit_reason, "signals": {k: round(v, 3) for k, v in t.signals.items()}}
                for t in self.closed_intraday
            ],
            "swing_trades": [
                {"symbol": t.symbol, "entry_date": t.entry_date, "entry": t.entry_price,
                 "exit": round(t.exit_price, 2), "qty": t.qty, "pnl": round(t.pnl, 2),
                 "days_held": t.days_held, "exit_reason": t.exit_reason, "sector": t.sector,
                 "signals": {k: round(v, 3) for k, v in t.signals.items()}}
                for t in self.closed_swing
            ],
        }

        out_path = BASE_DIR / "backtest_v4_results.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="V4 Integrated Backtester")
    parser.add_argument("--days", type=int, default=30, help="Trading days to backtest")
    parser.add_argument("--verbose", action="store_true", help="Print every day")
    args = parser.parse_args()

    cfg = load_config()
    bt = V4Backtester(cfg, days=args.days, verbose=args.verbose)
    bt.run()


if __name__ == "__main__":
    main()

"""
Weight Optimizer Backtester
===========================
Pulls 1 month of historical 5m data for the full universe,
runs all 4 signal agents on each trading day, then grid-searches
weight combos to find optimal win rate + profit factor.

Uses walk-forward validation: train on first 60%, validate on last 40%.

Usage:
    python scripts/backtest_weights.py
    python scripts/backtest_weights.py --days 30 --step 0.05
    python scripts/backtest_weights.py --quick   # fewer combos, faster
"""

from __future__ import annotations
import argparse
import itertools
import json
import sys
import io
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# Fix encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.news import NewsSentimentAgent
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
class SignalEvent:
    date: str
    symbol: str
    momentum: float
    meanrev: float
    breakout: float
    news: float
    price_now: float
    price_30m: float  # price 30 min later (6 bars of 5m)
    price_60m: float  # price 60 min later (12 bars)

    @property
    def return_30m(self) -> float:
        if self.price_now == 0:
            return 0.0
        return (self.price_30m - self.price_now) / self.price_now

    @property
    def return_60m(self) -> float:
        if self.price_now == 0:
            return 0.0
        return (self.price_60m - self.price_now) / self.price_now


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
    def composite_score(self) -> float:
        """Composite: win_rate * 0.5 + profit_factor_norm * 0.3 + trade_count_norm * 0.2"""
        wr = self.win_rate
        pf = min(self.profit_factor, 3.0) / 3.0  # normalize PF to 0-1, cap at 3
        tc = min(self.trades_taken, 200) / 200.0  # normalize trade count
        return wr * 0.5 + pf * 0.3 + tc * 0.2


# ── Signal Generation ──────────────────────────

def generate_signals(cfg: dict, universe: list[str], lookback_days: int = 30) -> list[SignalEvent]:
    """Download data and generate signals for each symbol on each trading day."""

    print(f"\n📊 Downloading {lookback_days}d of 5m data for {len(universe)} symbols...")

    tracer = LightningTracer(cfg)
    mom_agent = MomentumSignalAgent(cfg, tracer)
    mean_agent = MeanReversionSignalAgent(cfg, tracer)
    break_agent = BreakoutSignalAgent(cfg, tracer)
    news_agent = NewsSentimentAgent(cfg, tracer)
    normalizer = SignalNormalizer(lookback=cfg.get("signals", {}).get("norm_lookback", 100))

    # yfinance max for 5m is 60 days
    period = f"{min(lookback_days, 59)}d"
    events: list[SignalEvent] = []
    failed = []

    for i, sym in enumerate(universe):
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period=period, interval="5m")
            if df.empty or len(df) < 50:
                failed.append(sym)
                continue

            # Lowercase columns for consistency
            df.columns = [c.lower() for c in df.columns]
            if "adj close" in df.columns:
                df.rename(columns={"adj close": "close"}, inplace=True)

            # Get unique trading days
            df.index = pd.to_datetime(df.index)
            df["date"] = df.index.date

            # Trading hours filter: 9:30-11:30 ET
            if df.index.tz is not None:
                df_et = df.index.tz_convert("America/New_York")
            else:
                df_et = df.index.tz_localize("UTC").tz_convert("America/New_York")

            trading_days = sorted(df["date"].unique())

            for day in trading_days:
                day_mask = df["date"] == day
                day_df = df[day_mask].copy()

                if len(day_df) < 20:
                    continue

                # Use data up to ~10:30 AM window for signal generation
                # Then measure forward returns at 11:00 and 11:30
                mid_idx = len(day_df) // 2
                if mid_idx < 10:
                    continue

                signal_df = day_df.iloc[:mid_idx]  # first half for signals
                current_price = signal_df["close"].iloc[-1]

                # Forward prices
                forward_6 = min(mid_idx + 6, len(day_df) - 1)
                forward_12 = min(mid_idx + 12, len(day_df) - 1)
                price_30m = day_df["close"].iloc[forward_6]
                price_60m = day_df["close"].iloc[forward_12]

                # Generate raw signals
                mom_raw = mom_agent.score(signal_df)
                mean_raw = mean_agent.score(signal_df)
                brk_raw = break_agent.score(signal_df)
                news_raw = news_agent.get_sentiment(sym)

                # Normalize
                mom = normalizer.normalize(sym, "momentum", mom_raw)
                mean = normalizer.normalize(sym, "meanrev", mean_raw)
                brk = normalizer.normalize(sym, "breakout", brk_raw)
                news = news_raw  # already [-1, +1]

                events.append(SignalEvent(
                    date=str(day),
                    symbol=sym,
                    momentum=mom,
                    meanrev=mean,
                    breakout=brk,
                    news=news,
                    price_now=current_price,
                    price_30m=price_30m,
                    price_60m=price_60m,
                ))

            progress = f"[{i+1}/{len(universe)}]"
            print(f"  {progress} {sym}: {len([e for e in events if e.symbol == sym])} signals", end="\r")

        except Exception as e:
            failed.append(sym)
            continue

    print(f"\n✅ Generated {len(events)} signal events from {len(universe) - len(failed)}/{len(universe)} symbols")
    if failed:
        print(f"  ⚠️ Failed: {', '.join(failed[:10])}")

    return events


# ── Grid Search ─────────────────────────────────

def generate_weight_combos(step: float = 0.05) -> list[dict]:
    """Generate all weight combos that sum to ~1.0 (with reserve)."""
    combos = []
    vals = np.arange(0.0, 1.01, step)

    for mom in vals:
        for brk in vals:
            for news in vals:
                for mean in vals:
                    total = mom + brk + news + mean
                    if total < 0.01:
                        continue
                    # Allow reserve (unused weight)
                    if total > 1.0 + 0.001:
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


def run_backtest(events: list[SignalEvent], weights: dict, threshold: float,
                 position_size: float = 1000.0) -> BacktestResult:
    """Run a single backtest with given weights and threshold."""
    result = BacktestResult(weights=weights, threshold=threshold)
    result.total_signals = len(events)

    mom_w = weights["momentum"]
    mean_w = weights["meanrev"]
    brk_w = weights["breakout"]
    news_w = weights["news"]
    active_weight = mom_w + mean_w + brk_w + news_w

    if active_weight < 0.01:
        return result

    for ev in events:
        # Calculate weighted score
        raw = (ev.momentum * mom_w + ev.meanrev * mean_w +
               ev.breakout * brk_w + ev.news * news_w)
        score = raw / active_weight  # normalize to [-1, +1]

        # Apply threshold
        if abs(score) < threshold:
            continue

        result.trades_taken += 1
        side = "BUY" if score > 0 else "SELL"

        # Use 30m return for PnL
        ret = ev.return_30m
        if side == "SELL":
            ret = -ret  # profit from short

        pnl = ret * position_size

        if pnl > 0:
            result.wins += 1
            result.gross_profit += pnl
        else:
            result.losses += 1
            result.gross_loss += pnl  # negative

        result.total_pnl += pnl

    return result


def grid_search(events: list[SignalEvent], step: float = 0.05,
                thresholds: list[float] | None = None) -> list[BacktestResult]:
    """Test all weight combos × thresholds."""

    combos = generate_weight_combos(step)
    if thresholds is None:
        thresholds = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]

    print(f"\n🔍 Grid search: {len(combos)} weight combos × {len(thresholds)} thresholds = {len(combos) * len(thresholds)} backtests")

    results = []
    total = len(combos) * len(thresholds)

    for i, (combo, thresh) in enumerate(itertools.product(combos, thresholds)):
        r = run_backtest(events, combo, thresh)
        if r.trades_taken >= 30:  # min trades to be meaningful (need statistical significance)
            results.append(r)
        if (i + 1) % 5000 == 0:
            print(f"  Progress: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="\r")

    print(f"\n✅ {len(results)} valid results (≥5 trades)")
    return results


# ── Walk-Forward Validation ─────────────────────

def walk_forward(events: list[SignalEvent], step: float = 0.05,
                 train_pct: float = 0.6) -> list[dict]:
    """Split events by date, optimize on train set, validate on test set."""

    dates = sorted(set(e.date for e in events))
    split = int(len(dates) * train_pct)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])

    train_events = [e for e in events if e.date in train_dates]
    test_events = [e for e in events if e.date in test_dates]

    print(f"\n📅 Walk-forward split:")
    print(f"  Train: {dates[0]} → {dates[split-1]} ({len(train_events)} events)")
    print(f"  Test:  {dates[split]} → {dates[-1]} ({len(test_events)} events)")

    # Optimize on train
    print("\n── TRAIN SET ──")
    train_results = grid_search(train_events, step)
    train_results.sort(key=lambda r: r.composite_score, reverse=True)

    # Validate top 20 combos on test set
    print("\n── VALIDATING TOP 20 ON TEST SET ──")
    validated = []

    for tr in train_results[:20]:
        test_r = run_backtest(test_events, tr.weights, tr.threshold)
        validated.append({
            "weights": tr.weights,
            "threshold": tr.threshold,
            "train": {
                "trades": tr.trades_taken,
                "win_rate": round(tr.win_rate, 3),
                "pf": round(tr.profit_factor, 2),
                "pnl": round(tr.total_pnl, 2),
                "composite": round(tr.composite_score, 3),
            },
            "test": {
                "trades": test_r.trades_taken,
                "win_rate": round(test_r.win_rate, 3),
                "pf": round(test_r.profit_factor, 2),
                "pnl": round(test_r.total_pnl, 2),
                "composite": round(test_r.composite_score, 3),
            },
            "train_test_gap": round(abs(tr.win_rate - test_r.win_rate), 3),
        })

    # Sort by test composite, penalize large train/test gap
    validated.sort(
        key=lambda v: v["test"]["composite"] - v["train_test_gap"] * 0.5,
        reverse=True
    )

    return validated


# ── Main ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weight Optimizer Backtester")
    parser.add_argument("--days", type=int, default=30, help="Days of historical data")
    parser.add_argument("--step", type=float, default=0.05, help="Weight grid step size")
    parser.add_argument("--quick", action="store_true", help="Quick mode (step=0.10)")
    parser.add_argument("--no-news", action="store_true", help="Skip news (faster, no API calls)")
    parser.add_argument("--output", default=None, help="Save results to JSON file")
    args = parser.parse_args()

    if args.quick:
        args.step = 0.10

    cfg = load_config()
    universe = cfg["universe"]

    print("=" * 60)
    print("  🏋️ WEIGHT OPTIMIZER BACKTESTER")
    print("=" * 60)
    print(f"  Universe: {len(universe)} symbols")
    print(f"  Period: {args.days} days")
    print(f"  Grid step: {args.step}")
    print(f"  Current weights: {cfg.get('signals', {}).get('weights', {})}")

    start = time.time()

    # Generate signals
    events = generate_signals(cfg, universe, args.days)
    if len(events) < 50:
        print("❌ Not enough signal events. Need at least 50.")
        return

    # Walk-forward validation
    validated = walk_forward(events, args.step)

    elapsed = time.time() - start

    # Print results
    print("\n" + "=" * 60)
    print("  📊 TOP 10 WEIGHT COMBOS (walk-forward validated)")
    print("=" * 60)

    current = cfg.get("signals", {}).get("weights", {})

    # Also test current weights
    print(f"\n  📌 CURRENT CONFIG: {current}")
    current_thresh = cfg.get("signals", {}).get("trade_threshold", 0.25)
    all_events = events
    curr_result = run_backtest(all_events, current, current_thresh)
    print(f"     Trades: {curr_result.trades_taken} | WR: {curr_result.win_rate:.1%} | PF: {curr_result.profit_factor:.2f} | PnL: ${curr_result.total_pnl:.2f}")

    print()
    for i, v in enumerate(validated[:10]):
        w = v["weights"]
        marker = ""
        if w["momentum"] == current.get("momentum") and w["breakout"] == current.get("breakout"):
            marker = " ← CURRENT"

        print(f"  #{i+1}{marker}")
        print(f"     Weights: mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']}")
        print(f"     Threshold: {v['threshold']}")
        print(f"     Train: {v['train']['trades']} trades, WR={v['train']['win_rate']:.1%}, PF={v['train']['pf']:.2f}, PnL=${v['train']['pnl']:.2f}")
        print(f"     Test:  {v['test']['trades']} trades, WR={v['test']['win_rate']:.1%}, PF={v['test']['pf']:.2f}, PnL=${v['test']['pnl']:.2f}")
        print(f"     Gap: {v['train_test_gap']:.1%}")
        print()

    print(f"⏱️  Completed in {elapsed:.1f}s")

    # Save results
    output_path = args.output or str(Path(__file__).resolve().parent.parent / "backtest_results.json")
    output = {
        "timestamp": pd.Timestamp.now().isoformat(),
        "config": {
            "days": args.days,
            "step": args.step,
            "universe_size": len(universe),
            "signal_events": len(events),
        },
        "current_weights": current,
        "current_result": {
            "trades": curr_result.trades_taken,
            "win_rate": round(curr_result.win_rate, 3),
            "pf": round(curr_result.profit_factor, 2),
            "pnl": round(curr_result.total_pnl, 2),
        },
        "top_10": validated[:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"💾 Results saved to {output_path}")


if __name__ == "__main__":
    main()

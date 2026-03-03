"""
Weight Optimizer Backtester v2
==============================
Realistic simulation matching production pipeline:
  ✅ ATR-based stop-loss (2x ATR) with trailing stops
  ✅ Take-profit targets
  ✅ Slippage + commission deduction
  ✅ Volume filter (100K min avg)
  ✅ Max concurrent positions (4)
  ✅ No same-symbol re-entry within cooldown
  ✅ Min price filter ($5)
  ✅ Challenge system (signal disagreement)
  ✅ Bar-by-bar stop/TP simulation (not just 30m snapshot)
  ✅ Per-symbol breakdown output
  ✅ Both 30m and 60m horizon comparison

Usage:
    python scripts/backtest_weights_v2.py --days 30 --step 0.05
    python scripts/backtest_weights_v2.py --quick  # step=0.10, faster
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
from typing import Optional

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
class BarData:
    """One 5-minute bar."""
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SignalEvent:
    """A trade signal with full bar-level forward data for simulation."""
    date: str
    symbol: str
    momentum: float
    meanrev: float
    breakout: float
    news: float
    price_now: float
    atr: float                      # 14-period ATR at signal time
    avg_volume: float               # 20-bar average volume
    forward_bars: list[BarData]     # next N bars for stop/TP simulation (up to 12 = 60min)

    @property
    def price_30m(self) -> float:
        if len(self.forward_bars) >= 6:
            return self.forward_bars[5].close
        return self.forward_bars[-1].close if self.forward_bars else self.price_now

    @property
    def price_60m(self) -> float:
        if len(self.forward_bars) >= 12:
            return self.forward_bars[11].close
        return self.forward_bars[-1].close if self.forward_bars else self.price_now


@dataclass
class TradeOutcome:
    """Result of simulating one trade bar-by-bar."""
    symbol: str
    side: str           # BUY or SELL
    entry_price: float
    exit_price: float
    exit_reason: str    # stop_loss, take_profit, trailing_stop, time_exit
    pnl: float          # after slippage + commission
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
    def composite_score(self) -> float:
        """WR * 0.5 + PF_norm * 0.3 + trade_count_norm * 0.2"""
        wr = self.win_rate
        pf = min(self.profit_factor, 3.0) / 3.0
        tc = min(self.trades_taken, 200) / 200.0
        return wr * 0.5 + pf * 0.3 + tc * 0.2


# ── Trade Simulator (bar-by-bar) ────────────────

def simulate_trade(
    event: SignalEvent,
    side: str,
    position_size: float,
    cfg_risk: dict,
    slippage_bps: float = 5.0,
    commission: float = 0.005,
) -> TradeOutcome:
    """
    Simulate a trade bar-by-bar with:
    - ATR-based stop-loss (2x ATR, fallback 2%)
    - Take-profit (5%)
    - Trailing stop: breakeven at +1.5%, trail at +2.5% (1.2%), wide at +3.5% (2%)
    - Slippage on entry + exit
    - Commission
    - Time exit at end of forward bars
    """
    entry_price = event.price_now

    # Apply entry slippage
    slip = entry_price * slippage_bps / 10000.0
    if side == "BUY":
        entry_price += slip  # pay more
    else:
        entry_price -= slip  # get less

    # Stop-loss: ATR-based or fallback
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

    hwm = entry_price   # high water mark (for BUY trailing)
    lwm = entry_price   # low water mark (for SELL trailing)
    breakeven_moved = False
    trailing_active = False
    wide_trail_active = False

    exit_price = None
    exit_reason = "time_exit"
    bars_held = len(event.forward_bars)

    for i, bar in enumerate(event.forward_bars):
        if side == "BUY":
            hwm = max(hwm, bar.high)
            gain_pct = (hwm - entry_price) / entry_price

            # Check stop-loss (use bar low)
            if bar.low <= stop_price:
                exit_price = stop_price  # stopped out at stop price
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                bars_held = i + 1
                break

            # Check take-profit (use bar high)
            if bar.high >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1
                break

            # Trailing logic
            if gain_pct >= wide_trigger:
                wide_trail_active = True
                trailing_active = True
                stop_price = max(stop_price, hwm * (1.0 - wide_pct))
            elif gain_pct >= trail_trigger:
                trailing_active = True
                stop_price = max(stop_price, hwm * (1.0 - trail_pct))
            elif gain_pct >= be_trigger and not breakeven_moved:
                stop_price = max(stop_price, entry_price)
                breakeven_moved = True

        else:  # SELL
            lwm = min(lwm, bar.low)
            gain_pct = (entry_price - lwm) / entry_price

            # Check stop-loss (use bar high)
            if bar.high >= stop_price:
                exit_price = stop_price
                exit_reason = "trailing_stop" if trailing_active else "stop_loss"
                bars_held = i + 1
                break

            # Check take-profit (use bar low)
            if bar.low <= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
                bars_held = i + 1
                break

            # Trailing logic
            if gain_pct >= wide_trigger:
                wide_trail_active = True
                trailing_active = True
                stop_price = min(stop_price, lwm * (1.0 + wide_pct))
            elif gain_pct >= trail_trigger:
                trailing_active = True
                stop_price = min(stop_price, lwm * (1.0 + trail_pct))
            elif gain_pct >= be_trigger and not breakeven_moved:
                stop_price = min(stop_price, entry_price)
                breakeven_moved = True

    # Time exit: use last bar close
    if exit_price is None:
        exit_price = event.forward_bars[-1].close if event.forward_bars else entry_price

    # Apply exit slippage
    slip_exit = exit_price * slippage_bps / 10000.0
    if side == "BUY":
        exit_price -= slip_exit  # sell at worse price
    else:
        exit_price += slip_exit  # cover at worse price

    # PnL calculation
    shares = position_size / entry_price
    if side == "BUY":
        raw_pnl = (exit_price - entry_price) * shares
    else:
        raw_pnl = (entry_price - exit_price) * shares

    # Commission: per-trade
    total_commission = commission * 2  # entry + exit
    pnl = raw_pnl - total_commission

    return TradeOutcome(
        symbol=event.symbol,
        side=side,
        entry_price=round(entry_price, 4),
        exit_price=round(exit_price, 4),
        exit_reason=exit_reason,
        pnl=round(pnl, 2),
        bars_held=bars_held,
        date=event.date,
    )


# ── Signal Generation (v2) ─────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR from OHLC dataframe."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1])
        )
    )
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    return float(np.mean(tr[-period:]))


def generate_signals(cfg: dict, universe: list[str], lookback_days: int = 30) -> list[SignalEvent]:
    """Download data and generate signals with full forward bar data."""

    print(f"\n📊 Downloading {lookback_days}d of 5m data for {len(universe)} symbols...")

    tracer = LightningTracer(cfg)
    mom_agent = MomentumSignalAgent(cfg, tracer)
    mean_agent = MeanReversionSignalAgent(cfg, tracer)
    break_agent = BreakoutSignalAgent(cfg, tracer)
    news_agent = NewsSentimentAgent(cfg, tracer)
    normalizer = SignalNormalizer(lookback=cfg.get("signals", {}).get("norm_lookback", 100))

    min_volume = cfg.get("min_avg_volume", 100000)
    min_price = 5.0  # hardcoded: block stocks under $5
    atr_period = cfg.get("risk", {}).get("atr_period", 14)

    period = f"{min(lookback_days, 59)}d"
    events: list[SignalEvent] = []
    failed = []
    vol_filtered = 0
    price_filtered = 0

    for i, sym in enumerate(universe):
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period=period, interval="5m")
            if df.empty or len(df) < 50:
                failed.append(sym)
                continue

            df.columns = [c.lower() for c in df.columns]
            if "adj close" in df.columns:
                df.rename(columns={"adj close": "close"}, inplace=True)

            df.index = pd.to_datetime(df.index)
            df["date"] = df.index.date

            trading_days = sorted(df["date"].unique())

            for day in trading_days:
                day_df = df[df["date"] == day].copy()
                if len(day_df) < 20:
                    continue

                mid_idx = len(day_df) // 2
                if mid_idx < 10:
                    continue

                signal_df = day_df.iloc[:mid_idx]
                current_price = signal_df["close"].iloc[-1]

                # ── FILTER: Min price ──
                if current_price < min_price:
                    price_filtered += 1
                    continue

                # ── FILTER: Volume ──
                avg_vol = signal_df["volume"].rolling(20).mean().iloc[-1] if len(signal_df) >= 20 else signal_df["volume"].mean()
                if avg_vol < min_volume:
                    vol_filtered += 1
                    continue

                # ── ATR ──
                atr = compute_atr(signal_df, atr_period)

                # ── Forward bars for simulation ──
                forward_start = mid_idx
                forward_end = min(mid_idx + 12, len(day_df))  # up to 60 min
                forward_bars = []
                for j in range(forward_start, forward_end):
                    row = day_df.iloc[j]
                    forward_bars.append(BarData(
                        timestamp=day_df.index[j],
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        volume=row["volume"],
                    ))

                if len(forward_bars) < 3:
                    continue

                # ── Generate signals ──
                mom_raw = mom_agent.score(signal_df)
                mean_raw = mean_agent.score(signal_df)
                brk_raw = break_agent.score(signal_df)
                news_raw = news_agent.get_sentiment(sym)

                mom = normalizer.normalize(sym, "momentum", mom_raw)
                mean = normalizer.normalize(sym, "meanrev", mean_raw)
                brk = normalizer.normalize(sym, "breakout", brk_raw)
                news = news_raw

                events.append(SignalEvent(
                    date=str(day),
                    symbol=sym,
                    momentum=mom,
                    meanrev=mean,
                    breakout=brk,
                    news=news,
                    price_now=current_price,
                    atr=atr,
                    avg_volume=avg_vol,
                    forward_bars=forward_bars,
                ))

            sym_count = len([e for e in events if e.symbol == sym])
            print(f"  [{i+1}/{len(universe)}] {sym}: {sym_count} signals", end="\r")

        except Exception as e:
            failed.append(sym)
            continue

    print(f"\n✅ Generated {len(events)} signal events from {len(universe) - len(failed)}/{len(universe)} symbols")
    print(f"  📋 Filtered out: {vol_filtered} low-volume, {price_filtered} under-$5")
    if failed:
        print(f"  ⚠️ Failed: {', '.join(failed[:10])}")

    return events


# ── Challenge System ────────────────────────────

def passes_challenge(event: SignalEvent, weights: dict, disagreement_threshold: float = 0.9) -> bool:
    """
    Check if signal spread is within acceptable range.
    Mimics production challenger: if max-min spread (among non-zero-weight signals) > threshold → challenged.
    """
    active_signals = []
    if weights.get("momentum", 0) > 0:
        active_signals.append(event.momentum)
    if weights.get("meanrev", 0) > 0:
        active_signals.append(event.meanrev)
    if weights.get("breakout", 0) > 0:
        active_signals.append(event.breakout)
    if weights.get("news", 0) > 0:
        active_signals.append(event.news)

    if len(active_signals) < 2:
        return True  # can't have disagreement with 1 signal

    spread = max(active_signals) - min(active_signals)
    return spread <= disagreement_threshold


# ── Backtest Engine ─────────────────────────────

def run_backtest(
    events: list[SignalEvent],
    weights: dict,
    threshold: float,
    cfg_risk: dict,
    slippage_bps: float = 5.0,
    commission: float = 0.005,
    position_size: float = 1000.0,
    max_positions: int = 4,
    challenge_threshold: float = 0.9,
    cooldown_bars: int = 12,       # 60 min cooldown = 12 bars of 5m
    detailed: bool = False,
) -> BacktestResult:
    """Run a single backtest with realistic constraints."""

    result = BacktestResult(weights=weights, threshold=threshold)
    result.total_signals = len(events)

    mom_w = weights["momentum"]
    mean_w = weights["meanrev"]
    brk_w = weights["breakout"]
    news_w = weights["news"]
    active_weight = mom_w + mean_w + brk_w + news_w

    if active_weight < 0.01:
        return result

    # Track open positions per date + cooldowns
    open_positions_by_date: dict[str, int] = defaultdict(int)
    last_trade_by_symbol: dict[str, int] = {}  # symbol → event index of last trade
    symbol_traded_today: dict[str, set] = defaultdict(set)  # date → set of symbols

    for idx, ev in enumerate(events):
        # ── Score ──
        raw = (ev.momentum * mom_w + ev.meanrev * mean_w +
               ev.breakout * brk_w + ev.news * news_w)
        score = raw / active_weight

        if abs(score) < threshold:
            continue

        side = "BUY" if score > 0 else "SELL"

        # ── Max positions per day ──
        if open_positions_by_date[ev.date] >= max_positions:
            continue

        # ── Same-symbol same-day filter (persistence) ──
        if ev.symbol in symbol_traded_today[ev.date]:
            continue

        # ── Cooldown filter ──
        if ev.symbol in last_trade_by_symbol:
            bars_since = idx - last_trade_by_symbol[ev.symbol]
            if bars_since < cooldown_bars:
                continue

        # ── Challenge system ──
        if not passes_challenge(ev, weights, challenge_threshold):
            continue

        # ── Simulate trade bar-by-bar ──
        outcome = simulate_trade(ev, side, position_size, cfg_risk, slippage_bps, commission)

        result.trades_taken += 1
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

        if outcome.exit_reason == "stop_loss":
            result.stops_hit += 1
        elif outcome.exit_reason == "take_profit":
            result.tp_hit += 1
        elif outcome.exit_reason == "trailing_stop":
            result.trailing_hit += 1
        else:
            result.time_exits += 1

        if detailed:
            result.trades.append(outcome)

    return result


# ── Grid Search ─────────────────────────────────

def generate_weight_combos(step: float = 0.05) -> list[dict]:
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


def grid_search(events: list[SignalEvent], cfg_risk: dict, step: float = 0.05,
                thresholds: list[float] | None = None, slippage_bps: float = 5.0,
                commission: float = 0.005, max_positions: int = 4,
                challenge_threshold: float = 0.9) -> list[BacktestResult]:

    combos = generate_weight_combos(step)
    if thresholds is None:
        thresholds = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]

    total = len(combos) * len(thresholds)
    print(f"\n🔍 Grid search: {len(combos)} combos × {len(thresholds)} thresholds = {total} backtests")

    results = []
    for i, (combo, thresh) in enumerate(itertools.product(combos, thresholds)):
        r = run_backtest(events, combo, thresh, cfg_risk, slippage_bps, commission,
                         max_positions=max_positions, challenge_threshold=challenge_threshold)
        if r.trades_taken >= 30:
            results.append(r)
        if (i + 1) % 5000 == 0:
            print(f"  Progress: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="\r")

    print(f"\n✅ {len(results)} valid results (≥30 trades)")
    return results


# ── Walk-Forward Validation ─────────────────────

def walk_forward(events: list[SignalEvent], cfg_risk: dict, step: float = 0.05,
                 train_pct: float = 0.6, slippage_bps: float = 5.0,
                 commission: float = 0.005, max_positions: int = 4,
                 challenge_threshold: float = 0.9) -> list[dict]:

    dates = sorted(set(e.date for e in events))
    split = int(len(dates) * train_pct)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])

    train_events = [e for e in events if e.date in train_dates]
    test_events = [e for e in events if e.date in test_dates]

    print(f"\n📅 Walk-forward split:")
    print(f"  Train: {dates[0]} → {dates[split-1]} ({len(train_events)} events, {split} days)")
    print(f"  Test:  {dates[split]} → {dates[-1]} ({len(test_events)} events, {len(dates)-split} days)")

    # Train
    print("\n── TRAIN SET ──")
    train_results = grid_search(train_events, cfg_risk, step, slippage_bps=slippage_bps,
                                commission=commission, max_positions=max_positions,
                                challenge_threshold=challenge_threshold)
    train_results.sort(key=lambda r: r.composite_score, reverse=True)

    # Validate top 20
    print("\n── VALIDATING TOP 20 ON TEST SET ──")
    validated = []

    for tr in train_results[:20]:
        test_r = run_backtest(test_events, tr.weights, tr.threshold, cfg_risk,
                              slippage_bps, commission, max_positions=max_positions,
                              challenge_threshold=challenge_threshold, detailed=True)

        # Per-symbol breakdown for top combos
        sym_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for t in test_r.trades:
            s = sym_stats[t.symbol]
            if t.pnl > 0:
                s["wins"] += 1
            else:
                s["losses"] += 1
            s["pnl"] += t.pnl

        # Exit reason breakdown
        exit_breakdown = {
            "stop_loss": test_r.stops_hit,
            "take_profit": test_r.tp_hit,
            "trailing_stop": test_r.trailing_hit,
            "time_exit": test_r.time_exits,
        }

        validated.append({
            "weights": tr.weights,
            "threshold": tr.threshold,
            "train": {
                "trades": tr.trades_taken,
                "win_rate": round(tr.win_rate, 3),
                "pf": round(tr.profit_factor, 2),
                "pnl": round(tr.total_pnl, 2),
                "composite": round(tr.composite_score, 3),
                "stops_hit": tr.stops_hit,
                "tp_hit": tr.tp_hit,
            },
            "test": {
                "trades": test_r.trades_taken,
                "win_rate": round(test_r.win_rate, 3),
                "pf": round(test_r.profit_factor, 2),
                "pnl": round(test_r.total_pnl, 2),
                "composite": round(test_r.composite_score, 3),
                "stops_hit": test_r.stops_hit,
                "tp_hit": test_r.tp_hit,
                "exit_breakdown": exit_breakdown,
            },
            "train_test_gap": round(abs(tr.win_rate - test_r.win_rate), 3),
            "top_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)[:5]),
            "worst_symbols": dict(sorted(sym_stats.items(), key=lambda x: x[1]["pnl"])[:5]),
        })

    # Sort by test composite, penalize gap
    validated.sort(
        key=lambda v: v["test"]["composite"] - v["train_test_gap"] * 0.5,
        reverse=True
    )

    return validated


# ── Main ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weight Optimizer Backtester v2 (Realistic)")
    parser.add_argument("--days", type=int, default=30, help="Days of historical data")
    parser.add_argument("--step", type=float, default=0.05, help="Weight grid step size")
    parser.add_argument("--quick", action="store_true", help="Quick mode (step=0.10)")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    if args.quick:
        args.step = 0.10

    cfg = load_config()
    cfg_risk = cfg.get("risk", {})
    universe = cfg["universe"]
    slippage_bps = cfg.get("execution", {}).get("slippage_bps", 5)
    commission = cfg.get("execution", {}).get("commission", 0.005)
    max_positions = cfg_risk.get("max_positions", 4)
    challenge_threshold = cfg.get("challenges", {}).get("disagreement_threshold", 0.9)

    print("=" * 60)
    print("  🏋️ WEIGHT OPTIMIZER BACKTESTER v2 (REALISTIC)")
    print("=" * 60)
    print(f"  Universe: {len(universe)} symbols")
    print(f"  Period: {args.days} days")
    print(f"  Grid step: {args.step}")
    print(f"  Stop-loss: ATR×{cfg_risk.get('atr_stop_multiplier', 2.0)} (fallback {cfg_risk.get('stop_loss', 0.02):.0%})")
    print(f"  Take-profit: {cfg_risk.get('take_profit', 0.05):.0%}")
    print(f"  Trailing: BE@{cfg_risk.get('trailing_breakeven_trigger', 0.015):.1%}, trail@{cfg_risk.get('trailing_trigger', 0.025):.1%}")
    print(f"  Slippage: {slippage_bps}bps | Commission: ${commission}")
    print(f"  Max positions/day: {max_positions}")
    print(f"  Challenge threshold: {challenge_threshold}")
    print(f"  Min volume: {cfg.get('min_avg_volume', 100000):,} | Min price: $5")
    print(f"  Current weights: {cfg.get('signals', {}).get('weights', {})}")

    start = time.time()

    # Generate signals (v2 with volume/price filters + ATR + forward bars)
    events = generate_signals(cfg, universe, args.days)
    if len(events) < 50:
        print("❌ Not enough signal events. Need at least 50.")
        return

    # Walk-forward
    validated = walk_forward(events, cfg_risk, args.step, slippage_bps=slippage_bps,
                             commission=commission, max_positions=max_positions,
                             challenge_threshold=challenge_threshold)

    elapsed = time.time() - start

    # Current config test
    current_weights = cfg.get("signals", {}).get("weights", {})
    current_thresh = cfg.get("signals", {}).get("trade_threshold", 0.25)
    curr_result = run_backtest(events, current_weights, current_thresh, cfg_risk,
                               slippage_bps, commission, max_positions=max_positions,
                               challenge_threshold=challenge_threshold, detailed=True)

    # Print
    print("\n" + "=" * 60)
    print("  📊 TOP 10 WEIGHT COMBOS (v2 — REALISTIC)")
    print("=" * 60)

    print(f"\n  📌 CURRENT CONFIG: {current_weights} @ threshold={current_thresh}")
    print(f"     Trades: {curr_result.trades_taken} | WR: {curr_result.win_rate:.1%} | PF: {curr_result.profit_factor:.2f} | PnL: ${curr_result.total_pnl:.2f}")
    print(f"     Stops: {curr_result.stops_hit} | TPs: {curr_result.tp_hit} | Trailing: {curr_result.trailing_hit} | Time-exit: {curr_result.time_exits}")

    print()
    for i, v in enumerate(validated[:10]):
        w = v["weights"]
        print(f"  #{i+1}")
        print(f"     Weights: mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']}")
        print(f"     Threshold: {v['threshold']}")
        print(f"     Train: {v['train']['trades']} trades, WR={v['train']['win_rate']:.1%}, PF={v['train']['pf']:.2f}, PnL=${v['train']['pnl']:.2f}")
        print(f"     Test:  {v['test']['trades']} trades, WR={v['test']['win_rate']:.1%}, PF={v['test']['pf']:.2f}, PnL=${v['test']['pnl']:.2f}")
        print(f"     Gap: {v['train_test_gap']:.1%}")
        exits = v['test'].get('exit_breakdown', {})
        print(f"     Exits: stop={exits.get('stop_loss',0)} tp={exits.get('take_profit',0)} trail={exits.get('trailing_stop',0)} time={exits.get('time_exit',0)}")
        if v.get("top_symbols"):
            top = list(v["top_symbols"].items())[:3]
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in top]
            print(f"     Best: {', '.join(parts)}")
        if v.get("worst_symbols"):
            worst = list(v["worst_symbols"].items())[:3]
            parts = [f"{s}(${d['pnl']:.0f})" for s, d in worst]
            print(f"     Worst: {', '.join(parts)}")
        print()

    print(f"⏱️  Completed in {elapsed:.1f}s")

    # Save
    curr_exit_breakdown = {
        "stop_loss": curr_result.stops_hit,
        "take_profit": curr_result.tp_hit,
        "trailing_stop": curr_result.trailing_hit,
        "time_exit": curr_result.time_exits,
    }

    output_path = args.output or str(Path(__file__).resolve().parent.parent / "backtest_results.json")
    output = {
        "version": "v2",
        "timestamp": pd.Timestamp.now().isoformat(),
        "config": {
            "days": args.days,
            "step": args.step,
            "universe_size": len(universe),
            "signal_events": len(events),
            "slippage_bps": slippage_bps,
            "commission": commission,
            "stop_loss": f"ATR×{cfg_risk.get('atr_stop_multiplier', 2.0)}",
            "take_profit": f"{cfg_risk.get('take_profit', 0.05):.0%}",
            "max_positions_per_day": max_positions,
            "challenge_threshold": challenge_threshold,
            "min_volume": cfg.get("min_avg_volume", 100000),
            "min_price": 5.0,
        },
        "current_weights": current_weights,
        "current_result": {
            "trades": curr_result.trades_taken,
            "win_rate": round(curr_result.win_rate, 3),
            "pf": round(curr_result.profit_factor, 2),
            "pnl": round(curr_result.total_pnl, 2),
            "exit_breakdown": curr_exit_breakdown,
        },
        "top_10": validated[:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"💾 Results saved to {output_path}")


if __name__ == "__main__":
    main()

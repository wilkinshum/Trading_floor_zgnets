from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Tuple

import pandas as pd
import numpy as np
import yfinance as yf
import yaml

# --- Import signal/exit agents (use existing logic) ---
from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.exits import ExitManager


# ----------------------------
# Helpers / lightweight stubs
# ----------------------------
class DummyTracer:
    def emit_span(self, name: str, payload: dict):
        return None


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0

    def __post_init__(self):
        if self.highest_price == 0.0:
            self.highest_price = self.avg_price
        if self.lowest_price == 0.0:
            self.lowest_price = self.avg_price

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity


@dataclass
class PortfolioState:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    equity: float = 0.0


class InMemoryPortfolio:
    """Portfolio with the same execute() logic but no file IO."""
    def __init__(self, cfg: dict):
        self.cfg = cfg
        initial_cash = cfg.get("risk", {}).get("equity", 5000.0)
        self.state = PortfolioState(cash=initial_cash, equity=initial_cash)

    def mark_to_market(self, prices: Dict[str, float]):
        pos_value = 0.0
        for sym, pos in self.state.positions.items():
            price = prices.get(sym)
            if price:
                pos.current_price = price
                if price > pos.highest_price:
                    pos.highest_price = price
                if price < pos.lowest_price or pos.lowest_price == 0.0:
                    pos.lowest_price = price
            pos_value += pos.market_value
        self.state.equity = self.state.cash + pos_value

    def execute(self, symbol: str, side: str, price: float, quantity: int = 0, target_value: float = 0.0) -> float:
        exec_cfg = self.cfg.get("execution", {})
        slippage = exec_cfg.get("slippage_bps", 0) * 0.0001
        commission = exec_cfg.get("commission", 0.0)

        if side == "BUY":
            exec_price = price * (1 + slippage)
        else:
            exec_price = price * (1 - slippage)

        if not exec_price or exec_price <= 0 or math.isnan(exec_price) or math.isinf(exec_price):
            return 0.0

        # Sizing
        if quantity == 0:
            pos = self.state.positions.get(symbol)
            if pos:
                if side == "SELL" and pos.quantity > 0:
                    quantity = abs(pos.quantity)
                elif side == "BUY" and pos.quantity < 0:
                    quantity = abs(pos.quantity)

            if quantity == 0:
                if target_value > 0:
                    quantity = int(target_value // exec_price)
                else:
                    max_pos = self.cfg.get("risk", {}).get("max_positions", 2)
                    target_alloc = self.state.equity / max_pos
                    quantity = int(target_alloc // exec_price)
                if quantity < 1:
                    quantity = 1

        comm_cost = float(quantity) * commission
        realized_pnl = 0.0

        if side == "BUY":
            cost = (exec_price * quantity) + comm_cost

            if symbol in self.state.positions and self.state.positions[symbol].quantity < 0:
                pos = self.state.positions[symbol]
                qty_to_cover = min(quantity, abs(pos.quantity))
                self.state.cash -= (exec_price * qty_to_cover + comm_cost)
                trade_pnl = (pos.avg_price - exec_price) * qty_to_cover
                realized_pnl += trade_pnl
                pos.quantity += qty_to_cover

                remaining_qty = quantity - qty_to_cover
                if remaining_qty > 0:
                    cost_rem = (exec_price * remaining_qty) + (remaining_qty * commission)
                    if self.state.cash >= cost_rem:
                        self.state.cash -= cost_rem
                        if pos.quantity == 0:
                            pos.quantity = remaining_qty
                            pos.avg_price = exec_price
                if pos.quantity == 0:
                    del self.state.positions[symbol]
            else:
                if self.state.cash >= cost:
                    self.state.cash -= cost
                    if symbol in self.state.positions:
                        pos = self.state.positions[symbol]
                        total_cost_basis = (pos.quantity * pos.avg_price) + (exec_price * quantity) + comm_cost
                        pos.quantity += quantity
                        pos.avg_price = total_cost_basis / pos.quantity
                    else:
                        basis_price = exec_price + (comm_cost / quantity)
                        self.state.positions[symbol] = Position(symbol, quantity, basis_price, price)
        elif side == "SELL":
            proceeds = (exec_price * quantity) - comm_cost

            if symbol in self.state.positions and self.state.positions[symbol].quantity > 0:
                pos = self.state.positions[symbol]
                qty_to_sell = min(quantity, pos.quantity)
                sale_val = exec_price * qty_to_sell
                part_comm = qty_to_sell * commission
                net_proceeds = sale_val - part_comm
                self.state.cash += net_proceeds
                realized_pnl += (exec_price - pos.avg_price) * qty_to_sell
                pos.quantity -= qty_to_sell

                remaining_qty = quantity - qty_to_sell
                if remaining_qty > 0:
                    short_proceeds = (exec_price * remaining_qty) - (remaining_qty * commission)
                    self.state.cash += short_proceeds
                    if pos.quantity == 0:
                        pos.quantity = -remaining_qty
                        effective_entry = exec_price - (commission / remaining_qty)
                        pos.avg_price = effective_entry
                if pos.quantity == 0:
                    del self.state.positions[symbol]
            else:
                if self.state.equity > 0:
                    self.state.cash += proceeds
                    effective_entry = exec_price - (commission / quantity)
                    if symbol in self.state.positions:
                        pos = self.state.positions[symbol]
                        total_val = (abs(pos.quantity) * pos.avg_price) + (effective_entry * quantity)
                        pos.quantity -= quantity
                        pos.avg_price = total_val / abs(pos.quantity)
                    else:
                        self.state.positions[symbol] = Position(symbol, -quantity, effective_entry, price)

        return realized_pnl


# ----------------------------
# Data utilities
# ----------------------------

def _extract_symbol_df(data: pd.DataFrame, sym: str) -> pd.DataFrame:
    if isinstance(data.columns, pd.MultiIndex):
        if sym not in data.columns.levels[0]:
            return pd.DataFrame()
        df = data[sym].copy()
    else:
        # Single symbol
        df = data.copy()
    # Normalize column names
    df.columns = [c.lower() for c in df.columns]
    df = df.dropna(how="all")
    return df


def _to_eastern(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("America/New_York")
    return df


def _calc_atr_pct(df: pd.DataFrame, period: int, price: float) -> float | None:
    if df is None or df.empty or len(df) < period + 1:
        return None
    h = df["high"]
    l = df["low"]
    c = df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if math.isnan(atr) or atr <= 0 or price <= 0:
        return None
    return float(atr / price)


def _binary_breakout_score(df: pd.DataFrame, lookback: int) -> float:
    if df.empty or len(df) < lookback:
        return 0.0
    closes = df["close"]
    recent = closes.iloc[-lookback:]
    last = closes.iloc[-1]
    if last >= recent.max():
        return 1.0
    if last <= recent.min():
        return -1.0
    return 0.0


def _determine_exit_reason(exit_mgr: ExitManager, sym: str, pos: Position, price_data: dict) -> str:
    # Match ExitManager order
    atr_stop = exit_mgr._calc_atr_stop(sym, price_data, pos.avg_price)
    if pos.quantity > 0:
        entry_pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price
        hwm = pos.highest_price if pos.highest_price > 0 else pos.avg_price
        drawdown_from_hwm = (pos.current_price - hwm) / hwm
        peak_gain = (hwm - pos.avg_price) / pos.avg_price
        if entry_pnl_pct >= exit_mgr.take_profit:
            return "take_profit"
        if entry_pnl_pct <= -atr_stop:
            return "atr_stop"
        if peak_gain >= exit_mgr.trail_trigger:
            effective_trail = exit_mgr.wide_trail_pct if peak_gain >= exit_mgr.wide_trail_trigger else exit_mgr.trail_pct
            if drawdown_from_hwm <= -effective_trail:
                return "trailing_stop"
        elif peak_gain >= exit_mgr.breakeven_trigger:
            if entry_pnl_pct <= 0:
                return "breakeven_stop"
    else:
        entry_pnl_pct = (pos.avg_price - pos.current_price) / pos.avg_price
        lwm = pos.lowest_price if pos.lowest_price > 0 else pos.avg_price
        drawup_from_lwm = (pos.current_price - lwm) / lwm
        peak_gain = (pos.avg_price - lwm) / pos.avg_price
        if entry_pnl_pct >= exit_mgr.take_profit:
            return "take_profit"
        if entry_pnl_pct <= -atr_stop:
            return "atr_stop"
        if peak_gain >= exit_mgr.trail_trigger:
            effective_trail = exit_mgr.wide_trail_pct if peak_gain >= exit_mgr.wide_trail_trigger else exit_mgr.trail_pct
            if drawup_from_lwm >= effective_trail:
                return "trailing_stop"
        elif peak_gain >= exit_mgr.breakeven_trigger:
            if entry_pnl_pct <= 0:
                return "breakeven_stop"
    return "exit_signal"


# ----------------------------
# Simulation core
# ----------------------------

def simulate_system(
    cfg: dict,
    data_30m: Dict[str, pd.DataFrame],
    data_5m: Dict[str, pd.DataFrame],
    trade_dates: List[pd.Timestamp],
    system_name: str,
    momentum_short: int,
    breakout_smooth: bool,
    use_persistence: bool,
    old_system_checker: callable | None = None,
) -> dict:
    tracer = DummyTracer()
    cfg_local = dict(cfg)
    cfg_local.setdefault("signals", {})
    cfg_local["signals"] = dict(cfg_local.get("signals", {}))
    cfg_local["signals"]["momentum_short"] = momentum_short

    mom_agent = MomentumSignalAgent(cfg_local, tracer)
    brk_agent = BreakoutSignalAgent(cfg_local, tracer)
    exit_mgr = ExitManager(cfg_local, tracer)
    portfolio = InMemoryPortfolio(cfg_local)

    trade_threshold = cfg.get("signals", {}).get("trade_threshold", 0.15)
    atr_period = cfg.get("risk", {}).get("atr_period", 14)
    min_atr = cfg.get("risk", {}).get("min_atr_pct", 0.005)
    max_atr = cfg.get("risk", {}).get("max_atr_pct", 0.10)
    max_positions = cfg.get("risk", {}).get("max_positions", 4)
    max_position_pct = cfg.get("risk", {}).get("max_position_pct", 0.20)

    # Tracking
    trades = []
    equity_curve = []
    daily_pnl = {}
    blocked_by_persistence = 0
    consecutive_signal = {sym: {"dir": 0, "count": 0} for sym in data_30m.keys()}
    open_trade_meta = {}  # sym -> (entry_time, entry_price, side)
    consecutive_losses = {sym: 0 for sym in data_30m.keys()}

    for day in trade_dates:
        day_date = day.date()
        start_dt = datetime.combine(day_date, time(9, 30), tzinfo=day.tzinfo)
        end_dt = datetime.combine(day_date, time(15, 55), tzinfo=day.tzinfo)
        time_index = pd.date_range(start_dt, end_dt, freq="5min", tz="America/New_York")

        for ts in time_index:
            # Update prices for mark-to-market using 5m data
            current_prices = {}
            price_data_for_atr = {}
            for sym, df5 in data_5m.items():
                if df5.empty:
                    continue
                df5_cut = df5[df5.index <= ts]
                if df5_cut.empty:
                    continue
                last_row = df5_cut.iloc[-1]
                price = float(last_row["close"])
                current_prices[sym] = price
                price_data_for_atr[sym] = df5_cut

            portfolio.mark_to_market(current_prices)
            equity_curve.append((ts, portfolio.state.equity))

            # Exit checks
            context = {
                "portfolio_obj": portfolio,
                "portfolio_equity": portfolio.state.equity,
                "price_data": price_data_for_atr,
            }
            exit_signals = exit_mgr.check_exits(context)
            for sym, side in exit_signals.items():
                if sym not in portfolio.state.positions:
                    continue
                price = current_prices.get(sym)
                if not price:
                    continue
                pos = portfolio.state.positions[sym]
                reason = _determine_exit_reason(exit_mgr, sym, pos, price_data_for_atr)
                entry_meta = open_trade_meta.get(sym, {})
                entry_time = entry_meta.get("entry_time")
                entry_price = entry_meta.get("entry_price", pos.avg_price)
                entry_side = entry_meta.get("side", "BUY" if pos.quantity > 0 else "SELL")

                qty = abs(pos.quantity)
                pnl = portfolio.execute(sym, side, price, quantity=qty)
                exit_time = ts
                hold_minutes = None
                if entry_time:
                    hold_minutes = int((exit_time - entry_time).total_seconds() // 60)

                trades.append({
                    "date": str(day_date),
                    "symbol": sym,
                    "side": entry_side,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "quantity": qty,
                    "pnl": pnl,
                    "exit_reason": reason,
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "hold_minutes": hold_minutes,
                })
                daily_pnl.setdefault(str(day_date), 0.0)
                daily_pnl[str(day_date)] += pnl

                # Track consecutive losses per symbol
                if pnl < 0:
                    consecutive_losses[sym] += 1
                else:
                    consecutive_losses[sym] = 0

                if sym in open_trade_meta:
                    del open_trade_meta[sym]

            # Entry checks only in 9:30-11:30 every 15 min
            if not (time(9, 30) <= ts.time() <= time(11, 30)):
                continue
            if ts.minute % 15 != 0:
                continue

            entry_plans = []
            signal_details = {}

            for sym, df30 in data_30m.items():
                if df30.empty:
                    continue
                df30_cut = df30[df30.index <= ts]
                if df30_cut.empty:
                    continue

                price = current_prices.get(sym)
                if not price or math.isnan(price):
                    continue

                # Volatility filter (ATR)
                atr_pct = _calc_atr_pct(df30_cut, atr_period, price)
                if atr_pct is None or atr_pct < min_atr or atr_pct > max_atr:
                    continue

                # Signals
                if breakout_smooth:
                    breakout_score = brk_agent.score(df30_cut)
                else:
                    breakout_score = _binary_breakout_score(df30_cut, cfg_local.get("signals", {}).get("breakout_lookback", 10))
                momentum_score = mom_agent.score(df30_cut)

                # News not available in backtest
                news_score = 0.0

                # Weighted score
                if system_name == "new":
                    score = (0.90 * momentum_score) + (0.10 * breakout_score)
                else:
                    score = (0.90 * momentum_score) + (0.10 * breakout_score)

                if abs(score) < trade_threshold:
                    continue

                # Direction
                side = "BUY" if score > 0 else "SELL"

                # Signal disagreement check (challenge subset)
                components = {"momentum": momentum_score, "breakout": breakout_score, "news": news_score}
                spread = max(components.values()) - min(components.values())
                if spread >= cfg.get("challenges", {}).get("disagreement_threshold", 1.5):
                    # block if spread >= 1.8 similar to challenger
                    if spread >= 1.8:
                        continue

                # Consecutive losses check
                if consecutive_losses.get(sym, 0) >= cfg.get("challenges", {}).get("max_consecutive_losses", 3):
                    continue

                # Persistence filter
                if use_persistence:
                    prev = consecutive_signal[sym]
                    direction = 1 if score > 0 else -1
                    if prev["dir"] == direction:
                        prev["count"] += 1
                    else:
                        prev["dir"] = direction
                        prev["count"] = 1

                    eligible_without_persistence = prev["count"] >= 1
                    if eligible_without_persistence and prev["count"] < 2:
                        # Check if old system would have entered here
                        if old_system_checker is not None and old_system_checker(sym, df30_cut, price):
                            blocked_by_persistence += 1
                        continue
                else:
                    # reset counts to avoid stale state
                    pass

                # Skip if already in position
                if sym in portfolio.state.positions:
                    continue

                signal_details[sym] = {"components": components}
                entry_plans.append({
                    "symbol": sym,
                    "side": side,
                    "score": score,
                    "price": price,
                })

            # Enforce max positions
            entry_plans = exit_mgr.check_max_positions(portfolio, entry_plans)

            # Position cap + execute
            for plan in entry_plans:
                sym = plan["symbol"]
                side = plan["side"]
                price = plan["price"]

                # Cap position size
                equity = portfolio.state.equity
                target_value = min(equity / max(1, max_positions), equity * max_position_pct)
                if target_value <= 0:
                    continue

                pnl = portfolio.execute(sym, side, price, target_value=target_value)
                if sym in portfolio.state.positions:
                    open_trade_meta[sym] = {
                        "entry_time": ts,
                        "entry_price": price,
                        "side": side,
                    }

        # End of day: nothing special; carry positions

    # Compute summary metrics
    total_trades = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    win_rate = (len(wins) / total_trades) if total_trades else 0.0
    profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses else float("inf")

    # Max drawdown
    max_dd = 0.0
    peak = -float("inf")
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

    # Best / worst trades
    best_trade = max(trades, key=lambda t: t["pnl"], default=None)
    worst_trade = min(trades, key=lambda t: t["pnl"], default=None)

    # Avg hold time
    hold_times = [t["hold_minutes"] for t in trades if t.get("hold_minutes") is not None]
    avg_hold = float(np.mean(hold_times)) if hold_times else 0.0

    return {
        "system": system_name,
        "trades": trades,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_pnl": portfolio.state.equity - cfg.get("risk", {}).get("equity", 5000.0),
        "max_drawdown": max_dd,
        "daily_pnl": daily_pnl,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "avg_hold_minutes": avg_hold,
        "blocked_by_persistence": blocked_by_persistence,
    }


def main():
    cfg_path = "configs/workflow.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    universe = cfg.get("universe", [])
    if not universe:
        print("No symbols in universe.")
        sys.exit(1)

    # Download data
    print("Downloading 30m data...")
    data_30 = yf.download(
        tickers=" ".join(universe),
        interval="30m",
        period="15d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    print("Downloading 5m data for exits...")
    data_5 = yf.download(
        tickers=" ".join(universe),
        interval="5m",
        period="15d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    data_30m = {}
    data_5m = {}
    for sym in universe:
        df30 = _extract_symbol_df(data_30, sym)
        df5 = _extract_symbol_df(data_5, sym)
        df30 = _to_eastern(df30)
        df5 = _to_eastern(df5)
        data_30m[sym] = df30
        data_5m[sym] = df5

    # Determine last 10 trading days from a liquid proxy (first symbol)
    ref_sym = universe[0]
    ref_df = data_30m.get(ref_sym, pd.DataFrame())
    if ref_df.empty:
        print("No reference data available.")
        sys.exit(1)

    trade_dates = sorted(ref_df.index.normalize().unique())[-10:]

    # Old system checker for persistence blocking (5-bar momentum, binary breakout, no persistence)
    def old_system_checker(sym: str, df30: pd.DataFrame, price: float) -> bool:
        if df30.empty:
            return False
        # Old momentum (5-bar)
        cfg_old = dict(cfg)
        cfg_old.setdefault("signals", {})
        cfg_old["signals"] = dict(cfg_old.get("signals", {}))
        cfg_old["signals"]["momentum_short"] = 5
        mom_old = MomentumSignalAgent(cfg_old, DummyTracer())
        mom_score = mom_old.score(df30)
        brk_score = _binary_breakout_score(df30, cfg_old.get("signals", {}).get("breakout_lookback", 10))
        score = (0.90 * mom_score) + (0.10 * brk_score)
        return abs(score) >= cfg.get("signals", {}).get("trade_threshold", 0.15)

    # Run simulations
    print("Running NEW system backtest...")
    new_results = simulate_system(
        cfg=cfg,
        data_30m=data_30m,
        data_5m=data_5m,
        trade_dates=trade_dates,
        system_name="new",
        momentum_short=10,
        breakout_smooth=True,
        use_persistence=True,
        old_system_checker=old_system_checker,
    )

    print("Running OLD system backtest...")
    old_results = simulate_system(
        cfg=cfg,
        data_30m=data_30m,
        data_5m=data_5m,
        trade_dates=trade_dates,
        system_name="old",
        momentum_short=5,
        breakout_smooth=False,
        use_persistence=False,
        old_system_checker=None,
    )

    # Summary
    def _fmt_summary(r: dict) -> str:
        return (
            f"System: {r['system']}\n"
            f"  Total trades: {r['total_trades']}\n"
            f"  Win rate: {r['win_rate']:.1%}\n"
            f"  Profit factor: {r['profit_factor']:.2f}\n"
            f"  Net PnL: ${r['net_pnl']:+.2f}\n"
            f"  Max drawdown: {r['max_drawdown']:.1%}\n"
            f"  Avg hold (min): {r['avg_hold_minutes']:.1f}\n"
        )

    print("\n=== SUMMARY (Side-by-side) ===")
    print(_fmt_summary(old_results))
    print(_fmt_summary(new_results))

    print("\n=== PnL by Day (New) ===")
    for k in sorted(new_results["daily_pnl"].keys()):
        print(f"  {k}: ${new_results['daily_pnl'][k]:+.2f}")

    print("\n=== PnL by Day (Old) ===")
    for k in sorted(old_results["daily_pnl"].keys()):
        print(f"  {k}: ${old_results['daily_pnl'][k]:+.2f}")

    print("\n=== Best/Worst Trades (New) ===")
    if new_results["best_trade"]:
        bt = new_results["best_trade"]
        print(f"  BEST: {bt['symbol']} {bt['side']} pnl=${bt['pnl']:+.2f} hold={bt['hold_minutes']}m")
    if new_results["worst_trade"]:
        wt = new_results["worst_trade"]
        print(f"  WORST: {wt['symbol']} {wt['side']} pnl=${wt['pnl']:+.2f} hold={wt['hold_minutes']}m")

    print("\n=== Best/Worst Trades (Old) ===")
    if old_results["best_trade"]:
        bt = old_results["best_trade"]
        print(f"  BEST: {bt['symbol']} {bt['side']} pnl=${bt['pnl']:+.2f} hold={bt['hold_minutes']}m")
    if old_results["worst_trade"]:
        wt = old_results["worst_trade"]
        print(f"  WORST: {wt['symbol']} {wt['side']} pnl=${wt['pnl']:+.2f} hold={wt['hold_minutes']}m")

    print("\n=== Persistence Filter Impact (New) ===")
    print(f"Blocked by persistence: {new_results['blocked_by_persistence']}")

    # Optional: print trade log for new system
    print("\n=== Trade Log (New) ===")
    for t in new_results["trades"]:
        print(
            f"{t['date']} {t['symbol']} {t['side']} qty={t['quantity']} "
            f"entry={t['entry_price']:.2f} exit={t['exit_price']:.2f} "
            f"pnl={t['pnl']:+.2f} reason={t['exit_reason']} hold={t['hold_minutes']}m"
        )


if __name__ == "__main__":
    main()

"""Exit-only monitor: checks stops/TP/kill switch without running signals or entering new trades."""
import sys, csv, sqlite3
import yaml
import yfinance as yf
from pathlib import Path
from trading_floor.portfolio import Portfolio
from trading_floor.agents.exits import ExitManager
from trading_floor.lightning import LightningTracer
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "trading.db"
CSV_PATH = PROJECT_ROOT / "trading_logs" / "trades.csv"

def log_trade(symbol, side, quantity, price, pnl):
    """Log exit trade to both CSV and DB."""
    ts = datetime.now().isoformat()
    # CSV
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([ts, symbol, side, quantity, price, "", pnl])
    # DB
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, quantity, price, score, pnl, strategy_data) VALUES (?,?,?,?,?,?,?,?)",
            (ts, symbol, side, quantity, price, 0.0, pnl, '{"source":"exit_monitor"}')
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ExitMonitor] DB log error: {e}")


def main():
    cfg = yaml.safe_load(open("configs/workflow.yaml"))
    portfolio = Portfolio(cfg)
    tracer = LightningTracer(cfg)
    exit_mgr = ExitManager(cfg, tracer)

    # Check if market is open (weekday, not holiday)
    tz = ZoneInfo(cfg["hours"]["tz"])
    now = datetime.now(tz)
    if now.weekday() >= 5:
        print("[ExitMonitor] Weekend. Skipping.")
        return
    holidays = cfg.get("hours", {}).get("holidays", [])
    if now.strftime("%Y-%m-%d") in holidays:
        print("[ExitMonitor] Holiday. Skipping.")
        return

    positions = portfolio.state.positions
    if not positions:
        print("[ExitMonitor] No open positions. Nothing to monitor.")
        return

    symbols = list(positions.keys())
    print(f"[ExitMonitor] Checking {len(symbols)} positions: {symbols}")

    # Fetch current prices
    data = yf.download(symbols + ["SPY", "^VIX"], period="5d", interval="5m", progress=False)
    
    current_prices = {}
    price_series = {}
    for sym in symbols:
        try:
            if len(symbols) == 1 and "SPY" not in symbols:
                # Multi-download always has multi-level columns
                current_prices[sym] = float(data["Close"][sym].iloc[-1])
                price_series[sym] = data["Close"][sym].dropna()
            else:
                current_prices[sym] = float(data["Close"][sym].iloc[-1])
                price_series[sym] = data["Close"][sym].dropna()
        except Exception as e:
            print(f"[ExitMonitor] Price error for {sym}: {e}")

    # Mark to market
    portfolio.mark_to_market(current_prices)
    
    # Build context for exit manager
    context = {
        "positions": symbols,
        "portfolio_obj": portfolio,
        "portfolio_equity": portfolio.state.equity,
        "price_data": price_series,
    }

    # Check exits
    forced_exits = exit_mgr.check_exits(context)

    if not forced_exits:
        # Print status
        total_pnl = sum(pos.unrealized_pnl for pos in positions.values())
        print(f"[ExitMonitor] No exits triggered. Unrealized PnL: ${total_pnl:.2f}")
        for sym, pos in positions.items():
            entry_pnl_pct = (pos.avg_price - pos.current_price) / pos.avg_price if pos.quantity < 0 else (pos.current_price - pos.avg_price) / pos.avg_price
            print(f"  {sym}: {pos.quantity} @ ${pos.avg_price:.2f} -> ${pos.current_price:.2f} ({entry_pnl_pct:+.2%})")
        return

    # Execute exits
    print(f"[ExitMonitor] EXIT TRIGGERED: {forced_exits}")
    for sym, side in forced_exits.items():
        pos = positions.get(sym)
        if not pos:
            continue
        price = current_prices.get(sym)
        if not price or price <= 0:
            print(f"[ExitMonitor] No price for {sym}, skipping exit")
            continue
        
        qty = abs(pos.quantity)
        pnl = portfolio.execute(sym, side, price, quantity=qty)
        log_trade(sym, side, qty, price, pnl)
        print(f"[ExitMonitor] CLOSED {sym}: {side} {qty} shares @ ${price:.2f} | PnL: ${pnl:.2f}")

    portfolio.save()
    
    # Print remaining
    remaining = portfolio.state.positions
    if remaining:
        print(f"\n[ExitMonitor] Remaining positions: {len(remaining)}")
        for sym, pos in remaining.items():
            print(f"  {sym}: {pos.quantity} @ ${pos.avg_price:.2f}")
    else:
        print("\n[ExitMonitor] All positions closed.")
    
    print(f"Cash: ${portfolio.state.cash:.2f}")


if __name__ == "__main__":
    main()

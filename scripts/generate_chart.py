import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
from datetime import datetime

# Config
REPO_ROOT = Path(__file__).resolve().parents[1]
EVENTS_CSV = REPO_ROOT / "trading_logs" / "events.csv"
TRADES_CSV = REPO_ROOT / "trading_logs" / "trades.csv"
OUTPUT_IMG = REPO_ROOT / "web" / "chart_pnl.png"

def generate_chart():
    if not TRADES_CSV.exists():
        print("No trades.csv found.")
        return

    # Load trades
    trades = pd.read_csv(TRADES_CSV)
    if trades.empty:
        print("Trades file empty.")
        return
        
    trades['timestamp'] = pd.to_datetime(trades['timestamp'])
    trades = trades.sort_values('timestamp')

    # Calculate Cumulative PnL
    # Only realized PnL is in trades.csv? 
    # Yes, opening trades have 0.0 PnL.
    
    trades['cum_pnl'] = trades['pnl'].cumsum()
    
    # We want to plot Equity Curve.
    # Base Equity is 5000. 
    base_equity = 5000.0
    trades['equity'] = base_equity + trades['cum_pnl']

    # Setup Plot
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # Plot Equity Curve
    ax.plot(trades['timestamp'], trades['equity'], color='#00ff00', linewidth=2, label='Equity')
    
    # Highlight Trades?
    # Maybe scatter points for Buy vs Sell?
    # buys = trades[trades['side'] == 'BUY']
    # sells = trades[trades['side'] == 'SELL']
    # ax.scatter(buys['timestamp'], buys['equity'], color='cyan', marker='^', s=50, label='Buy')
    # ax.scatter(sells['timestamp'], sells['equity'], color='magenta', marker='v', s=50, label='Sell')

    # Formatting
    ax.set_title("Trading Floor Equity Curve", fontsize=16, color='white')
    ax.set_ylabel("Equity ($)", fontsize=12)
    ax.grid(True, linestyle='--', alpha=0.3)
    ax.legend()
    
    # Date formatting
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))
    plt.xticks(rotation=45)
    
    # Save
    OUTPUT_IMG.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(OUTPUT_IMG, dpi=100)
    print(f"Chart saved to {OUTPUT_IMG}")
    plt.close()

if __name__ == "__main__":
    generate_chart()

"""
FVG as Confirmation Filter Backtest
Tests whether FVG detection improves momentum/breakout signal quality.
Groups: (A) signals WITH FVG confirmation vs (B) signals WITHOUT.
"""
import yfinance as yf
import pandas as pd
import numpy as np
import os
from datetime import datetime

SLIPPAGE_BPS = 5
COMMISSION_PER_SHARE = 0.005
SHARES = 100
HOLD_BARS = 30  # hold for 30 minutes then exit
MOMENTUM_SHORT = 5
BREAKOUT_LOOKBACK = 10
FVG_LOOKBACK = 5  # how many bars back to look for FVG confirmation

UNIVERSE = ['SPY', 'QQQ', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META']


def get_data(ticker):
    tk = yf.Ticker(ticker)
    df = tk.history(period="1mo", interval="1m")
    if df.empty:
        df = tk.history(period="5d", interval="1m")
    if not df.empty:
        df.index = df.index.tz_convert("America/New_York")
    return df


def detect_fvg_at(df, idx):
    """Check if a bullish or bearish FVG exists at position idx (using idx-2, idx-1, idx)."""
    if idx < 2:
        return None
    c0 = df.iloc[idx - 2]
    c1 = df.iloc[idx - 1]
    c2 = df.iloc[idx]
    if c2['Low'] > c0['High']:
        return 'bullish'
    if c2['High'] < c0['Low']:
        return 'bearish'
    return None


def has_fvg_confirmation(df, pos, direction, lookback=FVG_LOOKBACK):
    """Check if there's an FVG in the same direction within lookback bars."""
    start = max(2, pos - lookback)
    for i in range(start, pos + 1):
        fvg = detect_fvg_at(df, i)
        if direction == 'long' and fvg == 'bullish':
            return True
        if direction == 'short' and fvg == 'bearish':
            return True
    return False


def generate_signals(df):
    """Generate momentum and breakout signals on 1-min bars during market hours."""
    signals = []
    trading = df.between_time('09:45', '15:30')  # skip first 15min, stop 30min before close
    if len(trading) < BREAKOUT_LOOKBACK + 1:
        return signals

    closes = trading['Close'].values
    dates = trading.index

    for i in range(BREAKOUT_LOOKBACK, len(trading)):
        # Momentum score: (close - SMA_short) / SMA_short
        sma = closes[i - MOMENTUM_SHORT:i].mean()
        if sma == 0:
            continue
        mom_score = (closes[i] - sma) / sma

        # Breakout score: (close - recent_high) / recent_high
        recent_high = closes[i - BREAKOUT_LOOKBACK:i].max()
        recent_low = closes[i - BREAKOUT_LOOKBACK:i].min()
        if recent_high == 0:
            continue
        brk_score = (closes[i] - recent_high) / recent_high

        # Signal: momentum > 0.1% AND breakout > 0 => long
        #         momentum < -0.1% AND close < recent_low => short
        direction = None
        strategy = None
        if mom_score > 0.001 and brk_score > 0:
            direction = 'long'
            strategy = 'momentum+breakout'
        elif mom_score < -0.001 and closes[i] < recent_low:
            direction = 'short'
            strategy = 'momentum+breakout'

        if direction:
            # Find this bar's position in the full df for FVG lookback
            full_pos = df.index.get_loc(dates[i])
            if isinstance(full_pos, slice):
                full_pos = full_pos.start
            signals.append({
                'time': dates[i],
                'price': closes[i],
                'direction': direction,
                'strategy': strategy,
                'mom_score': mom_score,
                'brk_score': brk_score,
                'full_pos': full_pos,
            })

    return signals


def simulate_trades(df, signals):
    """Simulate trades with fixed hold period."""
    trades = []
    # Throttle: max 1 trade per 60 bars to avoid overlapping
    last_trade_bar = -999

    for sig in signals:
        bar_idx = sig['full_pos']
        if bar_idx - last_trade_bar < 60:
            continue

        entry_raw = sig['price']
        direction = sig['direction']
        slip = entry_raw * SLIPPAGE_BPS / 10000

        if direction == 'long':
            entry_price = entry_raw + slip
        else:
            entry_price = entry_raw - slip

        # Exit after HOLD_BARS or end of day
        exit_idx = min(bar_idx + HOLD_BARS, len(df) - 1)
        # Don't hold past 15:55
        for j in range(bar_idx + 1, exit_idx + 1):
            if j >= len(df):
                break
            if df.index[j].hour == 15 and df.index[j].minute >= 55:
                exit_idx = j
                break
            # Also check if we crossed into next day
            if df.index[j].date() != df.index[bar_idx].date():
                exit_idx = j - 1
                break

        if exit_idx <= bar_idx:
            continue

        exit_raw = df.iloc[exit_idx]['Close']
        if direction == 'long':
            exit_price = exit_raw - exit_raw * SLIPPAGE_BPS / 10000
            pnl_per_share = exit_price - entry_price
        else:
            exit_price = exit_raw + exit_raw * SLIPPAGE_BPS / 10000
            pnl_per_share = entry_price - exit_price

        commission = COMMISSION_PER_SHARE * SHARES * 2
        net_pnl = pnl_per_share * SHARES - commission

        # Check FVG confirmation
        fvg_confirmed = has_fvg_confirmation(df, bar_idx, direction)

        trades.append({
            'time': str(sig['time']),
            'direction': direction,
            'entry_price': round(entry_price, 4),
            'exit_price': round(exit_price, 4),
            'net_pnl': round(net_pnl, 2),
            'fvg_confirmed': fvg_confirmed,
            'mom_score': round(sig['mom_score'], 6),
            'brk_score': round(sig['brk_score'], 6),
            'hold_bars': exit_idx - bar_idx,
        })
        last_trade_bar = bar_idx

    return trades


def compute_stats(trades_df, label=""):
    if trades_df.empty:
        return {'group': label, 'total_trades': 0}

    wins = trades_df[trades_df['net_pnl'] > 0]
    losses = trades_df[trades_df['net_pnl'] <= 0]
    gross_wins = wins['net_pnl'].sum() if len(wins) > 0 else 0
    gross_losses = abs(losses['net_pnl'].sum()) if len(losses) > 0 else 0

    cumulative = trades_df['net_pnl'].cumsum()
    peak = cumulative.cummax()
    max_dd = (cumulative - peak).min()

    return {
        'group': label,
        'total_trades': len(trades_df),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate_pct': round(len(wins) / len(trades_df) * 100, 1),
        'avg_pnl': round(trades_df['net_pnl'].mean(), 2),
        'total_pnl': round(trades_df['net_pnl'].sum(), 2),
        'profit_factor': round(gross_wins / gross_losses, 2) if gross_losses > 0 else float('inf'),
        'max_drawdown': round(max_dd, 2),
        'avg_winner': round(wins['net_pnl'].mean(), 2) if len(wins) > 0 else 0,
        'avg_loser': round(losses['net_pnl'].mean(), 2) if len(losses) > 0 else 0,
    }


def main():
    all_trades = []
    for ticker in UNIVERSE:
        print(f"Processing {ticker}...")
        df = get_data(ticker)
        if df.empty:
            print(f"  No data for {ticker}")
            continue
        print(f"  {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")

        signals = generate_signals(df)
        print(f"  {len(signals)} raw signals")

        trades = simulate_trades(df, signals)
        for t in trades:
            t['ticker'] = ticker
        all_trades.extend(trades)
        print(f"  {len(trades)} trades after throttling")

    if not all_trades:
        print("\nNo trades generated!")
        return

    df_trades = pd.DataFrame(all_trades)

    # Split into groups
    with_fvg = df_trades[df_trades['fvg_confirmed'] == True].reset_index(drop=True)
    without_fvg = df_trades[df_trades['fvg_confirmed'] == False].reset_index(drop=True)

    stats_all = compute_stats(df_trades, "ALL")
    stats_fvg = compute_stats(with_fvg, "WITH_FVG")
    stats_no_fvg = compute_stats(without_fvg, "WITHOUT_FVG")

    # Print results
    print(f"\n{'='*70}")
    print("  FVG FILTER BACKTEST RESULTS")
    print(f"{'='*70}")

    for s in [stats_all, stats_fvg, stats_no_fvg]:
        print(f"\n--- {s['group']} ---")
        for k, v in s.items():
            if k != 'group':
                print(f"  {k:20s}: {v}")

    # Delta
    if stats_fvg['total_trades'] > 0 and stats_no_fvg['total_trades'] > 0:
        print(f"\n--- DELTA (FVG - No FVG) ---")
        print(f"  Win Rate Delta    : {stats_fvg['win_rate_pct'] - stats_no_fvg['win_rate_pct']:+.1f}pp")
        print(f"  PF Delta          : {stats_fvg['profit_factor'] - stats_no_fvg['profit_factor']:+.2f}")
        print(f"  Avg PnL Delta     : ${stats_fvg['avg_pnl'] - stats_no_fvg['avg_pnl']:+.2f}")

    # Save
    out_dir = r"C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets\data"
    os.makedirs(out_dir, exist_ok=True)

    df_trades.to_csv(os.path.join(out_dir, "fvg_filter_results.csv"), index=False)

    summary = pd.DataFrame([stats_all, stats_fvg, stats_no_fvg])
    summary.to_csv(os.path.join(out_dir, "fvg_filter_summary.csv"), index=False)
    print(f"\nResults saved to {out_dir}")


if __name__ == "__main__":
    main()

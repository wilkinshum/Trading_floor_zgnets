"""
Opening Range + Fair Value Gap (FVG) Backtest
Strategy: Mark 9:30-9:35 ET high/low, then trade FVG breaks through those levels on 1-min chart.
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os

SLIPPAGE_BPS = 5
COMMISSION_PER_SHARE = 0.005
SHARES = 100  # fixed position size
RR_RATIO = 2.0

def get_data(ticker):
    """Get max available 1-min data from yfinance."""
    tk = yf.Ticker(ticker)
    df = tk.history(period="1mo", interval="1m")
    if df.empty:
        df = tk.history(period="5d", interval="1m")
    df.index = df.index.tz_convert("America/New_York")
    return df

def detect_fvg(candles):
    """
    Check if 3 consecutive candles form an FVG.
    Bullish FVG: gap between candle[0].High and candle[2].Low (candle[1] jumps up)
    Bearish FVG: gap between candle[0].Low and candle[2].High (candle[1] drops down)
    Returns: 'bullish', 'bearish', or None
    """
    c0, c1, c2 = candles
    # Bullish FVG: c2.Low > c0.High (gap above)
    if c2['Low'] > c0['High']:
        return 'bullish'
    # Bearish FVG: c2.High < c0.Low (gap below)
    if c2['High'] < c0['Low']:
        return 'bearish'
    return None

def run_backtest(ticker):
    print(f"\n{'='*60}")
    print(f"  Backtesting {ticker}")
    print(f"{'='*60}")
    
    df = get_data(ticker)
    if df.empty:
        print(f"No data for {ticker}")
        return []
    
    print(f"Data range: {df.index[0]} to {df.index[-1]} ({len(df)} bars)")
    
    # Group by date
    df['date'] = df.index.date
    trades = []
    
    for date, day_df in df.groupby('date'):
        # Find 9:30-9:34 candles (the first 5 one-minute candles = opening range)
        market_open = day_df.between_time('09:30', '09:34')
        if len(market_open) < 1:
            continue
        
        or_high = market_open['High'].max()
        or_low = market_open['Low'].min()
        or_range = or_high - or_low
        
        if or_range < 0.01:  # skip tiny ranges
            continue
        
        # Get candles after 9:35 up to 15:55
        trading_candles = day_df.between_time('09:35', '15:55')
        if len(trading_candles) < 4:
            continue
        
        # Scan for FVG through opening range levels
        day_trade_taken = False
        for i in range(len(trading_candles) - 3):
            if day_trade_taken:
                break
            
            c0 = trading_candles.iloc[i]
            c1 = trading_candles.iloc[i+1]
            c2 = trading_candles.iloc[i+2]
            entry_candle = trading_candles.iloc[i+3] if i+3 < len(trading_candles) else None
            
            if entry_candle is None:
                continue
            
            fvg_type = detect_fvg([c0, c1, c2])
            if fvg_type is None:
                continue
            
            # Check if FVG breaks through opening range level
            if fvg_type == 'bullish' and c1['High'] > or_high:
                # Long trade: FVG broke above opening range high
                entry_price = entry_candle['Open'] * (1 + SLIPPAGE_BPS/10000)
                stop_loss = or_low  # below opening range
                risk = entry_price - stop_loss
                if risk <= 0:
                    continue
                target = entry_price + RR_RATIO * risk
                direction = 'long'
                
            elif fvg_type == 'bearish' and c1['Low'] < or_low:
                # Short trade: FVG broke below opening range low
                entry_price = entry_candle['Open'] * (1 - SLIPPAGE_BPS/10000)
                stop_loss = or_high  # above opening range
                risk = stop_loss - entry_price
                if risk <= 0:
                    continue
                target = entry_price - RR_RATIO * risk
                direction = 'short'
            else:
                continue
            
            # Simulate trade on remaining candles
            remaining = trading_candles.iloc[i+3:]
            result = None
            exit_price = None
            exit_time = None
            
            for _, bar in remaining.iterrows():
                if direction == 'long':
                    if bar['Low'] <= stop_loss:
                        exit_price = stop_loss * (1 - SLIPPAGE_BPS/10000)
                        result = 'loss'
                        exit_time = bar.name
                        break
                    if bar['High'] >= target:
                        exit_price = target * (1 - SLIPPAGE_BPS/10000)
                        result = 'win'
                        exit_time = bar.name
                        break
                else:  # short
                    if bar['High'] >= stop_loss:
                        exit_price = stop_loss * (1 + SLIPPAGE_BPS/10000)
                        result = 'loss'
                        exit_time = bar.name
                        break
                    if bar['Low'] <= target:
                        exit_price = target * (1 + SLIPPAGE_BPS/10000)
                        result = 'win'
                        exit_time = bar.name
                        break
            
            if result is None:
                # Close at EOD
                exit_price = remaining.iloc[-1]['Close']
                exit_time = remaining.iloc[-1].name
                if direction == 'long':
                    exit_price *= (1 - SLIPPAGE_BPS/10000)
                else:
                    exit_price *= (1 + SLIPPAGE_BPS/10000)
                result = 'eod_close'
            
            # Calculate P&L
            if direction == 'long':
                pnl_per_share = exit_price - entry_price
            else:
                pnl_per_share = entry_price - exit_price
            
            commission = COMMISSION_PER_SHARE * SHARES * 2  # round trip
            gross_pnl = pnl_per_share * SHARES
            net_pnl = gross_pnl - commission
            
            trades.append({
                'ticker': ticker,
                'date': str(date),
                'direction': direction,
                'fvg_type': fvg_type,
                'entry_price': round(entry_price, 4),
                'stop_loss': round(stop_loss, 4),
                'target': round(target, 4),
                'exit_price': round(exit_price, 4),
                'result': result,
                'gross_pnl': round(gross_pnl, 2),
                'net_pnl': round(net_pnl, 2),
                'risk_per_share': round(risk, 4),
                'entry_time': str(trading_candles.index[i+3]),
                'exit_time': str(exit_time),
            })
            day_trade_taken = True
    
    return trades

def compute_stats(trades_df):
    if trades_df.empty:
        return {}
    
    wins = trades_df[trades_df['net_pnl'] > 0]
    losses = trades_df[trades_df['net_pnl'] <= 0]
    
    total_pnl = trades_df['net_pnl'].sum()
    gross_wins = wins['net_pnl'].sum() if len(wins) > 0 else 0
    gross_losses = abs(losses['net_pnl'].sum()) if len(losses) > 0 else 0
    
    # Max drawdown
    cumulative = trades_df['net_pnl'].cumsum()
    peak = cumulative.cummax()
    drawdown = cumulative - peak
    max_dd = drawdown.min()
    
    stats = {
        'total_trades': len(trades_df),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': f"{len(wins)/len(trades_df)*100:.1f}%",
        'avg_pnl': round(trades_df['net_pnl'].mean(), 2),
        'total_pnl': round(total_pnl, 2),
        'profit_factor': round(gross_wins / gross_losses, 2) if gross_losses > 0 else float('inf'),
        'max_drawdown': round(max_dd, 2),
        'avg_winner': round(wins['net_pnl'].mean(), 2) if len(wins) > 0 else 0,
        'avg_loser': round(losses['net_pnl'].mean(), 2) if len(losses) > 0 else 0,
    }
    return stats

def main():
    all_trades = []
    for ticker in ['SPY', 'QQQ']:
        trades = run_backtest(ticker)
        all_trades.extend(trades)
        print(f"\n{ticker}: {len(trades)} trades found")
    
    if not all_trades:
        print("\nNo trades generated. Insufficient data or no signals.")
        return
    
    df = pd.DataFrame(all_trades)
    
    # Save CSV
    out_dir = r"C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets\data"
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "fvg_backtest_results.csv")
    df.to_csv(csv_path, index=False)
    print(f"\nResults saved to {csv_path}")
    
    # Overall stats
    print(f"\n{'='*60}")
    print("  OVERALL RESULTS")
    print(f"{'='*60}")
    stats = compute_stats(df)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    
    # Per-ticker stats
    for ticker in ['SPY', 'QQQ']:
        tdf = df[df['ticker'] == ticker]
        if tdf.empty:
            continue
        print(f"\n--- {ticker} ---")
        s = compute_stats(tdf)
        for k, v in s.items():
            print(f"  {k:20s}: {v}")
    
    # Trade list
    print(f"\n{'='*60}")
    print("  TRADE LOG")
    print(f"{'='*60}")
    for _, t in df.iterrows():
        print(f"  {t['date']} {t['ticker']:4s} {t['direction']:5s} | entry={t['entry_price']:.2f} exit={t['exit_price']:.2f} | {t['result']:10s} | PnL=${t['net_pnl']:+.2f}")

if __name__ == "__main__":
    main()

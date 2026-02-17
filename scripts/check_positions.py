import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import yaml
from trading_floor.data import YahooDataProvider
from trading_floor.portfolio import Portfolio

cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), '..', 'configs', 'workflow.yaml')))
portfolio = Portfolio(cfg)

data = YahooDataProvider(interval='5m', lookback='1d')
syms = list(portfolio.state.positions.keys())
if not syms:
    print('No positions')
    exit()

md = data.fetch(syms)
current = {}
for sym, m in md.items():
    if not m.df.empty:
        current[sym] = float(m.df['close'].iloc[-1])

stop_loss = cfg.get('risk', {}).get('stop_loss', 0.02)
take_profit = cfg.get('risk', {}).get('take_profit', 0.05)

print(f"Stop Loss: {stop_loss*100:.0f}% | Take Profit: {take_profit*100:.0f}%")
print(f"{'Symbol':6s} {'Qty':>6s} {'Avg Entry':>10s} {'Current':>10s} {'SL Price':>10s} {'TP Price':>10s} {'Unreal PnL':>12s} {'PnL%':>8s}")
print('-' * 75)

total_pnl = 0
for sym, pos in portfolio.state.positions.items():
    qty = pos.quantity
    avg = pos.avg_price
    cur = current.get(sym, 0)
    
    if qty < 0:  # short
        sl_price = avg * (1 + stop_loss)
        tp_price = avg * (1 - take_profit)
        pnl = (avg - cur) * abs(qty)
        pnl_pct = (avg - cur) / avg * 100 if avg > 0 else 0
    else:  # long
        sl_price = avg * (1 - stop_loss)
        tp_price = avg * (1 + take_profit)
        pnl = (cur - avg) * qty
        pnl_pct = (cur - avg) / avg * 100 if avg > 0 else 0
    
    total_pnl += pnl
    print(f"{sym:6s} {qty:>6d} ${avg:>9.2f} ${cur:>9.2f} ${sl_price:>9.2f} ${tp_price:>9.2f} ${pnl:>+11.2f} {pnl_pct:>+7.2f}%")

print('-' * 75)
print(f"{'TOTAL':6s} {'':>6s} {'':>10s} {'':>10s} {'':>10s} {'':>10s} ${total_pnl:>+11.2f}")

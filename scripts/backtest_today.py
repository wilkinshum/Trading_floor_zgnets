"""Backtest: run the full workflow against today's data, bypassing time gate."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import yaml
import numpy as np
from trading_floor.data import YahooDataProvider, filter_trading_window, latest_timestamp
from trading_floor.portfolio import Portfolio
from trading_floor.agents.scout import ScoutAgent
from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.news import NewsSentimentAgent
from trading_floor.agents.risk import RiskAgent
from trading_floor.agents.pm import PMAgent
from trading_floor.agents.compliance import ComplianceAgent
from trading_floor.signal_normalizer import SignalNormalizer
from trading_floor.shadow import ShadowRunner
from trading_floor.lightning import LightningTracer

cfg_path = os.path.join(os.path.dirname(__file__), '..', 'configs', 'workflow.yaml')
cfg = yaml.safe_load(open(cfg_path))
tracer = LightningTracer(cfg)

print("=" * 60)
print("TRADING FLOOR — BACKTEST vs TODAY's DATA")
print("=" * 60)

data = YahooDataProvider(interval='5m', lookback='5d')
fetch_list = list(set(cfg['universe'] + ['SPY', '^VIX']))
md = data.fetch(fetch_list)

# Market regime
market_regime = {'is_downtrend': False, 'is_fear': False}
if 'SPY' in md and not md['SPY'].df.empty:
    spy_series = md['SPY'].df['close']
    if len(spy_series) >= 20:
        ma20 = float(spy_series.rolling(20).mean().iloc[-1])
        spy_now = float(spy_series.iloc[-1])
        market_regime['is_downtrend'] = spy_now < ma20
        print(f"SPY: ${spy_now:.2f} vs MA20: ${ma20:.2f} -> downtrend={market_regime['is_downtrend']}")

if '^VIX' in md and not md['^VIX'].df.empty:
    vix_val = float(md['^VIX'].df['close'].iloc[-1])
    market_regime['is_fear'] = vix_val > 25.0
    print(f"VIX: {vix_val:.2f} -> fear={market_regime['is_fear']}")

print(f"Market Regime: {market_regime}")

# Filter + prices
windowed = {}
current_prices = {}
price_series = {}
for sym, m in md.items():
    if sym not in cfg['universe']:
        continue
    windowed[sym] = filter_trading_window(
        m.df, tz=cfg['hours']['tz'],
        start=cfg['hours']['start'], end=cfg['hours']['end']
    )
    if not m.df.empty:
        current_prices[sym] = float(m.df['close'].iloc[-1])
        price_series[sym] = m.df['close']

# Scout
scout = ScoutAgent(cfg, tracer)
ranked = scout.rank(windowed)
print(f"\n--- Scout Top 10 ---")
for r in ranked[:10]:
    sym = r['symbol']
    price = current_prices.get(sym, 0)
    print(f"  {sym:6s} ${price:>8.2f}  trend={r['trend']:+.4f}  vol={r['vol']:.4f}")

# Signals
weights = cfg.get('signals', {}).get('weights', {
    'momentum': 0.25, 'meanrev': 0.25, 'breakout': 0.25, 'news': 0.25
})
scout_top_n = cfg.get('scout_top_n', 5)
top_symbols = set(r['symbol'] for r in ranked[:scout_top_n])

signal_mom = MomentumSignalAgent(cfg, tracer)
signal_mean = MeanReversionSignalAgent(cfg, tracer)
signal_break = BreakoutSignalAgent(cfg, tracer)
signal_news = NewsSentimentAgent(cfg, tracer)
normalizer = SignalNormalizer(lookback=cfg.get('signals', {}).get('norm_lookback', 100))

signals = {}
signal_details = {}
for sym, df in windowed.items():
    if df.empty or sym not in top_symbols:
        continue
    mom_raw = signal_mom.score(df)
    mean_raw = signal_mean.score(df)
    brk_raw = signal_break.score(df)
    news_raw = signal_news.get_sentiment(sym)

    mom = normalizer.normalize(sym, 'momentum', mom_raw)
    mean = normalizer.normalize(sym, 'meanrev', mean_raw)
    brk = normalizer.normalize(sym, 'breakout', brk_raw)

    score = (
        mom * weights.get('momentum', 0.25) +
        mean * weights.get('meanrev', 0.25) +
        brk * weights.get('breakout', 0.25) +
        news_raw * weights.get('news', 0.25)
    )
    signals[sym] = score
    signal_details[sym] = {
        'momentum': mom, 'meanrev': mean, 'breakout': brk, 'news': news_raw,
        'raw_mom': mom_raw, 'raw_mean': mean_raw, 'raw_brk': brk_raw,
        'final': score
    }

print(f"\n--- Signals (Scout Top {scout_top_n}) ---")
for sym, det in sorted(signal_details.items(), key=lambda x: abs(x[1]['final']), reverse=True):
    d = det
    price = current_prices.get(sym, 0)
    print(f"  {sym:6s} ${price:>8.2f}  final={d['final']:+.4f} | mom={d['momentum']:+.4f} mean={d['meanrev']:+.4f} brk={d['breakout']:+.4f} news={d['news']:+.4f}")

# PM plan
portfolio = Portfolio(cfg)
portfolio.mark_to_market(current_prices)
context = {
    'timestamp': latest_timestamp(),
    'universe': cfg['universe'],
    'market_regime': market_regime,
    'ranked': ranked,
    'signals': signals,
    'price_data': price_series,
    'portfolio_equity': portfolio.state.equity,
    'portfolio_cash': portfolio.state.cash,
    'positions': list(portfolio.state.positions.keys()),
    'portfolio_obj': portfolio,
}

pm = PMAgent(cfg, tracer)
plan, plan_notes = pm.create_plan(context)
print(f"\n--- PM Plan ({plan_notes}) ---")
if not plan.get('plans'):
    print("  (no trades)")
for p in plan.get('plans', []):
    sym = p['symbol']
    price = current_prices.get(sym, 0)
    tv = p.get('target_value', 0)
    qty = int(tv // price) if price > 0 and tv and not (isinstance(tv, float) and tv != tv) else 0
    print(f"  {p['side']:4s} {sym:6s} @ ${price:.2f} | score={p['score']:+.4f} | target=${p.get('target_value', 0):.0f} (~{qty} shares)")

# Risk + Compliance
risk = RiskAgent(cfg, tracer)
compliance = ComplianceAgent(cfg, tracer)
risk_ok, risk_notes = risk.evaluate(context)
compliance_ok, compliance_notes = compliance.review(plan)
print(f"\n--- Risk: {'PASS' if risk_ok else 'FAIL'} --- {risk_notes}")
print(f"--- Compliance: {'PASS' if compliance_ok else 'FAIL'} --- {compliance_notes}")

# Shadow
shadow_cfg = cfg.get('shadow_mode', {})
if shadow_cfg.get('enabled'):
    shadow = ShadowRunner(
        db_path=str(cfg['logging'].get('db_path', 'trading.db')),
        config=shadow_cfg
    )
    spy_prices = md['SPY'].df['close'] if 'SPY' in md else None
    vix_prices = md['^VIX'].df['close'] if '^VIX' in md else None
    shadow_result = shadow.run(
        price_data=price_series, spy_data=spy_prices, vix_data=vix_prices,
        existing_signals=signals, existing_regime={'label': 'bull_low_vol'}
    )
    hmm = shadow_result.get('hmm', {})
    print(f"\n--- Shadow ---")
    print(f"  Kalman agrees: {shadow_result['kalman_agree']}/{shadow_result['kalman_total_compared']}")
    print(f"  HMM: {hmm.get('state_label', '?')} ({hmm.get('confidence', 0):.0%} conf, transition_risk={hmm.get('transition_risk', 0):.2%})")

print(f"\n--- Portfolio ---")
print(f"  Equity: ${portfolio.state.equity:.2f}")
print(f"  Cash: ${portfolio.state.cash:.2f}")
print(f"  Active positions: {list(portfolio.state.positions.keys()) or 'none'}")
print("=" * 60)

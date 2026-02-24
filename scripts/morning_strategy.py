"""
Morning Strategy Planner — runs at 8:30 AM ET (1 hour before market open).
Analyzes pre-market data, sector sentiment, and signal patterns to build
a high-confidence watchlist for the day.

Output:
  - Sector sentiment scan (blocked/green sectors)
  - Pre-market movers (gap up/down)
  - Historical win rate by stock and sector
  - Recommended focus list (highest confidence stocks)
  - Strategy for the day (long-biased, short-biased, mixed, or sit-out)
"""
import sys, os, io, json, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.sector_filter import get_all_sector_sentiments, check_sector_filter
from trading_floor.sector_map import SECTOR_MAP, get_all_sectors

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if '__file__' in dir() else os.getcwd()
DB_PATH = os.path.join(PROJECT_ROOT, 'trading.db')
PORTFOLIO_PATH = os.path.join(PROJECT_ROOT, 'portfolio.json')


def load_portfolio():
    try:
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    except Exception:
        return {"cash": 0, "equity": 0, "positions": {}}


def get_historical_stats(db):
    """Get win rate by symbol from past trades."""
    rows = db.execute('''
        SELECT symbol, side,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl) as total_pnl,
               AVG(pnl) as avg_pnl
        FROM trades
        WHERE pnl != 0
        GROUP BY symbol
        ORDER BY SUM(pnl) DESC
    ''').fetchall()
    return rows


def get_recent_signal_performance(db, days=5):
    """Which stocks had strong signals that led to wins?"""
    rows = db.execute('''
        SELECT s.symbol,
               AVG(s.final_score) as avg_signal,
               AVG(s.score_news) as avg_news,
               AVG(s.score_mom) as avg_mom,
               COUNT(*) as signal_count
        FROM signals s
        WHERE date(s.timestamp) >= date('now', ? || ' days')
        GROUP BY s.symbol
        HAVING COUNT(*) >= 2
        ORDER BY AVG(s.final_score) DESC
    ''', (str(-days),)).fetchall()
    return rows


def get_premarket_movers():
    """Check pre-market price changes via yfinance."""
    try:
        import yfinance as yf
        # Get a sample of our universe
        symbols = [s for s in SECTOR_MAP.keys() if SECTOR_MAP[s]['sector'] != 'ETF']
        # Just check a few key ones for pre-market
        key_symbols = ['SPY', 'QQQ', 'NVDA', 'TSLA', 'AMD', 'IREN', 'RGTI', 'RKLB', 'JPM', 'COIN']
        movers = []
        for sym in key_symbols:
            try:
                t = yf.Ticker(sym)
                info = t.fast_info
                prev_close = getattr(info, 'previous_close', None)
                current = getattr(info, 'last_price', None) or getattr(info, 'open', None)
                if prev_close and current and prev_close > 0:
                    change_pct = (current - prev_close) / prev_close * 100
                    movers.append((sym, change_pct, current, prev_close))
            except Exception:
                pass
        movers.sort(key=lambda x: abs(x[1]), reverse=True)
        return movers
    except Exception:
        return []


def compute_confidence_score(sym, sector_score, avg_signal, historical_pnl, news_score):
    """
    Composite confidence score for daily planning.
    Higher = more confident in a profitable trade.
    """
    score = 0.0
    # Sector sentiment (25% weight)
    score += sector_score * 0.25
    # Recent signal strength (30% weight)
    score += (avg_signal or 0) * 0.30
    # Historical profitability (25% weight)
    if historical_pnl is not None:
        score += (0.25 if historical_pnl > 0 else -0.25)
    # News quality (20% weight)
    score += (news_score or 0) * 0.20
    return score


def run_morning_strategy():
    now = datetime.now()
    print("=" * 70)
    print(f"MORNING STRATEGY PLANNER — {now.strftime('%Y-%m-%d %H:%M ET')}")
    print("=" * 70)

    portfolio = load_portfolio()
    cash = portfolio.get('cash', 0)
    equity = portfolio.get('equity', 0)
    positions = portfolio.get('positions', {})
    print(f"\nPortfolio: ${equity:,.2f} equity | ${cash:,.2f} cash | {len(positions)} positions")

    # 1. Sector Sentiments
    print("\n" + "=" * 70)
    print("1. SECTOR SENTIMENT SCAN")
    print("=" * 70)
    sentiments = get_all_sector_sentiments()
    blocked_sectors = []
    green_sectors = []
    for sector, score in sorted(sentiments.items(), key=lambda x: x[1], reverse=True):
        emoji = "GREEN" if score > 0.05 else "RED" if score < -0.15 else "NEUTRAL"
        print(f"  [{emoji:7s}] {sector:25s} {score:+.3f}")
        if score < -0.15:
            blocked_sectors.append(sector)
        elif score > 0.05:
            green_sectors.append(sector)

    # 2. Pre-market movers
    print("\n" + "=" * 70)
    print("2. PRE-MARKET MOVERS")
    print("=" * 70)
    movers = get_premarket_movers()
    if movers:
        for sym, chg, price, prev in movers:
            direction = "UP" if chg > 0 else "DOWN"
            print(f"  {sym:6s} ${price:.2f} ({chg:+.1f}% {direction} from ${prev:.2f})")
    else:
        print("  (pre-market data not available yet)")

    # 3. Historical performance
    print("\n" + "=" * 70)
    print("3. HISTORICAL WIN RATE BY STOCK")
    print("=" * 70)
    db = sqlite3.connect(DB_PATH)
    hist_stats = get_historical_stats(db)
    hist_pnl_map = {}
    if hist_stats:
        print(f"  {'Symbol':8s} {'Trades':>6s} {'Wins':>5s} {'Losses':>6s} {'WinRate':>8s} {'TotalPnL':>10s}")
        for sym, side, trades, wins, losses, total_pnl, avg_pnl in hist_stats:
            wins = wins or 0
            losses = losses or 0
            wr = wins / trades * 100 if trades > 0 else 0
            hist_pnl_map[sym] = total_pnl
            print(f"  {sym:8s} {trades:6d} {wins:5d} {losses:6d} {wr:7.0f}% ${total_pnl:+9.2f}")
    else:
        print("  (no historical trades)")

    # 4. Recent signal quality
    print("\n" + "=" * 70)
    print("4. RECENT SIGNAL QUALITY (last 5 days)")
    print("=" * 70)
    recent = get_recent_signal_performance(db)
    signal_map = {}
    news_map = {}
    if recent:
        print(f"  {'Symbol':8s} {'AvgSignal':>10s} {'AvgNews':>8s} {'AvgMom':>8s} {'Signals':>8s} {'Sector':>20s}")
        for sym, avg_sig, avg_news, avg_mom, count in recent:
            sector = SECTOR_MAP.get(sym, {}).get('sector', '???')
            signal_map[sym] = avg_sig
            news_map[sym] = avg_news
            print(f"  {sym:8s} {avg_sig:+10.3f} {avg_news:+8.3f} {avg_mom:+8.3f} {count:8d} {sector:>20s}")

    # 5. Build confidence-ranked watchlist
    print("\n" + "=" * 70)
    print("5. HIGH-CONFIDENCE WATCHLIST")
    print("=" * 70)
    
    candidates = []
    for sym, info in SECTOR_MAP.items():
        sector = info['sector']
        if sector == 'ETF':
            continue
        if sector in blocked_sectors:
            continue
        
        sector_score = sentiments.get(sector, 0)
        avg_signal = signal_map.get(sym)
        historical_pnl = hist_pnl_map.get(sym)
        news_score = news_map.get(sym)
        
        confidence = compute_confidence_score(sym, sector_score, avg_signal, historical_pnl, news_score)
        candidates.append({
            'symbol': sym,
            'sector': sector,
            'confidence': confidence,
            'sector_score': sector_score,
            'avg_signal': avg_signal,
            'hist_pnl': historical_pnl,
            'news_score': news_score,
        })
    
    # Sort by confidence
    candidates.sort(key=lambda x: x['confidence'], reverse=True)
    
    # Top longs
    longs = [c for c in candidates if c['confidence'] > 0.10]
    shorts = [c for c in candidates if c['confidence'] < -0.10]
    
    print(f"\n  TOP LONG CANDIDATES ({len(longs)} stocks):")
    print(f"  {'Rank':4s} {'Symbol':6s} {'Sector':20s} {'Conf':>7s} {'SectorSent':>10s} {'AvgSig':>7s} {'HistPnL':>8s}")
    for i, c in enumerate(longs[:10], 1):
        sig_str = f"{c['avg_signal']:+.3f}" if c['avg_signal'] else "  n/a  "
        pnl_str = f"${c['hist_pnl']:+.0f}" if c['hist_pnl'] else "  n/a"
        print(f"  {i:4d} {c['symbol']:6s} {c['sector']:20s} {c['confidence']:+.3f} {c['sector_score']:+10.3f} {sig_str} {pnl_str}")
    
    if shorts:
        shorts.sort(key=lambda x: x['confidence'])
        print(f"\n  TOP SHORT CANDIDATES ({len(shorts)} stocks):")
        for i, c in enumerate(shorts[:5], 1):
            sig_str = f"{c['avg_signal']:+.3f}" if c['avg_signal'] else "  n/a  "
            print(f"  {i:4d} {c['symbol']:6s} {c['sector']:20s} {c['confidence']:+.3f} {c['sector_score']:+10.3f} {sig_str}")

    # 6. Day Strategy
    print("\n" + "=" * 70)
    print("6. DAY STRATEGY")
    print("=" * 70)
    
    if len(blocked_sectors) >= len(sentiments) * 0.5:
        strategy = "SIT OUT — too many sectors red"
    elif len(longs) >= 5 and len(shorts) <= 2:
        strategy = "LONG-BIASED — multiple high-confidence long setups"
    elif len(shorts) >= 5 and len(longs) <= 2:
        strategy = "SHORT-BIASED — multiple high-confidence short setups"
    elif len(longs) >= 3 and len(shorts) >= 3:
        strategy = "PAIRS/MIXED — long green sectors, short red sectors"
    elif len(green_sectors) >= 8:
        strategy = "BROAD LONG — most sectors green, look for breakouts"
    else:
        strategy = "SELECTIVE — few clear setups, trade only highest conviction"
    
    print(f"  Strategy: {strategy}")
    print(f"  Green sectors: {len(green_sectors)}/{len(sentiments)}")
    print(f"  Blocked sectors: {len(blocked_sectors)}/{len(sentiments)}")
    print(f"  Long candidates: {len(longs)}")
    print(f"  Short candidates: {len(shorts)}")
    
    # Focus stocks
    focus = longs[:3] if longs else []
    if focus:
        focus_syms = [c['symbol'] for c in focus]
        print(f"\n  FOCUS STOCKS: {', '.join(focus_syms)}")
        for c in focus:
            print(f"    {c['symbol']} ({c['sector']}) — confidence {c['confidence']:+.3f}")
    
    if shorts[:2]:
        short_syms = [c['symbol'] for c in shorts[:2]]
        print(f"  SHORT WATCH: {', '.join(short_syms)}")
    
    print("\n" + "=" * 70)
    
    db.close()
    
    return {
        'strategy': strategy,
        'focus_longs': [c['symbol'] for c in longs[:5]],
        'focus_shorts': [c['symbol'] for c in shorts[:3]],
        'blocked_sectors': blocked_sectors,
        'green_sectors': green_sectors,
    }


if __name__ == '__main__':
    run_morning_strategy()

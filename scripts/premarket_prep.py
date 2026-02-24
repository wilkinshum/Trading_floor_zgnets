"""
Pre-Market Prep Script — runs at 8:30 AM ET to set the daily plan.
1. Scans all sector sentiments
2. Identifies which sectors are green/red
3. Reviews overnight market moves (SPY, QQQ futures)
4. Sets sector blocks for the day
5. Outputs a morning briefing
"""
import sys
import os
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_floor.sector_filter import get_all_sector_sentiments, _scrape_sector_news
from trading_floor.sector_map import SECTOR_MAP, get_all_sectors
from trading_floor.agents.news import _keyword_score, _scrape_google_news
import json
from datetime import datetime

def run_prep():
    print("=" * 60)
    print(f"PRE-MARKET PREP — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print("=" * 60)

    # 1. Broad market check
    print("\n📊 BROAD MARKET SENTIMENT")
    for query_name, query in [
        ("US Market", "US stock market today premarket futures"),
        ("S&P 500", "S&P 500 futures premarket today"),
        ("Nasdaq", "Nasdaq futures tech stocks premarket today"),
    ]:
        headlines = _scrape_sector_news(query, max_headlines=5)
        if headlines:
            scores = [_keyword_score(h) for h in headlines]
            avg = sum(scores) / len(scores)
            emoji = "🟢" if avg > 0.05 else "🔴" if avg < -0.05 else "⚪"
            print(f"  {emoji} {query_name}: {avg:+.3f}")
            for h in headlines[:3]:
                s = _keyword_score(h)
                print(f"      [{s:+.2f}] {h[:80]}")
        else:
            print(f"  ⚪ {query_name}: no data")

    # 2. Sector sentiments
    print("\n📈 SECTOR SENTIMENTS")
    sentiments = get_all_sector_sentiments()
    blocked = []
    green = []
    for sector, score in sorted(sentiments.items(), key=lambda x: x[1], reverse=True):
        emoji = "🟢" if score > 0.05 else "🔴" if score < -0.15 else "⚪"
        print(f"  {emoji} {sector:25s} {score:+.3f}")
        if score < -0.15:
            blocked.append(sector)
        elif score > 0.05:
            green.append(sector)

    # 3. Count stocks per sector
    print("\n📋 UNIVERSE BY SECTOR")
    from collections import Counter
    sector_counts = Counter(info["sector"] for info in SECTOR_MAP.values())
    for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        status = "🔴 BLOCKED" if sector in blocked else "🟢 ACTIVE" if sector in green else "⚪ NEUTRAL"
        print(f"  {sector:25s} {count:3d} stocks  {status}")

    # 4. Summary
    print("\n" + "=" * 60)
    total_stocks = len([s for s, info in SECTOR_MAP.items() if info["sector"] != "ETF"])
    blocked_stocks = len([s for s, info in SECTOR_MAP.items() if info["sector"] in blocked])
    print(f"UNIVERSE: {total_stocks} stocks across {len(sector_counts)} sectors")
    print(f"BLOCKED SECTORS: {len(blocked)} ({blocked_stocks} stocks affected)")
    if blocked:
        for b in blocked:
            stocks = [s for s, info in SECTOR_MAP.items() if info["sector"] == b]
            print(f"  🔴 {b}: {', '.join(stocks)}")
    print(f"GREEN SECTORS: {len(green)}")
    if green:
        for g in green:
            stocks = [s for s, info in SECTOR_MAP.items() if info["sector"] == g]
            print(f"  🟢 {g}: {', '.join(stocks[:8])}{'...' if len(stocks) > 8 else ''}")
    print("=" * 60)

    return {
        "sentiments": sentiments,
        "blocked": blocked,
        "green": green,
        "total_stocks": total_stocks,
        "blocked_stocks": blocked_stocks,
    }

if __name__ == "__main__":
    run_prep()

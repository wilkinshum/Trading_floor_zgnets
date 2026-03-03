"""Quick test: verify Alpaca API keys and check data availability."""
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timedelta

API_KEY = "PKNKYWEJY5OADXJKFL4MRYJ4GC"
API_SECRET = "6AYzrBrri3WMNLbYWavHWtUvUMW3Gdut3DgQ5zVBPmtn"

client = StockHistoricalDataClient(API_KEY, API_SECRET)

# Test 1: 1-year hourly bars
print("=== 1-Year Hourly Bars (NVDA) ===")
req = StockBarsRequest(
    symbol_or_symbols=["NVDA"],
    timeframe=TimeFrame.Hour,
    start=datetime(2025, 3, 1),
    end=datetime(2026, 3, 1),
)
bars = client.get_stock_bars(req)
nvda = bars["NVDA"]
print(f"  Total bars: {len(nvda)}")
print(f"  First: {nvda[0].timestamp}")
print(f"  Last: {nvda[-1].timestamp}")

# Test 2: 5m bars
print("\n=== 5-Minute Bars (SPY, 1 week) ===")
req_5m = StockBarsRequest(
    symbol_or_symbols=["SPY"],
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime(2026, 2, 24),
    end=datetime(2026, 2, 28),
)
bars_5m = client.get_stock_bars(req_5m)
spy = bars_5m["SPY"]
print(f"  Total 5m bars: {len(spy)}")
print(f"  First: {spy[0].timestamp}")
print(f"  Last: {spy[-1].timestamp}")

# Test 3: 5m bars from 6 months ago
print("\n=== 5m Bars Depth Test (SPY, Sep 2025) ===")
req_old = StockBarsRequest(
    symbol_or_symbols=["SPY"],
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime(2025, 9, 1),
    end=datetime(2025, 9, 5),
)
bars_old = client.get_stock_bars(req_old)
spy_old = bars_old["SPY"]
print(f"  Total bars: {len(spy_old)}")
if spy_old:
    print(f"  First: {spy_old[0].timestamp}")
    print(f"  Last: {spy_old[-1].timestamp}")

# Test 4: News
print("\n=== News API ===")
try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    news_client = NewsClient(API_KEY, API_SECRET)
    news_req = NewsRequest(
        symbols="NVDA",
        start=datetime(2026, 2, 20),
        end=datetime(2026, 2, 28),
        limit=5
    )
    news = news_client.get_news(news_req)
    for article in news.news[:5]:
        syms = [s for s in article.symbols] if article.symbols else []
        print(f"  [{article.created_at}] [{','.join(syms[:3])}] {article.headline[:80]}")
    print(f"  Total articles: {len(news.news)}")
except Exception as e:
    print(f"  News error: {type(e).__name__}: {e}")

print("\n✅ Done!")

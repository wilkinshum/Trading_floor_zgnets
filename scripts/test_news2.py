# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from datetime import datetime

API_KEY = "PKNKYWEJY5OADXJKFL4MRYJ4GC"
API_SECRET = "6AYzrBrri3WMNLbYWavHWtUvUMW3Gdut3DgQ5zVBPmtn"

news_client = NewsClient(API_KEY, API_SECRET)
news_req = NewsRequest(
    symbols="NVDA",
    start=datetime(2026, 2, 20),
    end=datetime(2026, 2, 28),
    limit=5
)
result = news_client.get_news(news_req)

# Access via .data
print(f"Has .data: {hasattr(result, 'data')}")
data = result.data
print(f"Data type: {type(data)}")
if isinstance(data, dict):
    print(f"Keys: {list(data.keys())[:5]}")
    for key, val in list(data.items())[:2]:
        print(f"\nKey: {key}, Val type: {type(val)}")
        if hasattr(val, '__iter__'):
            for i, item in enumerate(val):
                print(f"  Item {i}: {type(item)}")
                print(f"  Attrs: {[a for a in dir(item) if not a.startswith('_')][:10]}")
                if hasattr(item, 'headline'):
                    print(f"  Headline: {item.headline[:80]}")
                if hasattr(item, 'created_at'):
                    print(f"  Created: {item.created_at}")
                if hasattr(item, 'symbols'):
                    print(f"  Symbols: {item.symbols}")
                if i >= 2:
                    break
elif isinstance(data, list):
    print(f"List len: {len(data)}")
    for i, item in enumerate(data[:3]):
        print(f"\nItem {i}: {type(item)}")
        if hasattr(item, 'headline'):
            print(f"  Headline: {item.headline[:80]}")

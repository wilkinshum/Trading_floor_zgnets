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
print(f"Type: {type(result)}")
print(f"Dir: {[a for a in dir(result) if not a.startswith('_')]}")

# Try iterating
if hasattr(result, '__iter__'):
    for i, article in enumerate(result):
        print(f"\n--- Article {i+1} ---")
        print(f"  Type: {type(article)}")
        print(f"  Attrs: {[a for a in dir(article) if not a.startswith('_')][:15]}")
        if hasattr(article, 'headline'):
            print(f"  Headline: {article.headline[:80]}")
        if hasattr(article, 'created_at'):
            print(f"  Created: {article.created_at}")
        if hasattr(article, 'symbols'):
            print(f"  Symbols: {article.symbols}")
        if hasattr(article, 'summary'):
            print(f"  Summary: {str(article.summary)[:100]}")
        if i >= 4:
            break

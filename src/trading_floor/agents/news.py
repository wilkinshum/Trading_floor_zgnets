from __future__ import annotations

import yfinance as yf
from textblob import TextBlob

class NewsSentimentAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.cache = {}

    def get_sentiment(self, symbol: str) -> float:
        """
        Fetches news for a symbol and returns a sentiment score (-1.0 to 1.0).
        Uses yfinance news + TextBlob for simple sentiment analysis.
        """
        self.tracer.emit_span("news.sentiment", {"symbol": symbol})
        
        # Simple caching for the run duration (optional, but good practice)
        if symbol in self.cache:
            return self.cache[symbol]
        
        try:
            ticker = yf.Ticker(symbol)
            news = ticker.news
            if not news:
                self.cache[symbol] = 0.0
                return 0.0
            
            scores = []
            for item in news[:5]: # Analyze top 5 recent headlines
                title = item.get("title", "")
                if title:
                    blob = TextBlob(title)
                    scores.append(blob.sentiment.polarity)
            
            if not scores:
                avg_score = 0.0
            else:
                avg_score = sum(scores) / len(scores)
                
            self.cache[symbol] = avg_score
            return avg_score
            
        except Exception as e:
            # Fallback on error (e.g. rate limit)
            print(f"[NewsAgent] Error for {symbol}: {e}")
            self.cache[symbol] = 0.0
            return 0.0

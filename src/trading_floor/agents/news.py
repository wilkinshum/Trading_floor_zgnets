from __future__ import annotations

import hashlib
import logging
import re
import urllib.request
import urllib.parse
from html import unescape

import yfinance as yf

from trading_floor.agent_memory import AgentMemory

logger = logging.getLogger(__name__)

# Keyword-based sentiment as robust fallback (no TextBlob dependency issues)
_POSITIVE = {
    "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies",
    "gain", "gains", "rise", "rises", "bull", "bullish", "upgrade", "upgrades",
    "beat", "beats", "record", "high", "boom", "profit", "revenue", "growth",
    "strong", "outperform", "buy", "positive", "optimism", "recover", "recovery",
    "breakout", "upside", "accelerate", "expand", "deal", "partnership", "innovative",
}
_NEGATIVE = {
    "crash", "crashes", "plunge", "plunges", "drop", "drops", "fall", "falls",
    "decline", "declines", "bear", "bearish", "downgrade", "downgrades", "miss",
    "misses", "low", "loss", "losses", "weak", "sell", "negative", "fear",
    "recession", "layoff", "layoffs", "cut", "cuts", "risk", "warning", "warn",
    "debt", "default", "lawsuit", "fraud", "investigation", "probe", "fine",
    "slump", "tumble", "sink", "sinks", "concern", "volatile", "uncertainty",
}


def _keyword_score(text: str) -> float:
    """Score text from -1 to +1 using keyword matching."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    pos = len(words & _POSITIVE)
    neg = len(words & _NEGATIVE)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total  # Range: -1 to +1


def _scrape_google_news(symbol: str, max_headlines: int = 8) -> list[str]:
    """Scrape Google News RSS for headlines. No API key needed."""
    try:
        query = urllib.parse.quote(f"{symbol} stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
        # Parse titles from RSS XML
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
        if not titles:
            titles = re.findall(r"<title>(.*?)</title>", xml)
        # Skip the first title (it's the feed title)
        headlines = [unescape(t) for t in titles[1:max_headlines + 1]]
        return headlines
    except Exception:
        return []


class NewsSentimentAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.cache: dict[str, float] = {}
        self._seen_headlines: set[str] = set()  # dedup hashes

        # Memory integration
        mem_cfg = cfg.get("agent_memory", {})
        self.memory_enabled = mem_cfg.get("enabled", False)
        self.memory = None
        if self.memory_enabled:
            db_path = cfg.get("logging", {}).get("db_path", "trading.db")
            self.memory = AgentMemory("news", db_path, mem_cfg)

    def _headline_hash(self, headline: str) -> str:
        """Deterministic hash for dedup."""
        normalized = re.sub(r"\s+", " ", headline.strip().lower())
        return hashlib.md5(normalized.encode()).hexdigest()

    def get_sentiment(self, symbol: str) -> float:
        """
        Returns sentiment score in [-1.0, +1.0].
        Uses yfinance news titles + Google News RSS as fallback.
        Scoring via keyword matching (robust, no NLP library issues).
        """
        self.tracer.emit_span("news.sentiment", {"symbol": symbol})

        if symbol in self.cache:
            return self.cache[symbol]

        headlines: list[str] = []

        # Source 1: yfinance
        try:
            ticker = yf.Ticker(symbol)
            news = ticker.news or []
            for item in news[:8]:
                # yfinance >= 0.2.31 nests under 'content'
                if isinstance(item, dict):
                    title = item.get("title") or ""
                    if not title and "content" in item:
                        title = item["content"].get("title", "")
                    if title:
                        headlines.append(title)
        except Exception:
            pass

        # Source 2: Google News RSS (fallback / supplement)
        if len(headlines) < 3:
            headlines.extend(_scrape_google_news(symbol))

        if not headlines:
            self.cache[symbol] = 0.0
            return 0.0

        # Deduplicate headlines
        unique_headlines = []
        for h in headlines:
            h_hash = self._headline_hash(h)
            if h_hash not in self._seen_headlines:
                self._seen_headlines.add(h_hash)
                unique_headlines.append(h)

        if not unique_headlines:
            self.cache[symbol] = self.cache.get(symbol, 0.0)
            return self.cache[symbol]

        # Score each headline, applying memory-based keyword weight reduction
        scores = []
        for h in unique_headlines:
            base_score = _keyword_score(h)
            # If memory shows keywords in this headline have no predictive power, reduce
            if self.memory and self.memory_enabled:
                accuracy = self.memory.get_signal_accuracy(signal_type="news_keyword")
                if accuracy and accuracy["win_rate"] < 0.45:
                    # Reduce weight of news signal if historically inaccurate
                    base_score *= 0.5
                    logger.debug("News memory: reducing keyword weight (win_rate=%.2f)", accuracy["win_rate"])
            scores.append(base_score)

        avg = sum(scores) / len(scores)

        # Record observation in memory
        if self.memory and self.memory_enabled:
            from trading_floor.regime import detect_regime
            # We don't have regime data here, store with empty regime
            regime = context if 'context' in dir() else {}
            self.memory.record(
                {
                    "symbol": symbol,
                    "signal": "news_keyword",
                    "signal_value": avg,
                    "confidence": abs(avg),
                    "outcome": "pending",
                },
                regime if isinstance(regime, dict) else {},
            )

        self.cache[symbol] = avg
        return avg

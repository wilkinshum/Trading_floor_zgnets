from __future__ import annotations

import hashlib
import logging
import re
import urllib.request
import urllib.parse
from html import unescape

import yfinance as yf

from trading_floor.agent_memory import AgentMemory
from trading_floor.agents.news_finnhub import get_finnhub_news

logger = logging.getLogger(__name__)

# Keyword-based sentiment as robust fallback (no TextBlob dependency issues)
_NEGATORS = {
    "no", "not", "never", "without", "fail", "fails", "failed", "failing",
}

_POS_STRONG = {
    "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies",
    "breakout", "boom", "record", "milestone", "approve", "launch",
}
_POS_MEDIUM = {
    "gain", "gains", "rise", "rises", "upgrade", "upgrades", "beat", "beats",
    "profit", "revenue", "growth", "strong", "outperform", "buy", "recover",
    "recovery", "accelerate", "expand", "deal", "partnership", "innovative",
    "exceeds", "exceed", "tops", "top", "raises", "raise", "raised", "momentum",
    "demand", "rebound", "support", "dividend", "positive", "optimism",
    "overweight", "upbeat", "guidance",
}
_POS_WEAK = {
    "bull", "bullish", "high", "upside", "hold", "target", "targets",
    "reiterate", "reiterates", "reiterated",
}

_NEG_STRONG = {
    "crash", "crashes", "plunge", "plunges", "slump", "tumble", "sink", "sinks",
    "selloff", "correction", "bubble", "fraud", "default", "recall", "collapse",
}
_NEG_MEDIUM = {
    "drop", "drops", "fall", "falls", "decline", "declines", "downgrade",
    "downgrades", "miss", "misses", "loss", "losses", "weak", "sell", "recession",
    "layoff", "layoffs", "risk", "warning", "warn", "debt", "lawsuit",
    "investigation", "probe", "fine", "underperform", "underweight", "downside",
    "lower", "lowers", "lowered", "pressure", "pressured", "headwind", "slowdown",
    "delay", "suspend", "overvalued", "negative", "concern", "volatile",
    "uncertainty", "bear", "bearish", "hangover", "guidance",
}
_NEG_WEAK = {
    "low", "cut", "cuts", "fear",
}

_ANALYST_TERMS = {
    "analyst", "upgrade", "upgrades", "downgrade", "downgrades",
    "price target", "initiates", "initiation", "reiterates",
    "overweight", "underweight", "outperform", "underperform",
    "buy", "sell", "hold",
}

_EARNINGS_TERMS = {
    "earnings", "eps", "revenue", "guidance", "quarter",
    "q1", "q2", "q3", "q4",
}


def _build_weight_map(strong: set[str], medium: set[str], weak: set[str]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for word in strong:
        weights[word] = 1.0
    for word in medium:
        weights[word] = 0.6
    for word in weak:
        weights[word] = 0.3
    return weights


_POSITIVE_WEIGHTS = _build_weight_map(_POS_STRONG, _POS_MEDIUM, _POS_WEAK)
_NEGATIVE_WEIGHTS = _build_weight_map(_NEG_STRONG, _NEG_MEDIUM, _NEG_WEAK)
_AMBIGUOUS_KEYWORDS = set(_POSITIVE_WEIGHTS) & set(_NEGATIVE_WEIGHTS)


def _keyword_score(text: str) -> float:
    """Score text from -1 to +1 using weighted keyword matching."""
    normalized = text.lower().replace("n't", " not")
    tokens = re.findall(r"[a-z]+", normalized)
    if not tokens:
        return 0.0

    pos_weight = 0.0
    neg_weight = 0.0

    for idx, word in enumerate(tokens):
        if word in _AMBIGUOUS_KEYWORDS:
            continue
        weight = _POSITIVE_WEIGHTS.get(word)
        polarity = 1
        if weight is None:
            weight = _NEGATIVE_WEIGHTS.get(word)
            polarity = -1
        if weight is None:
            continue

        window = tokens[max(0, idx - 3):idx]
        if any(w in _NEGATORS for w in window):
            polarity *= -1

        if polarity > 0:
            pos_weight += weight
        else:
            neg_weight += weight

    total = pos_weight + neg_weight
    if total == 0.0:
        return 0.0
    return (pos_weight - neg_weight) / total  # Range: -1 to +1


def _has_term(headlines: list[str], terms: set[str]) -> bool:
    for headline in headlines:
        text = headline.lower()
        if any(term in text for term in terms):
            return True
    return False


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
        headlines = []
        for t in titles:
            headline = unescape(t).strip()
            if not headline:
                continue
            if headline.lower() == "google news":
                continue
            headlines.append(headline)
            if len(headlines) >= max_headlines:
                break
        return headlines
    except Exception:
        return []


_MACRO_QUERIES = [
    "US Iran war conflict military",
    "US China trade war tariff",
    "Federal Reserve interest rate decision",
    "oil price crisis middle east",
    "geopolitical risk stock market",
]

_MACRO_BEARISH = {
    "war", "conflict", "strike", "strikes", "attack", "attacks", "bomb", "bombing",
    "missile", "missiles", "invasion", "invade", "troops", "military",
    "tariff", "tariffs", "sanctions", "sanction", "embargo", "retaliation",
    "escalation", "escalate", "tension", "tensions", "threat", "threatens",
    "shutdown", "default", "crisis", "panic", "contagion", "emergency",
}

_MACRO_BULLISH = {
    "ceasefire", "peace", "deal", "agreement", "talks", "negotiate", "negotiation",
    "de-escalation", "truce", "treaty", "resolve", "resolved", "easing",
    "diplomacy", "diplomatic", "lift", "lifted", "relief",
}


def _scrape_macro_news(max_headlines: int = 10) -> list[str]:
    """Scrape Google News RSS for geopolitical/macro headlines."""
    all_headlines = []
    seen = set()
    for query_str in _MACRO_QUERIES:
        try:
            query = urllib.parse.quote(query_str)
            url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml = resp.read().decode("utf-8", errors="replace")
            titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
            if not titles:
                titles = re.findall(r"<title>(.*?)</title>", xml)
            for t in titles:
                headline = unescape(t).strip()
                if not headline or headline.lower() == "google news":
                    continue
                h_lower = headline.lower()
                if h_lower not in seen:
                    seen.add(h_lower)
                    all_headlines.append(headline)
                if len(all_headlines) >= max_headlines:
                    break
        except Exception:
            continue
        if len(all_headlines) >= max_headlines:
            break
    return all_headlines


def get_macro_sentiment() -> dict:
    """
    Returns macro/geopolitical sentiment.
    {
        "score": float (-1 to +1, negative = bearish macro risk),
        "headlines": list[str],
        "risk_level": "low" | "moderate" | "high" | "extreme",
        "key_themes": list[str]
    }
    """
    headlines = _scrape_macro_news()
    if not headlines:
        return {"score": 0.0, "headlines": [], "risk_level": "low", "key_themes": []}

    bearish_hits = 0
    bullish_hits = 0
    themes = set()

    for h in headlines:
        tokens = set(re.findall(r"[a-z]+", h.lower()))
        b_count = len(tokens & _MACRO_BEARISH)
        g_count = len(tokens & _MACRO_BULLISH)
        bearish_hits += b_count
        bullish_hits += g_count
        # Track themes
        if tokens & {"war", "conflict", "military", "attack", "missile", "invasion", "troops"}:
            themes.add("military-conflict")
        if tokens & {"tariff", "tariffs", "trade", "sanctions", "embargo"}:
            themes.add("trade-war")
        if tokens & {"oil", "energy", "crude"}:
            themes.add("oil-crisis")
        if tokens & {"iran"}:
            themes.add("us-iran")
        if tokens & {"china"}:
            themes.add("us-china")

    total = bearish_hits + bullish_hits
    if total == 0:
        score = 0.0
    else:
        score = (bullish_hits - bearish_hits) / total  # -1 to +1

    # Risk level
    if bearish_hits >= 8:
        risk_level = "extreme"
    elif bearish_hits >= 5:
        risk_level = "high"
    elif bearish_hits >= 2:
        risk_level = "moderate"
    else:
        risk_level = "low"

    return {
        "score": round(score, 3),
        "headlines": headlines,
        "risk_level": risk_level,
        "key_themes": sorted(themes),
    }


class NewsSentimentAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.cache: dict[str, float] = {}
        self.event_flags: dict[str, dict] = {}
        self._seen_headlines: set[str] = set()  # dedup hashes

        finnhub_cfg = cfg.get("finnhub", {})
        self.finnhub_enabled = finnhub_cfg.get("enabled", False)

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

        finnhub_data = None
        if self.finnhub_enabled:
            try:
                finnhub_data = get_finnhub_news(symbol)
            except Exception:
                finnhub_data = None

        if finnhub_data:
            self.event_flags[symbol] = {
                "has_earnings_event": finnhub_data.get("has_earnings_event", False),
                "has_analyst_action": finnhub_data.get("has_analyst_action", False),
                "news_volume_abnormal": finnhub_data.get("news_volume_abnormal", False),
                "categories": finnhub_data.get("categories", []),
            }
            if finnhub_data.get("article_count", 0) > 0 and finnhub_data.get("avg_sentiment") is not None:
                self.cache[symbol] = float(finnhub_data["avg_sentiment"])
                return self.cache[symbol]

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
            if symbol not in self.event_flags:
                self.event_flags[symbol] = {
                    "has_earnings_event": False,
                    "has_analyst_action": False,
                    "news_volume_abnormal": False,
                    "categories": [],
                }
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

        if symbol not in self.event_flags:
            self.event_flags[symbol] = {
                "has_earnings_event": _has_term(unique_headlines, _EARNINGS_TERMS),
                "has_analyst_action": _has_term(unique_headlines, _ANALYST_TERMS),
                "news_volume_abnormal": len(unique_headlines) > 10,
                "categories": [],
            }

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

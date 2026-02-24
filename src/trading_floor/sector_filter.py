"""
Sector News Filter — checks sector-level sentiment before allowing entries.
Queries Google News RSS for each sector, scores headlines, caches results.
"""
from __future__ import annotations

import logging
import time
import re
import urllib.request
import urllib.parse
from html import unescape
from typing import Optional

from trading_floor.sector_map import SECTOR_MAP, SECTOR_QUERIES, get_sector
from trading_floor.agents.news import _keyword_score

logger = logging.getLogger(__name__)

# Cache sector scores for N seconds to avoid hammering Google News
_CACHE_TTL = 600  # 10 minutes
_sector_cache: dict[str, tuple[float, float]] = {}  # sector -> (score, timestamp)


def _scrape_sector_news(query: str, max_headlines: int = 10) -> list[str]:
    """Scrape Google News RSS for sector-level headlines."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
        if not titles:
            titles = re.findall(r"<title>(.*?)</title>", xml)
        headlines = []
        for t in titles:
            headline = unescape(t).strip()
            if not headline or headline.lower() == "google news":
                continue
            headlines.append(headline)
            if len(headlines) >= max_headlines:
                break
        return headlines
    except Exception as e:
        logger.warning("Sector news fetch failed for '%s': %s", query, e)
        return []


def get_sector_sentiment(sector: str) -> float:
    """
    Get sector sentiment score [-1.0, +1.0].
    Caches results for _CACHE_TTL seconds.
    Returns 0.0 if sector not found in SECTOR_QUERIES.
    """
    now = time.time()

    # Check cache
    if sector in _sector_cache:
        cached_score, cached_time = _sector_cache[sector]
        if now - cached_time < _CACHE_TTL:
            return cached_score

    query = SECTOR_QUERIES.get(sector)
    if not query:
        return 0.0

    headlines = _scrape_sector_news(query)
    if not headlines:
        _sector_cache[sector] = (0.0, now)
        return 0.0

    scores = [_keyword_score(h) for h in headlines]
    avg = sum(scores) / len(scores)

    logger.info(
        "Sector '%s': %.3f avg from %d headlines. Top: %s",
        sector, avg, len(headlines),
        headlines[0][:80] if headlines else "none"
    )

    _sector_cache[sector] = (avg, now)
    return avg


def check_sector_filter(symbol: str, threshold: float = -0.15) -> tuple[bool, str, float]:
    """
    Check if a symbol passes the sector news filter.

    Args:
        symbol: Stock ticker
        threshold: Minimum sector sentiment to allow entry (default -0.15)

    Returns:
        (passed, reason, sector_score)
        - passed: True if entry is allowed
        - reason: Human-readable reason if blocked
        - sector_score: The sector sentiment score
    """
    info = get_sector(symbol)
    if not info:
        # Unknown sector — let it through
        return True, "unknown sector", 0.0

    sector = info["sector"]
    if sector == "ETF":
        return True, "ETF (no sector filter)", 0.0

    score = get_sector_sentiment(sector)

    if score < threshold:
        reason = f"Sector '{sector}' sentiment {score:.3f} < threshold {threshold}"
        logger.warning("BLOCKED %s: %s", symbol, reason)
        return False, reason, score

    return True, f"Sector '{sector}' sentiment {score:.3f} OK", score


def get_all_sector_sentiments() -> dict[str, float]:
    """Get sentiment scores for all sectors. Good for pre-market prep."""
    results = {}
    for sector in SECTOR_QUERIES:
        results[sector] = get_sector_sentiment(sector)
    return results

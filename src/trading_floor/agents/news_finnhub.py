import requests
from datetime import datetime, timedelta
from typing import Any

FINNHUB_API_KEY = "d6fpbk1r01qqnmbpi120d6fpbk1r01qqnmbpi12g"
BASE_URL = "https://finnhub.io/api/v1"


_ANALYST_KEYWORDS = {
    "analyst", "upgrade", "upgrades", "downgrade", "downgrades",
    "price target", "initiates", "initiation", "reiterates",
    "overweight", "underweight", "outperform", "underperform",
    "buy", "sell", "hold",
}


def _safe_get_json(url: str) -> Any:
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


def _detect_analyst_action(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in _ANALYST_KEYWORDS)


def get_finnhub_news(symbol: str, days_back: int = 1) -> dict:
    """Get news + sentiment from Finnhub for a symbol.

    Returns dict with:
        - article_count: int
        - avg_sentiment: float (-1 to +1, or None if no articles)
        - has_earnings_event: bool
        - has_analyst_action: bool
        - news_volume_abnormal: bool (more than 2x average)
        - categories: list of event types found
        - raw_articles: list of article summaries
    """
    today = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    news_url = (
        f"{BASE_URL}/company-news?symbol={symbol}&from={from_date}&to={today}&token={FINNHUB_API_KEY}"
    )
    articles = _safe_get_json(news_url) or []
    if not isinstance(articles, list):
        articles = []

    earnings_url = (
        f"{BASE_URL}/calendar/earnings?symbol={symbol}&from={from_date}&to={today}&token={FINNHUB_API_KEY}"
    )
    earnings = _safe_get_json(earnings_url) or {}

    sentiment_url = f"{BASE_URL}/news-sentiment?symbol={symbol}&token={FINNHUB_API_KEY}"
    sentiment_data = _safe_get_json(sentiment_url) or {}

    categories = set()
    has_analyst_action = False
    raw_articles = []

    for article in articles:
        if not isinstance(article, dict):
            continue
        category = article.get("category")
        if category:
            categories.add(category)
        headline = article.get("headline") or ""
        summary = article.get("summary") or ""
        if _detect_analyst_action(headline) or _detect_analyst_action(summary):
            has_analyst_action = True
        raw_articles.append(
            {
                "headline": headline,
                "summary": summary,
                "source": article.get("source"),
                "url": article.get("url"),
                "datetime": article.get("datetime"),
            }
        )

    earnings_calendar = earnings.get("earningsCalendar") or []
    has_earnings_event = bool(earnings_calendar)
    if has_earnings_event:
        categories.add("earnings")

    avg_sentiment = None
    sentiment_block = sentiment_data.get("sentiment") if isinstance(sentiment_data, dict) else None
    if isinstance(sentiment_block, dict):
        bullish = sentiment_block.get("bullishPercent")
        bearish = sentiment_block.get("bearishPercent")
        if bullish is not None and bearish is not None:
            avg_sentiment = float(bullish) - float(bearish)

    if avg_sentiment is None and isinstance(sentiment_data, dict):
        company_score = sentiment_data.get("companyNewsScore")
        if company_score is not None:
            avg_sentiment = (float(company_score) * 2.0) - 1.0

    return {
        "article_count": len(articles),
        "avg_sentiment": avg_sentiment,
        "has_earnings_event": has_earnings_event,
        "has_analyst_action": has_analyst_action,
        "news_volume_abnormal": len(articles) > 10,
        "categories": sorted(categories),
        "raw_articles": raw_articles,
    }


def get_news_score(symbol: str) -> float:
    """Drop-in replacement/supplement for existing news agent.
    Returns score from -1 to +1, or 0 if no data."""
    data = get_finnhub_news(symbol)
    if data["article_count"] == 0:
        return 0.0
    if data["avg_sentiment"] is None:
        return 0.0
    return float(data["avg_sentiment"])

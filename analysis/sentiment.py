"""
analysis/sentiment.py
──────────────────────
Uses the Anthropic Claude API to classify each news item as:
  BULLISH / BEARISH / NEUTRAL
with a confidence score from 1–10.

Only BULLISH signals with confidence >= cfg.min_sentiment_confidence are acted on.
"""

import json
import logging
import anthropic
from dataclasses import dataclass
from news.fetcher import NewsItem
from config.settings import cfg

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    return _client


SYSTEM_PROMPT = """You are a financial news analyst specialising in short-term stock price movements.

Your job is to assess whether a news headline + summary is likely to cause a significant
short-term (minutes to hours) price INCREASE for the named company.

Respond ONLY with a JSON object — no preamble, no markdown, no explanation:
{
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "confidence": <integer 1-10>,
  "reason": "<one sentence>"
}

Confidence scoring guide:
  9-10: Clear, unambiguous positive catalyst (earnings beat, major contract win, M&A premium, drug approval)
  7-8:  Likely positive (analyst upgrade, strong product launch, partnership announcement)
  5-6:  Mildly positive or mixed signals
  1-4:  Weak signal, vague, or already priced in

Be sceptical. Most news is NEUTRAL. Only score BULLISH when there is a clear, specific,
material positive catalyst that the market is unlikely to have already priced in."""


@dataclass
class SentimentResult:
    ticker: str
    headline: str
    sentiment: str      # BULLISH / BEARISH / NEUTRAL
    confidence: int     # 1–10
    reason: str
    is_actionable: bool  # True if BULLISH and confidence >= threshold


def analyse(item: NewsItem) -> SentimentResult | None:
    """
    Run sentiment analysis on a single NewsItem.
    Returns None if the API call fails.
    """
    prompt = f"""Company ticker: {item.ticker}
Headline: {item.headline}
Summary: {item.body}

Classify the likely short-term price impact on {item.ticker}."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        sentiment = data.get("sentiment", "NEUTRAL").upper()
        confidence = int(data.get("confidence", 0))
        reason = data.get("reason", "")

        result = SentimentResult(
            ticker=item.ticker,
            headline=item.headline,
            sentiment=sentiment,
            confidence=confidence,
            reason=reason,
            is_actionable=(
                sentiment == "BULLISH" and confidence >= cfg.min_sentiment_confidence
            ),
        )

        logger.info(
            "Sentiment [%s] %s | %s/10 | %s",
            item.ticker, sentiment, confidence, reason
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("Sentiment JSON parse failed for '%s': %s", item.headline[:60], exc)
        return None
    except Exception as exc:
        logger.error("Sentiment API error for '%s': %s", item.headline[:60], exc)
        return None


def analyse_batch(items: list[NewsItem]) -> list[SentimentResult]:
    """
    Analyse a list of news items. Returns only the non-None results.
    Items are processed sequentially to respect API rate limits.
    """
    results = []
    for item in items:
        result = analyse(item)
        if result is not None:
            results.append(result)
    return results

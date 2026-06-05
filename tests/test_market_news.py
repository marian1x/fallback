import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import market_news
from market_news import MarketNewsCollector, score_text_sentiment


class MockResponse:
    def __init__(self, data=None, text="", status_code=200):
        self._data = data
        self.content = text.encode("utf-8")
        self.status_code = status_code

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_score_text_sentiment_detects_simple_risk_terms():
    result = score_text_sentiment("Company shares plunge after weak guidance and probe")

    assert result["label"] == "negative"
    assert result["score"] < 0
    assert "weak" in result["negative_hits"]


def test_collector_normalizes_yahoo_and_google_sources(monkeypatch):
    rss = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>AAPL stock surges after analyst upgrade</title>
        <link>https://news.google.com/rss/articles/1</link>
        <guid>g1</guid>
        <pubDate>Tue, 02 Jun 2026 10:00:00 GMT</pubDate>
        <description><![CDATA[<a>AAPL stock surges</a>&nbsp;&nbsp;<font>Example</font>]]></description>
        <source url="https://example.com">Example</source>
      </item>
    </channel></rss>"""

    def mock_get(url, headers=None, params=None, timeout=None):
        if "query2.finance.yahoo.com" in url:
            return MockResponse(
                {
                    "news": [
                        {
                            "uuid": "y1",
                            "title": "Apple faces probe pressure",
                            "publisher": "Yahoo Finance",
                            "link": "https://finance.yahoo.com/news/y1",
                            "providerPublishTime": 1780439404,
                            "relatedTickers": ["AAPL"],
                        }
                    ]
                }
            )
        if "news.google.com" in url:
            return MockResponse(text=rss)
        raise AssertionError(url)

    monkeypatch.setattr(market_news.requests, "get", mock_get)
    collector = MarketNewsCollector(
        sources=["yahoo", "google"],
        limit=5,
        timeout_sec=1,
        alpaca_news_url="https://data.alpaca.markets/v1beta1/news",
        google_days=7,
    )

    context = collector.collect("AAPL")

    assert context["provider_errors"] == {}
    assert len(context["items"]) == 2
    assert {item["provider"] for item in context["items"]} == {"yahoo", "google_news"}
    assert context["aggregate"]["news_count"] == 2


def test_collector_records_provider_error_without_failing(monkeypatch):
    def mock_get(url, headers=None, params=None, timeout=None):
        return MockResponse(data=ValueError("not json"), status_code=200)

    monkeypatch.setattr(market_news.requests, "get", mock_get)
    collector = MarketNewsCollector(
        sources=["stocktwits"],
        limit=5,
        timeout_sec=1,
        alpaca_news_url="https://data.alpaca.markets/v1beta1/news",
        google_days=7,
    )

    context = collector.collect("AAPL")

    assert context["items"] == []
    assert context["investor_messages"] == []
    assert "stocktwits" in context["provider_errors"]

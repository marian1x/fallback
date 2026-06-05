import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import stock_intelligence
from stock_intelligence import StockIntelligenceService, parse_symbols


class MockResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_parse_symbols_normalizes_and_limits():
    assert parse_symbols("aapl, nvda; TSM BTC/USD AAPL $msft", limit=4) == ["AAPL", "NVDA", "TSM", "BTCUSD"]


def test_stock_intelligence_ask_uses_news_context_and_model(monkeypatch):
    captured = {}

    class FakeCollector:
        def collect(self, symbol, api_key=None, api_secret=None):
            assert api_key == "key"
            assert api_secret == "secret"
            return {
                "items": [{"headline": f"{symbol} stock rises", "source": "Example", "summary": "positive catalyst"}],
                "investor_messages": [],
                "provider_errors": {},
                "aggregate": {"news_count": 1, "sentiment_label": "positive", "sentiment_score": 0.5},
            }

    def mock_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return MockResponse(
            {
                "output": [
                    {"type": "message", "content": "Contextul este pozitiv, dar riscul ramane."}
                ]
            }
        )

    monkeypatch.setattr(stock_intelligence.requests, "post", mock_post)
    service = StockIntelligenceService(
        base_url="http://mini:1234",
        model="google/gemma-4-e4b",
        timeout_sec=3,
        max_tokens=400,
        temperature=0.2,
    )
    service.news_collector = FakeCollector()

    result = service.ask(question="Ce se intampla?", symbols=["AAPL"], alpaca_api_key="key", alpaca_api_secret="secret")

    assert captured["url"] == "http://mini:1234/api/v1/chat"
    assert captured["payload"]["model"] == "google/gemma-4-e4b"
    assert captured["payload"]["reasoning"] == "off"
    assert "AAPL" in captured["payload"]["input"]
    assert result["answer"] == "Contextul este pozitiv, dar riscul ramane."
    assert result["news_context"]["AAPL"]["aggregate"]["sentiment_label"] == "positive"


def test_stock_intelligence_cleans_reasoning_preamble():
    service = StockIntelligenceService(
        base_url="http://mini:1234",
        model="model",
        timeout_sec=3,
        max_tokens=400,
        temperature=0.2,
    )

    text = "The user is asking for risks. I will structure the answer. Riscurile principale sunt reglementarea si concurenta."

    assert service._clean_answer(text) == "Riscurile principale sunt reglementarea si concurenta."

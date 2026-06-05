import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import llm_trade_validator as validator_mod
from llm_trade_validator import LLMTradeValidator, create_llm_trade_validator, normalize_llm_decision


class MockResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def build_validator(tmp_path):
    return LLMTradeValidator(
        instance_path=str(tmp_path),
        logger=logging.getLogger("test"),
        base_url="http://mini-pc:1234/v1",
        model="local-model",
        timeout_sec=1,
        max_workers=1,
        log_path=str(tmp_path / "shadow.jsonl"),
        temperature=0.1,
        max_tokens=300,
        news_enabled=True,
        news_limit=2,
        news_timeout_sec=1,
        news_url="https://data.alpaca.markets/v1beta1/news",
    )


def build_native_validator(tmp_path):
    return LLMTradeValidator(
        instance_path=str(tmp_path),
        logger=logging.getLogger("test"),
        base_url="http://mini-pc:1234",
        model="google/gemma-4-e4b",
        timeout_sec=1,
        max_workers=1,
        log_path=str(tmp_path / "native-shadow.jsonl"),
        temperature=0.1,
        max_tokens=300,
        api_style="lmstudio_native",
        news_enabled=False,
        news_limit=0,
        news_timeout_sec=1,
        news_url="https://data.alpaca.markets/v1beta1/news",
    )


def test_create_validator_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_TRADE_VALIDATION_ENABLED", raising=False)
    assert create_llm_trade_validator(str(tmp_path), logging.getLogger("test")) is None


def test_normalize_llm_decision_clamps_confidence_and_flags():
    result = normalize_llm_decision(
        {
            "decision": "VETO",
            "confidence": 2,
            "reason": "material negative news",
            "risk_flags": "negative_news",
        }
    )

    assert result["decision"] == "veto"
    assert result["confidence"] == 1.0
    assert result["risk_flags"] == ["negative_news"]
    assert result["would_execute"] is False


def test_shadow_event_records_lmstudio_decision_and_news(tmp_path, monkeypatch):
    validator = build_validator(tmp_path)

    def mock_get(url, headers=None, params=None, timeout=None):
        assert params["symbols"] == "AAPL"
        assert "APCA-API-KEY-ID" in headers
        return MockResponse(
            {
                "news": [
                    {
                        "id": 1,
                        "headline": "AAPL faces regulatory pressure",
                        "summary": "<p>Regulators opened a new probe.</p>",
                        "source": "benzinga",
                        "created_at": "2026-06-02T14:00:00Z",
                        "symbols": ["AAPL"],
                    }
                ]
            }
        )

    def mock_post(url, json=None, headers=None, timeout=None):
        assert url == "http://mini-pc:1234/v1/chat/completions"
        assert json["model"] == "local-model"
        return MockResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"decision":"veto","confidence":0.82,"reason":"negative news conflicts with long entry","risk_flags":["negative_news"]}'
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(validator_mod.requests, "get", mock_get)
    monkeypatch.setattr(validator_mod.requests, "post", mock_post)

    event = validator._base_event(
        user_snapshot={"id": 7, "username": "ana"},
        payload={
            "symbol": "AAPL",
            "action": "buy",
            "amount": 1000,
            "client_order_id": "ls_AAPL_buy_123",
            "strategy_job_id": "job1",
            "bar_time": "2026-06-02T15:00:00Z",
            "local_reason": "Local KC Long Entry",
            "timeframe": "15Min",
        },
        technical_context={"latest_price": 200.0, "recent_bars": []},
    )

    validator._run_signal(event, "key", "secret")

    line = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").strip()
    recorded = json.loads(line)
    assert recorded["status"] == "ok"
    assert recorded["llm"]["decision"] == "veto"
    assert recorded["llm_would_execute"] is False
    assert recorded["keltner_would_execute"] is True
    assert recorded["news"]["items"][0]["summary"] == "Regulators opened a new probe."


def test_shadow_event_supports_lmstudio_native_chat_api(tmp_path, monkeypatch):
    validator = build_native_validator(tmp_path)

    def mock_post(url, json=None, headers=None, timeout=None):
        assert url == "http://mini-pc:1234/api/v1/chat"
        assert json["model"] == "google/gemma-4-e4b"
        assert "system_prompt" in json
        assert "input" in json
        assert json["store"] is False
        assert json["max_output_tokens"] == 300
        return MockResponse(
            {
                "model_instance_id": "google/gemma-4-e4b",
                "output": [
                    {
                        "type": "message",
                        "content": '{"decision":"approve","confidence":0.73,"reason":"no conflicting news","risk_flags":[]}',
                    }
                ],
            }
        )

    monkeypatch.setattr(validator_mod.requests, "post", mock_post)

    event = validator._base_event(
        user_snapshot={"id": 7, "username": "ana"},
        payload={
            "symbol": "AAPL",
            "action": "buy",
            "amount": 1000,
            "client_order_id": "ls_AAPL_buy_123",
            "bar_time": "2026-06-02T15:00:00Z",
            "local_reason": "Local KC Long Entry",
            "timeframe": "15Min",
        },
        technical_context={"latest_price": 200.0, "recent_bars": []},
    )

    validator._run_signal(event, "key", "secret")

    line = (tmp_path / "native-shadow.jsonl").read_text(encoding="utf-8").strip()
    recorded = json.loads(line)
    assert recorded["status"] == "ok"
    assert recorded["llm"]["api_style"] == "lmstudio_native"
    assert recorded["llm"]["decision"] == "approve"
    assert recorded["llm_would_execute"] is True

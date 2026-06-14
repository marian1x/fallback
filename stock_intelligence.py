#!/usr/bin/env python3
"""Stock Intelligence assistant backed by local LM Studio and market news."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, Iterable, List, Optional

import requests

from market_news import create_market_news_collector


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def compact_text(value, limit: int = 2000) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def lmstudio_chat_url(base_url: str) -> str:
    base = (base_url or "http://127.0.0.1:1234").rstrip("/")
    if base.endswith("/api/v1/chat"):
        return base
    if base.endswith("/api/v1"):
        return f"{base}/chat"
    return f"{base}/api/v1/chat"


def parse_symbols(raw: str, limit: int = 6) -> List[str]:
    symbols = []
    seen = set()
    for token in re.split(r"[\s,;]+", raw or ""):
        symbol = token.upper().replace("/", "").strip()
        symbol = re.sub(r"[^A-Z0-9.\-]", "", symbol)
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
        if len(symbols) >= limit:
            break
    return symbols


class StockIntelligenceService:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_sec: float,
        max_tokens: int,
        temperature: float,
        api_token: str = "",
        max_attempts: int = 2,
        instance_path: Optional[str] = None,
    ):
        self.chat_url = lmstudio_chat_url(base_url)
        self.model = model or "local-model"
        self.timeout_sec = timeout_sec
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_token = str(api_token or "").strip()
        self.max_attempts = max(1, int(max_attempts))
        # Use the user-editable source registry (instance/news_sources.json) when
        # available so Stock Intelligence queries the same sources as the engine.
        self.news_collector = create_market_news_collector(instance_path)

    @classmethod
    def from_env(cls, instance_path: Optional[str] = None):
        return cls(
            instance_path=instance_path,
            base_url=os.getenv("STOCK_INTELLIGENCE_BASE_URL", os.getenv("LLM_TRADE_VALIDATION_BASE_URL", "http://127.0.0.1:1234")),
            model=os.getenv("STOCK_INTELLIGENCE_MODEL", os.getenv("LLM_TRADE_VALIDATION_MODEL", "local-model")),
            timeout_sec=env_float("STOCK_INTELLIGENCE_TIMEOUT_SEC", 90.0, minimum=1.0),
            max_tokens=env_int("STOCK_INTELLIGENCE_MAX_TOKENS", 900, minimum=100),
            temperature=env_float("STOCK_INTELLIGENCE_TEMPERATURE", 0.2, minimum=0.0),
            api_token=os.getenv("STOCK_INTELLIGENCE_API_TOKEN", os.getenv("LLM_TRADE_VALIDATION_API_TOKEN", "")),
            max_attempts=env_int("STOCK_INTELLIGENCE_MAX_ATTEMPTS", 2, minimum=1),
        )

    def ask(
        self,
        *,
        question: str,
        symbols: Iterable[str],
        alpaca_api_key: Optional[str] = None,
        alpaca_api_secret: Optional[str] = None,
    ) -> Dict:
        started = time.time()
        question = compact_text(question, 3000)
        symbols = [symbol for symbol in symbols if symbol]
        news_contexts = {
            symbol: self.news_collector.collect(symbol, api_key=alpaca_api_key, api_secret=alpaca_api_secret)
            for symbol in symbols
        }
        prompt_payload = {
            "question": question,
            "symbols": symbols,
            "news_context": {symbol: self._compact_news_context(ctx) for symbol, ctx in news_contexts.items()},
        }
        response = self._call_model(prompt_payload)
        return {
            "answer": response,
            "symbols": symbols,
            "model": self.model,
            "endpoint": self.chat_url,
            "latency_sec": round(time.time() - started, 3),
            "news_context": news_contexts,
        }

    def _call_model(self, prompt_payload: Dict) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        system_prompt = (
            "Esti Stock Intelligence, un asistent pentru analiza pietelor bursiere. "
            "Raspunde direct in romana, concis si practic. Nu explica procesul de gandire. "
            "Nu mentiona 'thinking process'. Foloseste contextul de stiri agregat "
            "cand este disponibil si distinge clar intre fapte, inferente si incertitudini. "
            "Nu da instructiuni de executie directa si nu promite randamente. "
            "Pentru intrebari despre cumparare/vanzare, raspunde cu factori de risc, "
            "scenarii si ce ar trebui verificat, nu cu recomandari absolute."
        )
        user_prompt = (
            "Raspunde la intrebarea utilizatorului folosind contextul urmator. "
            "Daca stirile sunt insuficiente sau sursele au erori, spune explicit.\n\n"
            f"{json.dumps(prompt_payload, ensure_ascii=True, separators=(',', ':'))}"
        )
        payload = {
            "model": self.model,
            "system_prompt": system_prompt,
            "input": user_prompt,
            "temperature": self.temperature,
            "max_output_tokens": self.max_tokens,
            "reasoning": "off",
            "stream": False,
            "store": False,
        }
        answer = ""
        for _attempt in range(1, self.max_attempts + 1):
            response = requests.post(self.chat_url, json=payload, headers=headers, timeout=self.timeout_sec)
            response.raise_for_status()
            data = response.json()
            answer = self._clean_answer(self._extract_output(data))
            if answer.strip():
                break
        return answer

    def _extract_output(self, data: Dict) -> str:
        output = data.get("output") if isinstance(data, dict) else None
        if isinstance(output, list):
            parts = []
            for item in output:
                if isinstance(item, dict) and item.get("type") in ("message", "reasoning"):
                    parts.append(str(item.get("content", "")))
            if parts:
                return "\n".join(part.strip() for part in parts if part.strip())
        if isinstance(data, dict) and data.get("content"):
            return str(data.get("content") or "")
        return ""

    def _clean_answer(self, text: str) -> str:
        answer = (text or "").strip()
        markers = (
            "Riscurile principale",
            "Principalele riscuri",
            "Pe baza contextului",
            "Contextul sugereaza",
            "Contextul sugerează",
            "Pe scurt:",
            "Pentru ",
            "In rezumat",
            "În rezumat",
        )
        lower = answer.lower()
        if (
            "thinking process" in lower
            or "the user is asking" in lower
            or "i will structure" in lower
            or "analyze the request" in lower
            or "synthesize risks" in lower
        ):
            for marker in markers:
                idx = answer.find(marker)
                if idx > 0:
                    answer = answer[idx:].strip()
                    break
            else:
                lines = []
                skip_markers = (
                    "analyze the request",
                    "analyze the context",
                    "identify risks",
                    "synthesize risks",
                    "the user wants",
                    "the context is",
                )
                for line in answer.splitlines():
                    normalized = line.lower()
                    if any(marker in normalized for marker in skip_markers):
                        continue
                    if normalized.strip().startswith(("*   **", "1.  **", "2.  **", "3.  **")):
                        continue
                    lines.append(line)
                cleaned = "\n".join(line for line in lines if line.strip()).strip()
                if cleaned:
                    answer = cleaned
        return answer

    def _compact_news_context(self, context: Dict) -> Dict:
        items = []
        for item in list(context.get("items", []))[:5]:
            sentiment = item.get("sentiment") if isinstance(item.get("sentiment"), dict) else {}
            items.append({
                "headline": item.get("headline"),
                "source": item.get("source"),
                "published_at": item.get("published_at"),
                "summary": compact_text(item.get("summary"), 260),
                "provider": item.get("provider"),
                "sentiment_label": sentiment.get("label"),
                "sentiment_score": sentiment.get("score"),
            })
        return {
            "aggregate": context.get("aggregate", {}),
            "provider_errors": context.get("provider_errors", {}),
            "items": items,
            "investor_messages": list(context.get("investor_messages", []))[:5],
        }

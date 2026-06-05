#!/usr/bin/env python3
"""Shadow LLM validation for local strategy entry signals.

This module is intentionally isolated from order execution. It records what a
local LLM would have decided for a Keltner entry signal, but never changes the
trade payload or blocks the current strategy path.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from html import unescape
from typing import Dict, Iterable, List, Optional

import requests

from market_news import MarketNewsCollector, sources_from_env


SUPPORTED_DECISIONS = {"approve", "veto", "reduce_size", "manual_review", "unknown"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_text(value, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _chat_completions_url(base_url: str) -> str:
    base = (base_url or "http://127.0.0.1:1234/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _lmstudio_native_chat_url(base_url: str) -> str:
    base = (base_url or "http://127.0.0.1:1234").rstrip("/")
    if base.endswith("/api/v1/chat"):
        return base
    if base.endswith("/api/v1"):
        return f"{base}/chat"
    return f"{base}/api/v1/chat"


def _normalize_api_style(api_style: str, base_url: str) -> str:
    style = str(api_style or "openai").strip().lower()
    if style in ("lmstudio_native", "lmstudio", "native", "rest"):
        return "lmstudio_native"
    if style in ("openai", "openai_compatible", "chat_completions"):
        return "openai"
    base = str(base_url or "").rstrip("/")
    if "/api/v1" in base:
        return "lmstudio_native"
    return "openai"


def _extract_json_object(text: str) -> Dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def normalize_llm_decision(data: Dict, raw_content: str = "") -> Dict:
    decision = str(data.get("decision", "unknown") or "unknown").strip().lower()
    if decision not in SUPPORTED_DECISIONS:
        decision = "unknown"

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    risk_flags = data.get("risk_flags", [])
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags]
    if not isinstance(risk_flags, list):
        risk_flags = []
    risk_flags = [_compact_text(flag, 80) for flag in risk_flags[:10] if str(flag or "").strip()]

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": _compact_text(data.get("reason", ""), 1200),
        "risk_flags": risk_flags,
        "would_execute": decision in ("approve", "reduce_size"),
        "raw_content": _compact_text(raw_content, 2000),
    }


class LLMTradeValidator:
    def __init__(
        self,
        *,
        instance_path: str,
        logger,
        base_url: str,
        model: str,
        timeout_sec: float,
        max_workers: int,
        log_path: str,
        temperature: float,
        max_tokens: int,
        api_style: str = "openai",
        api_token: str = "",
        news_enabled: bool,
        news_limit: int,
        news_timeout_sec: float,
        news_url: str,
        max_attempts: int = 2,
        news_sources: Optional[Iterable[str]] = None,
    ):
        self.instance_path = instance_path
        self.logger = logger
        self.api_style = _normalize_api_style(api_style, base_url)
        self.chat_url = (
            _lmstudio_native_chat_url(base_url)
            if self.api_style == "lmstudio_native"
            else _chat_completions_url(base_url)
        )
        self.model = model or "local-model"
        self.timeout_sec = timeout_sec
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.api_token = str(api_token or "").strip()
        self.max_attempts = max(1, int(max_attempts))
        self.log_path = log_path
        self.news_enabled = news_enabled
        self.news_limit = news_limit
        self.news_timeout_sec = news_timeout_sec
        self.news_url = news_url
        self.news_collector = (
            MarketNewsCollector(
                sources=news_sources or ["alpaca"],
                limit=news_limit,
                timeout_sec=news_timeout_sec,
                alpaca_news_url=news_url,
                google_days=_env_int("NEWS_CONTEXT_GOOGLE_DAYS", 7, minimum=1),
                user_agent=os.getenv("NEWS_CONTEXT_USER_AGENT", ""),
            )
            if news_enabled
            else None
        )
        self._write_lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(max(1, max_workers))

    def submit_entry_signal(
        self,
        *,
        user_snapshot: Dict,
        payload: Dict,
        technical_context: Dict,
        alpaca_api_key: Optional[str] = None,
        alpaca_api_secret: Optional[str] = None,
    ) -> None:
        event = self._base_event(
            user_snapshot=user_snapshot,
            payload=payload,
            technical_context=technical_context,
        )
        if not self._semaphore.acquire(blocking=False):
            event["status"] = "skipped_busy"
            event["completed_at_utc"] = _utc_now_iso()
            self._write_event(event)
            self.logger.warning(
                "[LLM_SHADOW] skipped_busy symbol=%s action=%s client_order_id=%s",
                event.get("symbol"),
                event.get("action"),
                event.get("client_order_id"),
            )
            return

        thread = threading.Thread(
            target=self._run_signal_with_release,
            kwargs={
                "event": event,
                "alpaca_api_key": alpaca_api_key,
                "alpaca_api_secret": alpaca_api_secret,
            },
            name=f"llm-shadow-{event.get('symbol', 'signal')}",
            daemon=True,
        )
        thread.start()

    def _run_signal_with_release(self, event: Dict, alpaca_api_key: Optional[str], alpaca_api_secret: Optional[str]) -> None:
        try:
            self._run_signal(event, alpaca_api_key, alpaca_api_secret)
        finally:
            try:
                self._semaphore.release()
            except ValueError:
                pass

    def _run_signal(self, event: Dict, alpaca_api_key: Optional[str], alpaca_api_secret: Optional[str]) -> None:
        started = time.time()
        news = self._fetch_news(event.get("symbol"), alpaca_api_key, alpaca_api_secret)
        event["news"] = news
        request_payload = self._build_lmstudio_payload(event, news)
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            event["llm"] = self._call_llm_with_retries(request_payload, headers)
            event["status"] = "ok" if event["llm"]["decision"] != "unknown" else "parse_error"
        except Exception as exc:
            event["status"] = "error"
            event["error"] = _compact_text(str(exc), 1000)
            event["llm"] = {
                "model": self.model,
                "endpoint": self.chat_url,
                "api_style": self.api_style,
                "decision": "unknown",
                "confidence": 0.0,
                "would_execute": False,
                "reason": "",
                "risk_flags": ["llm_error"],
            }
            self.logger.warning(
                "[LLM_SHADOW] validation_error symbol=%s action=%s error=%s",
                event.get("symbol"),
                event.get("action"),
                exc,
            )

        event["latency_sec"] = round(time.time() - started, 3)
        event["completed_at_utc"] = _utc_now_iso()
        event["keltner_would_execute"] = True
        event["llm_would_execute"] = bool(event.get("llm", {}).get("would_execute"))
        self._write_event(event)
        self.logger.info(
            "[LLM_SHADOW] symbol=%s action=%s status=%s decision=%s confidence=%s llm_would_execute=%s",
            event.get("symbol"),
            event.get("action"),
            event.get("status"),
            event.get("llm", {}).get("decision"),
            event.get("llm", {}).get("confidence"),
            event.get("llm_would_execute"),
        )

    def _call_llm_with_retries(self, request_payload: Dict, headers: Dict) -> Dict:
        last_decision = None
        for attempt in range(1, self.max_attempts + 1):
            response = requests.post(
                self.chat_url,
                json=request_payload,
                headers=headers,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            data = response.json()
            content = self._extract_message_content(data)
            parsed = _extract_json_object(content)
            decision = normalize_llm_decision(parsed, raw_content=content)
            decision["attempt"] = attempt
            last_decision = decision
            if decision["decision"] != "unknown":
                break
        return {
            "model": self.model,
            "endpoint": self.chat_url,
            "api_style": self.api_style,
            **(last_decision or normalize_llm_decision({})),
        }

    def _base_event(self, *, user_snapshot: Dict, payload: Dict, technical_context: Dict) -> Dict:
        return {
            "schema_version": 1,
            "mode": "shadow",
            "created_at_utc": _utc_now_iso(),
            "status": "queued",
            "user": {
                "id": user_snapshot.get("id"),
                "username": user_snapshot.get("username"),
            },
            "symbol": payload.get("symbol"),
            "action": payload.get("action"),
            "amount": payload.get("amount"),
            "client_order_id": payload.get("client_order_id"),
            "strategy_job_id": payload.get("strategy_job_id"),
            "bar_time": payload.get("bar_time"),
            "local_reason": payload.get("local_reason"),
            "timeframe": payload.get("timeframe"),
            "technical_context": _json_safe(technical_context),
        }

    def _fetch_news(self, symbol: Optional[str], api_key: Optional[str], api_secret: Optional[str]) -> Dict:
        if not self.news_enabled:
            return {"enabled": False, "items": [], "error": None}
        if not self.news_collector:
            return {"enabled": True, "items": [], "error": "news_collector_not_configured"}
        return self.news_collector.collect(symbol, api_key=api_key, api_secret=api_secret)

    def _normalize_news_items(self, raw_items: Iterable) -> List[Dict]:
        items = []
        for item in list(raw_items or [])[: self.news_limit]:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "id": item.get("id"),
                    "headline": _compact_text(item.get("headline") or item.get("title"), 300),
                    "summary": _compact_text(item.get("summary"), 700),
                    "source": _compact_text(item.get("source"), 80),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "url": _compact_text(item.get("url"), 300),
                    "symbols": item.get("symbols", []),
                }
            )
        return items

    def _build_lmstudio_payload(self, event: Dict, news_context) -> Dict:
        if isinstance(news_context, list):
            news_context = {"enabled": True, "items": news_context, "aggregate": {}, "provider_errors": {}}
        input_payload = {
            "signal": {
                "symbol": event.get("symbol"),
                "action": event.get("action"),
                "amount": event.get("amount"),
                "timeframe": event.get("timeframe"),
                "bar_time": event.get("bar_time"),
                "reason": event.get("local_reason"),
                "client_order_id": event.get("client_order_id"),
            },
            "technical_context": event.get("technical_context", {}),
            "news_context": self._compact_news_context_for_prompt(news_context),
            "shadow_mode": True,
        }
        system_prompt = (
            "Esti un validator shadow pentru semnale de trading Keltner. "
            "Nu executi ordine si nu oferi recomandari generale; evaluezi doar daca "
            "semnalul dat este contrazis de stiri recente sau de riscuri evidente. "
            "Returneaza strict un singur obiect JSON valid, fara markdown, cu cheile: "
            "decision, confidence, reason, risk_flags. "
            "decision trebuie sa fie una dintre: approve, veto, reduce_size, manual_review. "
            "Foloseste veto doar pentru risc clar si material; foloseste manual_review cand "
            "stirile sunt ambigue sau lipsesc date importante."
        )
        user_prompt = (
            "Analizeaza semnalul urmator. Pentru long/buy, stirile negative sau evenimentele "
            "de risc pot justifica veto/manual_review. Pentru short/sell, stirile pozitive "
            "materiale pot justifica veto/manual_review. Daca nu exista stiri relevante, "
            "spune asta in reason. Tine cont de news_context.aggregate, de provider_errors "
            "si de investor_messages doar ca semnale auxiliare, nu ca adevar absolut.\n\n"
            f"{json.dumps(input_payload, ensure_ascii=True, separators=(',', ':'))}"
        )
        if self.api_style == "lmstudio_native":
            return {
                "model": self.model,
                "system_prompt": system_prompt,
                "input": user_prompt,
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
                "stream": False,
                "store": False,
            }
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

    def _compact_news_context_for_prompt(self, news_context: Dict) -> Dict:
        if not isinstance(news_context, dict):
            return {"items": [], "aggregate": {}, "provider_errors": {}}
        items = []
        for item in list(news_context.get("items", []))[: self.news_limit]:
            if not isinstance(item, dict):
                continue
            sentiment = item.get("sentiment") if isinstance(item.get("sentiment"), dict) else {}
            items.append({
                "provider": item.get("provider"),
                "headline": _compact_text(item.get("headline"), 220),
                "summary": _compact_text(item.get("summary"), 220),
                "source": item.get("source"),
                "published_at": item.get("published_at"),
                "sentiment_label": sentiment.get("label"),
                "sentiment_score": sentiment.get("score"),
            })
        investor_messages = []
        for item in list(news_context.get("investor_messages", []))[: self.news_limit]:
            if not isinstance(item, dict):
                continue
            investor_messages.append({
                "provider": item.get("provider"),
                "created_at": item.get("created_at"),
                "body": _compact_text(item.get("body"), 180),
                "sentiment": item.get("sentiment"),
                "sentiment_score": item.get("sentiment_score"),
            })
        return {
            "aggregate": news_context.get("aggregate", {}),
            "provider_errors": news_context.get("provider_errors", {}),
            "items": items,
            "investor_messages": investor_messages,
        }

    def _extract_message_content(self, data: Dict) -> str:
        output = data.get("output") if isinstance(data, dict) else None
        if isinstance(output, list):
            parts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message":
                    parts.append(str(item.get("content", "")))
            if parts:
                return "".join(parts)
        if isinstance(data, dict) and data.get("content"):
            return str(data.get("content") or "")

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return ""
        choice = choices[0] if choices else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
                else:
                    parts.append(str(part))
            return "".join(parts)
        return str(content or "")

    def _write_event(self, event: Dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            line = json.dumps(_json_safe(event), ensure_ascii=True, separators=(",", ":"))
            with self._write_lock:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            self.logger.error("[LLM_SHADOW] failed_to_write_log path=%s error=%s", self.log_path, exc)


def create_llm_trade_validator(instance_path: str, logger) -> Optional[LLMTradeValidator]:
    if not _env_bool("LLM_TRADE_VALIDATION_ENABLED", False):
        return None

    mode = os.getenv("LLM_TRADE_VALIDATION_MODE", "shadow").strip().lower()
    if mode != "shadow":
        logger.warning("[LLM_SHADOW] unsupported mode '%s'; only shadow mode is active.", mode)
        return None

    default_log_path = os.path.join(instance_path, "llm_trade_shadow.jsonl")
    validator = LLMTradeValidator(
        instance_path=instance_path,
        logger=logger,
        base_url=os.getenv("LLM_TRADE_VALIDATION_BASE_URL", "http://127.0.0.1:1234/v1"),
        model=os.getenv("LLM_TRADE_VALIDATION_MODEL", "local-model"),
        timeout_sec=_env_float("LLM_TRADE_VALIDATION_TIMEOUT_SEC", 25.0, minimum=0.5),
        max_workers=_env_int("LLM_TRADE_VALIDATION_MAX_WORKERS", 1, minimum=1),
        log_path=os.getenv("LLM_TRADE_VALIDATION_LOG_FILE", default_log_path),
        temperature=_env_float("LLM_TRADE_VALIDATION_TEMPERATURE", 0.1, minimum=0.0),
        max_tokens=_env_int("LLM_TRADE_VALIDATION_MAX_TOKENS", 500, minimum=100),
        api_style=os.getenv("LLM_TRADE_VALIDATION_API_STYLE", "openai"),
        api_token=os.getenv("LLM_TRADE_VALIDATION_API_TOKEN", ""),
        max_attempts=_env_int("LLM_TRADE_VALIDATION_MAX_ATTEMPTS", 2, minimum=1),
        news_enabled=_env_bool("LLM_TRADE_VALIDATION_NEWS_ENABLED", True),
        news_limit=_env_int("LLM_TRADE_VALIDATION_NEWS_LIMIT", 3, minimum=0),
        news_timeout_sec=_env_float("LLM_TRADE_VALIDATION_NEWS_TIMEOUT_SEC", 5.0, minimum=0.5),
        news_url=os.getenv("LLM_TRADE_VALIDATION_NEWS_URL", "https://data.alpaca.markets/v1beta1/news"),
        news_sources=sources_from_env(),
    )
    logger.info(
        "[LLM_SHADOW] enabled endpoint=%s api_style=%s model=%s log=%s news_enabled=%s",
        validator.chat_url,
        validator.api_style,
        validator.model,
        validator.log_path,
        validator.news_enabled,
    )
    return validator

#!/usr/bin/env python3
"""Market news and sentiment context used by LLM shadow validation."""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote_plus

import requests


DEFAULT_SOURCES = "alpaca,google"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

POSITIVE_TERMS = {
    "beat", "beats", "raise", "raises", "raised", "upgrade", "upgraded", "outperform",
    "buy", "bullish", "growth", "surge", "surges", "rally", "record", "strong",
    "profit", "profits", "gain", "gains", "optimism", "catalyst", "approval",
    "partnership", "contract", "launch", "higher", "boost", "boosts",
}
NEGATIVE_TERMS = {
    "miss", "misses", "cut", "cuts", "downgrade", "downgraded", "underperform",
    "sell", "bearish", "lawsuit", "probe", "investigation", "fraud", "warning",
    "weak", "loss", "losses", "drop", "drops", "fall", "falls", "plunge",
    "decline", "declines", "recall", "halt", "halts", "ban", "risk", "risks",
    "slump", "concern", "concerns", "pressure", "layoff", "layoffs",
}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def compact_text(value, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def compact_error_text(value, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except Exception:
            return None
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = parsedate_to_datetime(raw)
        except Exception:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def score_text_sentiment(text: str) -> Dict:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-']+", (text or "").lower())
    if not words:
        return {"score": 0.0, "label": "neutral", "positive_hits": [], "negative_hits": []}
    positive_hits = sorted({word for word in words if word in POSITIVE_TERMS})
    negative_hits = sorted({word for word in words if word in NEGATIVE_TERMS})
    raw_score = len(positive_hits) - len(negative_hits)
    score = max(-1.0, min(1.0, raw_score / 3.0))
    label = "neutral"
    if score >= 0.25:
        label = "positive"
    elif score <= -0.25:
        label = "negative"
    return {
        "score": round(score, 3),
        "label": label,
        "positive_hits": positive_hits[:8],
        "negative_hits": negative_hits[:8],
    }


class MarketNewsCollector:
    def __init__(
        self,
        *,
        sources: Iterable[str],
        limit: int,
        timeout_sec: float,
        alpaca_news_url: str,
        google_days: int,
        user_agent: str = DEFAULT_USER_AGENT,
        max_items: int = 0,
    ):
        self.sources = self._normalize_source_defs(sources)
        self.limit = max(0, int(limit))  # items fetched per source
        # Total items returned across all sources. Defaults to ``limit`` for
        # backward compatibility; set higher to include news from every source
        # rather than truncating to one source's worth.
        self.max_items = max(self.limit, int(max_items)) if max_items else self.limit
        self.timeout_sec = max(0.5, float(timeout_sec))
        self.alpaca_news_url = alpaca_news_url
        self.google_days = max(1, int(google_days))
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    @staticmethod
    def _normalize_source_defs(sources: Iterable) -> List[Dict]:
        """Accept legacy strings or structured dicts; emit canonical source dicts."""
        out: List[Dict] = []
        for source in sources or []:
            if isinstance(source, str):
                stype = source.strip().lower()
                if stype:
                    out.append({"name": stype, "type": stype, "url": "", "enabled": True})
            elif isinstance(source, dict):
                stype = str(source.get("type") or "").strip().lower()
                if not stype:
                    continue
                out.append({
                    "name": str(source.get("name") or stype).strip() or stype,
                    "type": stype,
                    "url": str(source.get("url") or "").strip(),
                    "enabled": bool(source.get("enabled", True)),
                })
        return out

    def collect(self, symbol: Optional[str], api_key: Optional[str] = None, api_secret: Optional[str] = None) -> Dict:
        symbol = str(symbol or "").upper().replace("/", "").strip()
        active = [s for s in self.sources if s.get("enabled", True)]
        context = {
            "enabled": True,
            "symbol": symbol,
            "generated_at_utc": utc_now_iso(),
            "requested_sources": [s.get("name") or s.get("type") for s in active],
            "items": [],
            "investor_messages": [],
            "provider_errors": {},
            "aggregate": {},
        }
        if not symbol or self.limit <= 0:
            context["aggregate"] = self._aggregate_context(context["items"], context["investor_messages"])
            return context

        for src in active:
            stype = src.get("type")
            label = src.get("name") or stype
            try:
                if stype == "alpaca":
                    context["items"].extend(self._fetch_alpaca(symbol, api_key, api_secret))
                elif stype == "yahoo":
                    context["items"].extend(self._fetch_yahoo_query(symbol))
                elif stype in ("google", "google_news"):
                    context["items"].extend(self._fetch_google_news(symbol))
                elif stype in ("stocktwits", "stocktwits_public"):
                    context["investor_messages"].extend(self._fetch_stocktwits(symbol))
                elif stype == "rss":
                    provider = "rss_" + re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
                    context["items"].extend(self._fetch_rss(src.get("url", ""), symbol, provider))
                else:
                    context["provider_errors"][label] = "unknown_source"
            except Exception as exc:
                context["provider_errors"][label] = compact_error_text(str(exc), 500)

        # Interleave across providers BEFORE truncating so one source (e.g. Alpaca,
        # which is Benzinga-backed) cannot fill every slot and hide the others.
        context["items"] = self._dedupe_items(self._interleave_by_provider(context["items"]))[: self.max_items]
        context["investor_messages"] = context["investor_messages"][: self.max_items]
        context["aggregate"] = self._aggregate_context(context["items"], context["investor_messages"])
        return context

    def _headers(self) -> Dict:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
        }

    def _fetch_alpaca(self, symbol: str, api_key: Optional[str], api_secret: Optional[str]) -> List[Dict]:
        if not api_key or not api_secret:
            raise RuntimeError("missing_alpaca_credentials")
        response = requests.get(
            self.alpaca_news_url,
            headers={
                **self._headers(),
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            params={"symbols": symbol, "limit": self.limit, "sort": "desc"},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        raw_items = data.get("news", data.get("articles", data if isinstance(data, list) else []))
        return [self._normalize_news_item(item, "alpaca") for item in raw_items if isinstance(item, dict)]

    def _fetch_yahoo_query(self, symbol: str) -> List[Dict]:
        response = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            headers=self._headers(),
            params={"q": symbol, "quotesCount": 0, "newsCount": self.limit},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        raw_items = data.get("news", [])
        return [self._normalize_yahoo_item(item, symbol) for item in raw_items if isinstance(item, dict)]

    def _fetch_google_news(self, symbol: str) -> List[Dict]:
        query = quote_plus(f"{symbol} stock when:{self.google_days}d")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        response = requests.get(url, headers=self._headers(), timeout=self.timeout_sec)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall(".//item")[: self.limit]:
            source = item.find("source")
            items.append(
                self._normalize_news_item(
                    {
                        "id": self._xml_text(item, "guid"),
                        "headline": self._xml_text(item, "title"),
                        "summary": self._xml_text(item, "description"),
                        "url": self._xml_text(item, "link"),
                        "created_at": parse_timestamp(self._xml_text(item, "pubDate")),
                        "source": source.text if source is not None else "Google News",
                        "symbols": [symbol],
                    },
                    "google_news",
                )
            )
        return items

    def _fetch_rss(self, url_template: str, symbol: str, provider: str) -> List[Dict]:
        """Fetch and parse any RSS or Atom feed. ``{symbol}`` is substituted."""
        if not url_template or "{symbol}" not in url_template:
            raise RuntimeError("invalid_rss_url")
        url = url_template.replace("{symbol}", quote_plus(symbol))
        response = requests.get(url, headers=self._headers(), timeout=self.timeout_sec)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items: List[Dict] = []
        rss_nodes = root.findall(".//item")
        if rss_nodes:
            for item in rss_nodes[: self.limit]:
                source = item.find("source")
                items.append(
                    self._normalize_news_item(
                        {
                            "id": self._xml_text(item, "guid") or self._xml_text(item, "link"),
                            "headline": self._xml_text(item, "title"),
                            "summary": self._xml_text(item, "description"),
                            "url": self._xml_text(item, "link"),
                            "created_at": parse_timestamp(self._xml_text(item, "pubDate")),
                            "source": (source.text if source is not None and source.text else provider),
                            "symbols": [symbol],
                        },
                        provider,
                    )
                )
            return items
        # Atom fallback
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns)[: self.limit]:
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            items.append(
                self._normalize_news_item(
                    {
                        "id": self._atom_text(entry, "a:id", ns) or link,
                        "headline": self._atom_text(entry, "a:title", ns),
                        "summary": self._atom_text(entry, "a:summary", ns) or self._atom_text(entry, "a:content", ns),
                        "url": link,
                        "created_at": parse_timestamp(self._atom_text(entry, "a:updated", ns) or self._atom_text(entry, "a:published", ns)),
                        "source": provider,
                        "symbols": [symbol],
                    },
                    provider,
                )
            )
        return items

    def _atom_text(self, node, path: str, ns: Dict) -> str:
        child = node.find(path, ns)
        return child.text if child is not None and child.text else ""

    def _fetch_stocktwits(self, symbol: str) -> List[Dict]:
        response = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{quote_plus(symbol)}.json",
            headers=self._headers(),
            params={"limit": self.limit},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        messages = []
        for item in data.get("messages", [])[: self.limit]:
            if not isinstance(item, dict):
                continue
            sentiment = ((item.get("entities") or {}).get("sentiment") or {}).get("basic")
            body = compact_text(item.get("body"), 300)
            score = 0.0
            label = "neutral"
            if str(sentiment or "").lower() == "bullish":
                score, label = 1.0, "bullish"
            elif str(sentiment or "").lower() == "bearish":
                score, label = -1.0, "bearish"
            messages.append(
                {
                    "provider": "stocktwits",
                    "id": item.get("id"),
                    "created_at": parse_timestamp(item.get("created_at")),
                    "body": body,
                    "sentiment": label,
                    "sentiment_score": score,
                    "user": (item.get("user") or {}).get("username"),
                }
            )
        return messages

    def _normalize_news_item(self, item: Dict, provider: str) -> Dict:
        headline = compact_text(item.get("headline") or item.get("title"), 300)
        summary = compact_text(item.get("summary") or item.get("content") or item.get("description"), 700)
        sentiment = score_text_sentiment(f"{headline} {summary}")
        return {
            "provider": provider,
            "id": item.get("id") or item.get("uuid"),
            "headline": headline,
            "summary": summary,
            "source": compact_text(item.get("source") or item.get("publisher"), 100),
            "published_at": parse_timestamp(item.get("created_at") or item.get("updated_at") or item.get("providerPublishTime")),
            "url": compact_text(item.get("url") or item.get("link"), 400),
            "symbols": item.get("symbols") or item.get("relatedTickers") or [],
            "sentiment": sentiment,
        }

    def _normalize_yahoo_item(self, item: Dict, symbol: str) -> Dict:
        normalized = self._normalize_news_item(item, "yahoo")
        related = normalized.get("symbols") or []
        if related and symbol not in related:
            normalized["relevance_note"] = "symbol_not_in_related_tickers"
        return normalized

    @staticmethod
    def _interleave_by_provider(items: List[Dict]) -> List[Dict]:
        """Round-robin items across their providers so the first N after truncation
        are spread across sources instead of dominated by whichever ran first."""
        buckets: "OrderedDict[str, List[Dict]]" = OrderedDict()
        for item in items:
            buckets.setdefault(str(item.get("provider") or "?"), []).append(item)
        out: List[Dict] = []
        depth = 0
        while True:
            added = False
            for lst in buckets.values():
                if depth < len(lst):
                    out.append(lst[depth])
                    added = True
            if not added:
                break
            depth += 1
        return out

    def _dedupe_items(self, items: List[Dict]) -> List[Dict]:
        seen = set()
        result = []
        for item in items:
            key = item.get("url") or item.get("id") or item.get("headline")
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _aggregate_context(self, items: List[Dict], messages: List[Dict]) -> Dict:
        scores = [float((item.get("sentiment") or {}).get("score", 0.0)) for item in items]
        investor_scores = [float(item.get("sentiment_score", 0.0)) for item in messages]
        all_scores = scores + investor_scores
        avg = sum(all_scores) / len(all_scores) if all_scores else 0.0
        label = "neutral"
        if avg >= 0.2:
            label = "positive"
        elif avg <= -0.2:
            label = "negative"
        return {
            "news_count": len(items),
            "investor_message_count": len(messages),
            "sentiment_score": round(avg, 3),
            "sentiment_label": label,
            "negative_news_count": sum(1 for item in items if (item.get("sentiment") or {}).get("label") == "negative"),
            "positive_news_count": sum(1 for item in items if (item.get("sentiment") or {}).get("label") == "positive"),
            "investor_bullish_count": sum(1 for item in messages if item.get("sentiment") == "bullish"),
            "investor_bearish_count": sum(1 for item in messages if item.get("sentiment") == "bearish"),
        }

    def _xml_text(self, item, name: str) -> str:
        child = item.find(name)
        return child.text if child is not None and child.text else ""


def sources_from_env() -> List[str]:
    raw = os.getenv("NEWS_CONTEXT_SOURCES", DEFAULT_SOURCES)
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_news_sources(instance_path: Optional[str] = None):
    """Editable source registry (instance/news_sources.json) when available,
    else the NEWS_CONTEXT_SOURCES env fallback."""
    if instance_path:
        try:
            from news_sources import load_news_sources

            sources = load_news_sources(instance_path, enabled_only=True)
            if sources:
                return sources
        except Exception:
            pass
    return sources_from_env()


def create_market_news_collector(instance_path: Optional[str] = None) -> MarketNewsCollector:
    return MarketNewsCollector(
        sources=resolve_news_sources(instance_path),
        limit=env_int("NEWS_CONTEXT_LIMIT", 8, minimum=0),
        max_items=env_int("NEWS_CONTEXT_MAX_ITEMS", 60, minimum=0),
        timeout_sec=env_float("NEWS_CONTEXT_TIMEOUT_SEC", env_float("LLM_TRADE_VALIDATION_NEWS_TIMEOUT_SEC", 5.0, minimum=0.5), minimum=0.5),
        alpaca_news_url=os.getenv("NEWS_CONTEXT_ALPACA_URL", os.getenv("LLM_TRADE_VALIDATION_NEWS_URL", "https://data.alpaca.markets/v1beta1/news")),
        google_days=env_int("NEWS_CONTEXT_GOOGLE_DAYS", 7, minimum=1),
        user_agent=os.getenv("NEWS_CONTEXT_USER_AGENT", DEFAULT_USER_AGENT),
    )


def print_context_summary(context: Dict) -> None:
    print(json.dumps(context, indent=2, sort_keys=True))

#!/usr/bin/env python3
"""User-editable registry of news sources for the local LLM gatekeeper.

Sources live in ``instance/news_sources.json`` so they can be added, removed or
toggled without touching code. Each source is a small dict:

    {"name": "Yahoo Finance RSS", "type": "rss",
     "url": "https://.../headline?s={symbol}", "enabled": true}

Supported ``type`` values:
  * ``alpaca``      - Alpaca news API (needs API key/secret, highest quality)
  * ``google``      - Google News RSS search
  * ``yahoo``       - Yahoo Finance search JSON
  * ``stocktwits``  - StockTwits investor messages (sentiment)
  * ``rss``         - ANY RSS/Atom feed; ``{symbol}`` in ``url`` is substituted

To add a source later: append an entry to the JSON file (or edit ``enabled``).
``{symbol}`` is replaced with the ticker at fetch time.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List


SCHEMA_VERSION = 1
_LOCK = threading.Lock()

# A broad default set. Symbol-templated RSS feeds are the easy extension point;
# unreliable/noisy ones ship disabled so the user can opt in.
DEFAULT_SOURCES: List[Dict] = [
    {"name": "Alpaca", "type": "alpaca", "enabled": True},
    {"name": "Google News", "type": "google", "enabled": True},
    {"name": "Yahoo Finance Search", "type": "yahoo", "enabled": True},
    {"name": "StockTwits", "type": "stocktwits", "enabled": True},
    {
        "name": "Yahoo Finance RSS",
        "type": "rss",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
        "enabled": True,
    },
    {
        "name": "Nasdaq",
        "type": "rss",
        "url": "https://www.nasdaq.com/feed/rssoutbound?symbol={symbol}",
        "enabled": True,
    },
    {
        "name": "Seeking Alpha",
        "type": "rss",
        "url": "https://seekingalpha.com/api/sa/combined/{symbol}.xml",
        "enabled": True,
    },
    {
        "name": "Bing News",
        "type": "rss",
        "url": "https://www.bing.com/news/search?q={symbol}%20stock&format=rss",
        "enabled": False,
    },
    {
        "name": "Investing.com (Google proxy)",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:investing.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": False,
    },
    # Publisher-specific feeds via Google News site-scoped queries (symbol-templated,
    # verified to return per-symbol results).
    {
        "name": "Economic Times India",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:economictimes.indiatimes.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": True,
    },
    {
        "name": "TipRanks",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:tipranks.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": True,
    },
    {
        "name": "The Motley Fool",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:fool.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": True,
    },
    {
        "name": "GuruFocus",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:gurufocus.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": True,
    },
    {
        "name": "Livemint",
        "type": "rss",
        "url": "https://news.google.com/rss/search?q={symbol}%20stock%20site:livemint.com&hl=en-US&gl=US&ceid=US:en",
        "enabled": True,
    },
]

VALID_TYPES = {"alpaca", "google", "google_news", "yahoo", "stocktwits", "stocktwits_public", "rss"}


def _config_path(instance_path: str) -> str:
    return os.getenv(
        "NEWS_SOURCES_FILE",
        os.path.join(instance_path, "news_sources.json"),
    )


def normalize_source(raw) -> Dict:
    """Coerce a string or dict into a canonical source dict (or {} if invalid)."""
    if isinstance(raw, str):
        stype = raw.strip().lower()
        if stype not in VALID_TYPES:
            return {}
        return {"name": stype, "type": stype, "url": "", "enabled": True}
    if not isinstance(raw, dict):
        return {}
    stype = str(raw.get("type") or "").strip().lower()
    if stype not in VALID_TYPES:
        return {}
    url = str(raw.get("url") or "").strip()
    if stype == "rss" and "{symbol}" not in url:
        # an rss source without a templated URL cannot be made symbol-specific
        return {}
    return {
        "name": str(raw.get("name") or stype).strip()[:80] or stype,
        "type": stype,
        "url": url,
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_sources(raw_list) -> List[Dict]:
    out: List[Dict] = []
    if not isinstance(raw_list, list):
        return out
    for item in raw_list:
        norm = normalize_source(item)
        if norm:
            out.append(norm)
    return out


def default_sources() -> List[Dict]:
    return [dict(s) for s in DEFAULT_SOURCES]


def load_news_sources(instance_path: str, *, enabled_only: bool = False, create: bool = True) -> List[Dict]:
    """Load the editable source list, materializing the default file if missing."""
    path = _config_path(instance_path)
    sources: List[Dict] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                sources = normalize_sources(data.get("sources"))
            elif isinstance(data, list):
                sources = normalize_sources(data)
        except Exception:
            sources = []
    if not sources:
        sources = default_sources()
        if create:
            try:
                save_news_sources(instance_path, sources)
            except Exception:
                pass
    if enabled_only:
        return [s for s in sources if s.get("enabled", True)]
    return sources


def save_news_sources(instance_path: str, sources: List[Dict]) -> None:
    path = _config_path(instance_path)
    normalized = normalize_sources(sources) or default_sources()
    payload = {"schema_version": SCHEMA_VERSION, "sources": normalized}
    with _LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

#!/usr/bin/env python3
"""Fetch market news context for one symbol."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from market_news import MarketNewsCollector, DEFAULT_SOURCES


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Fetch normalized market news context for one symbol.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--sources", default=os.getenv("NEWS_CONTEXT_SOURCES", DEFAULT_SOURCES))
    parser.add_argument("--limit", type=int, default=int(os.getenv("NEWS_CONTEXT_LIMIT", "8")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NEWS_CONTEXT_TIMEOUT_SEC", "5")))
    parser.add_argument("--google-days", type=int, default=int(os.getenv("NEWS_CONTEXT_GOOGLE_DAYS", "7")))
    parser.add_argument("--alpaca-key", default=os.getenv("ALPACA_KEY", ""))
    parser.add_argument("--alpaca-secret", default=os.getenv("ALPACA_SECRET", ""))
    args = parser.parse_args()

    collector = MarketNewsCollector(
        sources=[item.strip() for item in args.sources.split(",") if item.strip()],
        limit=args.limit,
        timeout_sec=args.timeout,
        alpaca_news_url=os.getenv("NEWS_CONTEXT_ALPACA_URL", os.getenv("LLM_TRADE_VALIDATION_NEWS_URL", "https://data.alpaca.markets/v1beta1/news")),
        google_days=args.google_days,
        user_agent=os.getenv("NEWS_CONTEXT_USER_AGENT", ""),
    )
    context = collector.collect(args.symbol, api_key=args.alpaca_key or None, api_secret=args.alpaca_secret or None)
    print(json.dumps(context, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

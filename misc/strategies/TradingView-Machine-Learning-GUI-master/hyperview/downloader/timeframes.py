from __future__ import annotations

import pandas as pd

WS_URL = "wss://prodata.tradingview.com/socket.io/websocket"
DEFAULT_USER_AGENT = "Mozilla/5.0"

INTERVAL_MAP: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "45m": "45",
    "1h": "60",
    "2h": "120",
    "3h": "180",
    "4h": "240",
    "1d": "1D",
    "1w": "1W",
    "1mo": "1M",
}

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "45m": 2700,
    "1h": 3600,
    "2h": 7200,
    "3h": 10800,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
    "1mo": 2592000,
}


def normalize_interval(timeframe: str) -> str:
    return INTERVAL_MAP.get(timeframe.strip().lower(), timeframe)


def timeframe_seconds(timeframe: str) -> int:
    lowered = timeframe.strip().lower()
    if lowered not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return TIMEFRAME_SECONDS[lowered]


def to_timestamp(value: str | None) -> int | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(timestamp.timestamp())


def estimate_bar_count(timeframe: str, start: str | None, end: str | None) -> int:
    if start is None or end is None:
        return 10_000
    start_ts = to_timestamp(start)
    end_ts = to_timestamp(end)
    if start_ts is None or end_ts is None or end_ts <= start_ts:
        return 10_000
    seconds = timeframe_seconds(timeframe)
    return min(max(int(((end_ts - start_ts) / seconds) * 1.5) + 100, 500), 20_000)


def backfill_chunk_size(timeframe: str) -> int:
    return max(1_000, min(5_000, int(30 * 86_400 / timeframe_seconds(timeframe))))


def max_backfill_requests(timeframe: str, start: str | None, end: str | None) -> int:
    if start is None or end is None:
        return 10

    start_ts = to_timestamp(start)
    end_ts = to_timestamp(end)
    if start_ts is None or end_ts is None or end_ts <= start_ts:
        return 10

    bars_needed = max(1, int((end_ts - start_ts) / timeframe_seconds(timeframe)))
    return max(1, min(25, int(bars_needed / backfill_chunk_size(timeframe)) + 2))

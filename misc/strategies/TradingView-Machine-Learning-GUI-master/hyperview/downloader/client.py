from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..models import CandleRequest
from .cache import CandleCache, merge_frames, slice_frame
from .session import ChartSession


class TradingViewDataClient:
    """Read-only TradingView candle downloader with local cache."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self._cache = CandleCache(Path(cache_dir or "data"))

    @property
    def cache_dir(self) -> Path:
        return self._cache.cache_dir

    def get_history(
        self,
        request: CandleRequest,
        force_refresh: bool = False,
        cache_only: bool = False,
    ) -> pd.DataFrame:
        cached = None if force_refresh else self._cache.load(request)
        if cached is not None and (cache_only or self._cache.covers_range(cached, request)):
            return slice_frame(cached, request)
        if cache_only:
            raise FileNotFoundError(
                f"No cached data for {request.exchange}:{request.symbol} "
                f"{request.timeframe} ({request.session}). "
                f"Run 'hyperview download-data' first."
            )

        dataframe = ChartSession(request).download()

        merged = merge_frames(cached, dataframe) if cached is not None else dataframe
        # Safety: never let a re-download shrink the cache.
        if cached is not None and len(merged) < len(cached):
            merged = cached
        self._cache.write(request, merged)
        return slice_frame(merged, request)

    def download_pairs(
        self,
        pairs: list[tuple[str, str]],
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        session: str = "regular",
        adjustment: str = "splits",
    ) -> dict[str, pd.DataFrame]:
        """Download candle data for multiple (symbol, exchange) pairs sequentially.

        Returns a dict keyed by ``"EXCHANGE:SYMBOL"`` with the candle DataFrame as value.
        """
        results: dict[str, pd.DataFrame] = {}
        total = len(pairs)
        for index, (symbol, exchange) in enumerate(pairs, start=1):
            key = f"{exchange}:{symbol}"
            print(f"  [{index}/{total}] Downloading {key} {timeframe} ...", flush=True)
            request = CandleRequest(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                start=start,
                end=end,
                session=session,
                adjustment=adjustment,
            )
            results[key] = self.get_history(request)
            print(f"           {len(results[key])} candles cached.", flush=True)
        return results

    def list_cached(self) -> list[dict[str, Any]]:
        """List all cached datasets with their date ranges."""
        entries: list[dict[str, Any]] = []
        for csv_path in sorted(self._cache.cache_dir.glob("*.csv")):
            try:
                # Load only the time column — all other columns are unused here.
                frame = pd.read_csv(csv_path, usecols=["time"])
            except ValueError:
                # "time" column absent in this file — skip it.
                continue
            except Exception:
                continue
            if frame.empty:
                continue
            exchange, symbol, timeframe, session, adjustment = _parse_cache_stem(csv_path.stem)
            if exchange is None or symbol is None or timeframe is None:
                continue
            min_time = int(frame["time"].min())
            max_time = int(frame["time"].max())
            entries.append({
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "session": session,
                "adjustment": adjustment,
                "bars": len(frame),
                "start": pd.Timestamp(min_time, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                "end": pd.Timestamp(max_time, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                "file": csv_path.name,
            })
        return entries


def _parse_cache_stem(stem: str) -> tuple[str | None, str | None, str | None, str, str]:
    """Parse cache filename stem across naming conventions.

    Preferred:  TF-session-EXCHANGE-SYMBOL[-adjustment]
    Legacy:     EX_SYM_TF_session[_adjustment]
    """
    # -- Preferred hyphen-delimited format --
    hyphen_parts = stem.split("-", 3)
    if len(hyphen_parts) == 4:
        timeframe, session, exchange, symbol_part = hyphen_parts
        adjustment = "splits"
        for candidate in ("dividends", "none"):
            suffix = f"-{candidate}"
            if symbol_part.endswith(suffix):
                symbol_part = symbol_part[: -len(suffix)]
                adjustment = candidate
                break
        if timeframe and session and exchange and symbol_part:
            return exchange, symbol_part, timeframe, session, adjustment

    # -- Underscore-delimited format --
    parts = stem.split("_")
    if len(parts) < 3:
        return None, None, None, "unknown", "unknown"

    exchange = parts[0]
    symbol = parts[1]

    if len(parts) >= 5 and parts[-2] in {"regular", "extended"}:
        session = parts[-2]
        adjustment = parts[-1]
        timeframe = "_".join(parts[2:-2])
        return exchange, symbol, timeframe, session, adjustment

    if len(parts) >= 4 and parts[-1] in {"regular", "extended"}:
        session = parts[-1]
        timeframe = "_".join(parts[2:-1])
        return exchange, symbol, timeframe, session, "splits"

    timeframe = "_".join(parts[2:])
    return exchange, symbol, timeframe, "unknown", "unknown"

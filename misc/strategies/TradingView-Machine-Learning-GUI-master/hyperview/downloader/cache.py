from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..models import CandleRequest
from .timeframes import to_timestamp


class CandleCache:
    """CSV-backed candle cache keyed by exchange/symbol/timeframe."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load(self, request: CandleRequest) -> pd.DataFrame | None:
        path = self._resolve_existing_path(request)
        if not path.exists():
            return None
        return pd.read_csv(path)

    def write(self, request: CandleRequest, frame: pd.DataFrame) -> None:
        path = self._path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        legacy_path = self._legacy_path(request)
        if legacy_path != path and legacy_path.exists():
            legacy_path.unlink()

    def covers_range(self, frame: pd.DataFrame, request: CandleRequest) -> bool:
        if frame.empty:
            return False
        start = to_timestamp(request.start)
        end = to_timestamp(request.end)
        if start is None and end is None:
            return True
        start_ok = True if start is None else int(frame["time"].min()) <= start
        end_ok = True if end is None else int(frame["time"].max()) >= end
        return start_ok and end_ok

    def _path(self, request: CandleRequest) -> Path:
        return self.cache_dir / f"{self._stem(request)}.csv"

    def _resolve_existing_path(self, request: CandleRequest) -> Path:
        preferred = self._path(request)
        if preferred.exists():
            return preferred
        legacy = self._legacy_path(request)
        if legacy.exists():
            return legacy
        return preferred

    def _stem(self, request: CandleRequest) -> str:
        sanitize = str.maketrans({":": "_"})
        timeframe = request.timeframe.translate(sanitize)
        session = request.session.translate(sanitize)
        exchange = request.exchange.translate(sanitize)
        symbol = request.symbol.translate(sanitize)
        adjustment = request.adjustment.translate(sanitize)
        filename = f"{timeframe}-{session}-{exchange}-{symbol}"
        if adjustment != "splits":
            filename += f"-{adjustment}"
        return filename

    def _legacy_path(self, request: CandleRequest) -> Path:
        sanitize = str.maketrans({":": "_"})
        exchange = request.exchange.translate(sanitize)
        symbol = request.symbol.translate(sanitize)
        timeframe = request.timeframe.translate(sanitize)
        session = request.session.translate(sanitize)
        adjustment = request.adjustment.translate(sanitize)
        filename = f"{exchange}_{symbol}_{timeframe}_{session}"
        if adjustment != "splits":
            filename += f"_{adjustment}"
        return self.cache_dir / f"{filename}.csv"


# ------------------------------------------------------------------
# Stateless DataFrame utilities
# ------------------------------------------------------------------

def slice_frame(frame: pd.DataFrame, request: CandleRequest) -> pd.DataFrame:
    result = frame
    start = to_timestamp(request.start)
    end = to_timestamp(request.end)
    if start is not None:
        result = result[result["time"] >= start]
    if end is not None:
        result = result[result["time"] < end]
    if result is frame:
        result = frame.copy()
    return result.reset_index(drop=True)


def merge_frames(left: pd.DataFrame | None, right: pd.DataFrame) -> pd.DataFrame:
    if left is None:
        return right
    return pd.concat([left, right]).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

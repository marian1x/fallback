from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from .indicators import to_unix_timestamp


class BaseStrategy(ABC):
    """Abstract base class for all HyperView strategies.

    Subclasses must define ``strategy_name`` and implement the three abstract
    methods below.  Register a strategy by decorating the class with
    ``@register_strategy`` (from ``strategy``).
    """

    strategy_name: str = ""

    @abstractmethod
    def generate_signals(self, candles: pd.DataFrame, settings: dict[str, Any]) -> pd.DataFrame:
        """Generate buy/sell signal columns on the candle DataFrame.

        The returned DataFrame **must** contain at least:
        - ``buy_signal``  (bool column)
        - ``sell_signal`` (bool column)
        - ``in_date_range`` (bool column)
        - ``enable_long`` (bool column)
        - ``enable_short`` (bool column)
        """

    @abstractmethod
    def default_settings(self) -> dict[str, Any]:
        """Return a dict of this strategy's default hyper-parameters."""

    @abstractmethod
    def required_columns(self) -> list[str]:
        """Return the OHLCV column names this strategy needs."""

    @staticmethod
    def prepare_candles(candles: pd.DataFrame) -> pd.DataFrame:
        """Normalize and sort a candle DataFrame for strategy use."""
        dataframe = candles.copy()
        normalized = {column.lower(): column for column in dataframe.columns}
        required = ["time", "open", "high", "low", "close"]
        missing = [column for column in required if column not in normalized]
        if missing:
            raise ValueError(f"Missing required candle columns: {', '.join(missing)}")

        dataframe = dataframe.rename(columns={normalized[key]: key for key in normalized})
        dataframe = dataframe[
            ["time", "open", "high", "low", "close"]
            + [column for column in dataframe.columns if column not in {"time", "open", "high", "low", "close"}]
        ]
        dataframe["time"] = dataframe["time"].astype("int64")
        for column in ["open", "high", "low", "close"]:
            dataframe[column] = dataframe[column].astype("float64")
        dataframe = dataframe.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
        return dataframe

    @staticmethod
    def _apply_date_range(dataframe: pd.DataFrame, start: str | None, end: str | None) -> None:
        """Set the ``in_date_range`` bool column on *dataframe* from optional ISO date strings."""
        start_ts = to_unix_timestamp(start)
        end_ts = to_unix_timestamp(end)
        if start_ts is None and end_ts is None:
            dataframe["in_date_range"] = True
            return
        lower = dataframe["time"] >= (start_ts if start_ts is not None else dataframe["time"].min())
        upper_bound = end_ts if end_ts is not None else dataframe["time"].max() + 1
        dataframe["in_date_range"] = lower & (dataframe["time"] < upper_bound)
